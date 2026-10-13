import hashlib
import hmac
import secrets
from datetime import UTC, date, datetime, time, timedelta
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import asyncpg

from app.config import settings
from app.db import audit
from app.errors import AppError
from app.schemas import BookingDraft, CallbackDraft

MINSK = ZoneInfo("Europe/Minsk")


def token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def manage_token(booking_id):
    return hmac.new(settings.management_secret.encode(), str(booking_id).encode(), hashlib.sha256).hexdigest()


def json_record(row):
    if row is None:
        return None
    return {
        key: value.isoformat()
        if isinstance(value, (datetime, date))
        else str(value)
        if isinstance(value, UUID)
        else value
        for key, value in dict(row).items()
    }


class Business:
    def __init__(self, pool):
        self.pool = pool

    async def new_session(self, channel, consent, voice_profile="male"):
        if not consent:
            raise AppError("consent_required", "Для разговора нужно согласие на сохранение обращения", 422)
        return await self._create_session(channel, consent, voice_profile, "website")

    async def new_transport_session(self, source, voice_profile):
        if source not in ("telegram", "sip"):
            raise ValueError("Unsupported call source")
        return await self._create_session("voice", False, voice_profile, source)

    async def _create_session(self, channel, consent, voice_profile, source):
        sid, token = uuid4(), secrets.token_urlsafe(32)
        expires = datetime.now(UTC) + timedelta(seconds=settings.session_ttl_seconds)
        await self.pool.execute(
            "INSERT INTO sessions(id,token_hash,channel,expires_at,consent,voice_profile,source) VALUES($1,$2,$3,$4,$5,$6,$7)",
            sid,
            token_hash(token),
            channel,
            expires,
            consent,
            voice_profile,
            source,
        )
        return {
            "id": str(sid),
            "token": token,
            "expires_at": expires.isoformat(),
            "voice_profile": voice_profile,
            "greeting": "Здравствуйте! Я виртуальный помощник тестовой компании «Точка». Могу рассказать об услугах, записать вас или передать вопрос сотруднику. Чем помочь?",
        }

    async def session(self, sid, token):
        row = await self.pool.fetchrow(
            "SELECT * FROM sessions WHERE id=$1 AND token_hash=$2 AND expires_at>now() AND ended_at IS NULL",
            sid,
            token_hash(token),
        )
        if not row:
            raise AppError("session_expired", "Сессия завершена. Начните новый разговор", 401)
        return dict(row)

    async def services(self):
        return [
            dict(r) for r in await self.pool.fetch("SELECT * FROM services WHERE active ORDER BY price,id")
        ]

    async def schedule(self, conn=None):
        return await (conn or self.pool).fetchval("SELECT value FROM settings WHERE key='schedule'")

    async def slots(self, service_id, day):
        service = await self.pool.fetchrow("SELECT * FROM services WHERE id=$1 AND active", service_id)
        if not service:
            raise AppError("service_not_found", "Услуга недоступна", 404)
        schedule = await self.schedule()
        today = datetime.now(MINSK).date()
        if day < today or day > today + timedelta(days=30):
            return []
        if day.weekday() not in schedule["weekdays"] or day.isoformat() in schedule["closed_dates"]:
            return []
        start = datetime.combine(day, time(schedule["opens"]), MINSK)
        close = datetime.combine(day, time(schedule["closes"]), MINSK)
        busy = await self.pool.fetch(
            "SELECT starts_at,ends_at FROM bookings WHERE status='confirmed' AND starts_at<$1 AND ends_at>$2",
            close,
            start,
        )
        slots = []
        duration = timedelta(minutes=service["duration"])
        while start + duration <= close:
            if start > datetime.now(UTC) + timedelta(minutes=10) and not any(
                start < r["ends_at"] and start + duration > r["starts_at"] for r in busy
            ):
                slots.append(
                    {
                        "starts_at": start.isoformat(),
                        "ends_at": (start + duration).isoformat(),
                        "label": start.strftime("%H:%M"),
                    }
                )
            start += timedelta(minutes=15)
        return slots

    async def validate_time(self, starts_at, duration, conn):
        local = starts_at.astimezone(MINSK)
        schedule = await self.schedule(conn)
        ends = local + timedelta(minutes=duration)
        if starts_at <= datetime.now(UTC) + timedelta(minutes=10) or starts_at > datetime.now(
            UTC
        ) + timedelta(days=30):
            raise AppError("invalid_date", "Выберите время от 10 минут до 30 дней вперёд", 422)
        if (
            local.weekday() not in schedule["weekdays"]
            or local.date().isoformat() in schedule["closed_dates"]
            or local.hour < schedule["opens"]
            or ends > datetime.combine(local.date(), time(schedule["closes"]), MINSK)
            or local.minute % 15
            or local.second
            or local.microsecond
        ):
            raise AppError("outside_schedule", "Это время вне рабочего расписания", 422)
        return ends

    async def draft(self, sid, kind, data):
        payload = data.model_dump(mode="json")
        async with self.pool.acquire() as conn, conn.transaction():
            session = await conn.fetchrow("SELECT * FROM sessions WHERE id=$1 FOR UPDATE", sid)
            if not session or session["ended_at"] or session["expires_at"] <= datetime.now(UTC):
                raise AppError("session_expired", "Сессия завершена", 401)
            if kind == "booking":
                service = await conn.fetchrow(
                    "SELECT * FROM services WHERE id=$1 AND active FOR SHARE", data.service_id
                )
                if not service:
                    raise AppError("service_not_found", "Услуга недоступна", 404)
                ends = await self.validate_time(data.starts_at, service["duration"], conn)
                if await conn.fetchval(
                    "SELECT EXISTS(SELECT 1 FROM bookings WHERE status='confirmed' AND starts_at<$1 AND ends_at>$2)",
                    ends,
                    data.starts_at,
                ):
                    raise AppError("slot_taken", "Это время уже занято. Выберите другое", 409)
                payload.update(
                    service_name=service["name"],
                    price=service["price"],
                    duration=service["duration"],
                    service_revision=service["revision"],
                )
            await conn.execute(
                "UPDATE drafts SET status='superseded' WHERE session_id=$1 AND status='awaiting_confirmation'",
                sid,
            )
            revision = await conn.fetchval(
                "SELECT coalesce(max(revision),0)+1 FROM drafts WHERE session_id=$1", sid
            )
            did = uuid4()
            await conn.execute(
                "INSERT INTO drafts(id,session_id,kind,payload,revision) VALUES($1,$2,$3,$4,$5)",
                did,
                sid,
                kind,
                payload,
                revision,
            )
            if kind == "booking":
                local = data.starts_at.astimezone(MINSK)
                context = {
                    "intent": "booking",
                    "service_id": data.service_id,
                    "name": data.name,
                    "phone": data.phone,
                    "date": local.date().isoformat(),
                    "time": local.strftime("%H:%M"),
                }
            else:
                context = {"intent": "callback", **data.model_dump(mode="json")}
            await conn.execute("UPDATE sessions SET context=$2 WHERE id=$1", sid, context)
            await conn.execute(
                "UPDATE sessions SET memory=memory || $2::jsonb WHERE id=$1",
                sid,
                {"customer_name": data.name},
            )
            await audit(conn, str(sid), "draft_created", did, {"kind": kind, "revision": revision})
        return {
            "id": str(did),
            "kind": kind,
            "payload": payload,
            "revision": revision,
            "status": "awaiting_confirmation",
            "summary": self.summary(kind, payload),
        }

    @staticmethod
    def summary(kind, payload):
        if kind == "booking":
            when = datetime.fromisoformat(payload["starts_at"]).astimezone(MINSK)
            return f"{payload['service_name']}, {when:%d.%m.%Y} в {when:%H:%M} (Минск), {payload['price']} BYN. {payload['name']}, {payload['phone']}. Подтверждаете запись?"
        return f"Обратный звонок: {payload['name']}, {payload['phone']}. Вопрос: {payload['message']}. Подтверждаете отправку?"

    async def confirm(self, sid, did, revision, key):
        try:
            async with self.pool.acquire() as conn, conn.transaction():
                # A per-session lock serializes HTTP, chat and voice confirmations.
                session = await conn.fetchrow("SELECT * FROM sessions WHERE id=$1 FOR UPDATE", sid)
                if not session or session["ended_at"] or session["expires_at"] <= datetime.now(UTC):
                    raise AppError("session_expired", "Сессия завершена", 401)
                old = await conn.fetchrow("SELECT * FROM actions WHERE idempotency_key=$1", key)
                if old:
                    if old["session_id"] != sid or old["draft_id"] != did:
                        raise AppError("idempotency_conflict", "Ключ запроса уже использован", 409)
                    return self.action_result(old)
                draft = await conn.fetchrow(
                    "SELECT * FROM drafts WHERE id=$1 AND session_id=$2 FOR UPDATE", did, sid
                )
                if not draft:
                    raise AppError("draft_not_found", "Подтверждение не найдено", 404)
                committed = await conn.fetchrow(
                    "SELECT * FROM actions WHERE draft_id=$1 AND session_id=$2", did, sid
                )
                if committed:
                    return self.action_result(committed)
                if draft["status"] != "awaiting_confirmation" or draft["revision"] != revision:
                    raise AppError("stale_confirmation", "Детали изменились. Подтвердите новую сводку", 409)
                payload = draft["payload"]
                action_id, rid = uuid4(), uuid4()
                result = {
                    "status": "succeeded",
                    "kind": draft["kind"],
                    "request_id": str(rid),
                    "crm_status": "pending_sync",
                }
                if draft["kind"] == "booking":
                    service = await conn.fetchrow(
                        "SELECT * FROM services WHERE id=$1 FOR SHARE", payload["service_id"]
                    )
                    if (
                        not service
                        or not service["active"]
                        or service["revision"] != payload["service_revision"]
                    ):
                        raise AppError(
                            "service_changed", "Услуга или цена изменились. Нужна новая сводка", 409
                        )
                    starts = datetime.fromisoformat(payload["starts_at"])
                    ends = await self.validate_time(starts, payload["duration"], conn)
                    result.update(
                        booking_id=str(rid),
                        starts_at=starts.isoformat(),
                        service_name=payload["service_name"],
                        name=payload["name"],
                        price=payload["price"],
                    )
                await conn.execute(
                    "INSERT INTO actions(id,session_id,draft_id,idempotency_key,kind,result) VALUES($1,$2,$3,$4,$5,$6)",
                    action_id,
                    sid,
                    did,
                    key,
                    draft["kind"],
                    result,
                )
                if draft["kind"] == "booking":
                    await conn.execute(
                        "INSERT INTO bookings(id,action_id,service_id,service_name,price,duration,starts_at,ends_at,name,phone,token_hash,token_expires_at) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)",
                        rid,
                        action_id,
                        payload["service_id"],
                        payload["service_name"],
                        payload["price"],
                        payload["duration"],
                        starts,
                        ends,
                        payload["name"],
                        payload["phone"],
                        token_hash(manage_token(rid)),
                        starts + timedelta(days=1),
                    )
                summary = (
                    self.summary(draft["kind"], payload)
                    .replace("Подтверждаете запись?", "")
                    .replace("Подтверждаете отправку?", "")
                    .strip()
                )
                await conn.execute(
                    "INSERT INTO requests(id,action_id,session_id,kind,name,phone,summary) VALUES($1,$2,$3,$4,$5,$6,$7)",
                    rid,
                    action_id,
                    sid,
                    draft["kind"],
                    payload["name"],
                    payload["phone"],
                    summary,
                )
                for entity, crm_payload in [
                    (
                        "Lead",
                        {
                            "firstName": payload["name"],
                            "phoneNumber": payload["phone"],
                            "description": summary,
                            "status": "New",
                        },
                    ),
                    (
                        "Call",
                        {
                            "name": "Обращение — " + payload["name"],
                            "description": summary,
                            "status": "Held",
                            "direction": "Inbound",
                            "dateStart": datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S"),
                            "dateEnd": datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S"),
                        },
                    ),
                    (
                        "Task",
                        {
                            "name": ("Запись: " if draft["kind"] == "booking" else "Перезвонить: ")
                            + payload["name"],
                            "description": summary,
                            "status": "Not Started",
                            "priority": "Normal",
                        },
                    ),
                ]:
                    crm_payload["cVoiceActionId"] = str(action_id)
                    await conn.execute(
                        "INSERT INTO outbox(id,action_id,entity,payload) VALUES($1,$2,$3,$4)",
                        uuid4(),
                        action_id,
                        entity,
                        crm_payload,
                    )
                await conn.execute("UPDATE drafts SET status='committed' WHERE id=$1", did)
                await conn.execute("UPDATE sessions SET context='{}' WHERE id=$1", sid)
                await audit(
                    conn,
                    str(sid),
                    "action_committed",
                    action_id,
                    {"kind": draft["kind"], "revision": revision},
                )
                return self.action_result({"id": action_id, "result": result})
        except asyncpg.ExclusionViolationError:
            raise AppError("slot_taken", "Это время только что заняли. Выберите другое", 409) from None
        except asyncpg.UniqueViolationError:
            raise AppError("idempotency_conflict", "Ключ запроса уже использован", 409) from None

    @staticmethod
    def action_result(action):
        result = dict(action["result"])
        if not result or result.get("deleted"):
            raise AppError("action_deleted", "Персональные данные этой заявки удалены", 410)
        result["action_id"] = str(action["id"])
        if result["kind"] == "booking":
            result["management_token"] = manage_token(result["booking_id"])
            result["message"] = "Запись подтверждена и сохранена. Сохраните ссылку управления записью."
        else:
            result["message"] = "Заявка на обратный звонок сохранена. Сотрудник увидит ваше обращение."
        return result

    async def active_draft(self, sid):
        row = await self.pool.fetchrow(
            "SELECT * FROM drafts WHERE session_id=$1 AND status='awaiting_confirmation' ORDER BY created_at DESC LIMIT 1",
            sid,
        )
        if row:
            result = json_record(row)
            result["summary"] = self.summary(row["kind"], row["payload"])
            return result

    async def management(self, token):
        row = await self.pool.fetchrow(
            "SELECT id,service_id,service_name,price,duration,starts_at,ends_at,name,status,revision FROM bookings WHERE token_hash=$1 AND token_expires_at>now()",
            token_hash(token),
        )
        if not row:
            raise AppError(
                "invalid_management_token", "Ссылка недействительна или срок её действия истёк", 404
            )
        return json_record(row)

    async def change_booking(self, data):
        booking = await self.management(data.token)
        if booking["revision"] != data.revision:
            raise AppError("booking_changed", "Запись изменилась. Обновите страницу", 409)
        if booking["status"] != "confirmed":
            raise AppError("booking_cancelled", "Запись уже отменена", 409)
        starts = None
        if data.operation == "reschedule":
            if data.starts_at is None or data.starts_at.tzinfo is None:
                raise AppError("invalid_date", "Укажите дату и время с часовым поясом", 422)
            starts = data.starts_at
            async with self.pool.acquire() as conn:
                ends = await self.validate_time(starts, booking["duration"], conn)
        summary = (
            "Отменить запись?"
            if data.operation == "cancel"
            else f"Перенести запись на {starts.astimezone(MINSK):%d.%m.%Y %H:%M} (Минск)?"
        )
        if not data.confirm:
            return {"status": "needs_confirmation", "summary": summary, "revision": data.revision}
        try:
            async with self.pool.acquire() as conn, conn.transaction():
                row = await conn.fetchrow(
                    "SELECT * FROM bookings WHERE id=$1 AND token_hash=$2 AND token_expires_at>now() FOR UPDATE",
                    UUID(booking["id"]),
                    token_hash(data.token),
                )
                if not row or row["revision"] != data.revision or row["status"] != "confirmed":
                    raise AppError("booking_changed", "Запись изменилась. Обновите страницу", 409)
                if starts:
                    ends = await self.validate_time(starts, row["duration"], conn)
                    await conn.execute(
                        "UPDATE bookings SET starts_at=$2,ends_at=$3,revision=revision+1,token_expires_at=$4 WHERE id=$1",
                        row["id"],
                        starts,
                        ends,
                        starts + timedelta(days=1),
                    )
                else:
                    await conn.execute(
                        "UPDATE bookings SET status='cancelled',revision=revision+1 WHERE id=$1", row["id"]
                    )
                await audit(conn, "management_link", "booking_" + data.operation, row["id"])
                updated = await conn.fetchrow("SELECT * FROM bookings WHERE id=$1", row["id"])
                updated_summary = f"{updated['service_name']}, {updated['starts_at'].astimezone(MINSK):%d.%m.%Y %H:%M}, {updated['name']}. Статус: {updated['status']}."
                await conn.execute(
                    "UPDATE requests SET summary=$2,updated_at=now() WHERE id=$1", row["id"], updated_summary
                )
                # Re-sync mapped records by PUT; pending records receive current content too.
                for event in await conn.fetch(
                    "SELECT * FROM outbox WHERE action_id=$1 FOR UPDATE", row["action_id"]
                ):
                    payload = dict(event["payload"])
                    payload["description"] = updated_summary
                    await conn.execute(
                        "UPDATE outbox SET payload=$2,status='pending',next_attempt_at=now(),error=NULL WHERE id=$1",
                        event["id"],
                        payload,
                    )
        except asyncpg.ExclusionViolationError:
            raise AppError("slot_taken", "Это время занято. Выберите другое", 409) from None
        return {"status": "succeeded", "message": "Запись перенесена" if starts else "Запись отменена"}

    async def knowledge(self):
        rows = await self.pool.fetch(
            "SELECT id,published_title AS title,published_content AS content,published_revision AS revision,source FROM knowledge WHERE published_revision IS NOT NULL AND NOT archived ORDER BY updated_at DESC LIMIT 100"
        )
        return [json_record(r) for r in rows]

    async def end(self, sid):
        await self.pool.execute(
            "UPDATE sessions SET ended_at=now(),state='ended' WHERE id=$1 AND ended_at IS NULL", sid
        )

    async def make_booking_draft(self, sid, fields):
        return await self.draft(sid, "booking", BookingDraft(**fields))

    async def make_callback_draft(self, sid, fields):
        return await self.draft(sid, "callback", CallbackDraft(**fields))
