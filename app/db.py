import json
from contextvars import ContextVar
from datetime import UTC, datetime

import asyncpg

from app.database_security import assert_runtime_role
from app.migration_runner import assert_current_schema

request_connection = ContextVar("voice_request_connection", default=None)


class RequestPool:
    def __init__(self, pool):
        self.raw = pool

    def __getattr__(self, name):
        context = request_connection.get()
        if (
            context
            and context.get("active")
            and name in ("execute", "fetch", "fetchrow", "fetchval", "executemany")
        ):
            return getattr(context["connection"], name)
        return getattr(self.raw, name)

    def acquire(self):
        raw = self.raw

        class Acquisition:
            async def __aenter__(self):
                context = request_connection.get()
                self.shared = bool(context and context.get("active"))
                self.connection = context["connection"] if self.shared else await raw.acquire()
                return self.connection

            async def __aexit__(self, *args):
                if not self.shared:
                    await raw.release(self.connection)

        return Acquisition()


async def connect(database_url):
    async def init(conn):
        for typename in ("json", "jsonb"):
            await conn.set_type_codec(typename, encoder=json.dumps, decoder=json.loads, schema="pg_catalog")

    pool = await asyncpg.create_pool(
        database_url, min_size=2, max_size=10, init=init, command_timeout=15,
        server_settings={"search_path": "pg_catalog,public", "application_name": "voice-runtime"},
    )
    try:
        async with pool.acquire() as conn:
            await assert_runtime_role(conn)
            await assert_current_schema(conn)
        from app.access import bootstrap_staff

        await bootstrap_staff(pool)
        return RequestPool(pool)
    except BaseException:
        await pool.close()
        raise


async def audit(
    conn, actor, event, resource_id=None, details=None, *, request_id=None, purpose=None, outcome="allowed"
):
    from app.config import settings
    from app.secure_store import SecureStore

    async def append(connection):
        await connection.execute("SELECT pg_advisory_xact_lock(7860003)")
        previous = (
            await connection.fetchval(
                "SELECT entry_hash FROM audit WHERE entry_hash IS NOT NULL ORDER BY id DESC LIMIT 1"
            )
            or ""
        )
        at = datetime.now(UTC)
        who = getattr(actor, "username", str(actor))
        uid = getattr(actor, "user_id", None)
        payload = {
            "actor": who,
            "user_id": str(uid) if uid else None,
            "event": event,
            "resource_id": str(resource_id) if resource_id else None,
            "details": details or {},
            "at": at.isoformat(),
            "previous": previous,
            "request_id": request_id,
            "purpose": purpose,
            "outcome": outcome,
        }
        digest = SecureStore(settings.private_runtime_dir).sign(
            json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
        )
        await connection.execute(
            "INSERT INTO audit(actor,user_id,event,resource_id,details,created_at,previous_hash,entry_hash,request_id,purpose,outcome) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)",
            who,
            uid,
            event,
            payload["resource_id"],
            payload["details"],
            at,
            previous,
            digest,
            request_id,
            purpose,
            outcome,
        )

    if hasattr(conn, "transaction"):
        if conn.is_in_transaction():
            await append(conn)
        else:
            async with conn.transaction():
                await append(conn)
    else:
        async with conn.acquire() as connection, connection.transaction():
            await append(connection)
