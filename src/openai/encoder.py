"""Streaming audio encoders for /v1/audio/speech.

Three encoders wrap an AsyncIterator[bytes] of PCM16 LE mono chunks:

- `pcm_stream`: passthrough. Used when response_format="pcm".
- `wav_stream`: prepends a 44-byte RIFF/WAVE header. Used when
  response_format="wav". The header is pre-calculated but the total
  audio length is unknown, so both ChunkSize and data Subchunk2Size
  are set to 0xFFFFFFFF (the RF64 sentinel). All modern decoders
  (ffmpeg, VLC, Chrome, wave.open) handle this correctly.
- `mp3_stream`: pipes chunks through an ffmpeg subprocess to produce
  MP3 (64 kbps CBR mono). Used when response_format="mp3".
"""

from __future__ import annotations

import asyncio
import shutil
import struct
from typing import AsyncIterator

from src.core.logger import get_logger

_log = get_logger(__name__)


def ffmpeg_available() -> bool:
    """Check whether the ffmpeg binary is on PATH."""
    return shutil.which("ffmpeg") is not None


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


_MP3_BITRATE = "64k"
_READ_CHUNK_SIZE = 4096


async def mp3_stream(
    source: AsyncIterator[bytes],
    sample_rate: int,
) -> AsyncIterator[bytes]:
    """Encode a PCM16 LE mono stream to MP3 (64 kbps CBR) via a piped ffmpeg subprocess.

    A background task feeds `source` chunks into ffmpeg's stdin and closes it
    once `source` is exhausted; this generator yields stdout chunks as ffmpeg
    produces them, so the first MP3 bytes can reach the client before the
    whole input has been synthesized.
    """
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg",
        "-loglevel", "error",
        "-f", "s16le",
        "-ar", str(sample_rate),
        "-ac", "1",
        "-i", "pipe:0",
        "-f", "mp3",
        "-b:a", _MP3_BITRATE,
        "pipe:1",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    async def _feed_stdin() -> None:
        try:
            async for chunk in source:
                proc.stdin.write(chunk)
                await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:
            _log.warning("mp3_stream: source raised while feeding ffmpeg: %s", exc)
        finally:
            if not proc.stdin.is_closing():
                proc.stdin.close()

    async def _drain_stderr() -> None:
        stderr = await proc.stderr.read()
        if stderr:
            _log.warning("ffmpeg stderr: %s", stderr.decode(errors="replace"))

    feed_task = asyncio.create_task(_feed_stdin())
    stderr_task = asyncio.create_task(_drain_stderr())

    try:
        while True:
            chunk = await proc.stdout.read(_READ_CHUNK_SIZE)
            if not chunk:
                break
            yield chunk
    finally:
        if proc.returncode is None:
            proc.kill()
        await asyncio.gather(feed_task, stderr_task, proc.wait(), return_exceptions=True)
