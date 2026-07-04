"""Unit tests for RIFF/WAVE header builder and streaming encoders."""

import asyncio
import shutil
import struct
import subprocess
from io import BytesIO
from wave import open as wave_open

import pytest

from src.openai.encoder import build_wav_header, pcm_stream, wav_stream


def _async_iter(chunks):
    """Create an AsyncIterator[bytes] from a list of chunks."""
    async def gen():
        for c in chunks:
            yield c
    return gen()


HEADER_LEN = 44


class TestWavHeaderBuilder:
    def test_returns_44_bytes(self):
        h = build_wav_header(24000)
        assert len(h) == HEADER_LEN

    def test_chunk_ids(self):
        h = build_wav_header(24000)
        assert h[0:4] == b"RIFF"
        assert h[8:12] == b"WAVE"
        assert h[12:16] == b"fmt "
        assert h[36:40] == b"data"

    def test_chunk_size_is_sentinel(self):
        h = build_wav_header(24000)
        assert struct.unpack("<I", h[4:8])[0] == 0xFFFFFFFF
        assert struct.unpack("<I", h[40:44])[0] == 0xFFFFFFFF

    def test_pcm_format_fields(self):
        h = build_wav_header(24000)
        assert struct.unpack("<H", h[20:22])[0] == 1     # AudioFormat = PCM
        assert struct.unpack("<H", h[22:24])[0] == 1     # NumChannels = 1
        assert struct.unpack("<I", h[24:28])[0] == 24000  # SampleRate
        # ByteRate = sample_rate * channels * bytes_per_sample = 24000 * 1 * 2
        assert struct.unpack("<I", h[28:32])[0] == 48000
        assert struct.unpack("<H", h[32:34])[0] == 2     # BlockAlign
        assert struct.unpack("<H", h[34:36])[0] == 16    # BitsPerSample

    @pytest.mark.parametrize("sample_rate", [8000, 16000, 24000, 32000, 44100, 48000])
    def test_different_sample_rates(self, sample_rate):
        h = build_wav_header(sample_rate)
        assert struct.unpack("<I", h[24:28])[0] == sample_rate
        assert struct.unpack("<I", h[28:32])[0] == sample_rate * 2
        assert len(h) == HEADER_LEN


class TestPcmStream:
    @pytest.mark.asyncio
    async def test_passthrough_unchanged(self):
        chunks = [b"\x01\x02", b"\x03\x04\x05\x06", b""]
        result = []
        async for c in pcm_stream(_async_iter(chunks)):
            result.append(c)
        assert result == chunks

    @pytest.mark.asyncio
    async def test_empty_input_yields_nothing(self):
        result = []
        async for c in pcm_stream(_async_iter([])):
            result.append(c)
        assert result == []


class TestWavStream:
    @pytest.mark.asyncio
    async def test_emits_header_then_passthrough(self):
        chunks = [b"\xAA\xBB", b"\xCC\xDD"]
        result = []
        async for c in wav_stream(_async_iter(chunks), 24000):
            result.append(c)

        # First item is the 44-byte header
        assert len(result[0]) == HEADER_LEN
        assert result[0][:4] == b"RIFF"
        # Remaining items are the chunks unchanged
        assert result[1:] == chunks

    @pytest.mark.asyncio
    async def test_empty_input_yields_only_header(self):
        result = []
        async for c in wav_stream(_async_iter([]), 24000):
            result.append(c)
        assert len(result) == 1
        assert len(result[0]) == HEADER_LEN


class TestWavHeaderStdlibCompat:
    """The Python stdlib `wave` module opens files in 'rb' mode for reading.
    Files with 0xFFFFFFFF length fields are tolerated (opening succeeds,
    though reads may return 0 frames). This validates that the header
    bytes are syntactically well-formed per RIFF spec."""

    def test_wave_open_succeeds_on_output(self):
        # Build a complete WAV file: header + silence frame
        header = build_wav_header(24000)
        silence = b"\x00" * (4800 * 2)  # 200ms silence at 24kHz
        wav_bytes = header + silence

        buf = BytesIO(wav_bytes)
        # If the header is malformed, wave.open will raise wave.Error or
        # struct.error. Sentinel sizes are accepted.
        with wave_open(buf, "rb") as w:
            assert w.getframerate() == 24000
            assert w.getnchannels() == 1
            assert w.getsampwidth() == 2


@pytest.mark.skipif(shutil.which("ffprobe") is None, reason="ffprobe not installed")
class TestWavHeaderFfprobeCompat:
    """ffprobe is the gold-standard validator. It will reject malformed
    headers. Sentinel sizes are accepted by ffmpeg."""

    def test_ffprobe_accepts_output(self, tmp_path):
        header = build_wav_header(24000)
        silence = b"\x00" * (4800 * 2)
        wav_path = tmp_path / "test.wav"
        wav_path.write_bytes(header + silence)

        result = subprocess.run(
            ["ffprobe", "-v", "error", "-i", str(wav_path)],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"ffprobe rejected: {result.stderr}"
