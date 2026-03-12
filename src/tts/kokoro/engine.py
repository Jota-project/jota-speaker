import asyncio
from typing import AsyncIterator

import numpy as np

from src.core.logger import get_logger
from src.tts.interface import ITTSEngine

logger = get_logger(__name__)

_CHUNK_SAMPLES = 4800  # 200ms at 24 kHz


class KokoroEngine(ITTSEngine):
    """Runs Kokoro ONNX inference in a thread pool to avoid blocking the event loop."""

    def __init__(self, model_path: str, voice: str, sample_rate: int = 24000) -> None:
        from kokoro_onnx import Kokoro  # type: ignore[import-untyped]

        self._voice = voice
        self._sample_rate = sample_rate
        logger.info("Loading Kokoro model from %s …", model_path)
        self._kokoro = Kokoro(model_path, "voices.json")
        logger.info("Kokoro model loaded.")

    @property
    def sample_rate(self) -> int:
        return self._sample_rate

    async def synthesize(self, text: str) -> AsyncIterator[bytes]:
        loop = asyncio.get_running_loop()
        audio: np.ndarray = await loop.run_in_executor(
            None, self._run_inference, text
        )
        # Divide into chunks and yield
        for start in range(0, len(audio), _CHUNK_SAMPLES):
            chunk = audio[start : start + _CHUNK_SAMPLES]
            pcm16 = (chunk * 32767).astype(np.int16).tobytes()
            yield pcm16

    # ── sync (runs in thread pool) ────────────────────────────────────────────

    def _run_inference(self, text: str) -> np.ndarray:
        samples, _ = self._kokoro.create(text, voice=self._voice, speed=1.0, lang="en-us")
        return samples.astype(np.float32)
