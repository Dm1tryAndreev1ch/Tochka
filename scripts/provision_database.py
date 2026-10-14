"""Provision a dedicated runtime database role; never load or rewrite application .env."""

import argparse
import asyncio
import os
import stat
import sys
from pathlib import Path

import asyncpg

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.database_security import DatabaseSecurityError, provision_runtime_role


def private_value(path, variable):
    if path:
        path = Path(path).expanduser()
        if path.is_symlink() or not path.is_file():
            raise DatabaseSecurityError("Credential file must be a private regular file")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        descriptor = os.open(path, flags)
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > 65536:
                raise DatabaseSecurityError("Credential file must be a small regular file")
            if os.name != "nt" and (stat.S_IMODE(metadata.st_mode) & 0o077 or metadata.st_uid != os.getuid()):
                raise DatabaseSecurityError("Credential file must be owned by this user with private permissions (0600)")
            with os.fdopen(descriptor, "r", encoding="utf-8-sig") as stream:
                descriptor = None
                value = stream.read(65537).strip()
        finally:
            if descriptor is not None:
                os.close(descriptor)
    else:
        value = os.environ.get(variable, "")
    if not value:
        raise DatabaseSecurityError(f"Supply a private file or {variable}")
    if variable == "DATABASE_MIGRATION_URL" and (
        not value.startswith(("postgresql://", "postgres://")) or any(c.isspace() for c in value)
    ):
        raise DatabaseSecurityError("Migration credential must be a PostgreSQL URL without whitespace")
    return value


async def provision(args):
    admin_url = private_value(args.admin_url_file, "DATABASE_MIGRATION_URL")
    password = private_value(args.password_file, "DATABASE_RUNTIME_PASSWORD")
    conn = None
    try:
        conn = await asyncpg.connect(admin_url, timeout=10, command_timeout=60)
        await provision_runtime_role(conn, args.runtime_role, password)
    finally:
        if conn is not None:
            await conn.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--admin-url-file", type=Path, help="Private file containing only the migration/admin DSN")
    parser.add_argument("--password-file", type=Path, help="Private file containing only the new runtime password")
    parser.add_argument("--runtime-role", default="voice_app")
    parser.add_argument("--apply", action="store_true", help="Apply role grants and revoke PUBLIC CREATE/TEMP on this database")
    args = parser.parse_args(argv)
    if not args.apply:
        print("Plan: create/update the dedicated runtime role; grant current application DML; protect audit/migrations;")
        print("revoke PUBLIC CREATE/TEMP on the target database and CREATE on public schema. Other roles may lose TEMP.")
        print("Object ownership and application .env stay unchanged. Pass --apply after reviewing the target database.")
        return 0
    try:
        asyncio.run(provision(args))
    except (DatabaseSecurityError, OSError, ValueError, asyncpg.PostgresError):
        print("Database provisioning failed. Check private credentials, role policy, privileges and migration state.", file=sys.stderr)
        return 1
    print("Runtime role provisioned. Configure the runtime DSN separately; credentials were not printed or written.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
