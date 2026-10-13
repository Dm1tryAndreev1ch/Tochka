"""Persisted channel selection shared by website, Telegram and SIP admission."""

import asyncio
import json
from pathlib import Path

import httpx

from app.config import settings
from app.db import audit
from app.errors import AppError


class CallModes:
    def __init__(self, pool):
        self.pool = pool

    async def selection(self):
        return await self.pool.fetchval("SELECT value FROM settings WHERE key='call_mode'") or {
            "mode": "website",
            "revision": 1,
        }

    async def require(self, source):
        if (await self.selection())["mode"] != source:
            raise AppError("channel_disabled", "Этот канал звонков сейчас выключен администратором", 409)

    async def update(self, mode, revision, incoming_voice=None):
        async with self.pool.acquire() as conn, conn.transaction():
            current = await conn.fetchval("SELECT value FROM settings WHERE key='call_mode' FOR UPDATE")
            current = current or {"mode": "website", "revision": 1}
            if current["revision"] != revision:
                raise AppError("revision_conflict", "Настройки изменились. Обновите страницу", 409)
            result = {"mode": mode, "revision": revision + 1}
            result["incoming_voice"] = incoming_voice or current.get("incoming_voice", "male")
            await conn.execute(
                "INSERT INTO settings(key,value) VALUES('call_mode',$1) ON CONFLICT(key) DO UPDATE SET value=$1",
                result,
            )
            await audit(conn, "admin", "call_mode_changed", details={"mode": mode})
            return result

    async def status(self):
        selection = await self.selection()
        telegram = {"configured": False, "authorized": False, "ready": False, "state": "unavailable"}
        try:
            async with httpx.AsyncClient(trust_env=False, timeout=2) as client:
                response = await client.get(
                    settings.telegram_worker_url + "/health",
                    headers={"Authorization": "Bearer " + settings.channel_api_key},
                )
                if response.status_code == 200:
                    telegram = response.json()
        except (httpx.HTTPError, ValueError):
            pass
        sip_path = Path(settings.private_runtime_dir) / "sip/metadata.json"
        sip = {"configured": sip_path.is_file(), "ready": False}
        if sip["configured"]:
            metadata = json.loads(sip_path.read_text())
            sip.update({k: metadata[k] for k in ("host", "port", "extensions")})
            # A bound listener and the configured PBX are reported independently.
            sip["pbx_ready"] = False
            try:
                reader, writer = await asyncio.wait_for(asyncio.open_connection(sip["host"], sip["port"]), 1)
                writer.close()
                await writer.wait_closed()
                sip["pbx_ready"] = True
            except (TimeoutError, OSError):
                pass
            sip["audio_bridge_ready"] = (
                settings.socket_transport_enabled and len(settings.channel_api_key) >= 16
            )
            sip["ready"] = sip["pbx_ready"] and sip["audio_bridge_ready"]
        return {**selection, "channels": {"website": {"ready": True}, "telegram": telegram, "sip": sip}}
