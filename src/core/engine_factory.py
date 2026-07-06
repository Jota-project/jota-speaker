from pathlib import Path

from src.core.config import Settings
from src.core.engine_registry import EngineRegistry
from src.tts.interface import ITTSEngine


def create_engine_registry(settings: Settings) -> EngineRegistry:
    match settings.engine:
        case "mock":
            from src.tts.mock_engine import MockEngine
            return EngineRegistry(
                {"mock": MockEngine(sample_rate=settings.sample_rate)}, "mock"
            )
        case "kokoro":
            from src.tts.kokoro.engine import KokoroEngine

            models_dir = Path(settings.kokoro_model).parent
            onnx_files = sorted(models_dir.glob("*.onnx"))
            if not onnx_files:
                raise ValueError(f"No .onnx model files found in {models_dir}")

            engines: dict[str, ITTSEngine] = {}
            for path in onnx_files:
                engines[path.stem] = KokoroEngine(
                    model_path=str(path),
                    voices_path=settings.kokoro_voices,
                    voice=settings.kokoro_voice,
                    lang=settings.kokoro_lang,
                    sample_rate=settings.sample_rate,
                    synthesize_timeout=settings.kokoro_synthesize_timeout,
                )

            default_model = Path(settings.kokoro_model).stem
            if default_model not in engines:
                raise ValueError(
                    f"Default model {default_model!r} (JOTA_KOKORO_MODEL) "
                    f"not found among discovered models: {sorted(engines)}"
                )
            return EngineRegistry(engines, default_model)
        case _:
            raise ValueError(f"Unknown TTS engine: {settings.engine!r}")
