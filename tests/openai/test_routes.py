"""End-to-end tests for /v1/audio/speech with the full FastAPI app.

Mirrors tests/integration/test_tts_stream.py: a small fixture app
with MockEngine + StubAuthProvider + PassthroughNormalizer wired in.
We never load Kokoro in tests (the project's convention).
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.auth.stub import StubAuthProvider
from src.openai.routes import register_exception_handlers, router as openai_router
from src.tts.mock_engine import MockEngine
from src.tts.normalizer import PassThroughNormalizer


@pytest.fixture
def app():
    """Build a minimal FastAPI app mirroring src.main lifespan state."""
    app = FastAPI()
    app.state.engine = MockEngine(sample_rate=24000)
    app.state.auth = StubAuthProvider()
    app.state.normalizer = PassThroughNormalizer()

    class _Settings:
        engine = "mock"
        kokoro_voice = "ef_dora"
        kokoro_lang = "es"
        sample_rate = 24000

    app.state.settings = _Settings()
    register_exception_handlers(app)
    app.include_router(openai_router)
    return app


class TestEndpointShape:
    def test_post_returns_200_streaming(self, app):
        client = TestClient(app)
        with client.stream(
            "POST",
            "/v1/audio/speech",
            headers={"Authorization": "Bearer t"},
            json={"model": "tts-1", "input": "hola", "response_format": "wav"},
        ) as resp:
            assert resp.status_code == 200
            chunks = list(resp.iter_bytes())
            assert len(chunks) >= 1
            assert chunks[0][:4] == b"RIFF"

    def test_extra_fields_dont_break(self, app):
        """OpenAI clients send fields like stream. They must be ignored, not 422."""
        client = TestClient(app)
        resp = client.post(
            "/v1/audio/speech",
            headers={"Authorization": "Bearer t"},
            json={
                "model": "tts-1",
                "input": "hola",
                "stream": True,
                "logprobs": 5,
            },
        )
        assert resp.status_code == 200

    def test_get_returns_405(self, app):
        client = TestClient(app)
        resp = client.get("/v1/audio/speech")
        assert resp.status_code == 405

    def test_put_returns_405(self, app):
        client = TestClient(app)
        resp = client.put("/v1/audio/speech", json={"model": "x", "input": "y"})
        assert resp.status_code == 405

    def test_missing_body_returns_422(self, app):
        client = TestClient(app)
        resp = client.post(
            "/v1/audio/speech",
            headers={"Authorization": "Bearer t", "Content-Type": "application/json"},
        )
        assert resp.status_code == 422

    def test_wrong_content_type_returns_422(self, app):
        client = TestClient(app)
        resp = client.post(
            "/v1/audio/speech",
            headers={"Authorization": "Bearer t", "Content-Type": "text/plain"},
            content="not json",
        )
        assert resp.status_code == 422


class TestResponseHeaders:
    def test_x_request_id_present(self, app):
        client = TestClient(app)
        resp = client.post(
            "/v1/audio/speech",
            headers={"Authorization": "Bearer t"},
            json={"model": "tts-1", "input": "hola"},
        )
        assert "x-request-id" in resp.headers
        # 12-char hex
        assert len(resp.headers["x-request-id"]) == 12

    def test_x_model_used_present(self, app):
        client = TestClient(app)
        resp = client.post(
            "/v1/audio/speech",
            headers={"Authorization": "Bearer t"},
            json={"model": "tts-1-hd", "input": "hola"},
        )
        assert resp.headers["x-model-used"] == "mock"

    def test_x_voice_used_echoes_engine_setting(self, app):
        client = TestClient(app)
        resp = client.post(
            "/v1/audio/speech",
            headers={"Authorization": "Bearer t"},
            json={"model": "tts-1", "input": "hola", "voice": "shimmer"},
        )
        # MVP ignores voice from request, echoes the engine setting
        assert resp.headers["x-voice-used"] == "ef_dora"

    def test_cache_control_no_store(self, app):
        client = TestClient(app)
        resp = client.post(
            "/v1/audio/speech",
            headers={"Authorization": "Bearer t"},
            json={"model": "tts-1", "input": "hola"},
        )
        assert resp.headers["cache-control"] == "no-store"


class TestWavHeaderSampleRate:
    def test_wav_header_uses_engine_sample_rate(self, app):
        """Verify the RIFF header's SampleRate matches state.engine.sample_rate."""
        client = TestClient(app)
        resp = client.post(
            "/v1/audio/speech",
            headers={"Authorization": "Bearer t"},
            json={"model": "tts-1", "input": "hola", "response_format": "wav"},
        )
        import struct
        # Header bytes 24..27 are the SampleRate (uint32 LE)
        sample_rate_in_header = struct.unpack("<I", resp.content[24:28])[0]
        assert sample_rate_in_header == 24000
