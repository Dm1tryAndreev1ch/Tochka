"""Apply migrations and switch .env to the dedicated, unprivileged runtime database role.

The compose PostgreSQL user (`voice`) is a superuser. The gateway refuses to run as it, so this script:

1. does nothing if DATABASE_URL already satisfies the runtime policy and the schema is current;
2. applies the pinned migrations with the administrative identity;
3. creates/updates the runtime role (default `voice_app`) with a random password;
4. rewrites only the DATABASE_URL line of .env (mode 0600 kept); passwords are never printed.

Administrative DSN: DATABASE_MIGRATION_URL if set, otherwise user `voice` with POSTGRES_PASSWORD on the
host/port/database of DATABASE_URL (the compose defaults).
"""

import argparse
import asyncio
import os
import re
import secrets
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import asyncpg  # noqa: E402
from dotenv import dotenv_values  # noqa: E402

from app.database_security import (  # noqa: E402
    DatabaseSecurityError,
    assert_runtime_role,
    provision_runtime_role,
)
from app.migration_runner import MigrationError, assert_current_schema  # noqa: E402


def with_credentials(url, user, password):
    parts = urlsplit(url)
    host = f"[{parts.hostname}]" if ":" in (parts.hostname or "") else parts.hostname
    netloc = quote(user, safe="") + ":" + quote(password, safe="") + "@" + host
    if parts.port:
        netloc += f":{parts.port}"
    return urlunsplit(parts._replace(netloc=netloc))


async def connect(url, attempts=30):
    """The database container may still be starting right after `docker compose up`."""
    last = None
    for _ in range(attempts):
        try:
            return await asyncpg.connect(url, timeout=5, command_timeout=60)
        except asyncpg.InvalidPasswordError:
            raise
        except (OSError, asyncpg.PostgresError, asyncio.TimeoutError) as exc:
            last = exc
            await asyncio.sleep(1)
    raise last


async def already_configured(url):
    try:
        conn = await connect(url, attempts=3)
    except (OSError, asyncpg.PostgresError, asyncio.TimeoutError):
        return False
    try:
        await assert_runtime_role(conn)
        await assert_current_schema(conn)
        return True
    except (DatabaseSecurityError, MigrationError, asyncpg.PostgresError):
        return False
    finally:
        await conn.close()


def run_migrations(admin_url, adopt_legacy):
    command = [sys.executable, str(ROOT / "scripts/migrate.py")]
    if adopt_legacy:
        command.append("--adopt-legacy-checksums")
    result = subprocess.run(command, env={**os.environ, "DATABASE_MIGRATION_URL": admin_url})
    return result.returncode == 0


def write_database_url(env_file, new_url):
    text = env_file.read_text(encoding="utf-8")
    line = "DATABASE_URL=" + new_url
    if re.search(r"^DATABASE_URL=.*$", text, flags=re.M):
        text = re.sub(r"^DATABASE_URL=.*$", lambda _: line, text, count=1, flags=re.M)
    else:
        text += ("" if text.endswith("\n") else "\n") + line + "\n"
    mode = env_file.stat().st_mode & 0o777
    handle, temporary = tempfile.mkstemp(dir=env_file.parent, prefix=".env.")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(text)
        os.chmod(temporary, mode or 0o600)
        os.replace(temporary, env_file)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


async def main_async(args):
    env_file = args.env_file
    if not env_file.is_file():
        raise SystemExit(f"{env_file} not found: run scripts/setup.py first")
    values = dotenv_values(env_file)
    database_url = values.get("DATABASE_URL") or ""
    if not database_url:
        raise SystemExit("DATABASE_URL is missing in " + str(env_file))
    if await already_configured(database_url):
        print("Database is already configured for the runtime role and the schema is current; nothing to do.")
        return 0

    admin_url = os.environ.get("DATABASE_MIGRATION_URL")
    if not admin_url:
        password = values.get("POSTGRES_PASSWORD")
        if not password:
            raise SystemExit("Set DATABASE_MIGRATION_URL or POSTGRES_PASSWORD (administrative database access)")
        admin_url = with_credentials(database_url, "voice", password)

    print("Applying migrations with the administrative identity...")
    if not await asyncio.to_thread(run_migrations, admin_url, args.adopt_legacy_checksums):
        print(
            "\nMigrations were not applied (see the message above).\n"
            "If it says legacy migration checksums need a baseline, your database was created before checksums "
            "existed. Take a backup, then confirm that you trust its schema and re-run with "
            "--adopt-legacy-checksums.",
            file=sys.stderr,
        )
        return 1

    current_user = urlsplit(database_url).username
    password = (
        urlsplit(database_url).password
        if current_user == args.runtime_role and urlsplit(database_url).password
        else secrets.token_urlsafe(32)
    )
    admin = await connect(admin_url)
    try:
        await provision_runtime_role(admin, args.runtime_role, password)
    finally:
        await admin.close()
    new_url = with_credentials(database_url, args.runtime_role, password)
    if not await already_configured(new_url):
        print("The new runtime role could not be verified; .env was left unchanged.", file=sys.stderr)
        return 1
    write_database_url(env_file, new_url)
    print(f"Done: DATABASE_URL in {env_file.name} now uses role '{args.runtime_role}' (previously '{current_user}').")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env")
    parser.add_argument("--runtime-role", default="voice_app")
    parser.add_argument(
        "--adopt-legacy-checksums",
        action="store_true",
        help="Trust the applied legacy migrations and record their checksums (take a backup first)",
    )
    args = parser.parse_args(argv)
    try:
        return asyncio.run(main_async(args))
    except asyncpg.InvalidPasswordError:
        print("Administrative database authentication failed; check POSTGRES_PASSWORD / DATABASE_MIGRATION_URL.", file=sys.stderr)
    except (DatabaseSecurityError, MigrationError, OSError, asyncpg.PostgresError) as exc:
        print(f"Database setup failed: {type(exc).__name__}: {exc}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
