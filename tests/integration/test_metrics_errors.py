"""Integration tests for error counter (jota_speaker_errors_total)."""
import json
import time

from fastapi.testclient import TestClient

from src.auth.stub import StubAuthProvider
from src.core.config import Settings
from src.core.engine_registry import EngineRegistry
from src.core.normalizer_factory import create_normalizer
from src.main import app
from src.tts.interface import ITTSEngine


class FailingEngine(ITTSEngine):
    @property
    def sample_rate(self) -> int:
        return 24000

    async def synthesize(self, text: str, voice: str | None = None, speed: float | None = None):
        raise RuntimeError("boom")
        yield  # unreachable, makes it a generator

    async def aclose(self) -> None:
        return None


def _setup() -> TestClient:
    settings = Settings(engine="mock", auth_provider="stub", min_flush_chars=5)
    app.state.settings = settings
    app.state.engine_registry = EngineRegistry({"test": FailingEngine()}, "test")
    app.state.auth = StubAuthProvider()
    app.state.normalizer = create_normalizer(settings)
    return TestClient(app)


def _counter(name: str, **labels) -> float:
    from prometheus_client import REGISTRY, generate_latest

    output = generate_latest(REGISTRY).decode()
    label_str = ",".join(f'{k}="{v}"' for k, v in sorted(labels.items()))
    needle = f"{name}{{{label_str}}} "
    for line in output.splitlines():
        if line.startswith(needle):
            return float(line.rsplit(" ", 1)[-1])
    return 0.0


def test_synthesis_error_increments_counter():
    before = _counter("jota_speaker_errors_total", code="synthesis_error")
    client = _setup()
    with client.websocket_connect("/ws") as ws:
        ws.send_text(json.dumps({"type": "auth", "token": "t"}))
        ws.receive_text()
        ws.send_text(json.dumps({"type": "token", "text": "Hi."}))
        deadline = time.time() + 2.0
        while time.time() < deadline:
            try:
                d = ws.receive()
            except Exception:
                break
            if d.get("type") == "websocket.send" and d.get("text"):
                m = json.loads(d["text"])
                if m["type"] == "error":
                    break
    assert _counter("jota_speaker_errors_total", code="synthesis_error") == before + 1.0
