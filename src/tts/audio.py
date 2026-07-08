"""Audio streaming helpers — chunking with cross-fade at boundaries.

Fix for issue #48: the TTS engines emit one float32 array per `synthesize()`
call. When we split it into fixed-size chunks (e.g. 4800 samples @ 24 kHz =
200 ms), any discontinuity at the chunk boundary becomes an audible click.
A small linear cross-fade between consecutive chunks smooths the seam without
altering the rest of the signal.
"""

import numpy as np

_CHUNK_SAMPLES = 4800  # 200 ms at 24 kHz — matches KokoroEngine / MockEngine.
_CROSSFADE_SAMPLES = 240  # 10 ms at 24 kHz — perceptual sweet spot.


def crossfade_blend(
    prev_tail: np.ndarray, curr_head: np.ndarray, n: int = _CROSSFADE_SAMPLES
) -> np.ndarray:
    """Return the `n`-sample blended region between two adjacent chunks.

    Output is a length-`n` array meant to replace the head of the new chunk.
    `out[0]` matches `prev_tail[-1]` (so the seam with the previous chunk is
    continuous) and `out[-1]` approaches `curr_head[n-1]`. The interpolation
    is linear in time: it walks from the most recent sample of `prev_tail`
    forward through the first `n` samples of `curr_head`.

    Note: `prev_tail[-n:]` is reversed so that index 0 of the slice is the
    sample *closest* to the seam (i.e. `prev_tail[-1]`), not the oldest one.
    Without the flip the seam click would shift backward by `n` samples.
    """
    if len(prev_tail) < n or len(curr_head) < n:
        raise ValueError(
            f"crossfade needs ≥{n} samples on each side, "
            f"got {len(prev_tail)} and {len(curr_head)}"
        )
    w = np.arange(n, dtype=np.float32) / n
    prev_aligned = prev_tail[-n:][::-1]  # [prev_tail[-1], prev_tail[-2], ..., prev_tail[-n]]
    return (1.0 - w) * prev_aligned + w * curr_head[:n]


def iter_chunks_with_crossfade(
    audio: np.ndarray,
    chunk_size: int = _CHUNK_SAMPLES,
    n: int = _CROSSFADE_SAMPLES,
):
    """Yield float32 chunks of `audio`, cross-fading the first `n` samples of
    each chunk with the last `n` samples of the previous one.

    State (the previous chunk's tail) lives in the generator's local frame,
    so concurrent callers don't trample each other — safe even when the engine
    that drives this is a process-wide singleton.

    The first chunk is emitted verbatim (no predecessor). The last chunk, if
    shorter than `chunk_size`, still has its first `n` samples blended against
    the previous chunk's tail; only chunks shorter than `n` on either side
    are emitted verbatim.
    """
    if n > chunk_size:
        raise ValueError(f"crossfade n={n} must be ≤ chunk_size={chunk_size}")
    prev: np.ndarray | None = None
    for start in range(0, len(audio), chunk_size):
        chunk = audio[start : start + chunk_size]
        if prev is None or len(prev) < n or len(chunk) < n:
            yield chunk
        else:
            head = crossfade_blend(prev, chunk, n)
            yield np.concatenate([head, chunk[n:]])
        prev = chunk