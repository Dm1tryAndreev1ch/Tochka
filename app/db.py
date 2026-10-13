import json
from pathlib import Path

import asyncpg


async def connect(database_url):
    async def init(conn):
        for typename in ("json", "jsonb"):
            await conn.set_type_codec(typename, encoder=json.dumps, decoder=json.loads, schema="pg_catalog")

    pool = await asyncpg.create_pool(database_url, min_size=2, max_size=10, init=init, command_timeout=15)
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(7860001)")
            await conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations(version int PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
            )
            for path in sorted((Path(__file__).parent / "migrations").glob("*.sql")):
                version = int(path.name.split("_")[0])
                if not await conn.fetchval("SELECT 1 FROM schema_migrations WHERE version=$1", version):
                    await conn.execute(path.read_text())
                    await conn.execute("INSERT INTO schema_migrations(version) VALUES($1)", version)
    return pool


async def audit(conn, actor, event, resource_id=None, details=None):
    await conn.execute(
        "INSERT INTO audit(actor,event,resource_id,details) VALUES($1,$2,$3,$4)",
        actor,
        event,
        str(resource_id) if resource_id else None,
        details or {},
    )
