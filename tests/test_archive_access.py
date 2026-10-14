from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from app.config import settings
from app.dialogue import Dialogue, EventBus
from app.privacy import enforce_retention
from app.schemas import CallbackDraft, ExtractedIntent


async def test_completed_transcript_is_encrypted_and_requires_purpose(client, business):
    from app.main import app

    class Model:
        async def extract(self, *args):
            return ExtractedIntent(intent="conversation", answer="Обсудим ваш вопрос.")

    session = await business.new_session("chat", True)
    sid = UUID(session["id"])
    await Dialogue(business, Model(), EventBus()).message(sid, "Хочу обсудить личный вопрос", uuid4())
    await business.end(sid)
    await app.state.archive.finalize(sid)
    assert await business.pool.fetchval("SELECT text FROM turns WHERE session_id=$1 AND role='user'", sid) == ""
    assert "личный".encode() not in app.state.archive.store.path(sid, "transcript").read_bytes()
    await client.post("/api/v1/operator/login", json={"password": settings.admin_password})
    base = "/api/v1/operator/archive/" + str(sid) + "/transcript"
    assert (await client.get(base)).status_code == 422
    response = await client.get(base + "?purpose=quality_review")
    assert response.status_code == 200
    assert response.json()["turns"][0]["text"] == "Хочу обсудить личный вопрос"
    assert await business.pool.fetchval("SELECT purpose FROM audit WHERE event='archive_transcript_read' ORDER BY id DESC LIMIT 1") == "quality_review"


async def test_expired_or_deleted_archive_does_not_disclose_content(client, business):
    from app.main import app

    session = await business.new_session("chat", True)
    sid = UUID(session["id"])
    await business.end(sid)
    await app.state.archive.finalize(sid)
    await client.post("/api/v1/operator/login", json={"password": settings.admin_password})
    await business.pool.execute("UPDATE archive_entries SET transcript_expires_at=$2 WHERE id=$1", sid, datetime(2000, 1, 1, tzinfo=UTC))
    assert (await client.get(f"/api/v1/operator/archive/{sid}/transcript?purpose=service_support")).status_code == 404


async def test_legacy_completed_session_cannot_bypass_archive_purpose(client, business):
    session = await business.new_session("chat", True)
    sid = UUID(session["id"])
    await business.pool.execute(
        "INSERT INTO turns(session_id,role,text) VALUES($1,'user','Закрытый тестовый разговор')", sid,
    )
    await business.end(sid)
    await client.post("/api/v1/operator/login", json={"password": settings.admin_password})
    response = await client.get(f"/api/v1/operator/sessions/{sid}")
    assert response.status_code == 422
    assert "Закрытый тестовый разговор" not in response.text


@pytest.mark.parametrize("committed_request", [False, True])
async def test_audio_retention_is_independent_of_transcript_retention(client, business, committed_request):
    from app.main import app

    sid = UUID((await business.new_transport_session("sip", "male"))["id"])
    await business.pool.execute("UPDATE sessions SET consent=true,audio_consent=true WHERE id=$1", sid)
    if committed_request:
        draft = await business.draft(sid, "callback", CallbackDraft(
            name="Тестовый клиент", phone="+375291234567", message="Тест хранения аудио",
        ))
        await business.confirm(sid, UUID(draft["id"]), draft["revision"], uuid4())
        await business.pool.execute("UPDATE requests SET created_at=now()-interval '31 days'")
    capture = app.state.archive.start_capture(sid)
    capture.append("caller", b"\xe8\x03" * 1600, 16000)
    await business.end(sid)
    await app.state.archive.finalize(sid, capture)
    await business.pool.execute(
        "UPDATE archive_entries SET transcript_expires_at=now()-interval '1 day',audio_expires_at=now()+interval '5 days' WHERE id=$1", sid,
    )
    if committed_request:
        await enforce_retention(business.pool, 30)
    await app.state.archive.purge()
    assert app.state.archive.store.path(sid, "capture").exists()
    assert not app.state.archive.store.path(sid, "transcript").exists()
    await client.post("/api/v1/operator/login", json={"password": settings.admin_password})
    assert (await client.get(f"/api/v1/operator/archive/{sid}/transcript?purpose=service_support")).status_code == 404
    assert (await client.get(f"/api/v1/operator/archive/{sid}/audio?purpose=service_support")).status_code == 200
