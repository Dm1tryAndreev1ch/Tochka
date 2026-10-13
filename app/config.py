from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / ".env", extra="ignore")
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
    llm_model: str = "qwen3-4b"
    whisper_model: str = "models/whisper-small"
    piper_model: str = "models/ru_RU-denis-medium.onnx"
    stt_backend: str = "faster-whisper"
    whisper_metal_model: str = "models/whisper-small-metal.bin"
    natural_tts_enabled: bool = False
    natural_tts_model: str = "models/qwen3-tts-base-0.6b"
    voice_reference_dir: str = "app/assets/voices"
    natural_tts_voice: str = "ryan"
    natural_tts_male_voice: str = "uncle_fu"
    natural_tts_female_voice: str = "serena"
    natural_tts_url: str = "http://127.0.0.1:18083"
    natural_tts_api_key: str = ""
    tts_streaming_interval: float = 0.32
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
    host: str = "127.0.0.1"
    port: int = 7860


settings = Settings()
