from types import SimpleNamespace

import httpx
import pytest_asyncio

from app import db
from app.archive import ConversationArchive
from app.business import Business
from app.config import settings
from app.dialogue import Dialogue, EventBus
from app.main import app, rate_buckets
from app.schemas import ExtractedIntent
from scripts.database_testbed import isolated_database


class FakeModels:
    ready = True
    error = None
    stats = {"stt_seconds": [], "tts_seconds": [], "llm_seconds": []}

    async def llm_ready(self):
        return True

    async def tts_status(self):
        return {"ready": True, "backend": "qwen3-tts-mlx", "natural_ready": True, "voice": "uncle_fu"}

    async def extract(self, *args):
        return ExtractedIntent(intent="unknown", answer="Уточните вопрос")


class FakeVoice:
    active = None
    metrics = {}

    async def end(self, sid):
        pass

    async def interrupt(self, sid):
        pass

    def request_handoff(self, sid):
        pass


@pytest_asyncio.fixture
async def database_testbed():
    async with isolated_database() as testbed:
        yield testbed


@pytest_asyncio.fixture
async def database_admin(database_testbed):
    return database_testbed["admin"]


@pytest_asyncio.fixture
async def business(tmp_path, monkeypatch, database_testbed):
    monkeypatch.setattr(settings, "private_runtime_dir", str(tmp_path))
    pool = await db.connect(database_testbed["runtime_url"])
    try:
        yield Business(pool)
    finally:
        await pool.close()


@pytest_asyncio.fixture
async def client(business):
    events = EventBus()
    app.state.pool = business.pool
    app.state.business = business
    app.state.events = events
    app.state.models = FakeModels()
    app.state.voice = FakeVoice()
    app.state.dialogue = Dialogue(business, app.state.models, events)
    app.state.crm = SimpleNamespace(last_ok=None)
    app.state.archive = ConversationArchive(business.pool)
    rate_buckets.clear()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost"
    ) as client:
        yield client


@pytest_asyncio.fixture
async def session(business):
    from uuid import UUID

    data = await business.new_session("chat", True)
    return {**data, "uuid": UUID(data["id"])}
