import asyncio
import re
from datetime import datetime

from pydantic import ValidationError

from app.business import MINSK
from app.errors import AppError
from app.fast_intents import extract_common, published_address

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


class Dialogue:
    def __init__(self, business, models, events):
        self.business = business
        self.pool = business.pool
        self.models = models
        self.events = events
        self.locks = {}

    async def message(self, sid, text, key):
        lock = self.locks.setdefault(sid, asyncio.Lock())
        async with lock:
            session = await self.pool.fetchrow(
                "SELECT * FROM sessions WHERE id=$1 AND ended_at IS NULL AND expires_at>now()", sid
            )
            if not session:
                raise AppError("session_expired", "Сессия завершена", 401)
            if not session["consent"]:
                raise AppError(
                    "consent_required", "Сначала нужно согласие на сохранение текста разговора", 422
                )
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
            if session["state"] == "human":
                reply = {"text": "Сообщение передано сотруднику.", "state": "human"}
            else:
                memory = dict(session.get("memory") or {})
                introduction = re.search(
                    r"(?i:меня зовут|моё имя|мое имя)\s+([А-ЯЁA-Zа-яёa-z][А-ЯЁA-Zа-яёa-z\-']{1,29}(?:\s+[А-ЯЁA-Z][А-ЯЁA-Zа-яёa-z\-']{1,29}){0,2})",
                    text,
                )
                if introduction:
                    memory["customer_name"] = introduction[1].strip()
                    await self.pool.execute("UPDATE sessions SET memory=$2 WHERE id=$1", sid, memory)
                reply = await self.respond(
                    sid, text, session["context"], key, session["voice_profile"], memory
                )
            stored = dict(reply)
            if "result" in stored:
                stored["result"] = {k: v for k, v in stored["result"].items() if k != "management_token"}
            async with self.pool.acquire() as conn, conn.transaction():
                await conn.execute(
                    "INSERT INTO turns(session_id,role,text,message_key,reply) VALUES($1,'user',$2,$3,$4)",
                    sid,
                    text,
                    key,
                    stored,
                )
                await conn.execute(
                    "INSERT INTO turns(session_id,role,text) VALUES($1,'assistant',$2)", sid, reply["text"]
                )
            await self.events.emit(sid, {"type": "turn", "role": "user", "text": text})
            await self.events.emit(sid, {"type": "reply", **reply})
            return reply

    def restore_result(self, reply):
        if reply.get("result", {}).get("booking_id"):
            from app.business import manage_token

            reply["result"]["management_token"] = manage_token(reply["result"]["booking_id"])
        return reply

    async def respond(self, sid, text, context, key, voice_profile="male", memory=None):
        normalized = re.sub(r"[^а-яёa-z0-9\s]", "", text.lower()).strip()
        if any(
            phrase in normalized
            for phrase in ("как тебя зовут", "как вас зовут", "кто ты", "кто вы", "твое имя", "твоё имя")
        ):
            return {
                "text": f"Меня зовут {'Мария' if voice_profile == 'female' else 'Геннадий'}. Я виртуальный помощник Точки. Могу ответить на вопросы и помочь с записью."
            }
        draft = await self.business.active_draft(sid)
        if draft and normalized in YES:
            result = await self.business.confirm(sid, uuid_from(draft["id"]), draft["revision"], key)
            return {"text": result["message"], "result": result}
        if normalized in NO:
            await self.pool.execute(
                "UPDATE drafts SET status='cancelled' WHERE session_id=$1 AND status='awaiting_confirmation'",
                sid,
            )
            await self.pool.execute("UPDATE sessions SET context='{}' WHERE id=$1", sid)
            return {"text": "Неподтверждённая заявка отменена. Чем ещё помочь?"}
        if any(word in normalized for word in ["отменить запись", "перенести запись", "отмените запись"]):
            return {
                "text": "Откройте секретную ссылку, выданную после записи. По ней можно перенести или отменить запись. Если ссылка потерялась, оставьте вопрос сотруднику: одного имени и телефона для отмены недостаточно."
            }
        services, knowledge = await asyncio.gather(self.business.services(), self.business.knowledge())
        if any(word in normalized for word in ("сколько", "цена", "стоит", "стоимость")):
            matched = [s for s in services if s["name"].lower()[:7] in normalized]
            if matched:
                return {
                    "text": "; ".join(
                        f"{s['name']} — {s['price']} BYN, {s['duration']} минут" for s in matched
                    )
                    + ". Хотите записаться?"
                }
        if any(word in normalized for word in ("часы работы", "когда работаете", "когда открыты")):
            schedule = await self.business.schedule()
            days = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
            return {
                "text": f"Работаем с {schedule['opens']}:00 до {schedule['closes']}:00 по Минску. Дни: "
                + ", ".join(days[d] for d in schedule["weekdays"])
                + "."
            }
        if any(word in normalized for word in ["какие услуги", "список услуг", "прайс", "стоимость услуг"]):
            return {
                "text": "В тестовой компании доступны: "
                + "; ".join(f"{s['name']} — {s['price']} BYN, {s['duration']} минут" for s in services)
                + ". На какую услугу хотите записаться?"
            }
        address = published_address(text, knowledge)
        if address:
            return {"text": address, "response_source": "knowledge"}
        extracted = extract_common(text, context, services, datetime.now(MINSK))
        history = [
            dict(row)
            for row in reversed(
                await self.pool.fetch(
                    "SELECT role,text FROM turns WHERE session_id=$1 ORDER BY id DESC LIMIT 32", sid
                )
            )
        ]
        if extracted is None:
            try:
                extracted = await self.models.extract(
                    text,
                    history,
                    {
                        **context,
                        "assistant_name": "Мария" if voice_profile == "female" else "Геннадий",
                        "customer_memory": memory or {},
                    },
                    services,
                    knowledge,
                    datetime.now(MINSK).isoformat(),
                )
            except AppError as error:
                return {"text": error.message, "degraded": True}
        if extracted.intent == "cancel_draft":
            await self.pool.execute(
                "UPDATE drafts SET status='cancelled' WHERE session_id=$1 AND status='awaiting_confirmation'",
                sid,
            )
            await self.pool.execute("UPDATE sessions SET context='{}' WHERE id=$1", sid)
            return {"text": "Неподтверждённая заявка отменена."}
        intent = extracted.intent
        if (
            intent in ("faq", "unknown")
            and context.get("intent")
            and any(getattr(extracted, f) for f in ("name", "phone", "date", "time", "service_id"))
        ):
            intent = context["intent"]
        if intent not in ("booking", "callback"):
            return {
                "text": extracted.answer
                or "Уточните, пожалуйста, вопрос. Я могу рассказать об услугах, записать вас или оставить заявку сотруднику."
            }
        fields = dict(context) if context.get("intent") == intent else {}
        fields["intent"] = intent
        for field in ("service_id", "name", "phone", "date", "time", "message"):
            value = getattr(extracted, field)
            if value:
                fields[field] = value
        # Any correction invalidates the previous confirmation before further clarification.
        await self.pool.execute(
            "UPDATE drafts SET status='superseded' WHERE session_id=$1 AND status='awaiting_confirmation'",
            sid,
        )
        await self.pool.execute("UPDATE sessions SET context=$2 WHERE id=$1", sid, fields)
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
