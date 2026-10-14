import asyncio
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime

from pydantic import ValidationError

from app.business import MINSK
from app.company_faq import company_reply, company_topic
from app.errors import AppError
from app.fast_intents import (
    control_request,
    extract_common,
    mixed_request,
    phone_correction_requested,
    requested_slots,
    requests_human,
    social_reply,
    unresolved_phone_correction,
)
from app.grounding import faq_topic, fast_faq_reply, grounded_reply

YES = {
    "да",
    "подтверждаю",
    "да подтверждаю",
    "всё верно",
    "все верно",
    "верно",
    "да всё верно",
    "да все верно",
    "согласен",
    "согласна",
    "записывайте",
}
NO = {"нет", "отмена", "не подтверждаю", "отмените", "передумал", "передумала"}


@dataclass(frozen=True)
class PreparedIntent:
    text: str
    fingerprint: str
    extracted: object


@dataclass(frozen=True)
class ExtractionInputs:
    history: list
    context: dict
    services: list
    knowledge: list
    now: str
    fingerprint: str


class Dialogue:
    def __init__(self, business, models, events):
        self.business = business
        self.pool = business.pool
        self.models = models
        self.events = events
        self.locks = {}
        self.on_handoff = None

    async def request_handoff(self, sid):
        result = await self.business.request_handoff(sid)
        if result["status"] == "pending":
            await self.events.emit(sid, {"type": "handoff_pending", "message": result["message"]})
            if self.on_handoff:
                self.on_handoff(sid)
        return result

    async def _cancel_draft(self, sid):
        async with self.pool.acquire() as conn, conn.transaction():
            current = await conn.fetchrow("SELECT * FROM sessions WHERE id=$1 FOR UPDATE", sid)
            self.business.require_customer_action(current)
            await conn.execute(
                "UPDATE drafts SET status='cancelled' WHERE session_id=$1 AND status='awaiting_confirmation'", sid,
            )
            await conn.execute("UPDATE sessions SET context='{}' WHERE id=$1", sid)

    async def _clarify_phone_correction(self, sid):
        async with self.pool.acquire() as conn, conn.transaction():
            current = await conn.fetchrow("SELECT * FROM sessions WHERE id=$1 FOR UPDATE", sid)
            self.business.require_customer_action(current)
            fields = dict(current["context"])
            intent = fields.get("intent")
            if intent not in {"booking", "callback"}:
                raise AppError("context_changed", "Состояние заявки изменилось. Уточните, какой телефон нужно изменить.", 409)
            self.business.require_allowed_action(current, intent)
            fields.pop("phone", None)
            await conn.execute(
                "UPDATE drafts SET status='superseded' WHERE session_id=$1 AND status='awaiting_confirmation'", sid,
            )
            await conn.execute("UPDATE sessions SET context=$2 WHERE id=$1", sid, fields)
        return {
            "text": "Не удалось однозначно разобрать новый телефон. Назовите его с кодом страны.",
            "collected": fields,
        }

    async def _session(self, sid):
        session = await self.pool.fetchrow(
            "SELECT * FROM sessions WHERE id=$1 AND ended_at IS NULL AND expires_at>now()", sid
        )
        self.business.require_customer_action(session, allow_human=True)
        return session

    @staticmethod
    def _memory(text, memory):
        memory = dict(memory or {})
        introduction = re.search(
            r"(?i:меня зовут|моё имя|мое имя)\s+([А-ЯЁA-Zа-яёa-z][А-ЯЁA-Zа-яёa-z\-']{1,29}(?:\s+[А-ЯЁA-Z][А-ЯЁA-Zа-яёa-z\-']{1,29}){0,2})",
            text,
        )
        if introduction:
            memory["customer_name"] = introduction[1].strip()
        return memory

    async def _extraction_inputs(self, sid, session, services, knowledge, draft, memory):
        history = [
            dict(row) for row in reversed(await self.pool.fetch(
                "SELECT role,text FROM turns WHERE session_id=$1 AND text<>'' ORDER BY id DESC LIMIT 32", sid
            ))
        ]
        agent_config = session.get("agent_config") or {}
        context = {
            **session["context"],
            "assistant_name": agent_config.get("name") or ("Мария" if session["voice_profile"] == "female" else "Геннадий"),
            "company_name": agent_config.get("company_name", "Точка"),
            "agent_instructions": agent_config.get("instructions", ""),
            "_session_id": str(sid),
            "_channel": "voice" if session.get("channel") == "voice" else "chat",
            "_category_schema": agent_config.get("category_schema", {}),
            "customer_memory": memory,
        }
        now = datetime.now(MINSK).isoformat()
        # Database values include UUIDs and datetimes. Canonical serialization
        # checks the inputs and permissions without depending on their types.
        fingerprint = hashlib.sha256(json.dumps({
            "session": {field: session.get(field) for field in (
                "id", "state", "consent", "data_erased", "ended_at", "expires_at", "channel", "voice_profile", "policy", "agent_config",
            )},
            "context": context, "history": history, "services": services,
            "knowledge": knowledge, "draft": draft, "date": now[:10],
        }, sort_keys=True, default=str, ensure_ascii=False).encode()).hexdigest()
        return ExtractionInputs(history, context, services, knowledge, now, fingerprint)

    @staticmethod
    def _skip_preparation(text):
        normalized = re.sub(r"[^а-яёa-z0-9\s]", "", text.lower()).strip()
        return (
            not normalized or normalized in YES | NO or requests_human(text)
            or bool(re.fullmatch(
                r"(?:да )?(?:я )?(?:согласен|согласна|разрешаю(?: запись(?: разговора)?| записать разговор)?)"
                r"|не (?:разрешаю|согласен|согласна)|можете записывать|не записывайте|без записи|только текст",
                normalized,
            ))
            or bool(re.search(r"(?:как меня зовут|как зовут меня|помнишь.{0,15}(?:имя|зовут)|как ко мне обращаться)", normalized))
            or any(phrase in normalized for phrase in (
                "как тебя зовут", "как вас зовут", "кто ты", "кто вы", "твое имя", "твоё имя",
                "отменить запись", "перенести запись", "отмените запись",
                "часы работы", "когда работаете", "когда открыты",
                "какие услуги", "список услуг", "прайс", "стоимость услуг",
            ))
        )

    async def prepare(self, sid, text):
        """Extract a possible intent; only finalized messages may change business state."""
        if self._skip_preparation(text):
            return None
        session = await self._session(sid)
        if session["state"] != "bot":
            return None
        services, knowledge, draft = await asyncio.gather(
            self.business.services(), self.business.knowledge(), self.business.active_draft(sid),
        )
        agent_config = session.get("agent_config") or {}
        use_fast_rules = not (agent_config.get("instructions") or agent_config.get("category_schema"))
        if ids := agent_config.get("knowledge_ids"):
            knowledge = [doc for doc in knowledge if str(doc.get("id")) in ids]
        if (
            control_request(text, session["context"], has_draft=bool(draft))
            or (use_fast_rules and (
                company_topic(text) is not None
                or social_reply(text) is not None
                or fast_faq_reply(text, services, knowledge) is not None
                or faq_topic(text) in {"hours", "weekends"}
                or requested_slots(text, session["context"], services, datetime.now(MINSK)) is not None
                or extract_common(text, session["context"], services, datetime.now(MINSK)) is not None
            ))
        ):
            return None
        inputs = await self._extraction_inputs(
            sid, session, services, knowledge, draft, self._memory(text, session.get("memory")),
        )
        extracted = await self._extract(text, inputs)
        return PreparedIntent(text, inputs.fingerprint, extracted)

    async def _extract(self, text, inputs):
        return await self.models.extract(
            text, inputs.history, inputs.context, inputs.services, inputs.knowledge, inputs.now,
        )

    @staticmethod
    async def _prepared_result(prepared):
        if isinstance(prepared, asyncio.Future):
            try:
                prepared = await prepared
            except asyncio.CancelledError:
                if asyncio.current_task().cancelling():
                    raise
                return None
            except Exception:
                return None
        return prepared if isinstance(prepared, PreparedIntent) else None

    async def message(self, sid, text, key, prepared=None):
        lock = self.locks.setdefault(sid, asyncio.Lock())
        async with lock:
            session = await self._session(sid)
            old = await self.pool.fetchrow(
                "SELECT text,reply FROM turns WHERE session_id=$1 AND message_key=$2", sid, key
            )
            if old:
                if old["text"] != text:
                    raise AppError("idempotency_conflict", "Ключ уже использован для другой реплики", 409)
                if old["reply"]:
                    return self.restore_result(old["reply"])
            count = await self.pool.fetchval("SELECT count(*) FROM turns WHERE session_id=$1", sid)
            if count >= 200:
                raise AppError("turn_limit", "Достигнут лимит разговора. Начните новую сессию", 429)
            if prepared is not None:
                prepared = await self._prepared_result(prepared)
                # Consent, agent permissions and memory may change while the
                # endpoint waits for preparation. Read them after that wait.
                session = await self._session(sid)
            if session["state"] == "human":
                reply = {"text": "Сообщение передано сотруднику.", "state": "human"}
            else:
                memory = self._memory(text, session.get("memory"))
                if memory != (session.get("memory") or {}):
                    changed = await self.pool.fetchval(
                        "UPDATE sessions SET memory=$2 WHERE id=$1 AND ended_at IS NULL AND consent "
                        "AND NOT data_erased AND expires_at>now() RETURNING id", sid, memory,
                    )
                    if not changed:
                        raise AppError("session_expired", "Сессия завершена", 401)
                reply = await self.respond(
                    sid, text, session["context"], key, session["voice_profile"], memory, session.get("agent_config") or {},
                    prepared=prepared, session=session,
                )
            stored = dict(reply)
            spoken = session["channel"] == "voice" and session["state"] != "human"
            if spoken:
                reply.update(utterance_id=str(key), delivery="pending")
                stored.update(utterance_id=str(key), delivery="pending")
            if "result" in stored:
                stored["result"] = {k: v for k, v in stored["result"].items() if k != "management_token"}
            async with self.pool.acquire() as conn, conn.transaction():
                current = await conn.fetchrow("SELECT * FROM sessions WHERE id=$1 FOR UPDATE", sid)
                self.business.require_customer_action(current, allow_human=session["state"] == "human")
                await conn.execute(
                    "INSERT INTO turns(session_id,role,text,message_key,reply) VALUES($1,'user',$2,$3,$4)",
                    sid,
                    text,
                    key,
                    stored,
                )
                await conn.execute(
                    "INSERT INTO turns(session_id,role,text,generated_text,in_reply_to,delivery) VALUES($1,'assistant',$2,$3,$4,$5)",
                    sid, "" if spoken else reply["text"], reply["text"], key, "pending" if spoken else "delivered",
                )
            await self.events.emit(sid, {"type": "turn", "role": "user", "text": text})
            await self.events.emit(sid, {"type": "reply", **reply})
            return reply

    async def audio_sent(self, sid, key, chars):
        await self.pool.execute(
            "UPDATE turns SET sent_chars=GREATEST(sent_chars,LEAST($3,char_length(generated_text))) "
            "WHERE session_id=$1 AND in_reply_to=$2 AND delivery IN ('pending','partial')", sid, key, chars,
        )

    async def acknowledge_playback(self, sid, key, chars):
        async with self.pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                "SELECT id,text,generated_text,sent_chars,delivery FROM turns WHERE session_id=$1 AND in_reply_to=$2 FOR UPDATE", sid, key,
            )
            if not row:
                raise AppError("playback_not_found", "Ответ не найден", 404)
            if row["delivery"] in ("interrupted", "legacy_unconfirmed"):
                return {"delivery": row["delivery"], "played_chars": len(row["text"])}
            if chars > row["sent_chars"]:
                raise AppError("playback_not_sent", "Этот фрагмент ещё не передан в голосовой канал", 409)
            played = max(chars, len(row["text"]))
            delivery = "delivered" if played == len(row["generated_text"] or "") else "partial"
            await conn.execute("UPDATE turns SET text=left(generated_text,$2),delivery=$3 WHERE id=$1", row["id"], played, delivery)
            return {"delivery": delivery, "played_chars": played}

    async def interrupt_playback(self, sid):
        await self.pool.execute(
            "UPDATE turns SET delivery='interrupted' WHERE session_id=$1 AND role='assistant' AND delivery IN ('pending','partial')", sid,
        )

    def restore_result(self, reply):
        if reply.get("result", {}).get("booking_id"):
            from app.business import manage_token

            reply["result"]["management_token"] = manage_token(reply["result"]["booking_id"])
        return reply

    async def respond(self, sid, text, context, key, voice_profile="male", memory=None, agent_config=None, *, prepared=None, session=None):
        agent_config = agent_config or {}
        actions = agent_config.get("allowed_actions", ["booking", "callback", "handoff"])
        use_fast_rules = not (agent_config.get("instructions") or agent_config.get("category_schema"))
        normalized = re.sub(r"[^а-яёa-z0-9\s]", "", text.lower()).strip()
        compound = mixed_request(text)
        if not compound and requests_human(text):
            if "handoff" not in actions:
                return {"text": "Передача сотруднику выключена в этом сценарии. Могу помочь с доступными вопросами."}
            handoff = await self.request_handoff(sid)
            return {"text": handoff["message"], "handoff": handoff}
        if not compound and re.search(r"(?:как меня зовут|как зовут меня|помнишь.{0,15}(?:имя|зовут)|как ко мне обращаться)", normalized):
            name = (memory or {}).get("customer_name")
            return {"text": f"Вас зовут {name}." if name else "Вы ещё не назвали своё имя. Как к вам обращаться?", "response_source": "session_memory"}
        if use_fast_rules and not compound and (reply := company_reply(text, allowed_actions=actions)):
            return reply
        if use_fast_rules and not compound and (reply := social_reply(
            text,
            assistant_name=agent_config.get("name") or ("Мария" if voice_profile == "female" else "Геннадий"),
            company_name=agent_config.get("company_name", "Точка"),
            allowed_actions=actions,
        )):
            return reply
        draft = await self.business.active_draft(sid)
        control = control_request(text, context, has_draft=bool(draft))
        if control == "confirm":
            result = await self.business.confirm(sid, uuid_from(draft["id"]), draft["revision"], key)
            return {"text": result["message"], "result": result}
        if control == "cancel_draft":
            await self._cancel_draft(sid)
            return {"text": "Неподтверждённая заявка отменена. Чем ещё помочь?"}
        if not compound and any(word in normalized for word in ["отменить запись", "перенести запись", "отмените запись"]):
            return {
                "text": "Откройте секретную ссылку, выданную после записи. По ней можно перенести или отменить запись. Если ссылка потерялась, оставьте вопрос сотруднику: одного имени и телефона для отмены недостаточно."
            }
        services, knowledge = await asyncio.gather(self.business.services(), self.business.knowledge())
        if ids := agent_config.get("knowledge_ids"):
            knowledge = [doc for doc in knowledge if str(doc.get("id")) in ids]
        if use_fast_rules and (lookup := requested_slots(text, context, services, datetime.now(MINSK))):
            available = await self.business.slots(lookup["service_id"], datetime.fromisoformat(lookup["date"]).date())
            service_name = next(service["name"] for service in services if service["id"] == lookup["service_id"])
            if available:
                answer = f"{service_name}, {lookup['date']}: ближайшие свободные варианты — " + ", ".join(slot["label"] for slot in available[:6]) + " по Минску. Это информация о доступности, время ещё не забронировано."
            else:
                answer = f"На {lookup['date']} свободных вариантов для услуги «{service_name}» не найдено. Можно проверить другую дату."
            return {"text": answer, "response_source": "availability"}
        topic = faq_topic(text) if use_fast_rules else None
        schedule = await self.business.schedule() if topic in {"hours", "weekends"} else None
        if use_fast_rules and (reply := fast_faq_reply(text, services, knowledge, schedule)):
            return reply
        extracted = extract_common(text, context, services, datetime.now(MINSK)) if use_fast_rules else None
        if extracted is None:
            inputs = await self._extraction_inputs(
                sid, session or {"context": context, "voice_profile": voice_profile, "agent_config": agent_config},
                services, knowledge, draft, memory or {},
            )
            try:
                extracted = (
                    prepared.extracted
                    if prepared is not None and prepared.text == text and prepared.fingerprint == inputs.fingerprint
                    else await self._extract(text, inputs)
                )
            except AppError as error:
                if context.get("intent") in actions and phone_correction_requested(text, context):
                    return await self._clarify_phone_correction(sid)
                return {"text": error.message, "degraded": True}
        if context.get("intent") in actions and unresolved_phone_correction(text, context, extracted.phone):
            return await self._clarify_phone_correction(sid)
        if extracted.intent == "cancel_draft":
            await self._cancel_draft(sid)
            return {"text": "Неподтверждённая заявка отменена."}
        if extracted.intent == "handoff":
            if "handoff" not in actions:
                return {"text": "Передача сотруднику выключена в этом сценарии."}
            handoff = await self.request_handoff(sid)
            return {"text": handoff["message"], "handoff": handoff}
        intent = extracted.intent
        if (
            intent in ("faq", "unknown")
            and context.get("intent")
            and any(getattr(extracted, f) for f in ("name", "phone", "date", "time", "service_id"))
        ):
            intent = context["intent"]
        if intent not in ("booking", "callback"):
            return grounded_reply(extracted, text, services, knowledge)
        if intent not in actions:
            return {"text": "Это действие выключено в настройках помощника. Могу ответить на вопрос."}
        fields = dict(context) if context.get("intent") == intent else {}
        fields["intent"] = intent
        for field in ("service_id", "name", "phone", "date", "time", "message"):
            value = getattr(extracted, field)
            if value:
                fields[field] = value
        # Any correction invalidates the previous confirmation before further clarification.
        async with self.pool.acquire() as conn, conn.transaction():
            current = await conn.fetchrow("SELECT * FROM sessions WHERE id=$1 FOR UPDATE", sid)
            self.business.require_customer_action(current)
            self.business.require_allowed_action(current, intent)
            await conn.execute(
                "UPDATE drafts SET status='superseded' WHERE session_id=$1 AND status='awaiting_confirmation'", sid,
            )
            await conn.execute("UPDATE sessions SET context=$2 WHERE id=$1", sid, fields)
        if intent == "booking":
            questions = {
                "service_id": "На какую услугу хотите записаться: "
                + ", ".join(s["name"] for s in services)
                + "?",
                "date": "На какую дату хотите записаться?",
                "time": "В какое время вам удобно?",
                "name": "Как к вам обращаться?",
                "phone": "Назовите телефон с кодом страны, например плюс триста семьдесят пять, двадцать девять, сто двадцать три, сорок пять, шестьдесят семь. Можно ввести номер текстом.",
            }
        else:
            questions = {
                "message": "Какой вопрос передать сотруднику?",
                "name": "Как к вам обращаться?",
                "phone": "Назовите телефон с кодом страны или введите его текстом.",
            }
        for field, question in questions.items():
            if not fields.get(field):
                return {"text": question, "collected": fields}
        try:
            if intent == "booking":
                when = datetime.fromisoformat(fields["date"] + "T" + fields["time"]).replace(tzinfo=MINSK)
                draft = await self.business.make_booking_draft(
                    sid,
                    {
                        "service_id": fields["service_id"],
                        "starts_at": when,
                        "name": fields["name"],
                        "phone": fields["phone"],
                    },
                )
            else:
                draft = await self.business.make_callback_draft(
                    sid, {k: fields[k] for k in ("name", "phone", "message")}
                )
            return {"text": draft["summary"], "draft": draft}
        except (ValidationError, ValueError):
            return {
                "text": "Не удалось проверить дату, имя или телефон. Уточните данные или воспользуйтесь формой записи.",
                "collected": fields,
            }
        except AppError as error:
            return {"text": error.message, "collected": fields}

    def release(self, sid):
        self.locks.pop(sid, None)


def uuid_from(value):
    from uuid import UUID

    return UUID(str(value))


class EventBus:
    def __init__(self):
        self.listeners = {}
        self.sequence = {}

    async def emit(self, sid, event):
        self.sequence[sid] = self.sequence.get(sid, 0) + 1
        event = {
            **event,
            "session_id": str(sid),
            "sequence": self.sequence[sid],
            "timestamp": datetime.now(MINSK).isoformat(),
        }
        for queue in list(self.listeners.get(sid, [])):
            if queue.full():
                queue.get_nowait()
            queue.put_nowait(event)

    def subscribe(self, sid):
        queue = asyncio.Queue(maxsize=100)
        self.listeners.setdefault(sid, []).append(queue)
        return queue

    def unsubscribe(self, sid, queue):
        if sid in self.listeners:
            self.listeners[sid] = [q for q in self.listeners[sid] if q is not queue]
            if not self.listeners[sid]:
                self.listeners.pop(sid)
                self.sequence.pop(sid, None)
