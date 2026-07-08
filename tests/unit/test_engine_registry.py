import pytest

from src.core.engine_registry import EngineRegistry
from src.tts.mock_engine import MockEngine


def test_resolve_returns_requested_when_loaded():
    a, b = MockEngine(), MockEngine()
    registry = EngineRegistry({"a": a, "b": b}, "a")
    model_id, engine = registry.resolve("b")
    assert model_id == "b"
    assert engine is b


def test_resolve_falls_back_to_default_when_not_loaded():
    a = MockEngine()
    registry = EngineRegistry({"a": a}, "a")
    model_id, engine = registry.resolve("nonexistent")
    assert model_id == "a"
    assert engine is a


def test_resolve_falls_back_to_default_when_none_requested():
    a = MockEngine()
    registry = EngineRegistry({"a": a}, "a")
    model_id, engine = registry.resolve(None)
    assert model_id == "a"
    assert engine is a


@pytest.mark.asyncio
async def test_aclose_closes_all_engines():
    closed = []

    class TrackedEngine(MockEngine):
        async def aclose(self) -> None:
            closed.append(self)

    a, b = TrackedEngine(), TrackedEngine()
    registry = EngineRegistry({"a": a, "b": b}, "a")
    await registry.aclose()
    assert set(closed) == {a, b}


def test_readiness_reports_is_ready_per_engine():
    class NotReadyEngine(MockEngine):
        @property
        def is_ready(self) -> bool:
            return False

    a, b = MockEngine(), NotReadyEngine()
    registry = EngineRegistry({"a": a, "b": b}, "a")
    assert registry.readiness() == {"a": True, "b": False}
