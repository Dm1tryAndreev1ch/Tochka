"""Consent-gated audio capture and encrypted completed conversations."""

import asyncio
import hashlib
import io
import json
import struct
import time
import wave
from contextlib import suppress
from datetime import UTC, datetime, timedelta

import numpy as np
import soxr

from app import db
from app.config import settings
from app.errors import AppError
from app.secure_store import SecureStore
from app.telemetry import event


class AudioCapture:
    def __init__(self, store, sid):
        self.store, self.sid = store, sid
        path = store.path(sid, "capture")
        self.file = path.open("wb")
        try:
            path.chmod(0o600)
            self.file.write(b"VDA1")
        except BaseException:
            with suppress(OSError):
                self.file.close()
            with suppress(OSError):
                path.unlink(missing_ok=True)
            raise
        self.started = time.monotonic()
        self.index = 0
        self.samples = 0
        self.closed = False

    def append(self, speaker, audio, sample_rate, channels=1):
        if self.closed or not audio or not any(audio):
            return
        data = np.frombuffer(audio, dtype=np.int16).astype(np.float32) / 32768
        if channels > 1:
            data = data.reshape(-1, channels).mean(axis=1)
        if sample_rate != 16000:
            data = soxr.resample(data, sample_rate, 16000)
        position = max(0, round((time.monotonic() - self.started) * 16000))
        if position > 16000 * 3600:
            return
        pcm = (np.clip(data, -1, 1) * 32767).astype(np.int16).tobytes()
        self.samples = max(self.samples, position + len(pcm) // 2)
        header = json.dumps({"position": position, "speaker": speaker}).encode()
        self._write(struct.pack("!I", len(header)) + header + pcm)

    def _write(self, payload):
        cipher = self.store.seal(self.sid, f"capture:{self.index}", payload)
        self.file.write(struct.pack("!I", len(cipher)) + cipher)
        self.index += 1

    def close(self):
        if not self.closed:
            self._write(b"END:" + str(self.samples).encode())
            self.file.flush()
            import os

            os.fsync(self.file.fileno())
            self.file.close()
            self.closed = True
        return self.samples / 16000


def read_capture(store, sid, *, collect_frames=True):
    path = store.path(sid, "capture")
    if not path.exists():
        raise AppError("archive_not_found", "Аудиозапись недоступна", 404)
    if path.stat().st_size > 512 * 1024 * 1024:
        raise AppError("archive_too_large", "Аудиозапись превышает допустимый размер", 413)
    frames = []
    samples = None
    with path.open("rb") as stream:
        if stream.read(4) != b"VDA1":
            raise AppError("archive_integrity", "Неверный формат записи", 503)
        index = 0
        while prefix := stream.read(4):
            if len(prefix) != 4:
                raise AppError("archive_integrity", "Запись повреждена", 503)
            size = struct.unpack("!I", prefix)[0]
            if not 31 <= size <= 2 * 1024 * 1024:
                raise AppError("archive_integrity", "Запись повреждена", 503)
            data = store.open(sid, f"capture:{index}", stream.read(size))
            index += 1
            if data.startswith(b"END:"):
                samples = int(data[4:])
                if stream.read(1):
                    raise AppError("archive_integrity", "Запись повреждена", 503)
                break
            header_size = struct.unpack("!I", data[:4])[0]
            if not 1 <= header_size <= 1024:
                raise AppError("archive_integrity", "Запись повреждена", 503)
            metadata = json.loads(data[4 : 4 + header_size])
            if collect_frames:
                frames.append((metadata, data[4 + header_size :]))
    if samples is None or not 0 <= samples <= 16000 * 3601:
        raise AppError("archive_integrity", "Запись не завершена или повреждена", 503)
    return frames, samples


def read_audio(store, sid):
    frames, samples = read_capture(store, sid)
    stereo = np.zeros((samples, 2), dtype=np.int16)
    for metadata, data in frames:
        start = metadata["position"]
        values = np.frombuffer(data, dtype=np.int16)
        end = min(samples, start + len(values))
        if not 0 <= start <= end:
            raise AppError("archive_integrity", "Запись повреждена", 503)
        stereo[start:end, 0 if metadata["speaker"] == "caller" else 1] = values[: end - start]
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as output:
        output.setnchannels(2)
        output.setsampwidth(2)
        output.setframerate(16000)
        output.writeframes(stereo.tobytes())
    return buffer.getvalue()


class ConversationArchive:
    def __init__(self, pool):
        self.pool = pool
        self.store = SecureStore(settings.private_runtime_dir)

    def start_capture(self, sid):
        return AudioCapture(self.store, sid)

    async def finalize(self, sid, capture=None):
        duration = 0
        if capture:
            try:
                duration = capture.close()
            except (OSError, ValueError) as exc:
                with suppress(OSError):
                    capture.file.close()
                capture = None
                event("audio_recording_failed", session_id=str(sid), error_type=type(exc).__name__)
        async with self.pool.acquire() as conn, conn.transaction():
            session = await conn.fetchrow("SELECT * FROM sessions WHERE id=$1 FOR UPDATE", sid)
            if not session or not session["consent"]:
                return
            if session.get("data_erased"):
                return
            if await conn.fetchval("SELECT EXISTS(SELECT 1 FROM archive_entries WHERE session_id=$1)", sid):
                return
            turns = await conn.fetch(
                "SELECT role,text,generated_text,delivery,created_at FROM turns WHERE session_id=$1 ORDER BY id",
                sid,
            )
            data = json.dumps([dict(r) for r in turns], ensure_ascii=False, default=str).encode()
            self.store.write(sid, "transcript", data)
            policy = session["policy"] or {"text_days": settings.retention_days, "audio_days": 30}
            now = session["ended_at"] or datetime.now(UTC)
            captured = bool(session["audio_consent"] and self.store.path(sid, "capture").exists())
            audio = captured
            audio_status = "complete" if audio else "unavailable" if session["audio_consent"] else "not_recorded"
            if audio and not capture:
                try:
                    _, samples = await asyncio.to_thread(read_capture, self.store, sid, collect_frames=False)
                    duration = samples / 16000
                except AppError as exc:
                    audio = False
                    audio_status = "incomplete"
                    event("audio_archive_incomplete", session_id=str(sid), error_type=exc.code)
            audio_hash = (
                hashlib.sha256(self.store.path(sid, "capture").read_bytes()).hexdigest() if captured else None
            )
            await conn.execute(
                "INSERT INTO archive_entries(id,session_id,voice_profile,source,started_at,ended_at,transcript_expires_at,audio_expires_at,audio_available,policy,transcript_hash,audio_hash,audio_seconds,audio_status) VALUES($1,$1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)",
                sid,
                session["voice_profile"],
                session["source"],
                session["started_at"],
                now,
                now + timedelta(days=policy.get("text_days", 30)),
                now + timedelta(days=policy.get("audio_days", 30)) if captured else None,
                audio,
                policy,
                hashlib.sha256(data).hexdigest(),
                audio_hash,
                duration,
                audio_status,
            )
            # Completed transcripts live only in the protected archive, not a
            # plaintext duplicate in the working turn/context/draft tables.
            await conn.execute(
                "UPDATE turns SET text='',generated_text=NULL,reply=NULL WHERE session_id=$1", sid
            )
            await conn.execute("UPDATE sessions SET context='{}',memory='{}' WHERE id=$1", sid)
            await conn.execute(
                "UPDATE drafts SET payload='{}',status='expired' WHERE session_id=$1 AND status='awaiting_confirmation'",
                sid,
            )
            await db.audit(
                conn,
                "system",
                "conversation_archived",
                sid,
                {"audio": audio, "audio_status": audio_status, "policy_version": policy.get("policy_version")},
            )
            return True

    async def recover_completed(self):
        rows = await self.pool.fetch(
            "SELECT s.id FROM sessions s WHERE s.ended_at IS NOT NULL AND s.consent "
            "AND NOT s.data_erased AND NOT EXISTS(SELECT 1 FROM archive_entries a WHERE a.id=s.id) "
            "ORDER BY s.ended_at,s.id LIMIT 100"
        )
        completed = 0
        for row in rows:
            try:
                completed += bool(await self.finalize(row["id"]))
            except Exception as exc:
                event("archive_recovery_failed", session_id=str(row["id"]), error_type=type(exc).__name__)
        return completed

    async def transcript(self, sid):
        return json.loads(self.store.read(sid, "transcript"))

    async def audio(self, sid):
        return await asyncio.to_thread(read_audio, self.store, sid)

    async def purge(self):
        async with self.pool.acquire() as conn, conn.transaction():
            rows = await conn.fetch(
                "SELECT id,audio_expires_at,transcript_expires_at,deleted_at,transcript_purged_at FROM archive_entries "
                "WHERE purged_at IS NULL AND (deleted_at IS NOT NULL OR audio_expires_at<now() "
                "OR (transcript_expires_at<now() AND transcript_purged_at IS NULL)) "
                "ORDER BY ended_at,id LIMIT 100 FOR UPDATE SKIP LOCKED"
            )
            for row in rows:
                now = datetime.now(UTC)
                text_expired = row["transcript_expires_at"] <= now
                audio_expired = not row["audio_expires_at"] or row["audio_expires_at"] <= now
                if row["deleted_at"] or (text_expired and audio_expired):
                    self.store.delete(row["id"])
                    await conn.execute(
                        "UPDATE archive_entries SET deleted_at=coalesce(deleted_at,now()),audio_available=false,purged_at=now(),transcript_purged_at=coalesce(transcript_purged_at,now()),audio_status=$2 WHERE id=$1",
                        row["id"],
                        "deleted" if row["deleted_at"] else "expired",
                    )
                else:
                    if text_expired and not row["transcript_purged_at"]:
                        self.store.path(row["id"], "transcript").unlink(missing_ok=True)
                        await conn.execute("UPDATE archive_entries SET transcript_purged_at=now() WHERE id=$1", row["id"])
                    if row["audio_expires_at"] and audio_expired:
                        self.store.path(row["id"], "capture").unlink(missing_ok=True)
                        await conn.execute(
                            "UPDATE archive_entries SET audio_available=false,audio_expires_at=NULL,audio_status='expired' WHERE id=$1", row["id"],
                        )
            return len(rows)
