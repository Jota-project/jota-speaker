import pytest

from src.core.config import Settings
from src.core.normalizer_factory import create_normalizer
from src.tts.normalizer import (
    INormalizer,
    PassThroughNormalizer,
    SpanishNormalizer,
)


def test_factory_returns_spanish_by_default():
    s = Settings(_env_file=None)
    n = create_normalizer(s)
    assert isinstance(n, SpanishNormalizer)
    assert isinstance(n, INormalizer)


def test_factory_returns_passthrough_when_none():
    s = Settings(_env_file=None, normalizer="none")
    n = create_normalizer(s)
    assert isinstance(n, PassThroughNormalizer)


def test_factory_with_hour_format_12h():
    s = Settings(_env_file=None, hour_format="12h")
    n = create_normalizer(s)
    assert n.hour_format == "12h"


def test_factory_with_excluded_patterns():
    s = Settings(_env_file=None, normalizer_excluded_patterns=["postal_code"])
    n = create_normalizer(s)
    assert "postal_code" in n.excluded


def test_factory_unknown_normalizer_raises():
    s = Settings(_env_file=None, normalizer="klingon")
    with pytest.raises(ValueError):
        create_normalizer(s)
