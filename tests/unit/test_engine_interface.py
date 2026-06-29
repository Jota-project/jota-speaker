import asyncio
import pytest

from src.tts.interface import ITTSEngine
from src.tts.mock_engine import MockEngine


def test_ittsengine_requires_aclose():
    """Any subclass must implement aclose()."""

    class IncompleteEngine(ITTSEngine):
        async def synthesize(self, text: str):
            yield b""

        @property
        def sample_rate(self) -> int:
            return 24000

    with pytest.raises(TypeError):
        IncompleteEngine()  # missing aclose()


@pytest.mark.asyncio
async def test_ittsengine_synthesize_timeout_default_is_none():
    class DummyEngine(ITTSEngine):
        async def synthesize(self, text: str):
            yield b""

        @property
        def sample_rate(self) -> int:
            return 24000

        async def aclose(self) -> None:
            pass

    eng = DummyEngine()
    assert getattr(eng, "synthesize_timeout", None) is None


@pytest.mark.asyncio
async def test_mock_engine_has_aclose_and_default_timeout():
    eng = MockEngine()
    assert hasattr(eng, "aclose")
    assert await eng.aclose() is None
    assert eng.synthesize_timeout is None


@pytest.mark.asyncio
async def test_mock_engine_aclose_is_idempotent():
    eng = MockEngine()
    await eng.aclose()
    await eng.aclose()  # no debe lanzar