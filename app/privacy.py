"""Apply retention and explicit erasure to every durable copy owned by the app."""

from app.db import audit
from app.errors import AppError


async def erase_request(pool, rid, actor="admin"):
    async with pool.acquire() as conn, conn.transaction():
        # Same ordering as CRM writes: wait for any in-flight synchronization.
        await conn.execute("SELECT pg_advisory_xact_lock(7860002)")
        row = await conn.fetchrow("SELECT * FROM requests WHERE id=$1 FOR UPDATE", rid)
        if not row:
            raise AppError("not_found", "Обращение не найдено", 404)
        await conn.execute(
            "UPDATE requests SET name='Удалено',phone='',summary='Данные удалены',status='done',updated_at=now() WHERE id=$1",
            rid,
        )
        # Retention preserves a future booking's occupied slot; explicit erasure cancels it.
        if actor == "retention":
            await conn.execute(
                "UPDATE bookings SET name='Удалено',phone='',token_expires_at=now() WHERE action_id=$1",
                row["action_id"],
            )
        else:
            await conn.execute(
                "UPDATE bookings SET name='Удалено',phone='',token_expires_at=now(),status='cancelled' WHERE action_id=$1",
                row["action_id"],
            )
        await conn.execute("UPDATE actions SET result='{\"deleted\":true}' WHERE id=$1", row["action_id"])
        if row["session_id"]:
            await conn.execute("DELETE FROM turns WHERE session_id=$1", row["session_id"])
            await conn.execute(
                "UPDATE sessions SET context='{}',memory='{}',ended_at=coalesce(ended_at,now()),state='ended',data_erased=data_erased OR $2 WHERE id=$1",
                row["session_id"],
                actor != "retention",
            )
            if actor == "retention":
                await conn.execute("UPDATE archive_entries SET transcript_expires_at=least(transcript_expires_at,now()) WHERE id=$1", row["session_id"])
            else:
                # A live call may have a capture file before an archive row exists.
                # Keep its deletion job durable and atomic with the erasure.
                await conn.execute(
                    "INSERT INTO archive_entries(id,session_id,voice_profile,source,started_at,ended_at,"
                    "transcript_expires_at,audio_expires_at,audio_available,audio_status,deleted_at,policy,transcript_hash) "
                    "SELECT id,id,voice_profile,source,started_at,coalesce(ended_at,now()),now(),now(),false,'deleted',now(),policy,$2 "
                    "FROM sessions WHERE id=$1 ON CONFLICT(id) DO UPDATE "
                    "SET deleted_at=now(),audio_available=false,audio_status='deleted'",
                    row["session_id"],
                    "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
                )
            await conn.execute(
                "UPDATE drafts SET payload='{}',status='deleted' WHERE session_id=$1", row["session_id"]
            )
        for event in await conn.fetch("SELECT * FROM outbox WHERE action_id=$1 FOR UPDATE", row["action_id"]):
            if event["entity"] == "Lead":
                payload = {
                    "firstName": "Удалено",
                    "lastName": "",
                    "phoneNumber": "",
                    "description": "Данные удалены",
                    "cVoiceActionId": str(row["action_id"]),
                }
            else:
                payload = {
                    "name": "Удалено",
                    "description": "Данные удалены",
                    "cVoiceActionId": str(row["action_id"]),
                }
                payload.update(
                    {
                        k: v
                        for k, v in event["payload"].items()
                        if k in ("status", "dateStart", "dateEnd", "direction", "priority")
                    }
                )
            # Lookup by action id also covers a lost response after CRM commit.
            await conn.execute(
                "UPDATE outbox SET payload=$2,status='pending',next_attempt_at=now(),error=NULL WHERE id=$1",
                event["id"],
                payload,
            )
        await audit(conn, actor, "personal_text_expired" if actor == "retention" else "personal_data_deleted", rid)
        return row["session_id"]


async def enforce_retention(pool, days):
    for row in await pool.fetch(
        "SELECT r.id FROM requests r LEFT JOIN sessions s ON s.id=r.session_id "
        "WHERE r.created_at<now()-make_interval(days=>coalesce((s.policy->>'text_days')::int,$1)) "
        "AND r.phone<>'' ORDER BY r.created_at,r.id LIMIT 100", days
    ):
        await erase_request(pool, row["id"], actor="retention")
    # Sessions without committed requests also contain transcripts and draft fields.
    await pool.execute(
        "DELETE FROM sessions s WHERE ended_at<now()-make_interval(days=>coalesce((policy->>'text_days')::int,$1)) "
        "AND NOT (audio_consent AND NOT data_erased AND NOT EXISTS(SELECT 1 FROM archive_entries a WHERE a.id=s.id))", days
    )
