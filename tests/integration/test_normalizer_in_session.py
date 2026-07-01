import asyncio
import json

from fastapi.testclient import TestClient

from src.auth.stub import StubAuthProvider
from src.core.config import Settings
from src.core.normalizer_factory import create_normalizer
from src.main import app
from src.tts.interface import ITTSEngine


class CapturingEngine(ITTSEngine):
    """Records the text passed to synthesize()."""

    def __init__(self) -> None:
        self._sample_rate = 24000
        self.received_texts: list[str] = []

    @property
    def sample_rate(self) -> int:
        return 24000

    async def synthesize(self, text: str):
        self.received_texts.append(text)
        await asyncio.sleep(0)
        yield b"\x00\x00" * 4800

    async def aclose(self) -> None:
        return None


def _setup(engine: ITTSEngine, normalizer_settings: dict | None = None) -> TestClient:
    overrides = {"engine": "mock", "auth_provider": "stub", "min_flush_chars": 5}
    if normalizer_settings:
        overrides.update(normalizer_settings)
    settings = Settings(**overrides)
    app.state.settings = settings
    app.state.engine = engine
    app.state.auth = StubAuthProvider()
    app.state.normalizer = create_normalizer(settings)
    return TestClient(app)


def _collect_session(client: TestClient, token_text: str):
    with client.websocket_connect("/ws") as ws:
        ws.send_text(json.dumps({"type": "auth", "token": "t"}))
        ws.receive_text()
        ws.send_text(json.dumps({"type": "token", "text": token_text}))
        ws.send_text(json.dumps({"type": "end"}))
        for _ in range(100):
            try:
                data = ws.receive()
            except Exception:
                break
            if data.get("type") == "websocket.send" and data.get("text"):
                if json.loads(data["text"])["type"] == "done":
                    break


def test_spanish_normalizer_applied_before_synthesis():
    engine = CapturingEngine()
    client = _setup(engine)
    _collect_session(client, "Tengo 25 años")
    assert any("veinticinco" in t for t in engine.received_texts), engine.received_texts
    assert all("25" not in t for t in engine.received_texts), engine.received_texts


def test_passthrough_normalizer_leaves_text_intact():
    engine = CapturingEngine()
    client = _setup(engine, normalizer_settings={"normalizer": "none"})
    _collect_session(client, "Hola.")
    # Passthrough means segments are passed verbatim (no transformation).
    assert any("Hola" in t for t in engine.received_texts), engine.received_texts


def test_session_survives_normalizer_failure():
    """If the normalizer raises, the session must complete and engine gets original text."""

    class CrashingNormalizer:
        async def normalize(self, text: str) -> str:
            raise RuntimeError("simulated normalizer crash")

    engine = CapturingEngine()
    settings = Settings(engine="mock", auth_provider="stub", min_flush_chars=5)
    app.state.settings = settings
    app.state.engine = engine
    app.state.auth = StubAuthProvider()
    app.state.normalizer = CrashingNormalizer()
    client = TestClient(app)

    _collect_session(client, "Hola mundo")
    assert len(engine.received_texts) > 0
