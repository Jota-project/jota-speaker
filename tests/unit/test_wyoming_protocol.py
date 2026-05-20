import asyncio
import json

from src.core.config import Settings
from src.wyoming.protocol import read_event, write_event


def test_wyoming_defaults():
    s = Settings()
    assert s.wyoming_enabled is True
    assert s.wyoming_port == 20424


# ── read_event ────────────────────────────────────────────────────────────────

async def test_read_synthesize_no_payload():
    line = json.dumps({"type": "synthesize", "data": {"text": "Hello"}}) + "\n"
    reader = asyncio.StreamReader()
    reader.feed_data(line.encode())
    event_type, data, payload = await read_event(reader)
    assert event_type == "synthesize"
    assert data == {"text": "Hello"}
    assert payload == b""


async def test_read_event_with_binary_payload():
    binary = b"\x00\x01\x02\x03"
    header = {
        "type": "audio-chunk",
        "data": {"rate": 24000, "width": 2, "channels": 1},
        "payload_length": len(binary),
    }
    line = json.dumps(header) + "\n"
    reader = asyncio.StreamReader()
    reader.feed_data(line.encode() + binary)
    event_type, data, payload = await read_event(reader)
    assert event_type == "audio-chunk"
    assert payload == binary


async def test_read_event_eof_returns_empty_string():
    reader = asyncio.StreamReader()
    reader.feed_eof()
    event_type, data, payload = await read_event(reader)
    assert event_type == ""
    assert payload == b""


# ── write_event ───────────────────────────────────────────────────────────────

class _FakeWriter:
    def __init__(self):
        self.data = bytearray()

    def write(self, chunk: bytes) -> None:
        self.data.extend(chunk)

    async def drain(self) -> None:
        pass


async def test_write_event_no_payload_no_payload_length_key():
    w = _FakeWriter()
    await write_event(w, "audio-start", {"rate": 24000, "width": 2, "channels": 1})
    line = w.data.decode()
    assert line.endswith("\n")
    obj = json.loads(line)
    assert obj["type"] == "audio-start"
    assert obj["data"]["rate"] == 24000
    assert "payload_length" not in obj


async def test_write_event_with_payload_encodes_length_and_bytes():
    w = _FakeWriter()
    payload = b"\xAA\xBB" * 4
    await write_event(
        w, "audio-chunk", {"rate": 24000, "width": 2, "channels": 1}, payload=payload
    )
    newline_idx = w.data.index(ord("\n"))
    header = json.loads(w.data[:newline_idx].decode())
    body = bytes(w.data[newline_idx + 1 :])
    assert header["payload_length"] == len(payload)
    assert body == payload


async def test_write_event_stop_no_data_field():
    w = _FakeWriter()
    await write_event(w, "audio-stop", {"timestamp": 0})
    obj = json.loads(w.data.decode().strip())
    assert obj["type"] == "audio-stop"
    assert obj["data"]["timestamp"] == 0
