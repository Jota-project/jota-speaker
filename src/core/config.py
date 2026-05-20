from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="JOTA_", env_file=".env", extra="ignore")

    engine: str = "mock"
    kokoro_model: str = "kokoro-v1.0.int8.onnx"
    kokoro_voices: str = "voices-v1.0.bin"
    kokoro_voice: str = "af_heart"
    kokoro_lang: str = "en-us"
    sample_rate: int = 24000
    min_flush_chars: int = 80
    auth_provider: str = "stub"
    jota_db_url: str = "http://localhost:8001"
    jota_db_auth_path: str = "/auth/validate"
    jota_db_timeout: float = 5.0
    session_timeout: float = 300.0
    queue_maxsize: int = 100
    wyoming_enabled: bool = True
    wyoming_port: int = 20424


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
