"""Explicit database provisioning and fail-closed runtime privilege checks."""

import re

import asyncpg

ROLE_MARKER = "voice-runtime-role-v1"
TABLES = (
    "schema_migrations", "services", "settings", "sessions", "turns", "drafts",
    "actions", "bookings", "requests", "knowledge", "outbox", "audit",
    "admin_sessions", "company", "staff_users", "session_usage", "archive_entries", "agents",
)
WRITABLE = (
    "services", "settings", "sessions", "turns", "drafts", "actions", "bookings",
    "requests", "knowledge", "outbox", "staff_users", "session_usage", "archive_entries",
)


class DatabaseSecurityError(RuntimeError):
    """A safe diagnostic that contains neither a DSN nor database error payloads."""


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", value) or value.startswith("pg_"):
        raise DatabaseSecurityError("Use a lowercase database role identifier of at most 63 characters")
    return '"' + value + '"'


def _quoted(value):
    return '"' + value.replace('"', '""') + '"'


ROLE_QUERY = """
SELECT oid, rolname, rolsuper, rolcreatedb, rolcreaterole, rolreplication,
       rolbypassrls, rolcanlogin, rolinherit,
       pg_catalog.shobj_description(oid, 'pg_authid') AS marker
FROM pg_catalog.pg_roles WHERE rolname=$1
"""


async def _assert_role_policy(conn, role):
    if not role or not role["rolcanlogin"] or any(role[key] for key in (
        "rolsuper", "rolcreatedb", "rolcreaterole", "rolreplication", "rolbypassrls",
    )):
        raise DatabaseSecurityError("Runtime database role must be an unprivileged login")
    oid = role["oid"]
    if await conn.fetchval("SELECT EXISTS(SELECT 1 FROM pg_catalog.pg_auth_members WHERE member=$1)", oid):
        raise DatabaseSecurityError("Runtime database role must not belong to other roles")
    if await conn.fetchval("""
        SELECT EXISTS(SELECT 1 FROM pg_catalog.pg_database WHERE datdba=$1)
          OR pg_catalog.has_database_privilege($1,oid,'CREATE,TEMP')
        FROM pg_catalog.pg_database WHERE datname=pg_catalog.current_database()
    """, oid):
        raise DatabaseSecurityError("Runtime database role must not own the database or have CREATE/TEMP privileges")
    if await conn.fetchval("""
        SELECT EXISTS(SELECT 1 FROM pg_catalog.pg_namespace
          WHERE nspowner=$1 OR pg_catalog.has_schema_privilege($1,oid,'CREATE'))
    """, oid):
        raise DatabaseSecurityError("Runtime database role must not own or create objects in a schema")
    if await conn.fetchval("""
        SELECT EXISTS(SELECT 1 FROM pg_catalog.pg_class WHERE relowner=$1)
        OR EXISTS(SELECT 1 FROM pg_catalog.pg_proc WHERE proowner=$1)
        OR EXISTS(SELECT 1 FROM pg_catalog.pg_type WHERE typowner=$1)
        OR EXISTS(SELECT 1 FROM pg_catalog.pg_extension WHERE extowner=$1)
    """, oid):
        raise DatabaseSecurityError("Runtime database role must not own database objects")
    if await conn.fetchval("""
        SELECT EXISTS(SELECT 1 FROM pg_catalog.pg_class c
          JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
          WHERE n.nspname='public' AND c.relkind IN ('r','p','v','m','f')
          AND CASE WHEN c.relkind IN ('r','p','v','m','f') THEN (pg_catalog.has_table_privilege($1::oid,c.oid,'TRUNCATE,TRIGGER,REFERENCES')
            OR (c.relname='audit' AND (pg_catalog.has_table_privilege($1,c.oid,'UPDATE,DELETE')
                OR pg_catalog.has_any_column_privilege($1,c.oid,'UPDATE')))
            OR (c.relname='schema_migrations' AND (pg_catalog.has_table_privilege($1,c.oid,'INSERT,UPDATE,DELETE')
                OR pg_catalog.has_any_column_privilege($1,c.oid,'INSERT,UPDATE')))
            OR pg_catalog.has_table_privilege($1,c.oid,
                'SELECT WITH GRANT OPTION,INSERT WITH GRANT OPTION,UPDATE WITH GRANT OPTION,DELETE WITH GRANT OPTION'))
            ELSE false END)
        OR EXISTS(SELECT 1 FROM pg_catalog.pg_class c
          JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
          WHERE n.nspname='public' AND c.relkind='S'
          AND CASE WHEN c.relkind='S' THEN pg_catalog.has_sequence_privilege($1,c.oid,'UPDATE') ELSE false END)
    """, oid):
        raise DatabaseSecurityError("Runtime database role has unsafe table, audit, migration or sequence privileges")


async def assert_runtime_role(conn):
    """Validate effective privileges before exposing a runtime connection pool."""
    try:
        identity = await conn.fetchrow("SELECT current_user AS role, session_user AS login")
        if identity["role"] != identity["login"]:
            raise DatabaseSecurityError("Runtime database login must not use a different effective role")
        role = await conn.fetchrow(ROLE_QUERY, identity["role"])
        await _assert_role_policy(conn, role)
    except asyncpg.PostgresError:
        raise DatabaseSecurityError("Unable to verify runtime database privileges") from None


async def provision_runtime_role(conn, runtime_role, password):
    """Grant only current application capabilities; keep legacy object ownership.

    The caller explicitly administers this database. PUBLIC CREATE/TEMP is revoked
    here because inherited PUBLIC privileges cannot be denied to just one role.
    New tables receive no automatic runtime grants: each migration reviews them.
    """
    target = identifier(runtime_role)
    if not isinstance(password, str) or len(password) < 32 or any(not c.isprintable() or c.isspace() for c in password):
        raise DatabaseSecurityError("Runtime password must contain at least 32 printable non-whitespace characters")
    try:
        async with conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(7860006)")
            database = await conn.fetchval("SELECT pg_catalog.current_database()")
            present = await conn.fetch("SELECT tablename FROM pg_catalog.pg_tables WHERE schemaname='public'")
            # asyncpg Record and small test doubles both support named access.
            names = {row["tablename"] for row in present}
            if not set(TABLES) <= names:
                raise DatabaseSecurityError("Apply the complete project migrations before provisioning the runtime role")
            role = await conn.fetchrow(ROLE_QUERY, runtime_role)
            if role:
                if role["marker"] != ROLE_MARKER or role["rolinherit"]:
                    raise DatabaseSecurityError("Existing role is not a recognized dedicated runtime role")
                await _assert_role_policy(conn, role)
            secret = "'" + password.replace("'", "''") + "'"
            if role is None:
                await conn.execute(f"CREATE ROLE {target} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS NOINHERIT PASSWORD {secret}")
                await conn.execute(f"COMMENT ON ROLE {target} IS '{ROLE_MARKER}'")
            else:
                await conn.execute(f"ALTER ROLE {target} PASSWORD {secret}")
            db_name = _quoted(database)
            await conn.execute(f"REVOKE CREATE,TEMP ON DATABASE {db_name} FROM PUBLIC")
            await conn.execute(f"REVOKE ALL PRIVILEGES ON DATABASE {db_name} FROM {target}")
            await conn.execute(f"GRANT CONNECT ON DATABASE {db_name} TO {target}")
            await conn.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
            await conn.execute(f"REVOKE ALL PRIVILEGES ON SCHEMA public FROM {target}")
            await conn.execute(f"GRANT USAGE ON SCHEMA public TO {target}")
            await conn.execute(f"REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM {target}")
            await conn.execute(f"REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM {target}")
            await conn.execute(f"REVOKE ALL PRIVILEGES ON ALL FUNCTIONS IN SCHEMA public FROM {target}")
            def tables(values):
                return ",".join("public." + _quoted(value) for value in values)

            await conn.execute(f"GRANT SELECT ON {tables(TABLES)} TO {target}")
            await conn.execute(f"GRANT INSERT,UPDATE ON {tables(WRITABLE)} TO {target}")
            await conn.execute(f"GRANT UPDATE ON public.company,public.agents TO {target}")
            await conn.execute(f"GRANT INSERT ON public.admin_sessions,public.audit TO {target}")
            await conn.execute(f"GRANT DELETE ON public.sessions,public.turns,public.admin_sessions TO {target}")
            await conn.execute(f"GRANT USAGE ON SEQUENCE public.turns_id_seq,public.audit_id_seq TO {target}")
            await conn.execute("ALTER DEFAULT PRIVILEGES REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC")
            await conn.execute(f"ALTER ROLE {target} IN DATABASE {db_name} SET search_path TO pg_catalog,public")
            await _assert_role_policy(conn, await conn.fetchrow(ROLE_QUERY, runtime_role))
    except asyncpg.PostgresError:
        raise DatabaseSecurityError("Runtime role provisioning failed; its transaction was rolled back") from None
