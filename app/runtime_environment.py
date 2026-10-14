"""Explicit child configuration; this is not an OS or filesystem security boundary.

Only named runtime variables and each role's reviewed application settings cross
the process boundary. Unknown settings fail closed until their ownership is
reviewed. No child automatically reopens the launcher's shared dotenv file.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType

ENV_FILE_CONTROL = "VOICE_ENV_FILE"

# Windows needs SystemRoot for side-by-side assemblies and its system DLLs.
# Canonical uppercase names also avoid duplicate Path/PATH entries in a Windows
# environment block. Python and Windows environment lookups are case insensitive.
SYSTEM_ENVIRONMENT_VARIABLES = frozenset({
    "PATH", "HOME", "USER", "LOGNAME", "USERPROFILE", "HOMEDRIVE", "HOMEPATH",
    "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "APPDATA", "LOCALAPPDATA",
    "PROGRAMDATA", "TMP", "TEMP", "TMPDIR", "LANG", "LANGUAGE", "LC_ALL",
    "LC_COLLATE", "LC_CTYPE", "LC_MESSAGES", "LC_MONETARY", "LC_NUMERIC", "LC_TIME",
    "TZ", "XDG_CACHE_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "VIRTUAL_ENV",
    "PYTHONPYCACHEPREFIX", "PYTHONDONTWRITEBYTECODE", "PYTHONUNBUFFERED",
    "PYTHONIOENCODING", "PYTHONUTF8", "SSL_CERT_FILE", "SSL_CERT_DIR",
})

# Native inference resource selection is independent of application credentials.
# Loader/code injection variables (LD_PRELOAD, DYLD_*, PYTHONPATH, PYTHONHOME),
# proxy credentials and provider tokens are deliberately not inherited.
INFERENCE_ENVIRONMENT_VARIABLES = frozenset({
    "CUDA_VISIBLE_DEVICES", "CUDA_DEVICE_ORDER", "CUDA_PATH", "HIP_VISIBLE_DEVICES",
    "ROCR_VISIBLE_DEVICES", "GPU_DEVICE_ORDINAL", "OMP_NUM_THREADS",
    "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "VECLIB_MAXIMUM_THREADS",
    "NUMEXPR_NUM_THREADS", "TOKENIZERS_PARALLELISM",
})

GATEWAY_SETTINGS = frozenset({
    "DATABASE_URL", "ADMIN_PASSWORD", "OPERATOR_PASSWORD", "MANAGEMENT_SECRET",
    "ESPO_URL", "ESPO_API_KEY", "ESPO_ASSIGNED_USER_ID", "LLM_API_KEY", "ICE_HOST_IP",
    "LLM_URL", "LLM_REMOTE", "LLM_MODEL", "LLM_MODEL_PATH", "LLM_THINKING",
    "LLM_PROMPT_PROFILE", "LLM_GPU_LAYERS", "LLM_THREADS", "LLM_CONTEXT_TOKENS",
    "LLM_REPLY_TOKENS", "LLM_VOICE_ANSWER_CHARS", "WHISPER_MODEL", "PIPER_MODEL", "STT_BACKEND", "WHISPER_METAL_MODEL",
    "VAD_STOP_SECONDS", "VAD_SENSITIVE_STOP_SECONDS", "STT_PREEMPTIVE_ENABLED",
    "STT_PREEMPTIVE_SILENCE_SECONDS", "VOICE_ACKNOWLEDGEMENT_ENABLED", "VOICE_ACKNOWLEDGEMENT_DELAY_SECONDS",
    "NATURAL_TTS_ENABLED", "NATURAL_TTS_MODEL",
    "VOICE_REFERENCE_DIR", "NATURAL_TTS_VOICE", "NATURAL_TTS_MALE_VOICE",
    "NATURAL_TTS_FEMALE_VOICE", "NATURAL_TTS_URL", "NATURAL_TTS_API_KEY",
    "TTS_STREAMING_INTERVAL", "TTS_CACHE_MAX_BYTES", "TTS_CACHE_MAX_ENTRIES", "MALE_SPEECH_RATE",
    "CHANNEL_API_KEY", "PRIVATE_RUNTIME_DIR", "TELEGRAM_WORKER_URL", "SOCKET_TRANSPORT_ENABLED",
    "SOCKET_TRANSPORT_PORT", "LAN_ENABLED", "LAN_IP", "ALLOWED_HOSTS", "ALLOWED_ORIGINS",
    "COOKIE_SECURE", "SESSION_TTL_SECONDS", "RETENTION_DAYS", "CALL_QUEUE_CAPACITY",
    "CALL_QUEUE_WAIT_SECONDS", "HOST", "PORT",
})

TTS_SETTINGS = frozenset({
    "NATURAL_TTS_MODEL", "VOICE_REFERENCE_DIR", "NATURAL_TTS_VOICE", "NATURAL_TTS_MALE_VOICE",
    "NATURAL_TTS_FEMALE_VOICE", "NATURAL_TTS_API_KEY", "TTS_STREAMING_INTERVAL",
    "TTS_CACHE_MAX_BYTES", "TTS_CACHE_MAX_ENTRIES", "MALE_SPEECH_RATE", "VOICE_ACKNOWLEDGEMENT_ENABLED",
})

ROLE_SETTINGS = MappingProxyType({
    "gateway": GATEWAY_SETTINGS,
    "natural-tts": TTS_SETTINGS,
    "telegram-worker": frozenset({"CHANNEL_API_KEY", "PRIVATE_RUNTIME_DIR"}),
    "llm": frozenset(),  # Its own API key is supplied through a private key file.
    "caddy": frozenset(),
    "mobile-setup": frozenset(),  # Reads only the explicit public onboarding files.
})


def configured_env_file(default: Path, environment: Mapping[str, str] | None = None) -> Path | None:
    """Resolve dotenv policy; an explicitly empty selector disables file loading.

    Relative selectors are anchored to the default file's directory, making
    configuration independent of the shell's working directory. This function
    selects a path without opening or creating any file.
    """
    environment = os.environ if environment is None else environment
    selector = environment.get(ENV_FILE_CONTROL)
    if selector is None:
        return Path(default)
    if not selector.strip():
        return None
    path = Path(selector).expanduser()
    return path if path.is_absolute() else Path(default).parent / path


def _normalized(values: Mapping) -> dict:
    return {key.upper(): value for key, value in values.items() if isinstance(key, str)}


def _environment_value(name: str, value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (str, int, float, Path)):
        result = str(value)
        if "\x00" not in result:
            return result
    raise ValueError(f"Invalid environment value for {name}; expected a scalar without NUL bytes")


def build_child_environment(
    role: str,
    settings: Mapping | None = None,
    *,
    inherited: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Build a fresh environment with no implicit application-secret inheritance.

    Pass resolved Settings.model_dump() (or an explicit configuration mapping).
    Inherited application settings are never used as a fallback: only the
    separate settings argument can supply credentials allowed for this role.
    """
    if role not in ROLE_SETTINGS:
        raise ValueError("Unknown child role; define an explicit environment policy before launching it")
    source = _normalized(os.environ if inherited is None else inherited)
    runtime_names = SYSTEM_ENVIRONMENT_VARIABLES
    if role in {"gateway", "natural-tts", "llm"}:
        runtime_names |= INFERENCE_ENVIRONMENT_VARIABLES
    result = {
        name: _environment_value(name, source[name])
        for name in runtime_names if source.get(name) is not None
    }
    configuration = _normalized({} if settings is None else settings)
    for name in ROLE_SETTINGS[role]:
        if configuration.get(name) is not None:
            result[name] = _environment_value(name, configuration[name])
    result[ENV_FILE_CONTROL] = ""
    return result
