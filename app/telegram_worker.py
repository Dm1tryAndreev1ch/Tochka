"""Isolated Telegram user-account calls; credentials and login codes never reach logs."""

import asyncio
import contextlib
import json
import os
import secrets
import struct
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import UUID

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from pytgcalls import PyTgCalls, filters
from pytgcalls.types import ChatUpdate, Device, Direction, ExternalMedia, MediaStream, RecordStream
from pytgcalls.types.raw import AudioParameters
from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError

from app.config import settings
from app.schemas import TelegramAuthorization, TelegramCode
from app.telemetry import event

os.umask(0o077)


class TelegramCalls:
    def __init__(self):
        self.directory = Path(settings.private_runtime_dir) / "telegram"
        self.directory.mkdir(parents=True, exist_ok=True)
        self.directory.chmod(0o700)
        self.client = None
        self.calls = None
        self.bridge = None
        self.phone = None
        self.phone_code_hash = None
        self.ready = False
        self.state = "not_configured"
        self.auth_lock = asyncio.Lock()
        self.incoming_lock = asyncio.Lock()

    async def connect(self, api_id, api_hash):
        if self.client:
            await self.client.disconnect()
        self.client = TelegramClient(
            str(self.directory / "account"),
            api_id,
            api_hash,
            device_model="Точка — голосовой помощник",
            app_version="0.2.0",
        )
        await self.client.connect()

    async def restore(self):
        config = self.directory / "config.json"
        if not config.exists():
            return
        self.state = "connecting"
        try:
            data = json.loads(config.read_text())
            await self.connect(data["api_id"], data["api_hash"])
            if await self.client.is_user_authorized():
                await self.start_calls()
            else:
                self.state = "login_required"
        except Exception as exc:
            self.state = "connection_failed"
            event("telegram_restore_failed", error_type=type(exc).__name__)

    async def start_calls(self):
        self.calls = PyTgCalls(self.client)
        self.calls.on_update(filters.chat_update(ChatUpdate.Status.INCOMING_CALL))(self.incoming)
        self.calls.on_update(filters.chat_update(ChatUpdate.Status.LEFT_CALL))(self.ended)
        self.calls.on_update(filters.stream_frame(Direction.INCOMING, Device.MICROPHONE))(self.frames)
        await self.calls.start()
        self.ready = True
        self.state = "ready"
        event("telegram_calls_ready")

    async def authorize(self, data):
        async with self.auth_lock:
            if self.bridge:
                raise HTTPException(409, "Сначала завершите текущий звонок")
            if self.ready:
                raise HTTPException(409, "Telegram уже подключён. Сначала отключите аккаунт")
            try:
                await self.connect(data.api_id, data.api_hash)
                sent = await self.client.send_code_request(data.phone)
                self.phone = data.phone
                self.phone_code_hash = sent.phone_code_hash
                (self.directory / "config.json").write_text(
                    json.dumps({"api_id": data.api_id, "api_hash": data.api_hash})
                )
                self.state = "code_required"
                return {"state": self.state}
            except Exception as exc:
                event("telegram_authorization_failed", error_type=type(exc).__name__)
                raise HTTPException(
                    422, "Не удалось отправить код. Проверьте API ID, API hash и номер телефона"
                ) from None

    async def code(self, data):
        async with self.auth_lock:
            if not self.client or not self.phone_code_hash:
                raise HTTPException(409, "Сначала запросите код входа")
            try:
                if self.state == "password_required":
                    await self.client.sign_in(password=data.password)
                else:
                    await self.client.sign_in(
                        phone=self.phone, code=data.code, phone_code_hash=self.phone_code_hash
                    )
            except SessionPasswordNeededError:
                self.state = "password_required"
                return {"state": self.state}
            except Exception as exc:
                event("telegram_login_failed", error_type=type(exc).__name__)
                raise HTTPException(
                    422, "Не удалось войти. Проверьте код или пароль двухэтапной проверки"
                ) from None
            await self.start_calls()
            self.phone = self.phone_code_hash = None
            return {"state": "ready"}

    async def incoming(self, client, update):
        async with self.incoming_lock:
            if not self.ready or self.bridge:
                with contextlib.suppress(Exception):
                    await self.calls.leave_call(update.chat_id)
                return
            connection = None
            try:
                async with httpx.AsyncClient(trust_env=False, timeout=8) as http:
                    reply = await http.post(
                        "http://127.0.0.1:7860/api/v1/transport-calls",
                        data={"source": "telegram", "profile": ""},
                        headers={"Authorization": "Bearer " + settings.channel_api_key},
                    )
                    reply.raise_for_status()
                    registration = reply.json()
                await self.calls.play(
                    update.chat_id, MediaStream(ExternalMedia.AUDIO, AudioParameters(48000, 1))
                )
                await self.calls.record(
                    update.chat_id, RecordStream(audio=True, audio_parameters=AudioParameters(48000, 1))
                )
                await self.calls.unmute(update.chat_id)
                reader, writer = await asyncio.open_connection("127.0.0.1", registration["port"])
                writer.write(struct.pack("!BH", 1, 16) + UUID(registration["call_id"]).bytes)
                await writer.drain()
                connection = {
                    "peer": update.chat_id,
                    "reader": reader,
                    "writer": writer,
                    "lock": asyncio.Lock(),
                    "task": None,
                }
                self.bridge = connection
                connection["task"] = asyncio.create_task(self.play_audio(connection))
                event("telegram_call_connected")
            except Exception as exc:
                event("telegram_call_failed", error_type=type(exc).__name__)
                if connection:
                    await self.close_bridge(connection)
                with contextlib.suppress(Exception):
                    await self.calls.leave_call(update.chat_id)

    async def frames(self, client, update):
        bridge = self.bridge
        if not bridge or bridge["peer"] != update.chat_id:
            return
        try:
            async with bridge["lock"]:
                for frame in update.frames:
                    audio = frame.frame
                    for i in range(0, len(audio), 1920):
                        data = audio[i : i + 1920]
                        bridge["writer"].write(struct.pack("!BH", 0x16, len(data)) + data)
                async with asyncio.timeout(2):
                    await bridge["writer"].drain()
        except (ConnectionError, TimeoutError):
            await self.close_bridge(bridge)

    async def play_audio(self, bridge):
        try:
            while True:
                header = await bridge["reader"].readexactly(3)
                kind, size = struct.unpack("!BH", header)
                if size > 4096:
                    raise ValueError("Invalid audio packet")
                data = await bridge["reader"].readexactly(size)
                if kind == 0:
                    break
                if kind != 0x16 or len(data) % 2:
                    raise ValueError("Invalid audio format")
                for i in range(0, len(data), 960):
                    await self.calls.send_frame(
                        bridge["peer"], Device.MICROPHONE, data[i : i + 960].ljust(960, b"\0")
                    )
                    await asyncio.sleep(0.01)
        except (asyncio.IncompleteReadError, ConnectionError, ValueError):
            pass
        finally:
            await self.close_bridge(bridge)
            with contextlib.suppress(Exception):
                await self.calls.leave_call(bridge["peer"])

    async def ended(self, client, update):
        if self.bridge and self.bridge["peer"] == update.chat_id:
            await self.close_bridge(self.bridge)

    async def close_bridge(self, bridge):
        if self.bridge is bridge:
            self.bridge = None
        bridge["writer"].close()
        if bridge.get("task") and bridge["task"] is not asyncio.current_task():
            bridge["task"].cancel()
            await asyncio.gather(bridge["task"], return_exceptions=True)
        with contextlib.suppress(Exception):
            await bridge["writer"].wait_closed()

    async def logout(self):
        async with self.auth_lock:
            if self.bridge:
                await self.close_bridge(self.bridge)
            self.ready = False
            if self.client:
                await self.client.log_out()
                await self.client.disconnect()
            self.client = self.calls = None
            self.phone = self.phone_code_hash = None
            for path in self.directory.glob("account.session*"):
                path.unlink(missing_ok=True)
            (self.directory / "config.json").unlink(missing_ok=True)
            self.state = "not_configured"
            return {"state": self.state}

    async def close(self):
        self.ready = False
        if self.bridge:
            await self.close_bridge(self.bridge)
        if self.client:
            await self.client.disconnect()


engine = TelegramCalls()


def authorized(authorization: str = Header(default="")):
    if len(settings.channel_api_key) < 16 or not secrets.compare_digest(
        authorization.encode(), ("Bearer " + settings.channel_api_key).encode()
    ):
        raise HTTPException(401, "Unauthorized")


@asynccontextmanager
async def lifespan(app):
    restore = asyncio.create_task(engine.restore())
    yield
    restore.cancel()
    await asyncio.gather(restore, return_exceptions=True)
    await engine.close()


app = FastAPI(title="Private Telegram call adapter", lifespan=lifespan, docs_url=None, redoc_url=None)


@app.exception_handler(HTTPException)
async def error(request, exc):
    return JSONResponse({"error": {"message": exc.detail}}, status_code=exc.status_code)


@app.get("/health", dependencies=[Depends(authorized)])
async def health():
    return {
        "configured": (engine.directory / "config.json").exists(),
        "authorized": engine.ready,
        "ready": engine.ready,
        "state": engine.state,
    }


@app.post("/authorization", dependencies=[Depends(authorized)])
async def authorize(data: TelegramAuthorization):
    return await engine.authorize(data)


@app.post("/authorization/code", dependencies=[Depends(authorized)])
async def code(data: TelegramCode):
    return await engine.code(data)


@app.delete("/authorization", dependencies=[Depends(authorized)])
async def logout():
    return await engine.logout()
