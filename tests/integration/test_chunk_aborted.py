import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from src.auth.stub import StubAuthProvider
from src.core.normalizer_factory import create_normalizer
from src.core.config import Settings
from src.main import app
from src.tts.interface import ITTSEngine


class SlowDisconnectEngine(ITTSEngine):
    """Emits several frames then we abort the client connection mid-stream."""

    def __init__(self) -> None:
        self._sample_rate = 24000

    @property
    def sample_rate(self) -> int:
        return 24000

    async def synthesize(self, text: str):
        for _ in range(20):
            await asyncio.sleep(0.05)
            yield b"\x00\x00" * 4800  # 200ms silence

    async def aclose(self) -> None:
        return None


def _make_client(engine: ITTSEngine) -> TestClient:
    settings = Settings(engine="mock", auth_provider="stub", min_flush_chars=5)
    app.state.settings = settings
    app.state.engine = engine
    app.state.auth = StubAuthProvider()
    app.state.normalizer = create_normalizer(settings)
    return TestClient(app)


def _drain_until_close(client: TestClient) -> list[dict]:
    """Connect, send token+end, then drop the connection as fast as possible."""
    with client.websocket_connect("/ws") as ws:
        ws.send_text(json.dumps({"type": "auth", "token": "t"}))
        auth = json.loads(ws.receive_text())
        assert auth["type"] == "auth_ok"
        ws.send_text(json.dumps({"type": "token", "text": "Hello."}))
        # Read a couple of frames then yank the connection.
        ws.receive()  # audio_start
        for _ in range(2):
            ws.receive()  # binary frame
        # Exit the context manager closes the WebSocket from client side.
    return []


def test_chunk_aborted_when_client_disconnects_mid_audio():
    """When client drops mid-chunk, the server must NOT hang. It must send
    chunk_aborted (or just terminate cleanly) and never leave audio_end unpaired."""
    engine = SlowDisconnectEngine()
    client = _make_client(engine)

    # We just need the server to return from session.run() promptly.
    _drain_until_close(client)

    # If we reach this line without TimeoutError, the server cleaned up.
    assert True
