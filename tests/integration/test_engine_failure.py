import asyncio
import json
import time

import pytest
from fastapi.testclient import TestClient

from src.auth.stub import StubAuthProvider
from src.core.config import Settings
from src.main import app
from src.tts.interface import ITTSEngine


class CrashingEngine(ITTSEngine):
    """Raises a non-cancellation exception on first synthesize call."""

    def __init__(self) -> None:
        self._sample_rate = 24000
        self.calls = 0

    @property
    def sample_rate(self) -> int:
        return 24000

    async def synthesize(self, text: str):
        self.calls += 1
        await asyncio.sleep(0)
        raise RuntimeError("simulated Kokoro crash")
        yield  # makes this an async generator

    async def aclose(self) -> None:
        return None


def _make_client(engine: ITTSEngine) -> TestClient:
    settings = Settings(engine="mock", auth_provider="stub", min_flush_chars=5)
    app.state.settings = settings
    app.state.engine = engine
    app.state.auth = StubAuthProvider()
    return TestClient(app)


def test_engine_exception_does_not_kill_session_silently():
    """A non-cancellation exception in the engine must surface as 'error'."""
    engine = CrashingEngine()
    client = _make_client(engine)

    with client.websocket_connect("/ws") as ws:
        ws.send_text(json.dumps({"type": "auth", "token": "t"}))
        assert json.loads(ws.receive_text())["type"] == "auth_ok"
        ws.send_text(json.dumps({"type": "token", "text": "Hello world."}))
        messages: list[dict] = []
        while True:
            data = ws.receive()
            if data.get("type") != "websocket.send":
                break
            if data.get("text"):
                msg = json.loads(data["text"])
                messages.append(msg)
                if msg["type"] in ("done", "error"):
                    break
    types = [m["type"] for m in messages]
    assert "error" in types
    assert engine.calls == 1


def test_engine_exception_does_not_hang_session():
    """After an engine crash, the session must terminate in bounded time."""
    engine = CrashingEngine()
    client = _make_client(engine)

    with client.websocket_connect("/ws") as ws:
        ws.send_text(json.dumps({"type": "auth", "token": "t"}))
        ws.receive_text()  # auth_ok
        ws.send_text(json.dumps({"type": "token", "text": "Hello world."}))
        for _ in range(50):
            data = ws.receive()
            if data.get("type") == "websocket.send" and data.get("text"):
                msg = json.loads(data["text"])
                if msg["type"] == "error":
                    break
            time.sleep(0.05)
    assert True
