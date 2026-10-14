import io
import json
import urllib.error

import pytest

from app.config import Settings
from scripts import launch, run


def arguments(*values):
    return launch.parser().parse_args(values)


def test_distributed_and_portable_overrides_reach_child_environment(tmp_path):
    (tmp_path / ".env").write_text("NATURAL_TTS_ENABLED=true\nSTT_BACKEND=whisper.cpp\nLAN_ENABLED=true\n")
    env = launch.environment(arguments("--mode", "distributed", "--speech", "portable"), tmp_path, {})
    assert env["LLM_REMOTE"] == "true"
    assert env["NATURAL_TTS_ENABLED"] == "false"
    assert env["STT_BACKEND"] == "faster-whisper"
    assert env["LAN_ENABLED"] == "false"


def test_configured_profile_preserves_approved_voice_settings(tmp_path):
    inherited = {"NATURAL_TTS_ENABLED": "true", "NATURAL_TTS_FEMALE_VOICE": "approved",
                 "VOICE_REFERENCE_DIR": "approved-references"}
    env = launch.environment(arguments(), tmp_path, inherited)
    assert env.items() >= inherited.items()
    assert env["LLM_REMOTE"] == "false"


def test_download_paths_match_catalog_even_with_existing_custom_environment(tmp_path):
    args = arguments("--download-models", "--model-dir", "new-models")
    env = launch.environment(args, tmp_path, {"LLM_MODEL_PATH": "obsolete.gguf"})
    assert env["LLM_MODEL_PATH"] == str(tmp_path / "new-models/qwen3-4b-q4.gguf")
    assert env["WHISPER_METAL_MODEL"] == str(tmp_path / "new-models/whisper-turbo-metal.bin")
    assert env["VOICE_REFERENCE_DIR"] == str(tmp_path / "new-models/voices")


def test_distributed_downloads_exclude_local_llm_and_mixed_speech_gets_both_catalogs():
    args = arguments("--mode", "distributed", "--download-only")
    commands = launch.download_commands(args, {"NATURAL_TTS_ENABLED": "true", "STT_BACKEND": "faster-whisper"})
    assert [command[command.index("--profile") + 1] for command in commands] == ["mac-metal", "portable"]
    assert all("--exclude-llm" in command for command in commands)


def test_explicit_mac_profile_sets_backend_before_selecting_catalog(tmp_path):
    args = arguments("--speech", "mac-metal")
    env = launch.environment(args, tmp_path, {})
    assert env["STT_BACKEND"] == "whisper.cpp"
    assert env["NATURAL_TTS_ENABLED"] == "true"
    assert launch.model_profile(args, env) == "mac-metal"


def test_default_speech_settings_download_portable_models():
    assert launch.model_profile(arguments(), {}) == "portable"


def test_remote_probe_disables_redirects_and_environment_proxies(monkeypatch):
    handlers = []

    class Opener:
        def open(self, request, timeout):
            return io.BytesIO(b'{"data": [{"id": "qwen3-4b"}]}')

    def build(*values):
        handlers.extend(values)
        return Opener()

    monkeypatch.setattr(run.urllib.request, "build_opener", build)
    run.check_llm_runtime(Settings(_env_file=None, llm_remote=True))
    assert handlers[0].proxies == {}
    assert handlers[1].redirect_request(None, None, 302, "", {}, "https://other.example") is None


@pytest.mark.parametrize("url", ["https://user:secret@host/v1", "file:///v1", "http://host:0/v1",
                                 "http://host/v1?key=secret", "http://host:99999/v1"])
def test_endpoint_rejects_credential_leaking_or_invalid_urls(url):
    with pytest.raises(ValueError):
        launch.endpoint(url)


def test_dry_run_never_downloads_launches_reads_key_or_reveals_secret(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(launch, "ROOT", tmp_path)
    monkeypatch.setenv("LLM_API_KEY", "top-secret-token")

    def forbidden(*args, **kwargs):
        pytest.fail("Dry run attempted a side effect")

    monkeypatch.setattr(launch.subprocess, "run", forbidden)
    monkeypatch.setattr(launch.subprocess, "call", forbidden)
    monkeypatch.setattr(launch, "read_token", forbidden)
    result = launch.main(["--dry-run", "--download-models", "--mode", "distributed", "--llm-url",
                          "http://127.0.0.1:28082/v1", "--llm-api-key-file", "missing-secret-file"])
    assert result == 0
    assert "top-secret-token" not in capsys.readouterr().out
    assert list(tmp_path.iterdir()) == []


def test_worker_command_keeps_token_out_of_arguments_and_preserves_spaces(tmp_path):
    args = arguments("--role", "llm", "--llm-api-key-file", "private key", "--model-path", "large model.gguf",
                     "--server-binary", "C:/llama cpp/llama-server.exe")
    env = launch.environment(args, tmp_path, {"LLM_API_KEY": "secret-token"})
    command = launch.worker_command(args, env, tmp_path)
    assert command[0] == "C:/llama cpp/llama-server.exe"
    assert command[command.index("-m") + 1] == str(tmp_path / "large model.gguf")
    assert command[command.index("--host") + 1] == "127.0.0.1"
    assert "secret-token" not in " ".join(command)


@pytest.mark.parametrize("system", ["Linux", "Windows"])
def test_nonmac_requires_explicit_portable_profile(system, tmp_path, monkeypatch):
    monkeypatch.setattr(launch.importlib.util, "find_spec", lambda name: object())
    monkeypatch.setattr(launch.shutil, "which", lambda name: name)
    env = {"NATURAL_TTS_ENABLED": "true", "STT_BACKEND": "whisper.cpp", "LAN_ENABLED": "true"}
    errors, _ = launch.diagnostics(arguments(), env, tmp_path, system)
    assert any("--speech portable" in error for error in errors)
    assert any("LAN" in error for error in errors)


def test_remote_loopback_tunnel_validates_alias_without_remote_filesystem_comparison():
    config = Settings(_env_file=None, llm_remote=True, llm_url="http://127.0.0.1:28082/v1")
    calls = []

    def opener(request, timeout):
        calls.append(request.full_url)
        assert request.full_url.endswith("/models")
        return io.BytesIO(json.dumps({"data": [{"id": config.llm_model}]}).encode())

    run.check_llm_runtime(config, opener=opener)
    assert len(calls) == 1


def test_unavailable_remote_fails_before_docker_or_local_process_start(monkeypatch):
    monkeypatch.setenv("LLM_REMOTE", "true")
    monkeypatch.setenv("LAN_ENABLED", "false")

    def unavailable(config):
        raise urllib.error.URLError("connection refused")

    def forbidden():
        pytest.fail("Dependencies must not start when remote LLM is unavailable")

    monkeypatch.setattr(run, "check_llm_runtime", unavailable)
    monkeypatch.setattr(run, "ensure_dependencies", forbidden)
    with pytest.raises(RuntimeError, match="no local LLM"):
        run.main()


def test_download_only_does_not_require_worker_key_or_launch_services(monkeypatch):
    commands = []
    monkeypatch.setattr(launch.subprocess, "run", lambda command, **kwargs: commands.append(command))
    monkeypatch.setattr(launch.subprocess, "call", lambda *args, **kwargs: pytest.fail("started service"))
    assert launch.main(["--download-only", "--role", "llm"]) == 0
    assert len(commands) == 1
    assert commands[0][-1] == "llm-worker"


def test_env_parser_preserves_windows_path_and_never_evaluates_shell(tmp_path):
    path = tmp_path / ".env"
    path.write_text('MODEL="C:\\Models\\Qwen.gguf" # a comment\nTOKEN=$(touch bad)\n')
    assert launch.read_env(path) == {"MODEL": "C:\\Models\\Qwen.gguf", "TOKEN": "$(touch bad)"}
    assert not (tmp_path / "bad").exists()
