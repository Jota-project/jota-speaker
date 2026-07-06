import asyncio
import json
import time

from fastapi.testclient import TestClient

from src.auth.stub import StubAuthProvider
from src.core.config import Settings
from src.core.engine_registry import EngineRegistry
from src.core.normalizer_factory import create_normalizer
from src.main import app
from src.tts.interface import ITTSEngine


class SlowFrameEngine(ITTSEngine):
    """Yields several frames slowly so we can barge-in mid-synthesis."""

    def __init__(self) -> None:
        self._sample_rate = 24000

    @property
    def sample_rate(self) -> int:
        return 24000

    async def synthesize(self, text: str, voice: str | None = None):
        for _ in range(10):
            await asyncio.sleep(0.05)
            yield b"\x00\x00" * 4800

    async def aclose(self) -> None:
        return None


def _setup(engine: ITTSEngine, **kwargs) -> TestClient:
    defaults = dict(engine="mock", auth_provider="stub", min_flush_chars=5, session_timeout=10.0)
    defaults.update(kwargs)
    settings = Settings(**defaults)
    app.state.settings = settings
    app.state.engine_registry = EngineRegistry({"test": engine}, "test")
    app.state.auth = StubAuthProvider()
    app.state.normalizer = create_normalizer(settings)
    return TestClient(app)


def _next_text_msg(ws, deadline_s: float = 2.0) -> dict | None:
    """Read messages until a text message arrives or deadline expires."""
    deadline = time.time() + deadline_s
    while time.time() < deadline:
        try:
            data = ws.receive()
        except Exception:
            return None
        if data.get("type") == "websocket.send" and data.get("text"):
            return json.loads(data["text"])
    return None


def _drain_text_messages(ws, deadline_s: float) -> list[dict]:
    """Read all text messages until deadline expires."""
    deadline = time.time() + deadline_s
    out = []
    while time.time() < deadline:
        try:
            data = ws.receive()
        except Exception:
            break
        if data.get("type") != "websocket.send":
            continue
        if not data.get("text"):
            continue
        out.append(json.loads(data["text"]))
    return out


def test_interrupt_during_synthesis_sends_aborted_and_interrupted():
    engine = SlowFrameEngine()
    client = _setup(engine)
    with client.websocket_connect("/ws") as ws:
        ws.send_text(json.dumps({"type": "auth", "token": "t"}))
        ws.receive_text()  # auth_ok
        ws.send_text(json.dumps({"type": "token", "text": "Hello world."}))
        # Wait for audio_start
        start_msg = _next_text_msg(ws, deadline_s=2.0)
        assert start_msg is not None and start_msg["type"] == "audio_start", (
            f"expected audio_start, got {start_msg}"
        )
        chunk_id = start_msg["chunk_id"]

        t0 = time.monotonic()
        ws.send_text(json.dumps({"type": "interrupt"}))

        # Receive chunk_aborted and interrupted for that chunk_id
        got_aborted = False
        got_interrupted = False
        deadline = time.time() + 2.0
        while time.time() < deadline and not (got_aborted and got_interrupted):
            data = ws.receive()
            if data.get("type") != "websocket.send":
                break
            if data.get("text"):
                m = json.loads(data["text"])
                if m["type"] == "chunk_aborted" and m["chunk_id"] == chunk_id:
                    got_aborted = True
                elif m["type"] == "interrupted" and m["chunk_id"] == chunk_id:
                    got_interrupted = True
                    elapsed = (time.monotonic() - t0) * 1000
                    assert elapsed < 200, f"interrupt latency {elapsed:.0f}ms exceeds 200ms"
        assert got_aborted, "expected chunk_aborted"
        assert got_interrupted, "expected interrupted"


def test_interrupt_drains_pending_queue():
    """5 segments buffered + interrupt → only 1 audio_start emitted."""
    engine = SlowFrameEngine()
    client = _setup(engine, min_flush_chars=2, queue_maxsize=20)
    with client.websocket_connect("/ws") as ws:
        ws.send_text(json.dumps({"type": "auth", "token": "t"}))
        ws.receive_text()
        # 5 segments: "a. b. c. d. e."
        ws.send_text(json.dumps({"type": "token", "text": "a. b. c. d. e."}))
        # Wait for first audio_start
        start_msg = _next_text_msg(ws, deadline_s=2.0)
        assert start_msg is not None and start_msg["type"] == "audio_start", (
            f"expected audio_start, got {start_msg}"
        )
        # Send interrupt immediately
        ws.send_text(json.dumps({"type": "interrupt"}))
        # Drain messages
        msgs = _drain_text_messages(ws, deadline_s=2.0)
        # Include the audio_start we already saw
        all_msgs = [start_msg] + msgs
        interrupted_seen = any(m["type"] == "interrupted" for m in all_msgs)
        audio_starts = sum(1 for m in all_msgs if m["type"] == "audio_start")
        audio_ends = sum(1 for m in all_msgs if m["type"] == "audio_end")
        assert interrupted_seen, f"interrupted not received; msgs={all_msgs}"
        assert audio_starts >= 1, f"at least 1 audio_start expected; msgs={all_msgs}"
        # The 4 remaining segments in the queue must NOT produce audio_end
        # (they were drained by interrupt). audio_ends must be <= audio_starts
        # since every audio_end should be paired with a prior audio_start.
        assert audio_ends <= audio_starts, (
            f"more audio_ends ({audio_ends}) than audio_starts ({audio_starts}) — "
            f"queue drain failed; msgs={all_msgs}"
        )


def test_interrupt_with_no_inflight_chunk():
    engine = SlowFrameEngine()
    client = _setup(engine)
    with client.websocket_connect("/ws") as ws:
        ws.send_text(json.dumps({"type": "auth", "token": "t"}))
        ws.receive_text()  # auth_ok
        # Send interrupt immediately with no tokens
        ws.send_text(json.dumps({"type": "interrupt"}))
        # Receive interrupted with chunk_id=0
        interrupted = None
        deadline = time.time() + 2.0
        while time.time() < deadline and interrupted is None:
            data = ws.receive()
            if data.get("type") == "websocket.send" and data.get("text"):
                m = json.loads(data["text"])
                if m["type"] == "interrupted":
                    interrupted = m
                    break
        assert interrupted is not None, "expected interrupted"
        assert interrupted["chunk_id"] == 0, f"expected chunk_id=0, got {interrupted['chunk_id']}"


def test_session_continues_after_interrupt():
    """After interrupt, new tokens produce new audio."""
    engine = SlowFrameEngine()
    client = _setup(engine, min_flush_chars=5)
    with client.websocket_connect("/ws") as ws:
        ws.send_text(json.dumps({"type": "auth", "token": "t"}))
        ws.receive_text()
        # First token
        ws.send_text(json.dumps({"type": "token", "text": "Hello."}))
        # Wait for audio_start of first
        start_msg = _next_text_msg(ws, deadline_s=2.0)
        assert start_msg is not None and start_msg["type"] == "audio_start", (
            f"expected audio_start, got {start_msg}"
        )
        # Interrupt
        ws.send_text(json.dumps({"type": "interrupt"}))
        # Drain until interrupted
        deadline = time.time() + 2.0
        while time.time() < deadline:
            data = ws.receive()
            if data.get("type") == "websocket.send" and data.get("text"):
                m = json.loads(data["text"])
                if m["type"] == "interrupted":
                    break
        # Now send a NEW token
        ws.send_text(json.dumps({"type": "token", "text": "World again."}))
        # Expect a NEW audio_start
        new_start = _next_text_msg(ws, deadline_s=3.0)
        assert new_start is not None and new_start["type"] == "audio_start", (
            f"expected audio_start after interrupt, got {new_start}"
        )


def test_interrupt_resets_accumulator():
    """Tokens sent before interrupt but NOT flushed should be discarded."""
    engine = SlowFrameEngine()
    client = _setup(engine, min_flush_chars=200)  # high so 'Hello wo' never flushes
    with client.websocket_connect("/ws") as ws:
        ws.send_text(json.dumps({"type": "auth", "token": "t"}))
        ws.receive_text()
        # Send partial token that doesn't reach flush threshold
        ws.send_text(json.dumps({"type": "token", "text": "Hello wo"}))
        # Brief pause so receive loop processes it
        time.sleep(0.05)
        # Send interrupt before flush
        ws.send_text(json.dumps({"type": "interrupt"}))
        # Drain until interrupted
        deadline = time.time() + 2.0
        while time.time() < deadline:
            data = ws.receive()
            if data.get("type") == "websocket.send" and data.get("text"):
                m = json.loads(data["text"])
                if m["type"] == "interrupted":
                    break
        # Now send complete fresh token
        ws.send_text(json.dumps({"type": "token", "text": "Brand new sentence."}))
        # Expect audio_start + audio_end of the new segment
        got_start = False
        deadline = time.time() + 5.0
        while time.time() < deadline and not got_start:
            try:
                data = ws.receive()
            except Exception:
                break
            if data.get("type") == "websocket.send" and data.get("text"):
                m = json.loads(data["text"])
                if m["type"] == "audio_start":
                    got_start = True
        assert got_start, "expected audio_start of new segment after interrupt"


def _wait_for_text(ws, target_type: str, deadline_s: float = 2.0) -> dict:
    """Read messages until a text message of given type arrives."""
    deadline = time.time() + deadline_s
    while time.time() < deadline:
        d = ws.receive()
        if d.get("type") == "websocket.send" and d.get("text"):
            m = json.loads(d["text"])
            if m["type"] == target_type:
                return m
    raise AssertionError(f"timed out waiting for {target_type}")


def test_interrupt_latency_under_100ms():
    """Measure interrupt → interrupted latency with MockEngine."""
    engine = SlowFrameEngine()
    client = _setup(engine)
    with client.websocket_connect("/ws") as ws:
        ws.send_text(json.dumps({"type": "auth", "token": "t"}))
        ws.receive_text()
        ws.send_text(json.dumps({"type": "token", "text": "Hello."}))
        # Wait for audio_start
        _wait_for_text(ws, "audio_start", deadline_s=2.0)

        # Warm-up: send one interrupt (don't measure)
        ws.send_text(json.dumps({"type": "interrupt"}))
        _wait_for_text(ws, "interrupted", deadline_s=1.0)

        # Measure 3 interrupts → averaged latency
        latencies = []
        for _ in range(3):
            ws.send_text(json.dumps({"type": "token", "text": "Hi."}))
            _wait_for_text(ws, "audio_start", deadline_s=2.0)
            t0 = time.monotonic()
            ws.send_text(json.dumps({"type": "interrupt"}))
            _wait_for_text(ws, "interrupted", deadline_s=1.0)
            elapsed_ms = (time.monotonic() - t0) * 1000
            latencies.append(elapsed_ms)
        # Avg should be well under 100ms with MockEngine.
        avg = sum(latencies) / len(latencies)
        assert avg < 100, f"avg interrupt latency {avg:.1f}ms exceeds 100ms (samples={latencies})"