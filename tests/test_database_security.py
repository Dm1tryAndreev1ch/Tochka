import os
from contextlib import asynccontextmanager

import asyncpg
import pytest

from app import database_security as security
from scripts import provision_database as cli


def role(**changes):
    metadata = dict(oid=123, rolname="voice_app", rolsuper=False, rolcreatedb=False,
                    rolcreaterole=False, rolreplication=False, rolbypassrls=False,
                    rolcanlogin=True, rolinherit=False, marker=security.ROLE_MARKER)
    metadata.update(changes)
    return metadata


class Connection:
    def __init__(self, existing=None, unsafe_at=None):
        self.existing = existing
        self.unsafe_at = unsafe_at
        self.policy_checks = 0
        self.commands = []
        self.transactions = 0

    @asynccontextmanager
    async def transaction(self):
        self.transactions += 1
        yield

    async def fetch(self, query):
        return [{"tablename": name} for name in security.TABLES]

    async def fetchrow(self, query, *args):
        if "session_user" in query:
            return {"role": "voice_app", "login": "voice_app"}
        return self.existing

    async def fetchval(self, query, *args):
        if query == "SELECT pg_catalog.current_database()":
            return "voice"
        self.policy_checks += 1
        return self.policy_checks == self.unsafe_at

    async def execute(self, query):
        self.commands.append(query)
        if query.startswith("CREATE ROLE"):
            self.existing = role()


@pytest.mark.parametrize("attribute", ["rolsuper", "rolcreatedb", "rolcreaterole", "rolreplication", "rolbypassrls"])
async def test_runtime_refuses_privileged_role_attributes(attribute):
    metadata = role()
    metadata[attribute] = True
    with pytest.raises(security.DatabaseSecurityError, match="unprivileged"):
        await security.assert_runtime_role(Connection(metadata))


@pytest.mark.parametrize("check", range(1, 6))
async def test_runtime_refuses_membership_ddl_ownership_and_unsafe_audit_grants(check):
    with pytest.raises(security.DatabaseSecurityError):
        await security.assert_runtime_role(Connection(role(), unsafe_at=check))


async def test_runtime_accepts_unprivileged_role_without_membership():
    await security.assert_runtime_role(Connection(role()))


async def test_runtime_refuses_set_role_session_that_can_reset_to_owner():
    conn = Connection(role())

    async def identity(*_):
        return {"role": "voice_app", "login": "voice_owner"}

    conn.fetchrow = identity
    with pytest.raises(security.DatabaseSecurityError, match="different effective role"):
        await security.assert_runtime_role(conn)


async def test_provision_grants_only_needed_mutations_and_keeps_ownership():
    conn = Connection()
    await security.provision_runtime_role(conn, "voice_app", "s" * 40)
    assert conn.transactions == 1
    assert conn.commands[0] == "SELECT pg_advisory_xact_lock(7860006)"
    assert any("NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS NOINHERIT" in q for q in conn.commands)
    grants = [q for q in conn.commands if q.startswith("GRANT")]
    assert not any("ALL" in q or "TRUNCATE" in q or "TRIGGER" in q or "REFERENCES" in q for q in grants)
    assert 'GRANT DELETE ON public.sessions,public.turns,public.admin_sessions TO "voice_app"' in grants
    assert 'GRANT INSERT ON public.admin_sessions,public.audit TO "voice_app"' in grants
    assert not any("UPDATE" in q and "public.audit" in q for q in grants)
    assert not any("UPDATE" in q and "schema_migrations" in q for q in grants)
    assert not any("REASSIGN" in q or "OWNER TO" in q for q in conn.commands)
    assert 'REVOKE CREATE,TEMP ON DATABASE "voice" FROM PUBLIC' in conn.commands
    assert not any("IN SCHEMA" in q for q in conn.commands if q.startswith("ALTER DEFAULT PRIVILEGES"))


async def test_recognized_role_can_be_provisioned_again_without_recreation():
    conn = Connection(role())
    await security.provision_runtime_role(conn, "voice_app", "s" * 40)
    assert not any(q.startswith("CREATE ROLE") for q in conn.commands)
    assert any(q.startswith("ALTER ROLE") and "PASSWORD" in q for q in conn.commands)


async def test_provision_does_not_adopt_existing_unrecognized_login():
    metadata = role()
    metadata["marker"] = None
    conn = Connection(metadata)
    with pytest.raises(security.DatabaseSecurityError, match="recognized"):
        await security.provision_runtime_role(conn, "voice_app", "s" * 40)
    assert not any("PASSWORD" in q or q.startswith("GRANT") for q in conn.commands)


@pytest.mark.parametrize("name", ["voice;DROP ROLE postgres", 'voice"app', "pg_unsafe", "a" * 64, ""])
async def test_provision_rejects_role_identifier_injection(name):
    conn = Connection()
    with pytest.raises(security.DatabaseSecurityError):
        await security.provision_runtime_role(conn, name, "s" * 40)
    assert conn.transactions == 0


async def test_database_errors_are_redacted():
    conn = Connection(role())

    async def fail(*_):
        raise asyncpg.PostgresError("postgresql://owner:highly-sensitive@host/database")

    conn.fetchrow = fail
    with pytest.raises(security.DatabaseSecurityError) as caught:
        await security.assert_runtime_role(conn)
    assert "highly-sensitive" not in str(caught.value)
    assert caught.value.__suppress_context__


def test_cli_plan_does_not_read_credentials_or_connect(monkeypatch, capsys):
    def forbidden(*_):
        pytest.fail("Plan attempted credential access or a connection")

    monkeypatch.setattr(cli, "private_value", forbidden)
    monkeypatch.setattr(cli.asyncio, "run", forbidden)
    assert cli.main([]) == 0
    assert "Other roles may lose TEMP" in capsys.readouterr().out


def test_cli_failure_never_prints_dsn_or_password(monkeypatch, capsys):
    async def fail(_):
        raise ValueError("postgresql://owner:highly-sensitive@host/database")

    monkeypatch.setattr(cli, "provision", fail)
    assert cli.main(["--apply"]) == 1
    captured = capsys.readouterr()
    assert "highly-sensitive" not in captured.err + captured.out


def test_provision_reads_private_regular_file_without_app_dotenv(tmp_path):
    path = tmp_path / "private-password"
    path.write_text("s" * 40 + "\n")
    path.chmod(0o600)
    assert cli.private_value(path, "DATABASE_RUNTIME_PASSWORD") == "s" * 40


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode check; Windows requires native ACL management")
def test_provision_refuses_group_readable_credential_file(tmp_path):
    path = tmp_path / "private-password"
    path.write_text("s" * 40)
    path.chmod(0o640)
    with pytest.raises(security.DatabaseSecurityError, match="private permissions"):
        cli.private_value(path, "DATABASE_RUNTIME_PASSWORD")


def test_provision_refuses_symlink_credentials(tmp_path):
    path = tmp_path / "private-password"
    path.write_text("s" * 40)
    path.chmod(0o600)
    alias = tmp_path / "alias"
    try:
        alias.symlink_to(path)
    except OSError:
        pytest.skip("Symlink creation unavailable on this platform")
    with pytest.raises(security.DatabaseSecurityError, match="regular file"):
        cli.private_value(alias, "DATABASE_RUNTIME_PASSWORD")
