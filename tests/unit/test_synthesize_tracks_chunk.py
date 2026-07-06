"""Test _synthesize_segment sets _current_chunk_id during synthesis and clears it.

Runs the segment method against a fake ws (records send_text/send_bytes calls)
and a fake engine (synchronously yields frames).
"""

import asyncio
from unittest.mock import MagicMock

from src.core.engine_registry import EngineRegistry
from src.server.session import SpeakerSession


class FakeWS:
    """Minimal WS that records sends and lets us drive receive_text."""

    def __init__(self) -> None:
        from fastapi.websockets import WebSocketState

        self.client_state = WebSocketState.CONNECTED
        self.sent_text: list[str] = []
        self.sent_bytes: list[bytes] = []
        self._connected = True

    async def send_text(self, data: str) -> None:
        if not self._connected:
            raise RuntimeError("WS closed")
        self.sent_text.append(data)

    async def send_bytes(self, data: bytes) -> None:
        if not self._connected:
            raise RuntimeError("WS closed")
        self.sent_bytes.append(data)


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


def _make_session(frames: list[bytes]) -> tuple[SpeakerSession, FakeWS, FakeEngine]:
    ws = FakeWS()
    engine = FakeEngine(frames)
    auth = MagicMock()
    normalizer = FakeNormalizer()
    session = SpeakerSession(
        ws=ws,
        registry=EngineRegistry({"mock": engine}, "mock"),
        auth=auth,
        normalizer=normalizer,
        min_flush_chars=80,
        queue_maxsize=10,
        session_timeout=10.0,
    )
    return session, ws, engine


def _parse_sent_text(ws: FakeWS) -> list[dict]:
    import json

    return [json.loads(t) for t in ws.sent_text]


def test_synthesize_segment_sets_and_clears_current_chunk_id():
    session, ws, _ = _make_session([b"\x00\x00" * 100])
    asyncio.run(session._synthesize_segment("hello"))
    assert session._current_chunk_id is None, (
        f"expected None after normal completion, got {session._current_chunk_id}"
    )
    # Verify both audio_start and audio_end were emitted
    msgs = _parse_sent_text(ws)
    types = [m["type"] for m in msgs]
    assert "audio_start" in types
    assert "audio_end" in types
    # Both should refer to chunk 0
    audio_end = next(m for m in msgs if m["type"] == "audio_end")
    assert audio_end["chunk_id"] == 0


def test_synthesize_segment_sets_current_chunk_id_to_zero_during_call():
    """Inspect _current_chunk_id while the engine is producing frames."""
    session, _ws, _engine = _make_session([b"\x00\x00" * 100, b"\x00\x00" * 100])

    observed: list[int | None] = []

    # Patch _send to observe state right before audio_start emit
    original_send = session._send

    async def capturing_send(msg):
        if msg.__class__.__name__ == "AudioStartMessage":
            observed.append(session._current_chunk_id)
        await original_send(msg)

    session._send = capturing_send  # type: ignore[method-assign]

    asyncio.run(session._synthesize_segment("hello"))
    assert observed == [0], f"expected chunk_id 0 during synthesis, got {observed}"


def test_synthesize_segment_clears_current_chunk_id_on_normal_completion():
    """Run twice — second invocation must reset chunk_id to 1, then back to None."""
    session, _ws, _engine = _make_session([b"\x00\x00" * 50])
    asyncio.run(session._synthesize_segment("first"))
    assert session._current_chunk_id is None
    # Capture during second call
    observed: list[int | None] = []

    original_send = session._send

    async def capturing_send(msg):
        if msg.__class__.__name__ == "AudioStartMessage":
            observed.append(session._current_chunk_id)
        await original_send(msg)

    session._send = capturing_send  # type: ignore[method-assign]
    asyncio.run(session._synthesize_segment("second"))
    assert observed == [1], f"expected chunk_id 1 on second run, got {observed}"
    assert session._current_chunk_id is None