import numpy as np

from src.tts.audio import crossfade_blend, iter_chunks_with_crossfade


def test_crossfade_blend_smooths_step_discontinuity():
    prev = np.ones(240, dtype=np.float32)
    curr = -np.ones(240, dtype=np.float32)
    out = crossfade_blend(prev, curr, n=240)
    assert abs(out[0] - 1.0) < 1e-6
    assert abs(out[-1] - (-1.0)) < 0.01
    diffs = np.diff(out)
    assert (diffs <= 0).all()
    assert np.abs(diffs).max() < 0.02  # slope bounded by 2/N ≈ 0.008


def test_crossfade_blend_rejects_short_inputs():
    prev = np.ones(100, dtype=np.float32)
    curr = np.zeros(240, dtype=np.float32)
    try:
        crossfade_blend(prev, curr, n=240)
    except ValueError:
        return
    raise AssertionError("expected ValueError")


def test_iter_chunks_with_crossfade_first_chunk_unchanged():
    a = np.arange(9600, dtype=np.float32)
    chunks = list(iter_chunks_with_crossfade(a))
    np.testing.assert_array_equal(chunks[0], a[:4800])
    # chunk 1 starts with the blend at w=0 → fully prev_tail → matches a[4799].
    assert abs(chunks[1][0] - a[4799]) < 1e-3