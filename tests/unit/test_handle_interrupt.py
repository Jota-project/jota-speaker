"""Unit tests for SpeakerSession._handle_interrupt behavior.

These exercise the interrupt handler against a FakeWS + FakeEngine so we don't
need the FastAPI WebSocket plumbing. The handler is invoked directly (not via
the receive loop) to isolate its behavior.
"""

import asyncio
from unittest.mock import MagicMock

from fastapi.websockets import WebSocketState

from src.core.engine_registry import EngineRegistry
from src.server.session import SpeakerSession


class FakeWS:
    def __init__(self) -> None:
        self.client_state = WebSocketState.CONNECTED
        self.sent_text: list[str] = []
        self.sent_bytes: list[bytes] = []

    async def send_text(self, data: str) -> None:
        self.sent_text.append(data)

    async def send_bytes(self, data: bytes) -> None:
        self.sent_bytes.append(data)


class SlowEngine:
    """Yields frames slowly so we can interrupt mid-synthesis."""

    def __init__(self, frames: int = 5) -> None:
        self._frames = frames
        self._sample_rate = 24000

    @property
    def sample_rate(self) -> int:
        return self._sample_rate

    async def synthesize(self, text: str, voice: str | None = None):
        for _ in range(self._frames):
            await asyncio.sleep(0.02)
            yield b"\x00\x00" * 100

    async def aclose(self) -> None:
        return None


class FakeNorm:
    async def normalize(self, t: str) -> str:
        return t


def _make_session(min_flush_chars: int = 5) -> tuple[SpeakerSession, FakeWS, SlowEngine]:
    ws = FakeWS()
    engine = SlowEngine()
    session = SpeakerSession(
        ws=ws,
        registry=EngineRegistry({"mock": engine}, "mock"),
        auth=MagicMock(),
        normalizer=FakeNorm(),
        min_flush_chars=min_flush_chars,
        queue_maxsize=20,
        session_timeout=10.0,
    )
    return session, ws, engine


def _parse_sent(ws: FakeWS) -> list[dict]:
    import json

    return [json.loads(t) for t in ws.sent_text]


def test_handle_interrupt_method_exists():
    session, _ws, _engine = _make_session()
    assert hasattr(session, "_handle_interrupt"), "_handle_interrupt missing"
    assert asyncio.iscoroutinefunction(session._handle_interrupt)


def test_handle_interrupt_with_no_inflight_chunk_sends_interrupted_zero():
    """No chunk in flight → only interrupted {chunk_id: 0}, no chunk_aborted."""
    session, ws, _engine = _make_session()
    asyncio.run(session._handle_interrupt())
    msgs = _parse_sent(ws)
    types = [m["type"] for m in msgs]
    assert "interrupted" in types
    assert "chunk_aborted" not in types, f"unexpected chunk_aborted: {msgs}"
    interrupted = next(m for m in msgs if m["type"] == "interrupted")
    assert interrupted["chunk_id"] == 0


def test_handle_interrupt_drains_queue_and_resets_accumulator():
    """Pending segments in queue should be discarded; accumulator flushed."""
    session, _ws, _engine = _make_session()
    # Pretend tokens arrived but accumulator hasn't flushed yet
    session._accumulator.add("hello world")  # no flush yet at min_flush_chars=5
    # Inject something into the queue (simulating a flushed-but-not-yet-synthesized segment)
    session._queue.put_nowait("pending-1")
    session._queue.put_nowait("pending-2")
    asyncio.run(session._handle_interrupt())
    assert session._queue.empty(), f"queue should be drained, has {session._queue.qsize()} items"
    # Accumulator should have been flushed (state reset)
    state_after = session._accumulator.state() if hasattr(session._accumulator, "state") else None
    # The accumulator's buffer should be empty
    assert session._accumulator._buffer == "" or state_after is None


async def test_handle_interrupt_with_inflight_chunk_sends_aborted_then_interrupted():
    """Mid-synthesis interrupt: chunk_aborted(N) + interrupted(N) in order."""
    session, ws, engine = _make_session()

    # Inject a segment into the queue so the worker has work to do
    session._queue.put_nowait("hello world.")
    # Start a tts_worker that will block mid-synthesis (SlowEngine yields 5 frames)
    session._tts_task = asyncio.create_task(session._tts_worker())

    # Wait until _current_chunk_id is set (worker started _synthesize_segment)
    deadline = asyncio.get_event_loop().time() + 2.0
    while session._current_chunk_id is None:
        if asyncio.get_event_loop().time() > deadline:
            raise AssertionError("worker did not start synthesizing in time")
        await asyncio.sleep(0.01)

    chunk_id = session._current_chunk_id
    assert chunk_id is not None

    # Now interrupt
    await session._handle_interrupt()

    msgs = _parse_sent(ws)
    types = [m["type"] for m in msgs]
    assert "chunk_aborted" in types, f"missing chunk_aborted: {msgs}"
    assert "interrupted" in types, f"missing interrupted: {msgs}"

    aborted = next(m for m in msgs if m["type"] == "chunk_aborted")
    interrupted = next(m for m in msgs if m["type"] == "interrupted")
    assert aborted["chunk_id"] == chunk_id
    assert interrupted["chunk_id"] == chunk_id
    # Order: chunk_aborted must appear before interrupted
    assert types.index("chunk_aborted") < types.index("interrupted"), (
        f"chunk_aborted must precede interrupted; got order {types}"
    )


async def test_handle_interrupt_replaces_tts_task():
    """After _handle_interrupt, self._tts_task must be a fresh, non-done task."""
    session, _ws, engine = _make_session()

    # Start with a worker that ends quickly (empty queue → sentinel-less, will await forever)
    session._tts_task = asyncio.create_task(session._tts_worker())
    # Don't await — it will block on queue.get()
    await asyncio.sleep(0)  # yield control so task is scheduled
    old_task = session._tts_task
    assert old_task is not None

    await session._handle_interrupt()
    new_task = session._tts_task
    assert new_task is not None
    assert new_task is not old_task, "expected fresh tts_task after interrupt"
    assert not new_task.done(), "fresh tts_task should not be done"

    # Cleanup: cancel the new worker
    new_task.cancel()
    try:
        await new_task
    except asyncio.CancelledError:
        pass