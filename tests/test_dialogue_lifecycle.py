import asyncio
from uuid import UUID, uuid4

import pytest

from app.archive import ConversationArchive
from app.dialogue import Dialogue, EventBus
from app.errors import AppError
from app.privacy import erase_request
from app.schemas import CallbackDraft, ExtractedIntent


async def test_delayed_model_reply_cannot_recreate_a_completed_transcript(business, session):
    started, release = asyncio.Event(), asyncio.Event()

    class DelayedModel:
        async def extract(self, *args):
            started.set()
            await release.wait()
            return ExtractedIntent(intent="conversation", answer="Отложенный тестовый ответ")

    sid = session["uuid"]
    archive = ConversationArchive(business.pool)
    dialogue = Dialogue(business, DelayedModel(), EventBus())
    pending = asyncio.create_task(dialogue.message(sid, "Мне важно обсудить необычный вопрос", uuid4()))
    try:
        await asyncio.wait_for(started.wait(), timeout=3)
        await business.end(sid)
        await archive.finalize(sid)
        release.set()
        with pytest.raises(AppError) as error:
            await pending
        assert error.value.code == "session_expired"
        assert await business.pool.fetchval("SELECT count(*) FROM turns WHERE session_id=$1 AND text<>''", sid) == 0
    finally:
        release.set()
        await asyncio.gather(pending, return_exceptions=True)


async def test_delayed_extraction_cannot_restore_erased_customer_fields(business, session):
    started, release = asyncio.Event(), asyncio.Event()

    class DelayedModel:
        async def extract(self, *args):
            started.set()
            await release.wait()
            return ExtractedIntent(intent="callback", name="Тестовый клиент", phone="+375291234567")

    sid = session["uuid"]
    draft = await business.draft(sid, "callback", CallbackDraft(
        name="Тестовый клиент", phone="+375291234567", message="Тест удаления",
    ))
    result = await business.confirm(sid, UUID(draft["id"]), draft["revision"], uuid4())
    dialogue = Dialogue(business, DelayedModel(), EventBus())
    pending = asyncio.create_task(dialogue.message(sid, "Мне важно обсудить необычный вопрос", uuid4()))
    try:
        await asyncio.wait_for(started.wait(), timeout=3)
        await erase_request(business.pool, UUID(result["request_id"]))
        release.set()
        with pytest.raises(AppError):
            await pending
        row = await business.pool.fetchrow("SELECT context,memory FROM sessions WHERE id=$1", sid)
        assert row["context"] == {} and row["memory"] == {}
        assert await business.pool.fetchval("SELECT count(*) FROM turns WHERE session_id=$1", sid) == 0
    finally:
        release.set()
        await asyncio.gather(pending, return_exceptions=True)
