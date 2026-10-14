"""Explicit schema administration; the application runtime never invokes this writer."""

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

import asyncpg

MIGRATIONS = Path(__file__).parent / "migrations"
MIGRATION_LOCK = 7860001


class MigrationError(RuntimeError):
    """A safe diagnostic that never contains connection credentials or SQL data."""


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sql: str
    sha256: str


def discover_migrations(directory=MIGRATIONS):
    result = []
    for path in sorted(Path(directory).glob("*.sql")):
        match = re.fullmatch(r"([0-9]{3,})_[a-z0-9_]+\.sql", path.name)
        if not match:
            raise MigrationError("Invalid migration filename")
        content = path.read_bytes()
        result.append(Migration(int(match[1]), path.name, content.decode("utf-8"), hashlib.sha256(content).hexdigest()))
    result.sort(key=lambda migration: migration.version)
    if not result or [item.version for item in result] != list(range(1, len(result) + 1)):
        raise MigrationError("Migration versions must form one complete sequence without duplicates")
    return result


def check_history(rows, migrations, *, require_complete=False, allow_legacy=False):
    catalog = {item.version: item for item in migrations}
    versions = [row["version"] for row in rows]
    if versions != list(range(1, len(versions) + 1)) or any(version not in catalog for version in versions):
        raise MigrationError("Database migration history is incomplete or newer than this code")
    if require_complete and len(versions) != len(migrations):
        raise MigrationError("Database schema is not current; run the separate migration command")
    legacy = []
    for row in rows:
        checksum = row.get("source_sha256")
        if checksum is None:
            if not allow_legacy:
                raise MigrationError("Legacy migration checksums need an explicit administrative baseline")
            legacy.append(row["version"])
        elif checksum != catalog[row["version"]].sha256:
            raise MigrationError(f"Migration checksum mismatch for version {row['version']}")
        elif row.get("checksum_origin") not in {"applied", "legacy_baseline"}:
            raise MigrationError("Migration checksum provenance is invalid")
    return legacy


async def apply_migrations(conn, directory=MIGRATIONS, *, adopt_legacy=False):
    """Use a dedicated owner connection; retain one serialized, atomic migration run."""
    migrations = discover_migrations(directory)
    async with conn.transaction():
        await conn.execute("SET LOCAL search_path TO public,pg_catalog")
        await conn.execute("SELECT pg_advisory_xact_lock($1)", MIGRATION_LOCK)
        await conn.execute(
            "CREATE TABLE IF NOT EXISTS public.schema_migrations "
            "(version int PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
        )
        await conn.execute(
            "ALTER TABLE public.schema_migrations "
            "ADD COLUMN IF NOT EXISTS source_sha256 text, "
            "ADD COLUMN IF NOT EXISTS checksum_origin text, "
            "ADD COLUMN IF NOT EXISTS checksum_recorded_at timestamptz"
        )
        rows = await conn.fetch(
            "SELECT version,source_sha256,checksum_origin FROM public.schema_migrations ORDER BY version"
        )
        legacy = check_history(rows, migrations, allow_legacy=adopt_legacy)
        for version in legacy:
            await conn.execute(
                "UPDATE public.schema_migrations SET source_sha256=$2, "
                "checksum_origin='legacy_baseline', checksum_recorded_at=now() WHERE version=$1",
                version, migrations[version - 1].sha256,
            )
        applied = []
        for migration in migrations[len(rows):]:
            await conn.execute(migration.sql)
            await conn.execute(
                "INSERT INTO public.schema_migrations(version,source_sha256,checksum_origin,checksum_recorded_at) "
                "VALUES($1,$2,'applied',now())", migration.version, migration.sha256,
            )
            applied.append(migration.version)
    return {"applied": applied, "legacy_baselined": legacy, "latest_version": migrations[-1].version}


async def assert_current_schema(conn, directory=MIGRATIONS):
    """Read-only gate; no CREATE, ALTER, UPDATE or automatic repair occurs here."""
    migrations = discover_migrations(directory)
    try:
        rows = await conn.fetch(
            "SELECT version,source_sha256,checksum_origin FROM public.schema_migrations ORDER BY version"
        )
    except (asyncpg.UndefinedTableError, asyncpg.UndefinedColumnError) as exc:
        raise MigrationError("Schema is not prepared; run scripts/migrate.py with the migration identity") from exc
    check_history(rows, migrations, require_complete=True)

