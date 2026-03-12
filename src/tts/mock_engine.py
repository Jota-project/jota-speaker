import asyncio
from typing import AsyncIterator

from .interface import ITTSEngine

# 200ms of silence per frame at 24 kHz, PCM16 (2 bytes/sample)
_FRAME_SAMPLES = 4800
_SILENCE_FRAME = b"\x00" * (_FRAME_SAMPLES * 2)
_FRAMES_PER_CHAR = 1  # emit 1 frame per character so tests get audible output


class MockEngine(ITTSEngine):
    """Generates silence PCM16 frames. Used for dev/CI."""

    def __init__(self, sample_rate: int = 24000) -> None:
        self._sample_rate = sample_rate

    @property
    def sample_rate(self) -> int:
        return self._sample_rate

    async def synthesize(self, text: str) -> AsyncIterator[bytes]:
        frames = max(1, len(text) * _FRAMES_PER_CHAR)
        for _ in range(frames):
            await asyncio.sleep(0)  # yield control
            yield _SILENCE_FRAME
