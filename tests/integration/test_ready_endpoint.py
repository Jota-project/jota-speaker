"""Integration tests for the /ready endpoint (issue #37)."""
from fastapi.testclient import TestClient

from src.auth.stub import StubAuthProvider
from src.core.config import Settings
from src.core.engine_registry import EngineRegistry
from src.core.normalizer_factory import create_normalizer
from src.main import app
from src.tts.mock_engine import MockEngine
from src.tts.interface import ITTSEngine


def _setup(engines: dict[str, ITTSEngine], default: str) -> TestClient:
    settings = Settings(engine="mock", auth_provider="stub", min_flush_chars=5)
    app.state.settings = settings
    app.state.engine_registry = EngineRegistry(engines, default)
    app.state.auth = StubAuthProvider()
    app.state.normalizer = create_normalizer(settings)
    return TestClient(app)


def test_ready_returns_200_when_all_engines_ready():
    client = _setup({"test": MockEngine()}, "test")
    response = client.get("/ready")
    assert response.status_code == 200
    body = response.json()
    assert body == {"status": "ready", "engines": {"test": True}}


class NotReadyEngine(ITTSEngine):
    @property
    def sample_rate(self) -> int:
        return 24000

    @property
    def is_ready(self) -> bool:
        return False

    async def synthesize(self, text: str, voice: str | None = None, speed: float | None = None):
        raise RuntimeError("not ready")
        yield b""  # pragma: no cover

    async def aclose(self) -> None:
        return None


def test_ready_returns_503_when_an_engine_is_not_ready():
    client = _setup({"a": MockEngine(), "b": NotReadyEngine()}, "a")
    response = client.get("/ready")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "not_ready"
    assert body["engines"] == {"a": True, "b": False}


def test_health_stays_200_even_when_not_ready():
    """Liveness must not flap just because a readiness check fails."""
    client = _setup({"a": NotReadyEngine()}, "a")
    assert client.get("/health").status_code == 200
    assert client.get("/ready").status_code == 503
