"""Disposable test databases with the same restricted role used by the runtime."""

import os
import secrets
from contextlib import asynccontextmanager
from urllib.parse import quote, urlsplit, urlunsplit
from uuid import uuid4

import asyncpg

from app.database_security import provision_runtime_role
from app.migration_runner import apply_migrations


def test_admin_url():
    value = os.environ.get("TEST_DATABASE_ADMIN_URL")
    if not value:
        raise RuntimeError("Set TEST_DATABASE_ADMIN_URL to a disposable test PostgreSQL; runtime credentials are never used for test DDL")
    return value


def database_url(url, database, *, role=None, password=None):
    parsed = urlsplit(url)
    netloc = parsed.netloc
    if role is not None:
        host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
        netloc = quote(role, safe="") + ":" + quote(password, safe="") + "@" + host
        if parsed.port:
            netloc += ":" + str(parsed.port)
    return urlunsplit(parsed._replace(netloc=netloc, path="/" + database))


@asynccontextmanager
async def isolated_database(admin_url=None):
    """Never references settings.database_url or the real application database."""
    admin_url = admin_url or test_admin_url()
    suffix = uuid4().hex[:16]
    name, role, password = "voice_test_" + suffix, "voice_test_app_" + suffix, secrets.token_urlsafe(32)
    admin = await asyncpg.connect(admin_url, timeout=10)
    owner = None
    created = False
    try:
        await admin.execute(f'CREATE DATABASE "{name}"')
        created = True
        owner = await asyncpg.connect(database_url(admin_url, name), timeout=10)
        await apply_migrations(owner)
        await provision_runtime_role(owner, role, password)
        yield {
            "database": name, "role": role, "admin": owner,
            "runtime_url": database_url(admin_url, name, role=role, password=password),
        }
    finally:
        if owner is not None:
            await owner.close()
        if created:
            await admin.execute(f'DROP DATABASE "{name}" WITH (FORCE)')
        await admin.execute(f'DROP ROLE IF EXISTS "{role}"')
        await admin.close()
