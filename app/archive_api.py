"""Archive reads require an individual permission and an explicit purpose."""

from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response

from app import db
from app.business import json_record
from app.errors import AppError

Purpose = Literal["service_support", "quality_review", "customer_request", "security_review"]


async def archive_entry(pool, sid, kind):
    row = await pool.fetchrow("SELECT * FROM archive_entries WHERE id=$1 AND deleted_at IS NULL", sid)
    if not row:
        raise AppError("archive_not_found", "Запись отсутствует или срок хранения истёк", 404)
    now = datetime.now(UTC)
    text_valid = row["transcript_expires_at"] > now and not row["transcript_purged_at"]
    audio_valid = bool(row["audio_expires_at"] and row["audio_expires_at"] > now)
    if (kind == "transcript" and not text_valid) or (kind == "metadata" and not (text_valid or audio_valid)):
        raise AppError("archive_not_found", "Запись отсутствует или срок хранения истёк", 404)
    if kind == "audio" and (not row["audio_available"] or not audio_valid):
        raise AppError("archive_not_found", "Аудио отсутствует или срок хранения истёк", 404)
    return row


def archive_router(operator):
    router = APIRouter(prefix="/api/v1/operator/archive")

    async def entry(request, sid, kind):
        return await archive_entry(request.app.state.pool, sid, kind)

    @router.get("")
    async def archive_list(request: Request, before: datetime | None = None, limit: int = Query(default=30, ge=1, le=100), role=Depends(operator)):
        rows = await request.app.state.pool.fetch("SELECT id,voice_profile,source,started_at,ended_at,transcript_expires_at,audio_expires_at,(audio_available AND coalesce(audio_expires_at>now(),false)) AS audio_available,CASE WHEN audio_expires_at<=now() THEN 'expired' ELSE audio_status END AS audio_status,audio_seconds,policy,(transcript_expires_at>now() AND transcript_purged_at IS NULL) AS transcript_available FROM archive_entries WHERE deleted_at IS NULL AND (transcript_expires_at>now() OR audio_expires_at>now()) AND ($1::timestamptz IS NULL OR ended_at<$1) ORDER BY ended_at DESC LIMIT $2", before, limit + 1)
        return {"items": [json_record(r) for r in rows[:limit]], "next_cursor": rows[limit-1]["ended_at"].isoformat() if len(rows)>limit else None}

    @router.get("/{sid}/transcript")
    async def transcript(sid: UUID, purpose: Purpose, request: Request, role=Depends(operator)):
        role.require("archive.text")
        await entry(request, sid, "transcript")
        await db.audit(request.app.state.pool, role, "archive_transcript_read", sid, purpose=purpose, request_id=request.state.request_id)
        return {"turns": await request.app.state.archive.transcript(sid)}

    @router.get("/{sid}/audio")
    async def audio(sid: UUID, purpose: Purpose, request: Request, download: bool = False, role=Depends(operator)):
        role.require("archive.audio")
        if download:
            role.require("archive.export")
        await entry(request, sid, "audio")
        await db.audit(request.app.state.pool, role, "archive_audio_export" if download else "archive_audio_play", sid, purpose=purpose, request_id=request.state.request_id)
        payload = await request.app.state.archive.audio(sid)
        return Response(payload, media_type="audio/wav", headers={"Cache-Control": "no-store", "Content-Disposition": f"{'attachment' if download else 'inline'}; filename=conversation-{sid}.wav"})

    @router.delete("/{sid}", status_code=204)
    async def delete(sid: UUID, purpose: Purpose, request: Request, role=Depends(operator)):
        role.require("privacy.delete")
        await entry(request, sid, "metadata")
        await request.app.state.pool.execute("UPDATE archive_entries SET deleted_at=now(),audio_available=false WHERE id=$1", sid)
        await db.audit(request.app.state.pool, role, "archive_deleted", sid, purpose=purpose, request_id=request.state.request_id)

    return router
