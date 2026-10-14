from uuid import UUID

import asyncpg
import pytest

from app.archive import ConversationArchive
from app.privacy import enforce_retention, erase_request
from app.schemas import CallbackDraft


async def test_purge_progresses_past_a_full_batch_of_tombstones(business):
    archive = ConversationArchive(business.pool)
    ids = []
    for _ in range(101):
        sid = UUID((await business.new_session("chat", True))["id"])
        ids.append(sid)
        await business.end(sid)
        await archive.finalize(sid)
    await business.pool.execute("UPDATE archive_entries SET deleted_at=now()")
    assert await archive.purge() == 100
    assert await archive.purge() == 1
    assert await archive.purge() == 0
    assert all(not archive.store.path(sid, "transcript").exists() for sid in ids)


async def test_session_retention_preserves_file_cleanup_metadata(business):
    archive = ConversationArchive(business.pool)
    sid = UUID((await business.new_session("chat", True))["id"])
    await business.end(sid)
    await archive.finalize(sid)
    await business.pool.execute("UPDATE sessions SET ended_at=now()-interval '31 days' WHERE id=$1", sid)
    await business.pool.execute("UPDATE archive_entries SET transcript_expires_at=now()-interval '1 day' WHERE id=$1", sid)
    await enforce_retention(business.pool, 30)
    assert await business.pool.fetchval("SELECT EXISTS(SELECT 1 FROM archive_entries WHERE id=$1)", sid)
    await archive.purge()
    assert not archive.store.path(sid, "transcript").exists()


async def test_archive_recovery_preserves_audio_after_database_failure(business, database_admin):
    archive = ConversationArchive(business.pool)
    sid = UUID((await business.new_transport_session("sip", "female"))["id"])
    await business.pool.execute("UPDATE sessions SET consent=true,audio_consent=true WHERE id=$1", sid)
    await business.pool.execute("INSERT INTO turns(session_id,role,text) VALUES($1,'user','Тест восстановления')", sid)
    capture = archive.start_capture(sid)
    capture.append("caller", b"\xe8\x03" * 1600, 16000)
    await business.end(sid)
    await database_admin.execute(
        "CREATE FUNCTION fail_archive_insert() RETURNS trigger LANGUAGE plpgsql AS $$ "
        "BEGIN RAISE EXCEPTION 'test archive failure'; END; $$; "
        "CREATE TRIGGER fail_archive_insert BEFORE INSERT ON archive_entries "
        "FOR EACH ROW EXECUTE FUNCTION fail_archive_insert()"
    )
    with pytest.raises(asyncpg.RaiseError):
        await archive.finalize(sid, capture)
    await database_admin.execute("DROP TRIGGER fail_archive_insert ON archive_entries")
    assert await archive.recover_completed() == 1
    assert (await archive.transcript(sid))[0]["text"] == "Тест восстановления"
    assert await business.pool.fetchval("SELECT audio_available FROM archive_entries WHERE id=$1", sid)
    assert (await archive.audio(sid)).startswith(b"RIFF")
    assert await business.pool.fetchval("SELECT text FROM turns WHERE session_id=$1", sid) == ""
    assert await archive.recover_completed() == 0


async def test_incomplete_audio_does_not_block_the_protected_transcript(business):
    archive = ConversationArchive(business.pool)
    sid = UUID((await business.new_transport_session("sip", "male"))["id"])
    await business.pool.execute("UPDATE sessions SET consent=true,audio_consent=true WHERE id=$1", sid)
    await business.pool.execute("INSERT INTO turns(session_id,role,text) VALUES($1,'user','Тест обрыва записи')", sid)
    capture = archive.start_capture(sid)
    capture.append("caller", b"\xe8\x03" * 1600, 16000)
    capture.file.close()  # Reproduce an abrupt restart before the authenticated END footer.
    await business.end(sid)
    await archive.finalize(sid)
    row = await business.pool.fetchrow("SELECT * FROM archive_entries WHERE id=$1", sid)
    assert row["audio_status"] == "incomplete"
    assert not row["audio_available"] and row["audio_expires_at"] is not None
    assert (await archive.transcript(sid))[0]["text"] == "Тест обрыва записи"
    assert await business.pool.fetchval("SELECT text FROM turns WHERE session_id=$1", sid) == ""
    assert archive.store.path(sid, "capture").exists()


async def test_erasure_during_recording_keeps_a_durable_file_cleanup_job(business):
    from uuid import uuid4

    archive = ConversationArchive(business.pool)
    sid = UUID((await business.new_transport_session("sip", "male"))["id"])
    await business.pool.execute("UPDATE sessions SET consent=true,audio_consent=true WHERE id=$1", sid)
    capture = archive.start_capture(sid)
    capture.append("caller", b"\xe8\x03" * 1600, 16000)
    draft = await business.draft(sid, "callback", CallbackDraft(
        name="Тестовый клиент", phone="+375291234567", message="Тест удаления записи",
    ))
    result = await business.confirm(sid, UUID(draft["id"]), draft["revision"], uuid4())
    await erase_request(business.pool, UUID(result["request_id"]))
    await archive.finalize(sid, capture)
    await archive.purge()
    assert not archive.store.path(sid, "capture").exists()
    assert await business.pool.fetchval("SELECT deleted_at IS NOT NULL FROM archive_entries WHERE id=$1", sid)
