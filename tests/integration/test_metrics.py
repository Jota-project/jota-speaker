"""Integration tests for the /metrics endpoint."""
import asyncio
import json
import time

from fastapi.testclient import TestClient

from src.auth.stub import StubAuthProvider
from src.core.config import Settings
from src.core.engine_registry import EngineRegistry
from src.core.normalizer_factory import create_normalizer
from src.main import app
from src.tts.interface import ITTSEngine


def test_metrics_endpoint_returns_prometheus_format():
    settings = Settings(engine="mock", auth_provider="stub", min_flush_chars=5)
    app.state.settings = settings
    app.state.auth = StubAuthProvider()
    app.state.normalizer = create_normalizer(settings)

    client = TestClient(app)
    response = client.get("/metrics")
    assert response.status_code == 200
    assert "text/plain" in response.headers["content-type"]
    body = response.text
    assert "# HELP" in body
    assert "# TYPE" in body
    for name in (
        "jota_speaker_ttfb_ms",
        "jota_speaker_barge_in_latency_ms",
        "jota_speaker_sessions_active",
        "jota_speaker_errors_total",
    ):
        assert name in body, f"missing metric {name}"


class FrameEngine(ITTSEngine):
    @property
    def sample_rate(self) -> int:
        return 24000

    async def synthesize(self, text: str, voice: str | None = None, speed: float | None = None):
        await asyncio.sleep(0.01)
        yield b"\x00\x00" * 100

    async def aclose(self) -> None:
        return None


def test_metrics_endpoint_does_not_break_session():
    """A /metrics scrape concurrent with a session must not crash either."""
    settings = Settings(engine="mock", auth_provider="stub", min_flush_chars=5)
    app.state.settings = settings
    app.state.engine_registry = EngineRegistry({"test": FrameEngine()}, "test")
    app.state.auth = StubAuthProvider()
    app.state.normalizer = create_normalizer(settings)

    client = TestClient(app)
    with client.websocket_connect("/ws") as ws:
        ws.send_text(json.dumps({"type": "auth", "token": "t"}))
        ws.receive_text()
        ws.send_text(json.dumps({"type": "token", "text": "Hello."}))
        for _ in range(3):
            r = client.get("/metrics")
            assert r.status_code == 200
            time.sleep(0.01)
        deadline = time.time() + 2.0
        while time.time() < deadline:
            d = ws.receive()
            if d.get("type") == "websocket.send" and d.get("text"):
                m = json.loads(d["text"])
                if m["type"] == "audio_end":
                    break
