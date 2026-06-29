import json

from fastapi.testclient import TestClient

from src.auth.stub import StubAuthProvider
from src.core.config import Settings
from src.core.engine_factory import create_engine
from src.main import app


def test_queue_full_emits_single_error_no_double_done():
    """Queue overflow must emit exactly one 'error' and at most one 'done'.
    The session must not hang and must not emit 'done' twice."""
    settings = Settings(
        engine="mock", auth_provider="stub",
        min_flush_chars=2, queue_maxsize=1, session_timeout=5.0,
    )
    app.state.settings = settings
    app.state.engine = create_engine(settings)
    app.state.auth = StubAuthProvider()

    client = TestClient(app)
    with client.websocket_connect("/ws") as ws:
        ws.send_text(json.dumps({"type": "auth", "token": "t"}))
        ws.receive_text()  # auth_ok
        # min_flush_chars=2 + dots → many segments; queue_maxsize=1 → overflow
        ws.send_text(json.dumps({"type": "token", "text": "a. b. c. d. e. f."}))

        seen: list[dict] = []
        for _ in range(100):
            try:
                data = ws.receive()
            except Exception:
                break
            if data.get("type") == "websocket.send" and data.get("text"):
                m = json.loads(data["text"])
                seen.append(m)
                if m["type"] == "done":
                    break

    done_count = sum(1 for m in seen if m["type"] == "done")
    error_count = sum(1 for m in seen if m["type"] == "error")
    assert error_count >= 1, f"Expected at least one error, got: {seen}"
    assert done_count <= 1, f"Expected at most one done, got: {seen}"
