"""Integration tests for sessions gauge and chunks counter."""
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
        await asyncio.sleep(0.01)
        yield b"\x00\x00" * 100

    async def aclose(self) -> None:
        return None


def _setup(engine: ITTSEngine | None = None) -> TestClient:
    settings = Settings(engine="mock", auth_provider="stub", min_flush_chars=5)
    app.state.settings = settings
    app.state.engine_registry = EngineRegistry({"test": engine or FrameEngine()}, "test")
    app.state.auth = StubAuthProvider()
    app.state.normalizer = create_normalizer(settings)
    return TestClient(app)


def _gauge(name: str, **labels) -> float:
    from prometheus_client import REGISTRY, generate_latest

    output = generate_latest(REGISTRY).decode()
    needle = f'{name}{{{",".join(f"{k}=\"{v}\"" for k, v in sorted(labels.items()))}}} '
    for line in output.splitlines():
        if line.startswith(needle):
            return float(line.rsplit(" ", 1)[-1])
    return 0.0


def _counter(name: str, **labels) -> float:
    return _gauge(name, **labels)


def test_sessions_active_gauge_increments_and_decrements():
    metrics.SESSIONS_ACTIVE.clear()
    before_total = _counter("jota_speaker_sessions_total", session_type="ws", result="ok")
    client = _setup()
    with client.websocket_connect("/ws") as ws:
        ws.send_text(json.dumps({"type": "auth", "token": "t"}))
        ws.receive_text()
        time.sleep(0.05)
        assert _gauge("jota_speaker_sessions_active", session_type="ws") == 1.0
    time.sleep(0.05)
    assert _gauge("jota_speaker_sessions_active", session_type="ws") == 0.0
    after_total = _counter("jota_speaker_sessions_total", session_type="ws", result="ok")
    assert after_total >= before_total + 1.0


def test_chunk_finished_ok_on_normal_completion():
    before = _counter("jota_speaker_chunks_total", result="ok")
    client = _setup()
    with client.websocket_connect("/ws") as ws:
        ws.send_text(json.dumps({"type": "auth", "token": "t"}))
        ws.receive_text()
        ws.send_text(json.dumps({"type": "token", "text": "Hello."}))
        deadline = time.time() + 2.0
        while time.time() < deadline:
            d = ws.receive()
            if d.get("type") == "websocket.send" and d.get("text"):
                m = json.loads(d["text"])
                if m["type"] == "audio_end":
                    break
    assert _counter("jota_speaker_chunks_total", result="ok") >= before + 1.0


class SlowFrameEngine(ITTSEngine):
    @property
    def sample_rate(self) -> int:
        return 24000

    async def synthesize(self, text: str, voice: str | None = None, speed: float | None = None):
        for _ in range(10):
            await asyncio.sleep(0.02)
            yield b"\x00\x00" * 100

    async def aclose(self) -> None:
        return None


def test_chunk_finished_aborted_on_interrupt():
    before = _counter("jota_speaker_chunks_total", result="aborted")
    client = _setup(SlowFrameEngine())
    with client.websocket_connect("/ws") as ws:
        ws.send_text(json.dumps({"type": "auth", "token": "t"}))
        ws.receive_text()
        ws.send_text(json.dumps({"type": "token", "text": "Hello."}))
        deadline = time.time() + 2.0
        while time.time() < deadline:
            d = ws.receive()
            if d.get("type") == "websocket.send" and d.get("text"):
                m = json.loads(d["text"])
                if m["type"] == "audio_start":
                    break
        ws.send_text(json.dumps({"type": "interrupt"}))
        deadline = time.time() + 2.0
        while time.time() < deadline:
            d = ws.receive()
            if d.get("type") == "websocket.send" and d.get("text"):
                m = json.loads(d["text"])
                if m["type"] == "interrupted":
                    break
    assert _counter("jota_speaker_chunks_total", result="aborted") >= before + 1.0
