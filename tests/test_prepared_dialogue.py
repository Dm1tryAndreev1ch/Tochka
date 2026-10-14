import asyncio
from uuid import UUID, uuid4

import pytest

from app.dialogue import Dialogue, EventBus, PreparedIntent
from app.errors import AppError
from app.schemas import CallbackDraft, ExtractedIntent

TEXT = "Хочу обсудить необычный вопрос"


class CountingModel:
    def __init__(self, result=None):
        self.calls = []
        self.result = result or ExtractedIntent(intent="conversation", answer="Давайте обсудим этот вопрос.")

    async def extract(self, *args):
        self.calls.append(args)
        return self.result


async def test_prepare_is_pure_and_name_projection_reuses_one_extraction(business, session):
    sid = session["uuid"]
    events, model = EventBus(), CountingModel()
    queue = events.subscribe(sid)
    dialogue = Dialogue(business, model, events)
    text = "Меня зовут Алексей. Хочу обсудить необычный вопрос."
    prepared = await dialogue.prepare(sid, text)
    assert isinstance(prepared, PreparedIntent)
    assert model.calls[0][2]["customer_memory"] == {"customer_name": "Алексей"}
    row = await business.pool.fetchrow("SELECT context,memory FROM sessions WHERE id=$1", sid)
    assert row["context"] == row["memory"] == {}
    for table in ("turns", "drafts", "actions", "requests", "outbox"):
        assert await business.pool.fetchval(f"SELECT count(*) FROM {table}") == 0
    assert queue.empty()
    reply = await dialogue.message(sid, text, uuid4(), prepared=prepared)
    assert reply["text"] == model.result.answer
    assert len(model.calls) == 1
    assert await business.pool.fetchval("SELECT memory FROM sessions WHERE id=$1", sid) == {"customer_name": "Алексей"}


async def test_prepared_action_cannot_create_draft_until_final_message(business, session):
    sid = session["uuid"]
    model = CountingModel(ExtractedIntent(
        intent="callback", name="Алексей", phone="+375291234567", message="Необычный вопрос",
    ))
    dialogue = Dialogue(business, model, EventBus())
    prepared = await dialogue.prepare(sid, TEXT)
    assert await business.pool.fetchval("SELECT count(*) FROM drafts") == 0
    assert await business.pool.fetchval("SELECT context FROM sessions WHERE id=$1", sid) == {}
    reply = await dialogue.message(sid, TEXT, uuid4(), prepared=prepared)
    assert reply["draft"]["status"] == "awaiting_confirmation"
    assert len(model.calls) == 1
    assert await business.pool.fetchval("SELECT count(*) FROM actions") == 0
    assert await business.pool.fetchval("SELECT count(*) FROM requests") == 0


@pytest.mark.parametrize("intent", ["booking", "callback", "handoff", "cancel_draft"])
async def test_every_extracted_business_intent_stays_inert_during_prepare(business, session, intent):
    sid = session["uuid"]
    draft = await business.draft(sid, "callback", CallbackDraft(
        name="Алексей", phone="+375291234567", message="Исходная заявка",
    ))
    model = CountingModel(ExtractedIntent(intent=intent))
    dialogue = Dialogue(business, model, EventBus())
    before = await business.pool.fetchrow("SELECT context,memory,state FROM sessions WHERE id=$1", sid)
    prepared = await dialogue.prepare(sid, TEXT)
    assert prepared.extracted.intent == intent
    assert await business.pool.fetchrow("SELECT context,memory,state FROM sessions WHERE id=$1", sid) == before
    assert await business.pool.fetchval("SELECT status FROM drafts WHERE id=$1", UUID(draft["id"])) == "awaiting_confirmation"
    for table in ("turns", "actions", "requests", "outbox"):
        assert await business.pool.fetchval(f"SELECT count(*) FROM {table}") == 0


@pytest.mark.parametrize("text", [
    "Да, подтверждаю", "Нет", "Согласен", "Соедини с сотрудником", "Кто ты?",
    "Я согласен", "Да я согласна", "Я разрешаю запись разговора", "Не разрешаю",
    "Как меня зовут?", "Какие услуги?", "Часы работы", "Сколько стоит консультация?",
    "Запишите на консультацию завтра в десять", "Отменить запись",
])
async def test_known_paths_never_start_speculative_extraction(business, session, text):
    model = CountingModel()
    dialogue = Dialogue(business, model, EventBus())
    assert await dialogue.prepare(session["uuid"], text) is None
    assert model.calls == []
    assert await business.pool.fetchval("SELECT count(*) FROM turns") == 0


async def test_draft_confirmation_is_only_committed_by_final_message(business, session):
    sid = session["uuid"]
    draft = await business.draft(sid, "callback", CallbackDraft(
        name="Алексей", phone="+375291234567", message="Тестовый вопрос",
    ))
    model = CountingModel()
    dialogue = Dialogue(business, model, EventBus())
    assert await dialogue.prepare(sid, "Да, подтверждаю") is None
    assert await business.pool.fetchval("SELECT count(*) FROM actions") == 0
    assert await business.pool.fetchval("SELECT status FROM drafts WHERE id=$1", UUID(draft["id"])) == "awaiting_confirmation"
    reply = await dialogue.message(sid, "Да, подтверждаю", uuid4())
    assert reply["result"]["status"] == "succeeded"
    assert await business.pool.fetchval("SELECT count(*) FROM actions") == 1
    assert model.calls == []


@pytest.mark.parametrize("change", ["context", "memory", "permissions", "service", "knowledge", "history", "draft", "profile", "text"])
async def test_changed_extraction_inputs_reject_prepared_result(business, session, change):
    sid = session["uuid"]
    model = CountingModel()
    dialogue = Dialogue(business, model, EventBus())
    await business.pool.execute(
        "INSERT INTO turns(session_id,role,text) VALUES($1,'assistant','Начнём обсуждение.')", sid,
    )
    prepared = await dialogue.prepare(sid, TEXT)
    text = TEXT
    if change == "context":
        await business.pool.execute("UPDATE sessions SET context=$2 WHERE id=$1", sid, {"intent": "callback"})
    elif change == "memory":
        await business.pool.execute("UPDATE sessions SET memory=$2 WHERE id=$1", sid, {"customer_name": "Иван"})
    elif change == "permissions":
        await business.pool.execute("UPDATE sessions SET agent_config=$2 WHERE id=$1", sid, {"allowed_actions": []})
    elif change == "service":
        await business.pool.execute("UPDATE services SET price=price+1,revision=revision+1 WHERE id='consultation'")
    elif change == "knowledge":
        await business.pool.execute(
            "INSERT INTO knowledge(id,title,content,source,published_revision,published_title,published_content) "
            "VALUES($1,'Тест','Новый опубликованный факт.','test',1,'Тест','Новый опубликованный факт.')", uuid4(),
        )
    elif change == "history":
        await business.pool.execute("UPDATE turns SET text='Уже услышанная часть ответа.' WHERE session_id=$1", sid)
    elif change == "draft":
        await business.draft(sid, "callback", CallbackDraft(name="Алексей", phone="+375291234567", message="Тест"))
    elif change == "profile":
        await business.pool.execute("UPDATE sessions SET voice_profile='female' WHERE id=$1", sid)
    elif change == "text":
        text += ". Есть уточнение."
    await dialogue.message(sid, text, uuid4(), prepared=prepared)
    assert len(model.calls) == 2


async def test_permission_revocation_rechecks_action_before_any_draft(business, session):
    sid = session["uuid"]
    model = CountingModel(ExtractedIntent(intent="callback", name="Алексей", phone="+375291234567", message="Тест"))
    dialogue = Dialogue(business, model, EventBus())
    prepared = await dialogue.prepare(sid, TEXT)
    await business.pool.execute("UPDATE sessions SET agent_config=$2 WHERE id=$1", sid, {"allowed_actions": []})
    reply = await dialogue.message(sid, TEXT, uuid4(), prepared=prepared)
    assert "выключено" in reply["text"]
    assert len(model.calls) == 2
    assert await business.pool.fetchval("SELECT count(*) FROM drafts") == 0
    assert await business.pool.fetchval("SELECT context FROM sessions WHERE id=$1", sid) == {}


@pytest.mark.parametrize("change", ["consent", "queued", "human", "handoff_pending", "ended", "expired", "erased"])
async def test_prepare_requires_live_consented_bot_session(business, session, change):
    sid = session["uuid"]
    if change in ("queued", "human", "handoff_pending"):
        await business.pool.execute("UPDATE sessions SET state=$2 WHERE id=$1", sid, change)
    elif change == "consent":
        await business.pool.execute("UPDATE sessions SET consent=false WHERE id=$1", sid)
    elif change == "ended":
        await business.end(sid)
    elif change == "expired":
        await business.pool.execute("UPDATE sessions SET expires_at=now()-interval '1 second' WHERE id=$1", sid)
    elif change == "erased":
        await business.pool.execute("UPDATE sessions SET data_erased=true WHERE id=$1", sid)
    model = CountingModel()
    dialogue = Dialogue(business, model, EventBus())
    if change in ("human", "handoff_pending"):
        assert await dialogue.prepare(sid, TEXT) is None
    else:
        with pytest.raises(AppError):
            await dialogue.prepare(sid, TEXT)
    assert model.calls == []


async def test_pending_preparation_uses_one_extraction_and_reloads_after_wait(business, session):
    started, release = asyncio.Event(), asyncio.Event()
    model = CountingModel()
    extract = model.extract

    async def delayed(*args):
        result = await extract(*args)
        started.set()
        await release.wait()
        return result

    model.extract = delayed
    dialogue = Dialogue(business, model, EventBus())
    prepared = asyncio.create_task(dialogue.prepare(session["uuid"], TEXT))
    await asyncio.wait_for(started.wait(), 2)
    message = asyncio.create_task(dialogue.message(session["uuid"], TEXT, uuid4(), prepared=prepared))
    await asyncio.sleep(0)
    assert not message.done()
    assert await business.pool.fetchval("SELECT count(*) FROM turns") == 0
    release.set()
    await asyncio.wait_for(message, 2)
    assert len(model.calls) == 1


async def test_revoked_consent_while_preparation_pending_prevents_final_message(business, session):
    started, release = asyncio.Event(), asyncio.Event()

    class Model:
        async def extract(self, *args):
            started.set()
            await release.wait()
            return ExtractedIntent(intent="callback", name="Алексей", phone="+375291234567", message="Тест")

    sid = session["uuid"]
    dialogue = Dialogue(business, Model(), EventBus())
    prepared = asyncio.create_task(dialogue.prepare(sid, TEXT))
    await asyncio.wait_for(started.wait(), 2)
    message = asyncio.create_task(dialogue.message(sid, TEXT, uuid4(), prepared=prepared))
    await asyncio.sleep(0.02)
    await business.pool.execute("UPDATE sessions SET consent=false WHERE id=$1", sid)
    release.set()
    with pytest.raises(AppError, match="Сначала нужно согласие"):
        await asyncio.wait_for(message, 2)
    for table in ("drafts", "turns", "actions"):
        assert await business.pool.fetchval(f"SELECT count(*) FROM {table}") == 0


async def test_cancelling_final_message_cancels_pending_preparation_without_fallback(business, session):
    started, stopped, waiting = asyncio.Event(), asyncio.Event(), asyncio.Event()

    class Model(CountingModel):
        async def extract(self, *args):
            await super().extract(*args)
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()

    class WaitingDialogue(Dialogue):
        async def _prepared_result(self, task):
            waiting.set()
            return await super()._prepared_result(task)

    model = Model()
    dialogue = WaitingDialogue(business, model, EventBus())
    prepared = asyncio.create_task(dialogue.prepare(session["uuid"], TEXT))
    await asyncio.wait_for(started.wait(), 2)
    message = asyncio.create_task(dialogue.message(session["uuid"], TEXT, uuid4(), prepared=prepared))
    await asyncio.wait_for(waiting.wait(), 2)
    message.cancel()
    with pytest.raises(asyncio.CancelledError):
        await message
    assert prepared.cancelled() and stopped.is_set()
    assert len(model.calls) == 1
    assert await business.pool.fetchval("SELECT count(*) FROM turns") == 0


@pytest.mark.parametrize("failure", ["error", "cancelled"])
async def test_failed_preparation_falls_back_to_normal_extraction(business, session, failure):
    async def failed():
        if failure == "error":
            raise RuntimeError("Speculation failed")
        raise asyncio.CancelledError

    model = CountingModel()
    dialogue = Dialogue(business, model, EventBus())
    prepared = asyncio.create_task(failed())
    reply = await dialogue.message(session["uuid"], TEXT, uuid4(), prepared=prepared)
    assert reply["text"] == model.result.answer
    assert len(model.calls) == 1


async def test_model_failure_during_prepare_retries_once_for_final_message(business, session):
    class Model(CountingModel):
        async def extract(self, *args):
            result = await super().extract(*args)
            if len(self.calls) == 1:
                raise AppError("model_unavailable", "Временная ошибка модели", 503)
            return result

    model = Model()
    dialogue = Dialogue(business, model, EventBus())
    prepared = asyncio.create_task(dialogue.prepare(session["uuid"], TEXT))
    reply = await dialogue.message(session["uuid"], TEXT, uuid4(), prepared=prepared)
    assert reply["text"] == model.result.answer
    assert len(model.calls) == 2


async def test_replayed_message_keeps_idempotency_checks_with_preparation(business, session):
    model = CountingModel()
    dialogue = Dialogue(business, model, EventBus())
    sid, key = session["uuid"], uuid4()
    prepared = await dialogue.prepare(sid, TEXT)
    first = await dialogue.message(sid, TEXT, key, prepared=prepared)
    assert await dialogue.message(sid, TEXT, key, prepared=prepared) == first
    with pytest.raises(AppError, match="Ключ уже использован"):
        await dialogue.message(sid, TEXT + " Уточнение", key, prepared=prepared)
    assert len(model.calls) == 1
