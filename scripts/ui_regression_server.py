"""Run real UI/API regression against disposable data without model or CRM workers."""

import argparse
import math
import sys
import tempfile
from array import array
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import asyncpg  # noqa: E402
import uvicorn  # noqa: E402

from app import db  # noqa: E402
from app.archive import ConversationArchive  # noqa: E402
from app.business import Business  # noqa: E402
from app.config import settings  # noqa: E402
from app.dialogue import Dialogue, EventBus  # noqa: E402
from app.main import app  # noqa: E402
from app.policy import enable_audio_recording  # noqa: E402
from app.schemas import CallbackDraft  # noqa: E402
from app.voice import VoiceManager  # noqa: E402


class OfflineModels:
    ready = False
    error = "Модели отключены на отдельном стенде проверки интерфейса"
    stats = {"stt_seconds": [], "tts_seconds": [], "llm_seconds": []}

    async def llm_ready(self):
        return False

    async def tts_status(self):
        return {"ready": False, "backend": "disabled", "natural_ready": False}


async def seed_data(business, archive):
    await enable_audio_recording(business.pool, actor="ui_regression")
    sid = UUID((await business.new_transport_session("sip", "female"))["id"])
    await business.pool.execute(
        "UPDATE sessions SET consent=true,audio_consent=true,consent_method='oral',consent_at=now() WHERE id=$1", sid,
    )
    await business.pool.execute(
        "INSERT INTO turns(session_id,role,text,delivery) VALUES($1,'user','Тестовый вопрос об услуге','delivered'),"
        "($1,'assistant','Тестовая расшифровка для проверки архива','delivered')", sid,
    )
    capture = archive.start_capture(sid)
    for speaker, frequency in (("caller", 220), ("assistant", 330)):
        audio = array("h", (round(4000 * math.sin(2 * math.pi * frequency * n / 16000)) for n in range(8000))).tobytes()
        capture.append(speaker, audio, 16000)
    await business.end(sid)
    now = datetime.now(UTC)
    await business.pool.execute(
        "UPDATE sessions SET connected_at=$2,started_at=$3,ended_at=$4 WHERE id=$1",
        sid, now - timedelta(seconds=12), now - timedelta(seconds=10), now,
    )
    await archive.finalize(sid, capture)
    await business.pool.execute(
        "UPDATE session_usage SET llm_requests=1,prompt_tokens=37,completion_tokens=19 WHERE session_id=$1", sid,
    )
    chat = UUID((await business.new_session("chat", True))["id"])
    draft = await business.draft(chat, "callback", CallbackDraft(
        name="Тестовый клиент", phone="+375291234567", message="Проверка очереди CRM на отдельном стенде",
    ))
    await business.confirm(chat, UUID(draft["id"]), draft["revision"], uuid4())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=7861)
    args = parser.parse_args()
    database = "voice_ui_" + uuid4().hex[:12]
    source_url = settings.database_url
    settings.allowed_origins += f",http://127.0.0.1:{args.port}"
    settings.telegram_worker_url = "http://127.0.0.1:1"

    @asynccontextmanager
    async def lifespan(_app):
        connection = await asyncpg.connect(source_url)
        await connection.execute(f'CREATE DATABASE "{database}"')
        pool = None
        try:
            with tempfile.TemporaryDirectory(prefix="voice-ui-private-") as private:
                settings.private_runtime_dir = private
                pool = await db.connect(source_url.rsplit("/", 1)[0] + "/" + database)
                business, events, models = Business(pool), EventBus(), OfflineModels()
                dialogue = Dialogue(business, models, events)
                voice = VoiceManager(business, models, dialogue, events)
                archive = ConversationArchive(pool)
                app.state.pool, app.state.business, app.state.events = pool, business, events
                app.state.models, app.state.dialogue, app.state.voice = models, dialogue, voice
                app.state.archive, app.state.crm = archive, SimpleNamespace(last_ok=None)
                app.state.socket_calls, app.state.background_status = None, {}
                await seed_data(business, archive)
                print("UI regression server: disposable database; model/CRM workers disabled", flush=True)
                try:
                    yield
                finally:
                    await voice.close()
                    await pool.close()
                    pool = None
        finally:
            if pool is not None:
                await pool.close()
            await connection.execute(f'DROP DATABASE "{database}" WITH (FORCE)')
            await connection.close()

    app.router.lifespan_context = lifespan
    uvicorn.run(app, host="127.0.0.1", port=args.port, access_log=False)


if __name__ == "__main__":
    main()
