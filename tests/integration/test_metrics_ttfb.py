"""Integration tests for TTFB metric (jota_speaker_ttfb_ms)."""
import asyncio
import json
import time

from fastapi.testclient import TestClient

from src.auth.stub import StubAuthProvider
from src.core.config import Settings
from src.core.engine_registry import EngineRegistry
from src.core.normalizer_factory import create_normalizer
from src.main import app
from src.observability import metrics
from src.tts.interface import ITTSEngine


class FrameEngine(ITTSEngine):
    @property
    def sample_rate(self) -> int:
        return 24000

    async def synthesize(self, text: str, voice: str | None = None, speed: float | None = None):
        await asyncio.sleep(0.01)  # tiny delay so TTFB > 0
        yield b"\x00\x00" * 100

    async def aclose(self) -> None:
        return None


def _setup(engine: ITTSEngine) -> TestClient:
    settings = Settings(engine="mock", auth_provider="stub", min_flush_chars=5)
    app.state.settings = settings
    app.state.engine_registry = EngineRegistry({"test": engine}, "test")
    app.state.auth = StubAuthProvider()
    app.state.normalizer = create_normalizer(settings)
    return TestClient(app)


def _histogram_count(name: str, **labels) -> float:
    from prometheus_client import REGISTRY, generate_latest

    output = generate_latest(REGISTRY).decode()
    # prometheus_client serializes labels in sorted-key order, not call order.
    needle = f'{name}{{{",".join(f"{k}=\"{v}\"" for k, v in sorted(labels.items()))}}} '
    for line in output.splitlines():
        if line.startswith(needle):
            return float(line.rsplit(" ", 1)[-1])
    return 0.0


def test_ttfb_observed_after_token_stream():
    metrics.TTFB_HISTOGRAM.clear()
    client = _setup(FrameEngine())
    with client.websocket_connect("/ws") as ws:
        ws.send_text(json.dumps({"type": "auth", "token": "t"}))
        ws.receive_text()  # auth_ok
        ws.send_text(json.dumps({"type": "token", "text": "Hello."}))
        deadline = time.time() + 2.0
        while time.time() < deadline:
            d = ws.receive()
            if d.get("type") == "websocket.send" and d.get("text"):
                m = json.loads(d["text"])
                if m["type"] == "audio_end":
                    break
    count = _histogram_count("jota_speaker_ttfb_ms_count", session_type="ws", engine="frameengine")
    assert count >= 1.0


def test_ttfb_only_observed_once_per_session():
    """Multiple audio frames should produce exactly 1 TTFB observation per session."""
    metrics.TTFB_HISTOGRAM.clear()
    client = _setup(FrameEngine())
    with client.websocket_connect("/ws") as ws:
        ws.send_text(json.dumps({"type": "auth", "token": "t"}))
        ws.receive_text()
        ws.send_text(json.dumps({"type": "token", "text": "First."}))
        deadline = time.time() + 2.0
        while time.time() < deadline:
            d = ws.receive()
            if d.get("type") == "websocket.send" and d.get("text"):
                m = json.loads(d["text"])
                if m["type"] == "audio_start":
                    break
        ws.send_text(json.dumps({"type": "token", "text": "Second."}))
        deadline = time.time() + 2.0
        while time.time() < deadline:
            d = ws.receive()
            if d.get("type") == "websocket.send" and d.get("text"):
                m = json.loads(d["text"])
                if m["type"] == "audio_end":
                    break
    count = _histogram_count("jota_speaker_ttfb_ms_count", session_type="ws", engine="frameengine")
    assert count == 1.0, f"expected exactly 1 TTFB observation, got {count}"
