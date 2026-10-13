import asyncio
import struct
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.call_modes import CallModes
from app.config import settings
from app.dialogue import Dialogue, EventBus
from app.errors import AppError
from app.socket_transport import read_packet
from app.voice import VoiceManager


async def test_mode_switch_is_persisted_and_rejects_stale_revision(business):
    modes = CallModes(business.pool)
    initial = await modes.selection()
    result = await modes.update("sip", initial["revision"], "female")
    assert result["mode"] == "sip"
    assert (await modes.selection())["incoming_voice"] == "female"
    with pytest.raises(AppError, match="Обновите"):
        await modes.update("telegram", initial["revision"])
    with pytest.raises(AppError, match="выключен"):
        await modes.require("website")
    await modes.require("sip")


async def test_admin_mode_and_private_registration_authentication(client):
    assert (
        await client.put("/api/v1/operator/call-settings", json={"mode": "sip", "revision": 1})
    ).status_code == 401
    assert (
        await client.post("/api/v1/transport-calls", data={"source": "sip", "profile": "male"})
    ).status_code == 401
    await client.post("/api/v1/operator/login", json={"password": settings.admin_password, "role": "admin"})
    response = await client.put("/api/v1/operator/call-settings", json={"mode": "sip", "revision": 1})
    assert response.status_code == 200
    assert (
        await client.post("/api/v1/sessions", json={"channel": "voice", "consent": True})
    ).status_code == 409
    assert (
        await client.post("/api/v1/sessions", json={"channel": "chat", "consent": True})
    ).status_code == 201
    await client.post("/api/v1/operator/logout")
    await client.post(
        "/api/v1/operator/login", json={"password": settings.operator_password, "role": "operator"}
    )
    assert (await client.get("/api/v1/operator/sip/profile")).status_code == 403


async def test_protocol_rejects_oversize_packet_before_reading_payload():
    reader = asyncio.StreamReader()
    reader.feed_data(struct.pack("!BH", 0x10, 65535))
    reader.feed_eof()
    with pytest.raises(ValueError, match="too large"):
        await read_packet(reader)


async def test_external_consent_gates_storage_and_actions(business):
    data = await business.new_transport_session("sip", "male")
    from uuid import UUID

    sid = UUID(data["id"])
    events = EventBus()
    dialogue = Dialogue(business, SimpleNamespace(), events)
    with pytest.raises(AppError, match="согласие"):
        await dialogue.message(sid, "Хочу записаться", uuid4())
    assert await business.pool.fetchval("SELECT count(*) FROM turns WHERE session_id=$1", sid) == 0
    frames = []

    class Worker:
        async def queue_frame(self, frame):
            frames.append(frame)

        async def queue_frames(self, new):
            frames.extend(new)

    manager = VoiceManager(business, SimpleNamespace(), dialogue, events)
    manager.runtime[sid] = {"needs_consent": True, "worker": Worker()}
    assert await manager.handle_consent(sid, "я не согласен")
    assert not await business.pool.fetchval("SELECT consent FROM sessions WHERE id=$1", sid)
    await manager.handle_consent(sid, "да я согласен")
    assert not await business.pool.fetchval("SELECT consent FROM sessions WHERE id=$1", sid)
    second = await business.new_transport_session("sip", "male")
    sid = UUID(second["id"])
    manager.runtime[sid] = {"needs_consent": True, "worker": Worker()}
    await manager.handle_consent(sid, "да я согласен")
    assert await business.pool.fetchval("SELECT consent FROM sessions WHERE id=$1", sid)
    assert not manager.runtime[sid]["needs_consent"]
    assert await business.pool.fetchval("SELECT count(*) FROM turns WHERE session_id=$1", sid) == 0
