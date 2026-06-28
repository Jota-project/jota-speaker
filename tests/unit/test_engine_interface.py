import asyncio
import pytest

from src.tts.interface import ITTSEngine


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