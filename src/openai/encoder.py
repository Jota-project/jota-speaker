"""Streaming audio encoders for /v1/audio/speech.

Two encoders wrap an AsyncIterator[bytes] of PCM16 LE mono chunks:

- `pcm_stream`: passthrough. Used when response_format="pcm".
- `wav_stream`: prepends a 44-byte RIFF/WAVE header. Used when
  response_format="wav". The header is pre-calculated but the total
  audio length is unknown, so both ChunkSize and data Subchunk2Size
  are set to 0xFFFFFFFF (the RF64 sentinel). All modern decoders
  (ffmpeg, VLC, Chrome, wave.open) handle this correctly.
"""

from __future__ import annotations

import struct
from typing import AsyncIterator


def build_wav_header(sample_rate: int) -> bytes:
    """Build a 44-byte RIFF/WAVE header for streaming unknown-length PCM16 LE mono audio.

    Layout (all little-endian):
        0-3    "RIFF"
        4-7    ChunkSize = 0xFFFFFFFF  (unknown)
        8-11   "WAVE"
        12-15  "fmt "
        16-19  Subchunk1Size = 16  (PCM)
        20-21  AudioFormat = 1  (PCM)
        22-23  NumChannels = 1
        24-27  SampleRate = sample_rate
        28-31  ByteRate = sample_rate * NumChannels * BitsPerSample/8
        32-33  BlockAlign = NumChannels * BitsPerSample/8 = 2
        34-35  BitsPerSample = 16
        36-39  "data"
        40-43  Subchunk2Size = 0xFFFFFFFF  (unknown)
    """
    byte_rate = sample_rate * 1 * 2  # NumChannels * BitsPerSample/8
    block_align = 2  # NumChannels * BitsPerSample/8
    return struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF",
        0xFFFFFFFF,   # ChunkSize (streaming, unknown)
        b"WAVE",
        b"fmt ",
        16,           # Subchunk1Size (PCM)
        1,            # AudioFormat (PCM)
        1,            # NumChannels (mono)
        sample_rate,
        byte_rate,
        block_align,
        16,           # BitsPerSample
        b"data",
        0xFFFFFFFF,   # Subchunk2Size (streaming, unknown)
    )


async def pcm_stream(source: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
    """Passthrough: yield each chunk unchanged."""
    async for chunk in source:
        yield chunk


async def wav_stream(
    source: AsyncIterator[bytes],
    sample_rate: int,
) -> AsyncIterator[bytes]:
    """Emit the 44-byte header once, then passthrough each PCM16 chunk."""
    yield build_wav_header(sample_rate)
    async for chunk in source:
        yield chunk
