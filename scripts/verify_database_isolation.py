"""Run database tests against an owned, disposable PostgreSQL 16 container.

Requires a locally cached postgres:16 image. Never pulls images, runs Compose,
mounts application volumes, or accepts a production database connection string.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.runtime_environment import build_child_environment  # noqa: E402

IMAGE = "postgres:16"
OWNER_LABEL = "org.voice-desk.database-verification"
FOCUSED_TESTS = (
    "tests/test_database_security.py", "tests/test_database_isolation.py", "tests/test_business.py",
    "tests/test_privacy.py", "tests/test_archive_lifecycle.py", "tests/test_staff_access.py",
)


class VerificationError(RuntimeError):
    pass


def redact(text, password):
    return text.replace(password, "[REDACTED]") if password else text


def docker_environment(password):
    env = build_child_environment("caddy", inherited=os.environ)
    # Keep the user's Docker connection/context, without forwarding app secrets.
    for name in ("DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH"):
        if name in os.environ:
            env[name] = os.environ[name]
    env["POSTGRES_PASSWORD"] = password
    return env


class DisposablePostgres:
    def __init__(self, *, readiness_timeout=30):
        self.session = uuid4().hex
        self.name = "voice-db-verify-" + self.session
        self.password = secrets.token_urlsafe(32)
        self.env = docker_environment(self.password)
        self.readiness_timeout = readiness_timeout
        self.container_id = None
        self.creation_attempted = False

    def docker(self, *arguments, check=True, timeout=15):
        result = subprocess.run(
            ["docker", *arguments], env=self.env, capture_output=True, text=True, timeout=timeout,
        )
        if check and result.returncode:
            detail = redact((result.stderr or result.stdout or "Docker command failed").strip(), self.password)
            raise VerificationError(detail[-2000:])
        return result

    def __enter__(self):
        try:
            image = self.docker("image", "inspect", IMAGE, "--format", "{{.Id}}").stdout.strip()
            if not re.fullmatch(r"sha256:[0-9a-f]{64}", image):
                raise VerificationError("Local postgres:16 did not resolve to an immutable image ID")
            self.creation_attempted = True
            result = self.docker(
                "create", "--pull=never", "--name", self.name, "--label", f"{OWNER_LABEL}={self.session}",
                "--memory", "512m", "--memory-swap", "512m", "--pids-limit", "128",
                "--tmpfs", "/var/lib/postgresql/data:rw,nosuid,noexec,size=256m",
                "--publish", "127.0.0.1::5432", "--env", "POSTGRES_PASSWORD", image,
            )
            self.container_id = self.valid_id(result.stdout.strip())
            self.docker("start", self.container_id)
            deadline = time.monotonic() + self.readiness_timeout
            while time.monotonic() < deadline:
                result = self.docker(
                    "exec", self.container_id, "pg_isready", "-h", "127.0.0.1", "-U", "postgres", "-d", "postgres",
                    check=False, timeout=max(0.1, min(5, deadline - time.monotonic())),
                )
                if result.returncode == 0:
                    break
                time.sleep(min(0.5, max(0, deadline - time.monotonic())))
            else:
                raise VerificationError("Disposable PostgreSQL did not become ready within the configured timeout")
            ports = json.loads(self.docker(
                "inspect", "--type", "container", "--format", "{{json .NetworkSettings.Ports}}", self.container_id,
            ).stdout)
            bindings = ports.get("5432/tcp")
            if not isinstance(bindings, list) or len(bindings) != 1 or bindings[0].get("HostIp") != "127.0.0.1":
                raise VerificationError("Refusing a test database without exactly one IPv4 loopback binding")
            port = int(bindings[0]["HostPort"])
            if not 1 <= port <= 65535:
                raise VerificationError("Docker returned an invalid published port")
            self.url = f"postgresql://postgres:{quote(self.password, safe='')}@127.0.0.1:{port}/postgres"
            return self
        except BaseException:
            self.close()
            raise

    @staticmethod
    def valid_id(value):
        if not re.fullmatch(r"[0-9a-f]{64}", value):
            raise VerificationError("Docker did not return a full container ID")
        return value

    def recover_created_id(self):
        """Handle interrupted create: verify our unique label, then remove by ID only."""
        result = self.docker("inspect", "--type", "container", "--format", "{{json .}}", self.name, check=False)
        if result.returncode:
            return None
        record = json.loads(result.stdout)
        if (record.get("Name") != "/" + self.name
                or record.get("Config", {}).get("Labels", {}).get(OWNER_LABEL) != self.session):
            raise VerificationError("Container ownership could not be verified; no container was removed")
        return self.valid_id(record.get("Id", ""))

    def close(self):
        if self.creation_attempted and self.container_id is None:
            self.container_id = self.recover_created_id()
        if self.container_id is not None:
            self.docker("rm", "-f", self.container_id)
            self.container_id = None
        self.creation_attempted = False

    def __exit__(self, *exc):
        self.close()


def test_environment(database_url, private_directory):
    env = build_child_environment("gateway", inherited=os.environ)
    env.update(
        TEST_DATABASE_ADMIN_URL=database_url, DATABASE_URL=database_url,
        ADMIN_PASSWORD="disposable-test-admin-password", OPERATOR_PASSWORD="disposable-test-operator-password",
        MANAGEMENT_SECRET="disposable-test-management-secret-at-least-32-characters",
        LLM_API_KEY="disposable-test-llm-key", NATURAL_TTS_API_KEY="disposable-test-tts-key",
        CHANNEL_API_KEY="disposable-test-channel-key", PRIVATE_RUNTIME_DIR=str(private_directory),
        PYTHONUNBUFFERED="1",
    )
    return env


def test_command(full=False):
    candidate = ROOT / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    executable = str(candidate) if candidate.is_file() else sys.executable
    return [executable, "-m", "pytest", *( ["tests"] if full else FOCUSED_TESTS), "-q"]


def run_tests(command, env, password):
    process = subprocess.Popen(
        command, cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1,
    )
    try:
        for line in process.stdout:
            print(redact(line, password), end="", flush=True)
        return process.wait()
    except BaseException:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        raise
    finally:
        process.stdout.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--full", action="store_true", help="Run the complete tests directory (takes longer)")
    parser.add_argument("--dry-run", action="store_true", help="Show the plan without invoking Docker or tests")
    args = parser.parse_args(argv)
    command = test_command(args.full)
    print("Database verification: disposable postgres:16, cached image only, loopback port, tmpfs, 512 MiB / 128 PIDs.")
    print("Tests: " + ("complete suite" if args.full else ", ".join(FOCUSED_TESTS)))
    if args.dry_run:
        print("Dry run: no Docker commands, containers, downloads or test execution.")
        return 0
    previous_term = signal.getsignal(signal.SIGTERM)

    def interrupted(*_):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupted)
    database = DisposablePostgres()
    try:
        with tempfile.TemporaryDirectory(prefix="voice-db-verification-") as private, database:
            print("Disposable PostgreSQL ready; running tests. Database credentials are redacted.", flush=True)
            return run_tests(command, test_environment(database.url, private), database.password)
    except KeyboardInterrupt:
        print("Verification interrupted; owned container cleanup requested.", file=sys.stderr)
        return 130
    except (OSError, ValueError, subprocess.SubprocessError, VerificationError) as exc:
        print("Verification failed: " + redact(str(exc), database.password), file=sys.stderr)
        if database.container_id:
            print("Cleanup may require: docker rm -f " + database.container_id, file=sys.stderr)
        return 1
    finally:
        signal.signal(signal.SIGTERM, previous_term)


if __name__ == "__main__":
    raise SystemExit(main())
