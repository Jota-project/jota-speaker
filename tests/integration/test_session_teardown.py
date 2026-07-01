import asyncio
import json
import time

from fastapi.testclient import TestClient

from src.auth.stub import StubAuthProvider
from src.core.normalizer_factory import create_normalizer
from src.core.config import Settings
from src.main import app
from src.tts.interface import ITTSEngine


class TrackingEngine(ITTSEngine):
    def __init__(self) -> None:
        self._sample_rate = 24000
        self.aclose_called = 0

    @property
    def sample_rate(self) -> int:
        return 24000

    async def synthesize(self, text: str):
        await asyncio.sleep(0)
        yield b"\x00\x00" * 4800

    async def aclose(self) -> None:
        self.aclose_called += 1


def _setup(engine: ITTSEngine, **kwargs) -> TestClient:
    defaults = dict(engine="mock", auth_provider="stub", min_flush_chars=5, session_timeout=0.2)
    defaults.update(kwargs)
    settings = Settings(**defaults)
    app.state.settings = settings
    app.state.engine = engine
    app.state.auth = StubAuthProvider()
    app.state.normalizer = create_normalizer(settings)
    return TestClient(app)


def test_session_timeout_calls_aclose_on_engine():
    engine = TrackingEngine()
    client = _setup(engine)
    with client.websocket_connect("/ws") as ws:
        ws.send_text(json.dumps({"type": "auth", "token": "t"}))
        ws.receive_text()  # auth_ok
        # Wait for server-side session_timeout to fire.
        deadline = time.time() + 3.0
        while time.time() < deadline:
            try:
                data = ws.receive()
                if data.get("type") == "websocket.send" and data.get("text"):
                    msg = json.loads(data["text"])
                    if msg["type"] == "error":
                        break
            except Exception:
                break
            time.sleep(0.05)
    assert engine.aclose_called == 1


def test_session_normal_end_calls_aclose():
    engine = TrackingEngine()
    client = _setup(engine, session_timeout=10.0)
    with client.websocket_connect("/ws") as ws:
        ws.send_text(json.dumps({"type": "auth", "token": "t"}))
        ws.receive_text()  # auth_ok
        ws.send_text(json.dumps({"type": "end"}))
        deadline = time.time() + 3.0
        while time.time() < deadline:
            try:
                data = ws.receive()
                if data.get("type") == "websocket.send" and data.get("text"):
                    if json.loads(data["text"])["type"] == "done":
                        break
            except Exception:
                break
            time.sleep(0.05)
    assert engine.aclose_called == 1
