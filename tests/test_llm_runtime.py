import io
import json
import urllib.error
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.config import Settings
from scripts.run import (
    LLMConfigurationError,
    LLMNotReady,
    build_llama_command,
    check_llm_runtime,
)


def runtime_settings(**kwargs):
    return Settings(_env_file=None, **kwargs)


def fake_server(models, props=None, props_error=None):
    calls = []

    def open_response(request, timeout):
        calls.append((request.full_url, request.get_header("Authorization"), timeout))
        if request.full_url.endswith("/models"):
            return io.BytesIO(json.dumps(models).encode())
        if props_error:
            raise urllib.error.HTTPError(request.full_url, props_error, "unavailable", {}, None)
        return io.BytesIO(json.dumps(props).encode())

    return open_response, calls


def test_launcher_uses_selected_model_alias_port_and_runtime_parameters(tmp_path):
    weights = tmp_path / "models" / "small.gguf"
    weights.parent.mkdir()
    weights.touch()
    config = runtime_settings(
        llm_model="qwen-small",
        llm_model_path="models/small.gguf",
        llm_url="http://localhost:18111/v1/",
        llm_context_tokens=4096,
        llm_threads=6,
        llm_gpu_layers="all",
        llm_api_key="never-put-this-in-command",
    )
    command = build_llama_command(config, tmp_path / "key", "http://localhost:7860", root=tmp_path)
    assert command[command.index("-m") + 1] == str(weights.resolve())
    assert command[command.index("--alias") + 1] == "qwen-small"
    assert command[command.index("--host") + 1] == "localhost"
    assert command[command.index("--port") + 1] == "18111"
    assert command[command.index("-c") + 1] == "4096"
    assert command[command.index("-ngl") + 1] == "all"
    assert command[command.index("-t") + 1] == "6"
    assert command[command.index("--parallel") + 1] == "1"
    assert json.loads(command[command.index("--chat-template-kwargs") + 1]) == {"enable_thinking": False}
    assert config.llm_api_key not in command


def test_launcher_preserves_absolute_model_path_and_explicit_thinking(tmp_path):
    weights = tmp_path / "custom.gguf"
    weights.touch()
    config = runtime_settings(llm_model_path=str(weights), llm_thinking=True)
    command = build_llama_command(config, tmp_path / "key", "http://localhost:7860")
    assert command[command.index("-m") + 1] == str(weights)
    assert json.loads(command[command.index("--chat-template-kwargs") + 1]) == {"enable_thinking": True}


def test_launcher_reports_missing_weights_before_starting(tmp_path):
    with pytest.raises(LLMConfigurationError, match="LLM_MODEL_PATH does not exist"):
        build_llama_command(runtime_settings(llm_model_path="missing.gguf"), tmp_path / "key", "origin", tmp_path)


@pytest.mark.parametrize("url", ["https://localhost:18082/v1", "http://example.com/v1", "http://127.0.0.1:18082/api"])
def test_launcher_never_spawns_a_local_process_for_nonlocal_or_incompatible_url(tmp_path, url):
    with pytest.raises(LLMConfigurationError, match="loopback"):
        build_llama_command(runtime_settings(llm_url=url), tmp_path / "key", "origin", tmp_path)


@pytest.mark.parametrize("layers", ["-1", "many", "99;other"])
def test_launcher_rejects_invalid_gpu_layers(tmp_path, layers):
    weights = tmp_path / "model.gguf"
    weights.touch()
    config = runtime_settings(llm_model_path=str(weights), llm_gpu_layers=layers)
    with pytest.raises(LLMConfigurationError, match="LLM_GPU_LAYERS"):
        build_llama_command(config, tmp_path / "key", "origin")


def test_ready_server_must_match_alias_and_gguf_path(tmp_path):
    config = runtime_settings(llm_model_path="weights/current.gguf", llm_api_key="secret")
    opener, calls = fake_server(
        {"data": [{"id": config.llm_model}]},
        {"model_path": str(tmp_path / "weights/current.gguf")},
    )
    check_llm_runtime(config, root=tmp_path, opener=opener)
    assert [url for url, _, _ in calls] == ["http://127.0.0.1:18082/v1/models", "http://127.0.0.1:18082/props"]
    assert all(auth == "Bearer secret" and timeout == 2 for _, auth, timeout in calls)


def test_existing_server_with_wrong_alias_is_not_silently_reused():
    opener, calls = fake_server({"data": [{"id": "previous-model"}]})
    with pytest.raises(LLMConfigurationError, match="existing service was left running"):
        check_llm_runtime(runtime_settings(), opener=opener)
    assert len(calls) == 1


def test_alias_alone_does_not_allow_different_gguf(tmp_path):
    config = runtime_settings(llm_model_path="current.gguf")
    opener, _ = fake_server({"data": [{"id": config.llm_model}]}, {"model_path": str(tmp_path / "old.gguf")})
    with pytest.raises(LLMConfigurationError, match="different GGUF"):
        check_llm_runtime(config, root=tmp_path, opener=opener)


@pytest.mark.parametrize("status", [404, 405])
def test_openai_compatible_server_without_props_still_checks_alias(status):
    config = runtime_settings()
    opener, _ = fake_server({"data": [{"id": config.llm_model}]}, props_error=status)
    check_llm_runtime(config, opener=opener)


def test_remote_server_checks_alias_without_comparing_paths_from_another_machine():
    config = runtime_settings(llm_url="https://inference.example/v1")
    opener, calls = fake_server({"data": [{"id": config.llm_model}]})
    check_llm_runtime(config, opener=opener)
    assert len(calls) == 1


@pytest.mark.parametrize("payload", [{}, {"data": "qwen"}, []])
def test_invalid_model_response_is_not_treated_as_an_unavailable_server(payload):
    opener, _ = fake_server(payload)
    with pytest.raises(LLMConfigurationError, match="model list"):
        check_llm_runtime(runtime_settings(), opener=opener)


@pytest.mark.parametrize("status,error", [(401, LLMConfigurationError), (503, LLMNotReady)])
def test_responding_auth_failure_or_loading_service_does_not_trigger_a_second_server(status, error):
    def opener(request, timeout):
        raise urllib.error.HTTPError(request.full_url, status, "unavailable", {}, None)

    with pytest.raises(error):
        check_llm_runtime(runtime_settings(), opener=opener)


@pytest.mark.parametrize(
    "override",
    [
        {"llm_threads": 0}, {"llm_context_tokens": 0}, {"llm_reply_tokens": 0},
        {"vad_stop_seconds": 0.1}, {"vad_sensitive_stop_seconds": 0.2},
        {"stt_preemptive_silence_seconds": 0.95}, {"tts_cache_max_bytes": -1},
        {"tts_cache_max_entries": -1}, {"llm_prompt_profile": "unknown"},
    ],
)
def test_invalid_runtime_resource_and_timing_settings_are_rejected(override):
    with pytest.raises(ValidationError):
        runtime_settings(**override)


def test_zero_sized_audio_cache_is_supported():
    config = runtime_settings(tts_cache_max_bytes=0, tts_cache_max_entries=0)
    assert config.tts_cache_max_bytes == config.tts_cache_max_entries == 0


def test_gateway_and_launcher_share_normalized_llm_url():
    config = runtime_settings(llm_url="http://localhost:18111/v1/")
    assert config.llm_url == "http://localhost:18111/v1"


def test_properties_connection_failure_is_not_treated_as_a_missing_server():
    def opener(request, timeout):
        if request.full_url.endswith("/models"):
            return io.BytesIO(b'{"data":[{"id":"qwen3-4b"}]}')
        raise urllib.error.URLError("connection reset")

    with pytest.raises(LLMConfigurationError, match="/models responded"):
        check_llm_runtime(runtime_settings(), opener=opener)


def test_launcher_does_not_start_or_stop_external_services_when_model_mismatches(tmp_path, monkeypatch):
    from app.supervisor import ProcessSupervisor
    from scripts import run

    monkeypatch.setattr(run, "ROOT", tmp_path)
    monkeypatch.setattr(run, "ensure_dependencies", lambda: None)
    async def database_ready(_url):
        return None

    monkeypatch.setattr(run, "check_database_runtime", database_ready)
    monkeypatch.setattr(run.signal, "signal", lambda *_: None)
    monkeypatch.setenv("LAN_ENABLED", "false")
    monkeypatch.setenv("LLM_MODEL", "desired-model")
    monkeypatch.setenv("LLM_URL", "http://127.0.0.1:18082/v1")
    started = []
    monkeypatch.setattr(ProcessSupervisor, "start", lambda self, *args, **kwargs: started.append(args))

    def opener(request, timeout):
        if isinstance(request, str):
            raise urllib.error.URLError("gateway is not running")
        return io.BytesIO(b'{"data":[{"id":"external-model"}]}')

    monkeypatch.setattr(run.urllib.request, "urlopen", opener)
    monkeypatch.setattr(run.urllib.request, "build_opener", lambda *_: SimpleNamespace(open=opener))
    with pytest.raises(LLMConfigurationError, match="existing service was left running"):
        run.main()
    assert started == []
    assert json.loads((tmp_path / ".runtime/processes.json").read_text()) == {}
