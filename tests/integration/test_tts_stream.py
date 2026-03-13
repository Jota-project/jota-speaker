import json

import pytest
from fastapi.testclient import TestClient

from src.auth.stub import StubAuthProvider
from src.core.config import Settings
from src.core.engine_factory import create_engine
from src.main import app


def _make_client(settings: Settings | None = None) -> TestClient:
    if settings is None:
        settings = Settings(engine="mock", auth_provider="stub", min_flush_chars=5)
    app.state.settings = settings
    app.state.engine = create_engine(settings)
    app.state.auth = StubAuthProvider()
    return TestClient(app)


def _auth(ws) -> None:
    ws.send_text(json.dumps({"type": "auth", "token": "test"}))
    msg = json.loads(ws.receive_text())
    assert msg["type"] == "auth_ok"


def _collect(ws) -> tuple[list[dict], list[bytes]]:
    """Drain the WebSocket until done or disconnect, separating text and binary."""
    text_msgs: list[dict] = []
    binary_frames: list[bytes] = []
    while True:
        try:
            # Starlette TestClient: server→client messages arrive as websocket.send
            data = ws.receive()
        except Exception:
            break
        msg_type = data.get("type", "")
        if msg_type == "websocket.send":
            text = data.get("text")
            raw_bytes = data.get("bytes")
            if text:
                msg = json.loads(text)
                text_msgs.append(msg)
                if msg.get("type") == "done":
                    break
            elif raw_bytes:
                binary_frames.append(raw_bytes)
        elif msg_type in ("websocket.disconnect", "websocket.close"):
            break
    return text_msgs, binary_frames


# 1. Happy path: auth → token → end → done
def test_happy_path():
    client = _make_client()
    with client.websocket_connect("/ws") as ws:
        _auth(ws)
        ws.send_text(json.dumps({"type": "token", "text": "Hello."}))
        ws.send_text(json.dumps({"type": "end"}))
        messages, _ = _collect(ws)
    types = [m["type"] for m in messages]
    assert "audio_start" in types
    assert "audio_end" in types
    assert "done" in types


# 2. Auth rejected closes connection
def test_auth_rejected():
    from src.auth.interface import IAuthProvider

    class RejectAll(IAuthProvider):
        async def validate(self, token: str) -> bool:
            return False

    settings = Settings(engine="mock", auth_provider="stub")
    app.state.settings = settings
    app.state.engine = create_engine(settings)
    app.state.auth = RejectAll()

    client = TestClient(app)
    with client.websocket_connect("/ws") as ws:
        ws.send_text(json.dumps({"type": "auth", "token": "bad"}))
        msg = json.loads(ws.receive_text())
        assert msg["type"] == "auth_error"


# 3. First message not auth → auth_error
def test_first_message_not_auth():
    client = _make_client()
    with client.websocket_connect("/ws") as ws:
        ws.send_text(json.dumps({"type": "token", "text": "hi"}))
        msg = json.loads(ws.receive_text())
        assert msg["type"] == "auth_error"


# 4. flush message forces synthesis
def test_flush_forces_synthesis():
    client = _make_client(Settings(engine="mock", auth_provider="stub", min_flush_chars=200))
    with client.websocket_connect("/ws") as ws:
        _auth(ws)
        ws.send_text(json.dumps({"type": "token", "text": "Short"}))
        ws.send_text(json.dumps({"type": "flush"}))
        ws.send_text(json.dumps({"type": "end"}))
        messages, _ = _collect(ws)
    types = [m["type"] for m in messages]
    assert "audio_start" in types


# 5. end with no tokens → done without audio
def test_end_no_tokens():
    client = _make_client()
    with client.websocket_connect("/ws") as ws:
        _auth(ws)
        ws.send_text(json.dumps({"type": "end"}))
        messages, _ = _collect(ws)
    types = [m["type"] for m in messages]
    assert "done" in types
    assert "audio_start" not in types


# 6. Multiple segments are ordered by chunk_id
def test_multiple_segments_ordered():
    client = _make_client(Settings(engine="mock", auth_provider="stub", min_flush_chars=5))
    with client.websocket_connect("/ws") as ws:
        _auth(ws)
        ws.send_text(json.dumps({"type": "token", "text": "Hi. Bye."}))
        ws.send_text(json.dumps({"type": "end"}))
        messages, _ = _collect(ws)
    starts = [m for m in messages if m["type"] == "audio_start"]
    ids = [m["chunk_id"] for m in starts]
    assert ids == sorted(ids)


# 7. Binary PCM16 frames are even-length (valid 16-bit samples)
def test_pcm16_frames_even_length():
    client = _make_client()
    with client.websocket_connect("/ws") as ws:
        _auth(ws)
        ws.send_text(json.dumps({"type": "token", "text": "Hello world."}))
        ws.send_text(json.dumps({"type": "end"}))
        _, frames = _collect(ws)
    assert len(frames) > 0
    for frame in frames:
        assert len(frame) % 2 == 0


# 8. Session timeout closes connection with error
def test_session_timeout():
    settings = Settings(engine="mock", auth_provider="stub", min_flush_chars=5, session_timeout=0.05)
    client = _make_client(settings)
    with client.websocket_connect("/ws") as ws:
        _auth(ws)
        # Send nothing — wait for server-side timeout
        data = ws.receive()
        assert data["type"] == "websocket.send"
        msg = json.loads(data["text"])
        assert msg["type"] == "error"
        assert msg["code"] == "session_timeout"


# 9. Full queue sends error and aborts session
def test_queue_full():
    # queue_maxsize=1: first segment fills queue; second triggers QueueFull
    settings = Settings(engine="mock", auth_provider="stub", min_flush_chars=5, queue_maxsize=1)
    client = _make_client(settings)
    with client.websocket_connect("/ws") as ws:
        _auth(ws)
        # "Hi. Bye. Hello." → 3 segments with min_flush_chars=5; 2nd put will overflow
        ws.send_text(json.dumps({"type": "token", "text": "Hi. Bye. Hello."}))
        messages, _ = _collect(ws)
    assert any(m["type"] == "error" and m["code"] == "queue_full" for m in messages)
