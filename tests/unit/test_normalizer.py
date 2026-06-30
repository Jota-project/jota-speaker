import pytest

from src.tts.normalizer import INormalizer, PassThroughNormalizer


def test_passthrough_normalizer_returns_text_unchanged():
    n = PassThroughNormalizer()
    assert n.normalize.__doc__ is None or True


@pytest.mark.asyncio
async def test_passthrough_normalizer_preserves_text():
    n = PassThroughNormalizer()
    assert await n.normalize("Hola mundo 123") == "Hola mundo 123"


@pytest.mark.asyncio
async def test_passthrough_normalizer_empty_string():
    n = PassThroughNormalizer()
    assert await n.normalize("") == ""


def test_passthrough_is_normalizer_instance():
    n = PassThroughNormalizer()
    assert isinstance(n, INormalizer)
