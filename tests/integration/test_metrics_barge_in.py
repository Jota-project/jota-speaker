"""Integration tests for barge-in latency metric."""
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


def _setup() -> TestClient:
    settings = Settings(engine="mock", auth_provider="stub", min_flush_chars=5)
    app.state.settings = settings
    app.state.engine_registry = EngineRegistry({"test": SlowFrameEngine()}, "test")
    app.state.auth = StubAuthProvider()
    app.state.normalizer = create_normalizer(settings)
    return TestClient(app)


def _count(name: str, **labels) -> float:
    from prometheus_client import REGISTRY, generate_latest

    output = generate_latest(REGISTRY).decode()
    label_str = ",".join(f'{k}="{v}"' for k, v in sorted(labels.items()))
    needle = f"{name}{{{label_str}}} "
    for line in output.splitlines():
        if line.startswith(needle):
            return float(line.rsplit(" ", 1)[-1])
    return 0.0


def _count_no_labels(name: str) -> float:
    from prometheus_client import REGISTRY, generate_latest

    output = generate_latest(REGISTRY).decode()
    needle = f"{name} "
    for line in output.splitlines():
        if line.startswith(needle):
            return float(line.rsplit(" ", 1)[-1])
    return 0.0


def test_barge_in_latency_histogram_observed():
    metrics.BARGE_IN_LATENCY_HISTOGRAM.clear()
    before_interrupts = _count_no_labels("jota_speaker_interrupts_total")
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
    assert _count_no_labels("jota_speaker_interrupts_total") == before_interrupts + 1.0
    assert _count("jota_speaker_barge_in_latency_ms_count", session_type="ws") == 1.0


def test_each_interrupt_increments_counter():
    # INTERRUPTS_TOTAL has no labels, so .clear() isn't safe here (would
    # reset global state shared with other tests) — use a before/after delta.
    before = _count_no_labels("jota_speaker_interrupts_total")
    client = _setup()
    with client.websocket_connect("/ws") as ws:
        ws.send_text(json.dumps({"type": "auth", "token": "t"}))
        ws.receive_text()
        for i in range(3):
            ws.send_text(json.dumps({"type": "token", "text": f"Utterance {i}."}))
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
    assert _count_no_labels("jota_speaker_interrupts_total") == before + 3.0
