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
        # Optional: tests can set this to a controlled float32 array to drive
        # the chunk-splitting / cross-fade logic with a known signal.
        # When None, falls back to 1s of silence (existing behaviour).
        self.output: np.ndarray | None = None

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
            if self.output is not None:
                return self.output, 24000
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


# ── issue #48: click suppression at chunk boundaries ──────────────────────────


@pytest.mark.asyncio
async def test_synthesize_crossfades_step_at_chunk_boundary(fake_kokoro):
    """Regression for issue #48: a ±1.0 step at sample 4800 must not become a click."""
    # 4 chunks × 4800. Two boundaries with full ±1.0 steps in the source array.
    audio = np.concatenate(
        [
            np.ones(4800, dtype=np.float32),
            -np.ones(4800, dtype=np.float32),
            np.ones(4800, dtype=np.float32),
            -np.ones(4800, dtype=np.float32),
        ]
    )
    fake_kokoro.output = audio

    eng = KokoroEngine(model_path="x", voices_path="y", synthesize_timeout=None)
    chunks = [c async for c in eng.synthesize("hi")]
    assert len(chunks) == 4

    samples = [np.frombuffer(c, dtype=np.int16) for c in chunks]

    # 1. No seam click between chunk 1 and chunk 2 (headline regression).
    seam = int(samples[1][0]) - int(samples[0][-1])
    assert abs(seam) < 500, f"seam jump {seam} (≈ ±32767 without the fix)"

    # 2. The blend region (chunk 2's first 240 samples) is monotonically
    #    decreasing and bounded in slope.
    ramp = samples[1][:240].astype(np.int32)
    diffs = np.diff(ramp)
    assert (diffs <= 0).all(), "blend ramp must be monotonic"
    assert np.abs(diffs).max() < 500, "blend ramp slope too steep"

    # 3. The blend/body seam inside chunk 2 (sample 239 → 240) is also smooth.
    body_seam = int(samples[1][240]) - int(samples[1][239])
    assert abs(body_seam) < 500

    # 4. The unblended body of chunk 2 is flat at -1.0.
    body = samples[1][240:]
    assert body.min() == body.max()
    assert abs(int(body[0]) - (-32767)) <= 1


@pytest.mark.asyncio
async def test_first_chunk_emitted_verbatim(fake_kokoro):
    """The first chunk has no predecessor, so it must not be blended."""
    fake_kokoro.output = np.concatenate(
        [
            np.full(4800, 0.5, dtype=np.float32),
            np.zeros(4800, dtype=np.float32),
        ]
    )
    eng = KokoroEngine(model_path="x", voices_path="y", synthesize_timeout=None)
    chunks = [c async for c in eng.synthesize("hi")]
    samples = np.frombuffer(chunks[0], dtype=np.int16)
    # 0.5 * 32767 ≈ 16383; no blending on chunk 0.
    assert samples.min() == samples.max()
    assert abs(int(samples[0]) - 16383) <= 1


@pytest.mark.asyncio
async def test_short_last_chunk_still_gets_crossfade(fake_kokoro):
    """A last chunk shorter than _CHUNK_SAMPLES still has its first 240 samples
    blended against the previous chunk's tail (only chunks shorter than the
    cross-fade window on either side are emitted verbatim)."""
    fake_kokoro.output = np.concatenate(
        [
            np.full(4800, 0.1, dtype=np.float32),
            np.full(1200, 0.9, dtype=np.float32),
        ]
    )
    eng = KokoroEngine(model_path="x", voices_path="y", synthesize_timeout=None)
    chunks = [c async for c in eng.synthesize("hi")]
    assert len(chunks) == 2
    # Chunk 1 is 1200 samples = 2400 bytes.
    assert len(chunks[1]) == 2400
    s = np.frombuffer(chunks[1], dtype=np.int16)
    # Blend starts at w=0 → fully prev_tail ≈ 0.1 * 32767.
    assert abs(int(s[0]) - 3276) < 50
    # Last sample is part of the untouched body → ≈ 0.9 * 32767.
    assert abs(int(s[-1]) - 29490) < 50


@pytest.mark.asyncio
async def test_concurrent_synthesize_have_independent_crossfade_state(fake_kokoro):
    """Two concurrent synthesize() calls must not share cross-fade state.

    The KokoroEngine is a process-wide singleton (issue #37), but the
    cross-fade `prev` lives in a per-call generator closure, so sessions
    running concurrently must keep their streams independent.
    """
    fake_a = np.concatenate(
        [np.full(4800, 0.1, dtype=np.float32), np.full(4800, 0.9, dtype=np.float32)]
    )
    fake_b = np.concatenate(
        [np.full(4800, 0.5, dtype=np.float32), np.full(4800, 0.5, dtype=np.float32)]
    )
    outputs = {"a": fake_a, "b": fake_b}
    fake_kokoro.create = lambda text, voice=None, speed=None, lang=None: (
        outputs[text],
        24000,
    )

    eng = KokoroEngine(model_path="x", voices_path="y", synthesize_timeout=None)
    a_chunks, b_chunks = await asyncio.gather(
        eng.synthesize("a").__anext__(),
        eng.synthesize("b").__anext__(),
    )
    a0 = np.frombuffer(a_chunks, dtype=np.int16)
    b0 = np.frombuffer(b_chunks, dtype=np.int16)
    # First chunk is verbatim for both.
    assert a0.min() == a0.max(), "session 'a' first chunk must be flat"
    assert b0.min() == b0.max(), "session 'b' first chunk must be flat"
    assert abs(int(a0[0]) - 3276) < 50      # 0.1 * 32767
    assert abs(int(b0[0]) - 16383) <= 1     # 0.5 * 32767