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

    def get_voices(self) -> list[str]:
        return ["ef_dora"]

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


@pytest.mark.asyncio
async def test_kokoro_engine_is_ready_reflects_loaded_state(fake_kokoro):
    eng = KokoroEngine(model_path="x", voices_path="y", synthesize_timeout=None)
    assert eng.is_ready is True
    await eng.aclose()
    assert eng.is_ready is False


def test_default_voice_returns_configured_voice(fake_kokoro):
    eng = KokoroEngine(model_path="x", voices_path="y", voice="ef_dora", synthesize_timeout=None)
    assert eng.default_voice == "ef_dora"


def test_available_voices_delegates_to_kokoro_get_voices(fake_kokoro):
    fake_kokoro.get_voices = lambda: ["ef_dora", "em_alex"]
    eng = KokoroEngine(model_path="x", voices_path="y", synthesize_timeout=None)
    assert eng.available_voices() == ["ef_dora", "em_alex"]


def test_resolve_voice_falls_back_when_not_available(fake_kokoro):
    fake_kokoro.get_voices = lambda: ["ef_dora"]
    eng = KokoroEngine(model_path="x", voices_path="y", voice="ef_dora", synthesize_timeout=None)
    assert eng.resolve_voice("nonexistent") == "ef_dora"
    assert eng.resolve_voice("ef_dora") == "ef_dora"


@pytest.mark.asyncio
async def test_synthesize_passes_resolved_voice_to_kokoro_create(fake_kokoro):
    calls = []
    original_create = fake_kokoro.create

    def recording_create(text, voice=None, speed=None, lang=None):
        calls.append(voice)
        return original_create(text, voice=voice, speed=speed, lang=lang)

    fake_kokoro.create = recording_create
    eng = KokoroEngine(model_path="x", voices_path="y", voice="ef_dora", synthesize_timeout=None)
    async for _ in eng.synthesize("hola", voice="em_alex"):
        break
    assert calls == ["em_alex"]


def test_resolve_speed_defaults_to_one_when_none(fake_kokoro):
    eng = KokoroEngine(model_path="x", voices_path="y", synthesize_timeout=None)
    assert eng.resolve_speed(None) == 1.0


@pytest.mark.parametrize(
    "requested,expected",
    [
        (1.0, 1.0),
        (0.5, 0.5),
        (2.0, 2.0),
        (0.25, 0.5),  # below Kokoro's native floor -> clamped up
        (4.0, 2.0),  # above Kokoro's native ceiling -> clamped down
    ],
)
def test_resolve_speed_clamps_to_kokoro_native_range(fake_kokoro, requested, expected):
    eng = KokoroEngine(model_path="x", voices_path="y", synthesize_timeout=None)
    assert eng.resolve_speed(requested) == expected


@pytest.mark.asyncio
async def test_synthesize_passes_resolved_speed_to_kokoro_create(fake_kokoro):
    calls = []
    original_create = fake_kokoro.create

    def recording_create(text, voice=None, speed=None, lang=None):
        calls.append(speed)
        return original_create(text, voice=voice, speed=speed, lang=lang)

    fake_kokoro.create = recording_create
    eng = KokoroEngine(model_path="x", voices_path="y", synthesize_timeout=None)

    async for _ in eng.synthesize("hola", speed=4.0):  # out of range, must clamp
        break
    assert calls == [2.0]

    async for _ in eng.synthesize("hola"):  # no speed requested -> default 1.0
        break
    assert calls == [2.0, 1.0]