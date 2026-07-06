"""Structural test: SpeakerSession has barge-in state attributes after __init__.

This test verifies the attributes are present and properly typed. It runs
without a real WebSocket — we substitute a mock WS object so we can call __init__.
"""

from unittest.mock import MagicMock

from src.core.engine_registry import EngineRegistry
from src.server.session import SpeakerSession


def _make_session() -> SpeakerSession:
    ws = MagicMock()

    async def _aclose() -> None:
        return None

    engine = MagicMock()
    engine.aclose = _aclose

    async def _validate(_t: str) -> bool:
        return True

    auth = MagicMock()
    auth.validate = _validate

    async def _normalize(t: str) -> str:
        return t

    normalizer = MagicMock()
    normalizer.normalize = _normalize
    return SpeakerSession(
        ws=ws,
        registry=EngineRegistry({"mock": engine}, "mock"),
        auth=auth,
        normalizer=normalizer,
        min_flush_chars=80,
        queue_maxsize=10,
        session_timeout=10.0,
    )


def test_session_has_current_chunk_id_attribute():
    session = _make_session()
    assert hasattr(session, "_current_chunk_id"), "_current_chunk_id attribute missing"
    assert session._current_chunk_id is None


def test_session_has_interrupt_lock_attribute():
    session = _make_session()
    assert hasattr(session, "_interrupt_lock"), "_interrupt_lock attribute missing"
    assert session._interrupt_lock is False