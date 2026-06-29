import asyncio
import json
import time

from fastapi.testclient import TestClient

from src.auth.stub import StubAuthProvider
from src.core.config import Settings
from src.main import app
from src.tts.interface import ITTSEngine


class HangingEngine(ITTSEngine):
    """Simulates an engine whose blocking inference never returns."""

    def __init__(self, synthesize_timeout: float | None) -> None:
        self._sample_rate = 24000
        self.synthesize_timeout = synthesize_timeout

    @property
    def sample_rate(self) -> int:
        return 24000

    async def synthesize(self, text: str):
        loop = asyncio.get_running_loop()
        if self.synthesize_timeout is not None:
            try:
                await asyncio.wait_for(
                    loop.run_in_executor(None, time.sleep, 5),
                    timeout=self.synthesize_timeout,
                )
            except asyncio.TimeoutError:
                raise
        else:
            await loop.run_in_executor(None, time.sleep, 5)
        yield b"\x00\x00" * 4800

    async def aclose(self) -> None:
        return None


def _setup(engine: ITTSEngine) -> TestClient:
    settings = Settings(
        engine="mock", auth_provider="stub",
        min_flush_chars=5, session_timeout=10.0,
    )
    app.state.settings = settings
    app.state.engine = engine
    app.state.auth = StubAuthProvider()
    return TestClient(app)


def test_engine_timeout_surfaces_as_error():
    """When synthesize exceeds the timeout, session ends with 'error'."""
    engine = HangingEngine(synthesize_timeout=0.1)
    client = _setup(engine)

    with client.websocket_connect("/ws") as ws:
        ws.send_text(json.dumps({"type": "auth", "token": "t"}))
        ws.receive_text()  # auth_ok
        ws.send_text(json.dumps({"type": "token", "text": "Hello world."}))

        seen: list[dict] = []
        deadline = time.time() + 5.0
        while time.time() < deadline:
            try:
                data = ws.receive()
            except Exception:
                break
            if data.get("type") == "websocket.send" and data.get("text"):
                m = json.loads(data["text"])
                seen.append(m)
                if m["type"] in ("error", "done"):
                    break

    assert any(m["type"] == "error" for m in seen), f"Expected error in {seen}"
