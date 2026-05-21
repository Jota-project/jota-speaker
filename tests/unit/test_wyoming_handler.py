import asyncio
import json

import pytest

from src.core.config import Settings
from src.tts.mock_engine import MockEngine
from src.wyoming.handler import WyomingHandler


class _FakeWriter:
    def __init__(self):
        self.data = bytearray()
        self.closed = False

    def write(self, chunk: bytes) -> None:
        self.data.extend(chunk)

    async def drain(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        pass

    def get_extra_info(self, key: str, default=None):
        return ("127.0.0.1", 12345) if key == "peername" else default

    def parse_events(self) -> list[tuple[str, dict, bytes]]:
        """Return list of (event_type, data, payload) parsed from written bytes."""
        result = []
        buf = bytes(self.data)
        pos = 0
        while pos < len(buf):
            nl = buf.index(b"\n", pos)
            header = json.loads(buf[pos:nl].decode())
            pos = nl + 1
            pl = header.get("payload_length", 0)
            payload = buf[pos : pos + pl]
            pos += pl
            result.append((header["type"], header.get("data", {}), payload))
        return result


def _reader_with_synthesize(text: str) -> asyncio.StreamReader:
    line = json.dumps({"type": "synthesize", "data": {"text": text}}) + "\n"
    r = asyncio.StreamReader()
    r.feed_data(line.encode())
    r.feed_eof()
    return r


async def test_synthesize_sends_start_chunks_stop():
    handler = WyomingHandler(MockEngine(sample_rate=24000), Settings(engine="mock"))
    writer = _FakeWriter()
    await handler.handle(_reader_with_synthesize("Hi"), writer)
    types = [e[0] for e in writer.parse_events()]
    assert types[0] == "audio-start"
    assert types[-1] == "audio-stop"
    assert "audio-chunk" in types


async def test_audio_start_data_matches_engine_sample_rate():
    handler = WyomingHandler(MockEngine(sample_rate=24000), Settings(engine="mock"))
    writer = _FakeWriter()
    await handler.handle(_reader_with_synthesize("Hello"), writer)
    events = writer.parse_events()
    _, start_data, _ = events[0]
    assert start_data == {"rate": 24000, "width": 2, "channels": 1}


async def test_empty_text_sends_no_events():
    handler = WyomingHandler(MockEngine(), Settings(engine="mock"))
    writer = _FakeWriter()
    r = asyncio.StreamReader()
    r.feed_data(
        (json.dumps({"type": "synthesize", "data": {"text": ""}}) + "\n").encode()
    )
    r.feed_eof()
    await handler.handle(r, writer)
    assert writer.data == bytearray()


async def test_eof_closes_writer():
    handler = WyomingHandler(MockEngine(), Settings(engine="mock"))
    writer = _FakeWriter()
    r = asyncio.StreamReader()
    r.feed_eof()
    await handler.handle(r, writer)
    assert writer.closed


async def test_chunk_payloads_are_valid_pcm16():
    handler = WyomingHandler(MockEngine(sample_rate=24000), Settings(engine="mock"))
    writer = _FakeWriter()
    await handler.handle(_reader_with_synthesize("Hello world"), writer)
    for event_type, _, payload in writer.parse_events():
        if event_type == "audio-chunk":
            assert len(payload) % 2 == 0, "PCM16 payload must be even-length bytes"


async def test_describe_returns_info_with_language():
    handler = WyomingHandler(MockEngine(), Settings(engine="mock", kokoro_lang="es"))
    writer = _FakeWriter()
    line = json.dumps({"type": "describe"}) + "\n"
    r = asyncio.StreamReader()
    r.feed_data(line.encode())
    r.feed_eof()
    await handler.handle(r, writer)
    events = writer.parse_events()
    assert events[0][0] == "info"
    tts = events[0][1]["tts"]
    assert len(tts) == 1
    assert tts[0]["name"] == "jota-speaker"
    assert "es" in tts[0]["languages"]


async def test_engine_exception_does_not_crash_handler():
    from src.tts.interface import ITTSEngine
    from typing import AsyncIterator

    class FailingEngine(ITTSEngine):
        @property
        def sample_rate(self) -> int:
            return 24000

        async def synthesize(self, text: str) -> AsyncIterator[bytes]:
            raise RuntimeError("engine failure")
            yield  # make it an async generator

    handler = WyomingHandler(FailingEngine(), Settings(engine="mock"))
    writer = _FakeWriter()
    await handler.handle(_reader_with_synthesize("Hello"), writer)
    # Should complete without raising, writer should be closed
    assert writer.closed
