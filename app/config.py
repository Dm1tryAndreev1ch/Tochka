from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.runtime_environment import configured_env_file

ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / ".env", extra="ignore")

    def __init__(self, **values):
        if "_env_file" not in values:
            values["_env_file"] = configured_env_file(ROOT / ".env")
        super().__init__(**values)

    database_url: str = "postgresql://voice:voice@127.0.0.1:15432/voice"
    admin_password: str = ""
    operator_password: str = ""
    management_secret: str = ""
    espo_url: str = "http://127.0.0.1:18081"
    espo_api_key: str = ""
    espo_assigned_user_id: str = ""
    llm_api_key: str = ""
    ice_host_ip: str = "127.0.0.1"
    llm_url: str = "http://127.0.0.1:18082/v1"
    llm_remote: bool = False
    llm_model: str = "qwen3-4b"
    llm_model_path: str = "models/qwen3-4b-q4.gguf"
    llm_thinking: bool = False
    llm_prompt_profile: Literal["legacy", "compact"] = "compact"
    llm_gpu_layers: str = "99"
    llm_threads: int = Field(default=4, ge=1)
    llm_context_tokens: int = Field(default=8192, ge=1)
    llm_reply_tokens: int = Field(default=256, ge=1)
    llm_voice_answer_chars: int = Field(default=180, ge=80, le=400)
    whisper_model: str = "models/whisper-small"
    piper_model: str = "models/ru_RU-denis-medium.onnx"
    stt_backend: str = "faster-whisper"
    whisper_metal_model: str = "models/whisper-small-metal.bin"
    vad_stop_seconds: float = Field(default=0.95, gt=0.15, le=2.0)
    vad_sensitive_stop_seconds: float = Field(default=0.95, gt=0.15, le=2.0)
    stt_preemptive_enabled: bool = True
    stt_preemptive_silence_seconds: float = Field(default=0.12, gt=0)
    voice_acknowledgement_enabled: bool = True
    voice_acknowledgement_delay_seconds: float = Field(default=1.8, ge=0.5, le=10)
    natural_tts_enabled: bool = False
    natural_tts_model: str = "models/qwen3-tts-base-0.6b"
    voice_reference_dir: str = "app/assets/voices"
    natural_tts_voice: str = "ryan"
    natural_tts_male_voice: str = "uncle_fu"
    natural_tts_female_voice: str = "serena"
    natural_tts_url: str = "http://127.0.0.1:18083"
    natural_tts_api_key: str = ""
    tts_streaming_interval: float = 0.32
    tts_cache_max_bytes: int = Field(default=8 * 1024 * 1024, ge=0)
    tts_cache_max_entries: int = Field(default=128, ge=0)
    male_speech_rate: float = 1.10
    channel_api_key: str = ""
    private_runtime_dir: str = str(Path.home() / ".cache/voice-desk/private")
    telegram_worker_url: str = "http://127.0.0.1:18084"
    socket_transport_enabled: bool = True
    socket_transport_port: int = 9019
    lan_enabled: bool = False
    lan_ip: str = ""
    allowed_hosts: str = "localhost,127.0.0.1"
    allowed_origins: str = "http://127.0.0.1:7860,http://localhost:7860"
    cookie_secure: bool = False
    session_ttl_seconds: int = 3600
    retention_days: int = 30
    call_queue_capacity: int = 7
    call_queue_wait_seconds: int = 300
    host: str = "127.0.0.1"
    port: int = 7860

    @field_validator("llm_url")
    @classmethod
    def normalize_llm_url(cls, value):
        return value.rstrip("/")

    @model_validator(mode="after")
    def validate_voice_timing(self):
        if self.vad_sensitive_stop_seconds < self.vad_stop_seconds:
            raise ValueError("VAD_SENSITIVE_STOP_SECONDS must be at least VAD_STOP_SECONDS")
        if self.stt_preemptive_silence_seconds >= self.vad_stop_seconds:
            raise ValueError("STT_PREEMPTIVE_SILENCE_SECONDS must be below VAD_STOP_SECONDS")
        return self


settings = Settings()
