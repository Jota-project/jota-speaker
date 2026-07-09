"""Reproduces issue #47: Starlette raises RuntimeError('Cannot call "send"
once a close message has been sent.') instead of WebSocketDisconnect when the
WebSocket transitions to closed mid-synthesis. _synthesize_segment only
caught WebSocketDisconnect, so the RuntimeError escaped to _tts_worker's
generic except, which then tried to report the error via _send_error —
hitting the exact same RuntimeError a second time, this time unhandled,
killing the worker task permanently (session goes mute for its remaining
lifetime).
"""

import asyncio
from unittest.mock import MagicMock

from fastapi.websockets import WebSocketState

from src.core.engine_registry import EngineRegistry
from src.server.session import SpeakerSession


class ClosingWS:
    """Simulates the real Starlette close race: the first send_bytes call
    hits the close race and raises RuntimeError; every send afterwards
    (including send_text) raises the same RuntimeError, because Starlette's
    application_state has already flipped to closed."""

    def __init__(self) -> None:
        self.client_state = WebSocketState.CONNECTED
        self.sent_text: list[str] = []
        self.sent_bytes: list[bytes] = []
        self._closed = False

    async def send_text(self, data: str) -> None:
        if self._closed:
            raise RuntimeError('Cannot call "send" once a close message has been sent.')
        self.sent_text.append(data)

    async def send_bytes(self, data: bytes) -> None:
        if self._closed:
            raise RuntimeError('Cannot call "send" once a close message has been sent.')
        self._closed = True
        raise RuntimeError('Cannot call "send" once a close message has been sent.')


class FakeEngine:
    def __init__(self, frames: list[bytes]) -> None:
        self._frames = frames
        self._sample_rate = 24000

    @property
    def sample_rate(self) -> int:
        return self._sample_rate

    async def synthesize(self, text: str, voice: str | None = None):
        for f in self._frames:
            yield f

    async def aclose(self) -> None:
        return None


class FakeNormalizer:
    async def normalize(self, text: str) -> str:
        return text


def _make_session(frames: list[bytes]) -> tuple[SpeakerSession, ClosingWS]:
    ws = ClosingWS()
    engine = FakeEngine(frames)
    session = SpeakerSession(
        ws=ws,
        registry=EngineRegistry({"mock": engine}, "mock"),
        auth=MagicMock(),
        normalizer=FakeNormalizer(),
        min_flush_chars=80,
        queue_maxsize=10,
        session_timeout=10.0,
    )
    return session, ws


def test_synthesize_segment_survives_send_bytes_close_race():
    """_synthesize_segment must not propagate the close-race RuntimeError —
    same contract as the existing WebSocketDisconnect handling."""
    session, _ws = _make_session([b"\x00\x00" * 100, b"\x00\x00" * 100])
    asyncio.run(session._synthesize_segment("hello"))
    assert session._current_chunk_id is None


def test_tts_worker_does_not_crash_on_close_race():
    """The worker task must complete (not raise) even though both the audio
    frame send AND the follow-up error-reporting send hit the close race."""
    from src.server.session import _SENTINEL

    session, _ws = _make_session([b"\x00\x00" * 100])
    session._queue.put_nowait("hello")
    session._queue.put_nowait(_SENTINEL)

    asyncio.run(session._tts_worker())
