"""Apply pinned SQL migrations with a separate administrative identity."""

import argparse
import asyncio
import json
import os
import stat
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import asyncpg  # noqa: E402

from app.migration_runner import MigrationError, apply_migrations, discover_migrations  # noqa: E402


def administrative_url(path=None):
    if path is None:
        value = os.environ.get("DATABASE_MIGRATION_URL", "")
    else:
        path = Path(path).expanduser()
        if path.is_symlink() or not path.is_file():
            raise MigrationError("Migration URL file must be a private regular file")
        if os.name != "nt" and stat.S_IMODE(path.stat().st_mode) & 0o077:
            raise MigrationError("Migration URL file must have permissions 0600")
        value = path.read_text(encoding="utf-8-sig").strip()
    if not value.startswith(("postgresql://", "postgres://")) or any(char.isspace() for char in value):
        raise MigrationError("Set DATABASE_MIGRATION_URL or provide a private --admin-url-file")
    return value


async def execute(args):
    conn = None
    try:
        conn = await asyncpg.connect(
            administrative_url(args.admin_url_file), command_timeout=120,
            server_settings={"search_path": "public,pg_catalog", "application_name": "voice-migrator"},
        )
        return await apply_migrations(conn, adopt_legacy=args.adopt_legacy_checksums)
    finally:
        if conn is not None:
            await conn.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--admin-url-file", type=Path)
    parser.add_argument("--adopt-legacy-checksums", action="store_true")
    parser.add_argument("--dry-run", action="store_true", help="List local versions and checksums without any connection")
    args = parser.parse_args(argv)
    try:
        if args.dry_run:
            print(json.dumps({"migrations": [
                {"version": item.version, "sha256": item.sha256} for item in discover_migrations()
            ], "database_connected": False}, indent=2))
        else:
            print(json.dumps(asyncio.run(execute(args)), sort_keys=True))
        return 0
    except MigrationError as exc:
        print(f"Migration refused: {exc}", file=sys.stderr)
    except Exception as exc:
        print(f"Migration failed ({type(exc).__name__}); credentials and database payload are not printed", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
