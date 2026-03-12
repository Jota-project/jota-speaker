from src.core.config import Settings
from src.tts.interface import ITTSEngine


def create_engine(settings: Settings) -> ITTSEngine:
    match settings.engine:
        case "mock":
            from src.tts.mock_engine import MockEngine
            return MockEngine(sample_rate=settings.sample_rate)
        case "kokoro":
            from src.tts.kokoro.engine import KokoroEngine
            return KokoroEngine(
                model_path=settings.kokoro_model,
                voice=settings.kokoro_voice,
                sample_rate=settings.sample_rate,
            )
        case _:
            raise ValueError(f"Unknown TTS engine: {settings.engine!r}")
