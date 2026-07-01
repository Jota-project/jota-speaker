from src.core.config import Settings
from src.tts.normalizer import (
    INormalizer,
    PassThroughNormalizer,
    SpanishNormalizer,
)


def create_normalizer(settings: Settings) -> INormalizer:
    match settings.normalizer:
        case "none":
            return PassThroughNormalizer()
        case "spanish":
            return SpanishNormalizer(
                excluded_patterns=settings.normalizer_excluded_patterns,
                hour_format=settings.hour_format,
            )
        case _:
            raise ValueError(f"Unknown normalizer: {settings.normalizer!r}")
