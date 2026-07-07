"""Integration tests for Wyoming TTFB and sessions metric."""
import asyncio

import pytest

from src.core.config import Settings
from src.observability import metrics
from src.tts.interface import ITTSEngine
from src.wyoming.handler import WyomingHandler
from src.wyoming.protocol import read_event, write_event


class FrameEngine(ITTSEngine):
    @property
    def sample_rate(self) -> int:
        return 24000

    async def synthesize(self, text: str, voice: str | None = None, speed: float | None = None):
        await asyncio.sleep(0.01)
        yield b"\x00\x00" * 100

    async def aclose(self) -> None:
        return None


def _gauge(name: str, **labels) -> float:
    from prometheus_client import REGISTRY, generate_latest

    output = generate_latest(REGISTRY).decode()
    needle = f'{name}{{{",".join(f"{k}=\"{v}\"" for k, v in sorted(labels.items()))}}} '
    for line in output.splitlines():
        if line.startswith(needle):
            return float(line.rsplit(" ", 1)[-1])
    return 0.0


def _histogram_count(name: str, **labels) -> float:
    return _gauge(name + "_count", **labels)


@pytest.mark.asyncio
async def test_wyoming_ttfb_observed_with_session_type_wyoming():
    metrics.TTFB_HISTOGRAM.clear()
    settings = Settings(engine="mock", auth_provider="stub")
    handler = WyomingHandler(FrameEngine(), settings)

    server = await asyncio.start_server(handler.handle, host="127.0.0.1", port=0)
    port = server.sockets[0].getsockname()[1]
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        await write_event(writer, "synthesize", {"text": "Hello world."})
        while True:
            event_type, data, payload = await read_event(reader)
            if event_type == "audio-stop":
                break
        writer.close()
        await writer.wait_closed()
    finally:
        server.close()
        await server.wait_closed()

    assert _histogram_count("jota_speaker_ttfb_ms", session_type="wyoming", engine="frameengine") >= 1.0


@pytest.mark.asyncio
async def test_wyoming_session_gauge_increments_and_decrements():
    # Checking the gauge mid-connection would race the handler's task
    # scheduling on the same event loop as the test, so instead we assert
    # the full start->end lifecycle via the terminal counter plus the
    # gauge being back at baseline once the connection (and its handler
    # task) has fully closed.
    metrics.SESSIONS_ACTIVE.clear()
    metrics.SESSIONS_TOTAL.clear()
    settings = Settings(engine="mock", auth_provider="stub")
    handler = WyomingHandler(FrameEngine(), settings)

    server = await asyncio.start_server(handler.handle, host="127.0.0.1", port=0)
    port = server.sockets[0].getsockname()[1]
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        await write_event(writer, "synthesize", {"text": "Hi."})
        while True:
            event_type, _, _ = await read_event(reader)
            if event_type == "audio-stop":
                break
        writer.close()
        await writer.wait_closed()
    finally:
        server.close()
        await server.wait_closed()

    await asyncio.sleep(0.05)
    assert _gauge("jota_speaker_sessions_active", session_type="wyoming") == 0.0
    assert _gauge("jota_speaker_sessions_total", session_type="wyoming", result="ok") == 1.0
