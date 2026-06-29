import asyncio
import sys
import threading
import time
import types

import numpy as np
import pytest

from src.tts.kokoro.engine import KokoroEngine


class _FakeKokoro:
    """Stand-in for kokoro_onnx.Kokoro that records concurrent calls."""

    def __init__(self) -> None:
        self.active = 0
        self.max_active = 0
        self.lock = threading.Lock()
        self._hang = False

    def create(self, text, voice=None, speed=None, lang=None):
        with self.lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            if self._hang:
                # block until cancelled — but we cannot truly cancel threads.
                # Use a short sleep; the engine's executor.shutdown will reap it.
                time.sleep(10)
            # Return 1 second of silence at 24 kHz
            return np.zeros(24000, dtype=np.float32), 24000
        finally:
            with self.lock:
                self.active -= 1


@pytest.fixture
def fake_kokoro(monkeypatch):
    fake = _FakeKokoro()
    # Patch kokoro_onnx at the module level so the constructor's
    # `from kokoro_onnx import Kokoro` returns our fake.
    fake_mod = types.ModuleType("kokoro_onnx")
    fake_mod.Kokoro = lambda *a, **kw: fake
    monkeypatch.setitem(sys.modules, "kokoro_onnx", fake_mod)
    return fake


@pytest.mark.asyncio
async def test_kokoro_engine_serializes_concurrent_calls(fake_kokoro):
    eng = KokoroEngine(
        model_path="x", voices_path="y",
        synthesize_timeout=None,
    )
    # Two concurrent synthesize calls should never overlap inside _run_inference.
    results = await asyncio.gather(
        eng.synthesize("a").__anext__(),
        eng.synthesize("b").__anext__(),
    )
    assert len(results) == 2
    assert fake_kokoro.max_active == 1


@pytest.mark.asyncio
async def test_kokoro_engine_synthesize_timeout_raises(fake_kokoro):
    fake_kokoro._hang = True
    eng = KokoroEngine(
        model_path="x", voices_path="y",
        synthesize_timeout=0.05,
    )
    with pytest.raises(asyncio.TimeoutError):
        # Drain just the first chunk so the underlying run_in_executor runs.
        async for _ in eng.synthesize("slow"):
            break
    await eng.aclose()


@pytest.mark.asyncio
async def test_kokoro_engine_aclose_clears_resources(fake_kokoro):
    eng = KokoroEngine(model_path="x", voices_path="y", synthesize_timeout=None)
    assert eng._executor is not None
    await eng.aclose()
    assert eng._kokoro is None
    # Calling aclose twice is safe
    await eng.aclose()