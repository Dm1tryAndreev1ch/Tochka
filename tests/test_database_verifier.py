import io
import json
import subprocess
from types import SimpleNamespace

import pytest

from scripts import verify_database_isolation as verify


class FakeDocker:
    container_id = "a" * 64
    image_id = "sha256:" + "b" * 64

    def __init__(self):
        self.calls = []
        self.image_present = True
        self.interrupt_start = False
        self.ambiguous_create = False
        self.foreign_label = False
        self.binding_ip = "127.0.0.1"

    def run(self, command, **kwargs):
        self.calls.append((command, kwargs))
        assert command[0] == "docker"
        assert kwargs["env"]["POSTGRES_PASSWORD"] not in command
        action = command[1]
        output = ""
        code = 0
        if action == "image":
            assert command[2:4] == ["inspect", "postgres:16"]
            code = 0 if self.image_present else 1
            output = self.image_id if self.image_present else "No such image"
        elif action == "create":
            self.name = command[command.index("--name") + 1]
            self.owner = command[command.index("--label") + 1].split("=", 1)[1]
            if self.ambiguous_create:
                raise subprocess.TimeoutExpired(command, 15)
            output = self.container_id
        elif action == "start" and self.interrupt_start:
            raise KeyboardInterrupt
        elif action == "inspect":
            if command[-1] == self.container_id:
                output = json.dumps({"5432/tcp": [{"HostIp": self.binding_ip, "HostPort": "54321"}]})
            else:
                output = json.dumps({
                    "Id": self.container_id, "Name": "/" + self.name,
                    "Config": {"Labels": {verify.OWNER_LABEL: "someone-else" if self.foreign_label else self.owner}},
                })
        elif action not in {"start", "exec", "rm"}:
            pytest.fail("Unexpected Docker command")
        return SimpleNamespace(returncode=code, stdout=output, stderr="")


@pytest.fixture
def docker(monkeypatch):
    fake = FakeDocker()
    monkeypatch.setattr(verify.subprocess, "run", fake.run)
    return fake


def test_disposable_container_is_pinned_loopback_tmpfs_bounded_and_removed_by_id(docker):
    with verify.DisposablePostgres() as database:
        assert "@127.0.0.1:54321/postgres" in database.url
        password = database.password
    create, options = next(call for call in docker.calls if call[0][1] == "create")
    assert create[-1] == docker.image_id
    assert "--pull=never" in create
    assert create[create.index("--publish") + 1] == "127.0.0.1::5432"
    assert create[create.index("--memory") + 1] == "512m"
    assert create[create.index("--pids-limit") + 1] == "128"
    assert create[create.index("--tmpfs") + 1].startswith("/var/lib/postgresql/data:")
    assert create[create.index("--env") + 1] == "POSTGRES_PASSWORD"
    assert options["env"]["POSTGRES_PASSWORD"] == password
    assert all(password not in " ".join(command) for command, _ in docker.calls)
    assert not any("compose" in command or "--volume" in command for command, _ in docker.calls)
    assert docker.calls[-1][0] == ["docker", "rm", "-f", docker.container_id]


def test_missing_local_image_never_downloads_or_creates_container(docker):
    docker.image_present = False
    with pytest.raises(verify.VerificationError, match="No such image"):
        with verify.DisposablePostgres():
            pytest.fail("Missing image accepted")
    assert len(docker.calls) == 1


def test_interrupt_after_create_still_removes_only_owned_container(docker):
    docker.interrupt_start = True
    with pytest.raises(KeyboardInterrupt):
        with verify.DisposablePostgres():
            pytest.fail("Interrupted startup succeeded")
    assert docker.calls[-1][0] == ["docker", "rm", "-f", docker.container_id]


def test_ambiguous_create_recovers_verified_id_and_cleans_it(docker):
    docker.ambiguous_create = True
    with pytest.raises(subprocess.TimeoutExpired):
        with verify.DisposablePostgres():
            pytest.fail("Ambiguous create succeeded")
    assert docker.calls[-2][0][1] == "inspect"
    assert docker.calls[-1][0] == ["docker", "rm", "-f", docker.container_id]


def test_recovery_never_removes_a_container_with_another_ownership_label(docker):
    docker.ambiguous_create = True
    docker.foreign_label = True
    with pytest.raises(verify.VerificationError, match="ownership"):
        with verify.DisposablePostgres():
            pytest.fail("Foreign container accepted")
    assert all(command[1] != "rm" for command, _ in docker.calls)


def test_nonloopback_binding_is_rejected_and_owned_container_cleaned(docker):
    docker.binding_ip = "0.0.0.0"
    with pytest.raises(verify.VerificationError, match="loopback"):
        with verify.DisposablePostgres():
            pytest.fail("Public binding accepted")
    assert docker.calls[-1][0] == ["docker", "rm", "-f", docker.container_id]


def test_readiness_timeout_cleans_container_without_running_tests(docker):
    with pytest.raises(verify.VerificationError, match="ready"):
        with verify.DisposablePostgres(readiness_timeout=0):
            pytest.fail("Unready database accepted")
    assert docker.calls[-1][0] == ["docker", "rm", "-f", docker.container_id]


def test_pytest_environment_uses_only_disposable_credentials(tmp_path, monkeypatch):
    for name in ("DATABASE_URL", "TEST_DATABASE_ADMIN_URL", "ESPO_API_KEY", "BACKUP_ENCRYPTION_KEY",
                 "DATABASE_MIGRATION_URL", "ADMIN_PASSWORD"):
        monkeypatch.setenv(name, "real-application-secret-sentinel")
    monkeypatch.setenv("VOICE_ENV_FILE", "/private/real.env")
    env = verify.test_environment("postgresql://disposable-only", tmp_path)
    assert env["DATABASE_URL"] == env["TEST_DATABASE_ADMIN_URL"] == "postgresql://disposable-only"
    assert env["VOICE_ENV_FILE"] == ""
    assert env["PRIVATE_RUNTIME_DIR"] == str(tmp_path)
    assert "real-application-secret-sentinel" not in env.values()


def test_pytest_output_redacts_password_and_preserves_exit_code(monkeypatch, capsys):
    output = io.StringIO("connection postgres://user:random-db-password@127.0.0.1 failed\n")
    process = SimpleNamespace(stdout=output, wait=lambda: 2)
    monkeypatch.setattr(verify.subprocess, "Popen", lambda *args, **kwargs: process)
    assert verify.run_tests(["pytest"], {}, "random-db-password") == 2
    printed = capsys.readouterr().out
    assert "random-db-password" not in printed
    assert "[REDACTED]" in printed
    assert output.closed


def test_dry_run_invokes_neither_docker_nor_tests(monkeypatch, capsys):
    monkeypatch.setattr(verify, "DisposablePostgres", lambda: pytest.fail("Docker invoked"))
    monkeypatch.setattr(verify, "run_tests", lambda *args: pytest.fail("Tests invoked"))
    assert verify.main(["--full", "--dry-run"]) == 0
    assert "complete suite" in capsys.readouterr().out
