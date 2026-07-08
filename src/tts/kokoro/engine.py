import asyncio
from concurrent.futures import ThreadPoolExecutor
from typing import AsyncIterator

import numpy as np

from src.core.logger import get_logger
from src.tts.interface import ITTSEngine

logger = get_logger(__name__)

_CHUNK_SAMPLES = 4800  # 200ms at 24 kHz


class KokoroEngine(ITTSEngine):
    """Runs Kokoro ONNX inference in a dedicated single-worker thread pool.

    A dedicated executor (instead of the default shared one) lets us cancel
    pending inference on shutdown. A lock serializes calls because
    Kokoro.create() is not thread-safe.
    """

    def __init__(
        self,
        model_path: str,
        voices_path: str,
        voice: str = "af_heart",
        lang: str = "en-us",
        sample_rate: int = 24000,
        synthesize_timeout: float | None = None,
    ) -> None:
        from kokoro_onnx import Kokoro  # type: ignore[import-untyped]

        self._voice = voice
        self._lang = lang
        self._sample_rate = sample_rate
        self.synthesize_timeout = synthesize_timeout
        logger.info("Loading Kokoro model from %s …", model_path)
        self._kokoro = Kokoro(model_path, voices_path)
        self._executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="kokoro"
        )
        self._inference_lock = asyncio.Lock()
        logger.info("Kokoro model loaded.")

    @property
    def sample_rate(self) -> int:
        return self._sample_rate

    @property
    def is_ready(self) -> bool:
        return self._kokoro is not None

    async def synthesize(
        self, text: str, voice: str | None = None, speed: float | None = None
    ) -> AsyncIterator[bytes]:
        loop = asyncio.get_running_loop()
        resolved_voice = voice or self._voice
        resolved_speed = self.resolve_speed(speed)
        async with self._inference_lock:
            try:
                if self.synthesize_timeout is not None:
                    audio: np.ndarray = await asyncio.wait_for(
                        loop.run_in_executor(
                            self._executor,
                            self._run_inference,
                            text,
                            resolved_voice,
                            resolved_speed,
                        ),
                        timeout=self.synthesize_timeout,
                    )
                else:
                    audio = await loop.run_in_executor(
                        self._executor,
                        self._run_inference,
                        text,
                        resolved_voice,
                        resolved_speed,
                    )
            except asyncio.TimeoutError:
                logger.error("Kokoro inference timed out after %.2fs", self.synthesize_timeout)
                raise

        for start in range(0, len(audio), _CHUNK_SAMPLES):
            chunk = audio[start : start + _CHUNK_SAMPLES]
            pcm16 = (chunk * 32767).astype(np.int16).tobytes()
            yield pcm16

    @property
    def default_voice(self) -> str:
        return self._voice

    def available_voices(self) -> list[str] | None:
        return self._kokoro.get_voices()

    # Kokoro's create() asserts 0.5 <= speed <= 2.0 and raises outside it.
    _MIN_SPEED = 0.5
    _MAX_SPEED = 2.0

    def resolve_speed(self, requested: float | None) -> float:
        if requested is None:
            return 1.0
        return max(self._MIN_SPEED, min(self._MAX_SPEED, requested))

    # ── sync (runs in dedicated thread pool) ─────────────────────────────────

    def _run_inference(self, text: str, voice: str, speed: float) -> np.ndarray:
        samples, _ = self._kokoro.create(
            text, voice=voice, speed=speed, lang=self._lang
        )
        return samples.astype(np.float32)

    # ── lifecycle ─────────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        """Release native resources. Idempotent and safe to call twice."""
        if self._executor is not None:
            # cancel_futures=True ensures any queued inference is dropped
            self._executor.shutdown(wait=False, cancel_futures=True)
            self._executor = None
        self._kokoro = None
        logger.info("Kokoro engine closed.")