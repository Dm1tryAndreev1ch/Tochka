import json
from pathlib import Path

import pytest

from app import supervisor
from app.runtime_environment import ROLE_SETTINGS, build_child_environment, configured_env_file


@pytest.fixture
def application_settings():
    return {
        "database_url": "postgresql://runtime:database-secret@localhost/app",
        "admin_password": "admin-secret", "operator_password": "operator-secret",
        "management_secret": "management-secret", "espo_api_key": "crm-secret",
        "llm_api_key": "llm-secret", "channel_api_key": "telegram-secret",
        "natural_tts_api_key": "tts-secret", "natural_tts_model": "models/approved-tts",
        "voice_reference_dir": Path("models/approved-voices"), "tts_cache_max_bytes": 1048576,
        "male_speech_rate": 1.1, "natural_tts_enabled": True,
        "private_runtime_dir": "/private/runtime", "socket_transport_enabled": False,
        "database_migration_url": "migration-owner-secret",
        "migration_database_url": "other-owner-secret", "backup_encryption_key": "backup-root-secret",
        "archive_master_key": "archive-root-secret", "postgres_password": "docker-root-secret",
        "unexpected_future_api_key": "unknown-provider-secret",
        "VOICE_ENV_FILE": "/private/full-application.env",
    }


@pytest.mark.parametrize("role", ["natural-tts", "telegram-worker", "llm", "caddy", "mobile-setup"])
def test_workers_receive_only_their_own_application_settings(role, application_settings):
    inherited = {**application_settings, "PATH": "/usr/bin", "HF_TOKEN": "hf-provider-secret",
                 "AWS_SECRET_ACCESS_KEY": "cloud-provider-secret", "HTTP_PROXY": "http://proxy:password@host"}
    child = build_child_environment(role, application_settings, inherited=inherited)
    allowed_settings = ROLE_SETTINGS[role]
    for name, secret in application_settings.items():
        if name.upper() not in allowed_settings:
            assert name.upper() not in child or name == "VOICE_ENV_FILE"
            if isinstance(secret, str) and secret.endswith("secret"):
                assert secret not in child.values()
    assert child["PATH"] == "/usr/bin"
    assert child["VOICE_ENV_FILE"] == ""
    assert "HF_TOKEN" not in child
    assert "AWS_SECRET_ACCESS_KEY" not in child
    assert "HTTP_PROXY" not in child
    if role == "natural-tts":
        assert child["NATURAL_TTS_API_KEY"] == "tts-secret"
        assert child["VOICE_REFERENCE_DIR"] == "models/approved-voices"
        assert child["TTS_CACHE_MAX_BYTES"] == "1048576"
        assert child["MALE_SPEECH_RATE"] == "1.1"
    elif role == "telegram-worker":
        assert child["CHANNEL_API_KEY"] == "telegram-secret"
        assert child["PRIVATE_RUNTIME_DIR"] == "/private/runtime"


def test_gateway_gets_runtime_credentials_but_not_administrative_root_keys(application_settings):
    child = build_child_environment("gateway", application_settings, inherited={})
    assert child["DATABASE_URL"] == application_settings["database_url"]
    assert child["NATURAL_TTS_API_KEY"] == "tts-secret"
    assert child["CHANNEL_API_KEY"] == "telegram-secret"
    assert child["NATURAL_TTS_ENABLED"] == "true"
    assert child["SOCKET_TRANSPORT_ENABLED"] == "false"
    for name in ("DATABASE_MIGRATION_URL", "MIGRATION_DATABASE_URL", "BACKUP_ENCRYPTION_KEY",
                 "ARCHIVE_MASTER_KEY", "POSTGRES_PASSWORD", "UNEXPECTED_FUTURE_API_KEY"):
        assert name not in child


def test_voice_response_settings_reach_only_the_workers_that_use_them():
    settings = {"LLM_VOICE_ANSWER_CHARS": 200, "VOICE_ACKNOWLEDGEMENT_ENABLED": False,
                "VOICE_ACKNOWLEDGEMENT_DELAY_SECONDS": 2.5}
    gateway = build_child_environment("gateway", settings, inherited={})
    assert gateway["LLM_VOICE_ANSWER_CHARS"] == "200"
    assert gateway["VOICE_ACKNOWLEDGEMENT_DELAY_SECONDS"] == "2.5"
    assert gateway["VOICE_ACKNOWLEDGEMENT_ENABLED"] == "false"
    tts = build_child_environment("natural-tts", settings, inherited={})
    assert tts["VOICE_ACKNOWLEDGEMENT_ENABLED"] == "false"
    assert "LLM_VOICE_ANSWER_CHARS" not in tts
    assert "VOICE_ACKNOWLEDGEMENT_DELAY_SECONDS" not in tts


def test_settings_never_fall_back_to_inherited_application_secrets():
    child = build_child_environment("natural-tts", inherited={"NATURAL_TTS_API_KEY": "parent-secret"})
    assert "NATURAL_TTS_API_KEY" not in child
    gateway = build_child_environment("gateway", inherited={"DATABASE_URL": "parent-db-secret"})
    assert "DATABASE_URL" not in gateway


def test_windows_environment_retains_runtime_paths_with_canonical_names():
    child = build_child_environment("telegram-worker", inherited={
        "Path": r"C:\Python312;C:\Windows\System32", "SystemRoot": r"C:\Windows",
        "ComSpec": r"C:\Windows\System32\cmd.exe", "PATHEXT": ".COM;.EXE;.BAT;.CMD",
        "USERPROFILE": r"C:\Users\Operator", "TEMP": r"C:\Users\Operator\Temp",
        "TMP": r"C:\Users\Operator\Temp", "LOCALAPPDATA": r"C:\Users\Operator\AppData\Local",
        "APPDATA": r"C:\Users\Operator\AppData\Roaming",
    })
    assert child["SYSTEMROOT"] == r"C:\Windows"
    assert child["COMSPEC"].endswith("cmd.exe")
    assert child["PATH"].startswith("C:\\Python312")
    assert child["TEMP"] == child["TMP"]
    assert child["LOCALAPPDATA"].endswith("Local")
    assert "Path" not in child


def test_native_resource_knobs_are_scoped_and_injection_variables_are_removed():
    inherited = {"CUDA_VISIBLE_DEVICES": "2", "OMP_NUM_THREADS": "4", "HOME": "/home/voice",
                 "PYTHONPYCACHEPREFIX": "/cache/python", "PYTHONPATH": "/untrusted/python",
                 "PYTHONHOME": "/untrusted/runtime", "LD_PRELOAD": "/untrusted/lib.so",
                 "DYLD_INSERT_LIBRARIES": "/untrusted/lib.dylib"}
    llm = build_child_environment("llm", inherited=inherited)
    assert llm["CUDA_VISIBLE_DEVICES"] == "2"
    assert llm["OMP_NUM_THREADS"] == "4"
    assert llm["PYTHONPYCACHEPREFIX"] == "/cache/python"
    for name in ("PYTHONPATH", "PYTHONHOME", "LD_PRELOAD", "DYLD_INSERT_LIBRARIES"):
        assert name not in llm
    telegram = build_child_environment("telegram-worker", inherited=inherited)
    assert "CUDA_VISIBLE_DEVICES" not in telegram
    assert "OMP_NUM_THREADS" not in telegram


def test_unknown_role_and_invalid_allowed_values_fail_closed():
    with pytest.raises(ValueError, match="Unknown child role"):
        build_child_environment("new-worker", inherited={})
    with pytest.raises(ValueError, match="NATURAL_TTS_API_KEY"):
        build_child_environment("natural-tts", {"NATURAL_TTS_API_KEY": "contains\x00NUL"}, inherited={})


def test_config_file_policy_defaults_disables_or_selects_explicit_file(tmp_path):
    default = tmp_path / ".env"
    assert configured_env_file(default, {}) == default
    assert configured_env_file(default, {"VOICE_ENV_FILE": ""}) is None
    assert configured_env_file(default, {"VOICE_ENV_FILE": "  "}) is None
    assert configured_env_file(default, {"VOICE_ENV_FILE": "profiles/worker.env"}) == tmp_path / "profiles/worker.env"
    assert configured_env_file(default, {"VOICE_ENV_FILE": str(tmp_path / "other.env")}) == tmp_path / "other.env"
    assert list(tmp_path.iterdir()) == []


def test_settings_honor_selected_file_and_children_cannot_fall_back_to_it(tmp_path, monkeypatch):
    from app.config import Settings

    secret_file = tmp_path / "launcher.env"
    secret_file.write_text("ADMIN_PASSWORD=launcher-only-admin-sentinel\nESPO_API_KEY=launcher-only-crm-sentinel\n")
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    monkeypatch.delenv("ESPO_API_KEY", raising=False)
    monkeypatch.setenv("VOICE_ENV_FILE", str(secret_file))
    configured = Settings()
    selected_file_loaded = configured.admin_password == "launcher-only-admin-sentinel"
    assert selected_file_loaded, "Settings did not load the explicitly selected file"
    explicit_override = Settings(_env_file=None)
    assert explicit_override.admin_password == ""

    child = build_child_environment("natural-tts", {"NATURAL_TTS_API_KEY": "worker-owned-token"}, inherited={})
    for name, value in child.items():
        monkeypatch.setenv(name, value)
    isolated = Settings()
    assert isolated.admin_password == ""
    assert isolated.espo_api_key == ""
    assert isolated.natural_tts_api_key == "worker-owned-token"


class FakeProcess:
    def __init__(self, pid):
        self.pid = pid
        self.returncode = None
        self.terminated = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = 0

    def wait(self, timeout):
        return self.returncode


def test_supervisor_restarts_with_original_explicit_environment_and_never_persists_secrets(tmp_path, monkeypatch):
    launches = []
    processes = []

    def popen(command, **kwargs):
        launches.append(kwargs)
        process = FakeProcess(100 + len(processes))
        processes.append(process)
        return process

    monkeypatch.setattr(supervisor.subprocess, "Popen", popen)
    owner = supervisor.ProcessSupervisor(tmp_path, retry_base=0)
    child_env = {"PATH": "/runtime/bin", "VOICE_ENV_FILE": "", "NATURAL_TTS_API_KEY": "private-tts-token"}
    owner.start("natural-tts", ["python", "-m", "worker"], env=child_env)
    child_env["NATURAL_TTS_API_KEY"] = "caller-replaced-token"
    monkeypatch.setenv("NATURAL_TTS_API_KEY", "later-parent-token")
    processes[0].returncode = 7
    owner.tick()
    assert len(launches) == 2
    assert launches[0]["env"] == launches[1]["env"]
    assert launches[1]["env"]["NATURAL_TTS_API_KEY"] == "private-tts-token"
    assert launches[0]["env"] is not launches[1]["env"]
    saved = (tmp_path / "processes.json").read_text()
    assert "private-tts-token" not in saved
    assert "NATURAL_TTS_API_KEY" not in saved
    assert json.loads(saved)["natural-tts"]["restarts"] == 1
    assert set(owner.snapshot()["natural-tts"]) == {"pid", "state", "restarts", "last_exit"}
    owner.close()
    assert processes[1].terminated


def test_supervisor_requires_explicit_environment_before_starting_a_process(tmp_path, monkeypatch):
    monkeypatch.setattr(supervisor.subprocess, "Popen", lambda *args, **kwargs: pytest.fail("Unexpected process"))
    owner = supervisor.ProcessSupervisor(tmp_path)
    with pytest.raises(TypeError, match="env"):
        owner.start("forgot-environment", ["worker"])
    assert owner.records == {}
