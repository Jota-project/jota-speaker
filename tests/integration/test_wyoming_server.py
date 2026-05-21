import asyncio
import json

import pytest

from src.core.config import Settings
from src.tts.mock_engine import MockEngine
from src.wyoming.server import WyomingServer


async def _read_one_event(
    reader: asyncio.StreamReader,
) -> tuple[str, dict, bytes]:
    line = await reader.readline()
    if not line:
        return "", {}, b""
    header = json.loads(line.decode())
    event_type = header.get("type", "")
    data = header.get("data", {})
    pl = header.get("payload_length", 0)
    payload = await reader.readexactly(pl) if pl else b""
    return event_type, data, payload


async def _collect_until_stop(
    reader: asyncio.StreamReader,
) -> list[tuple[str, dict, bytes]]:
    events = []
    while True:
        event_type, data, payload = await asyncio.wait_for(
            _read_one_event(reader), timeout=5.0
        )
        events.append((event_type, data, payload))
        if event_type == "audio-stop":
            break
    return events


async def test_full_tcp_round_trip():
    settings = Settings(engine="mock", wyoming_port=0)
    engine = MockEngine(sample_rate=24000)
    server = WyomingServer(settings, engine)
    await server.start()
    port = server.port

    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)

        msg = json.dumps({"type": "synthesize", "data": {"text": "Hello Wyoming"}}) + "\n"
        writer.write(msg.encode())
        await writer.drain()

        events = await _collect_until_stop(reader)
        types = [e[0] for e in events]

        assert types[0] == "audio-start"
        assert types[-1] == "audio-stop"
        assert "audio-chunk" in types

        _, start_data, _ = events[0]
        assert start_data["rate"] == 24000
        assert start_data["width"] == 2
        assert start_data["channels"] == 1

        for event_type, _, payload in events:
            if event_type == "audio-chunk":
                assert len(payload) % 2 == 0

        writer.close()
        await writer.wait_closed()
    finally:
        await server.stop()


async def test_empty_text_no_response():
    settings = Settings(engine="mock", wyoming_port=0)
    engine = MockEngine(sample_rate=24000)
    server = WyomingServer(settings, engine)
    await server.start()
    port = server.port

    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        msg = json.dumps({"type": "synthesize", "data": {"text": ""}}) + "\n"
        writer.write(msg.encode())
        await writer.drain()
        writer.close()
        await writer.wait_closed()

        # Server must not crash — verify it still accepts a second connection
        reader2, writer2 = await asyncio.open_connection("127.0.0.1", port)
        writer2.close()
        await writer2.wait_closed()
    finally:
        await server.stop()


async def test_server_stop_releases_port():
    settings = Settings(engine="mock", wyoming_port=0)
    server = WyomingServer(settings, MockEngine())
    await server.start()
    port = server.port
    await server.stop()

    # Starting a new server on the same port must succeed
    settings2 = Settings(engine="mock", wyoming_port=port)
    server2 = WyomingServer(settings2, MockEngine())
    await server2.start()
    await server2.stop()
