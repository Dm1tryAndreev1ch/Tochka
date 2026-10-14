import asyncio
import logging
from datetime import UTC, datetime, timedelta

import httpx

from app.background import run_background
from app.config import settings

log = logging.getLogger("voice.crm")


class CRMWorker:
    def __init__(self, pool):
        self.pool = pool
        self.http = httpx.AsyncClient(
            base_url=settings.espo_url + "/api/v1/",
            headers={"X-Api-Key": settings.espo_api_key},
            timeout=8,
            trust_env=False,
        )
        self.stop = asyncio.Event()
        self.last_ok = None
        self.status = {}

    async def sync_one(self):
        if not settings.espo_api_key:
            return False
        # Transaction plus advisory lock serialize writes and management updates.
        async with self.pool.acquire() as conn, conn.transaction():
            if not await conn.fetchval("SELECT pg_try_advisory_xact_lock(7860002)"):
                return False
            event = await conn.fetchrow(
                "SELECT * FROM outbox o WHERE status='pending' AND next_attempt_at<=now() AND (entity='Lead' OR external_id IS NOT NULL OR EXISTS(SELECT 1 FROM outbox lead WHERE lead.action_id=o.action_id AND lead.entity='Lead' AND lead.external_id IS NOT NULL)) ORDER BY created_at,CASE entity WHEN 'Lead' THEN 0 WHEN 'Call' THEN 1 ELSE 2 END FOR UPDATE SKIP LOCKED LIMIT 1"
            )
            if not event:
                return False
            await conn.execute("UPDATE outbox SET last_attempt_at=now() WHERE id=$1", event["id"])
            try:
                payload = dict(event["payload"])
                if settings.espo_assigned_user_id:
                    payload["assignedUserId"] = settings.espo_assigned_user_id
                if event["entity"] in ("Call", "Task"):
                    lead_id = await conn.fetchval(
                        "SELECT external_id FROM outbox WHERE action_id=$1 AND entity='Lead'",
                        event["action_id"],
                    )
                    if lead_id:
                        payload.update(parentType="Lead", parentId=lead_id)
                external_id = event["external_id"]
                if not external_id:
                    response = await self.http.get(
                        event["entity"],
                        params={
                            "where[0][type]": "equals",
                            "where[0][attribute]": "cVoiceActionId",
                            "where[0][value]": str(event["action_id"]),
                            "maxSize": 1,
                        },
                    )
                    response.raise_for_status()
                    items = response.json().get("list", [])
                    if items:
                        external_id = items[0]["id"]
                if external_id:
                    response = await self.http.put(event["entity"] + "/" + external_id, json=payload)
                else:
                    # Different verified actions may share a phone; identity is
                    # not inferred from it. The unique action field deduplicates retries.
                    response = await self.http.post(
                        event["entity"], json=payload, headers={"X-Skip-Duplicate-Check": "true"}
                    )
                response.raise_for_status()
                external_id = response.json()["id"]
                await conn.execute(
                    "UPDATE outbox SET status='synced',external_id=$2,error=NULL,attempts=attempts+1,synced_at=now() WHERE id=$1",
                    event["id"],
                    external_id,
                )
                self.last_ok = datetime.now(UTC)
                log.info("crm_synced entity=%s action_id=%s", event["entity"], event["action_id"])
            except (httpx.HTTPError, ValueError, KeyError) as exc:
                attempts = event["attempts"] + 1
                error = type(exc).__name__
                if isinstance(exc, httpx.HTTPStatusError):
                    error += f": HTTP {exc.response.status_code}"
                await conn.execute(
                    "UPDATE outbox SET attempts=$2,error=$3,next_attempt_at=$4 WHERE id=$1",
                    event["id"],
                    attempts,
                    error,
                    datetime.now(UTC) + timedelta(seconds=min(300, 2 ** min(attempts, 8))),
                )
                log.warning(
                    "crm_retry entity=%s action_id=%s error=%s", event["entity"], event["action_id"], error
                )
            return True

    async def run(self):
        await run_background("crm", self.sync_one, interval=2, stop=self.stop, status=self.status)

    async def close(self):
        self.stop.set()
        await self.http.aclose()
