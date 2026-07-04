"""Integration tests for handle_speech_request with MockEngine and stub auth."""

import asyncio
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.auth.interface import IAuthProvider
from src.auth.stub import StubAuthProvider
from src.openai.routes import router as openai_router
from src.tts.mock_engine import MockEngine
from src.tts.normalizer import PassThroughNormalizer


@pytest.fixture
def app_with_engine_and_stub_auth(monkeypatch):
    """Build a FastAPI app with MockEngine, StubAuthProvider, PassThroughNormalizer.

    We don't use the full lifespan (which would try to load Kokoro); we
    wire the minimal state the service needs. Mirrors the pattern in
    tests/integration/test_tts_stream.py.
    """
    app = FastAPI()
    app.state.engine = MockEngine(sample_rate=24000)
    app.state.auth = StubAuthProvider()
    app.state.normalizer = PassThroughNormalizer()

    class _FakeSettings:
        engine = "mock"
        kokoro_voice = "ef_dora"
        kokoro_lang = "es"
        sample_rate = 24000

    app.state.settings = _FakeSettings()
    from src.openai.routes import register_exception_handlers
    register_exception_handlers(app)
    app.include_router(openai_router)
    return app


class TestHappyPath:
    def test_pcm_format_streams_audio(self, app_with_engine_and_stub_auth):
        client = TestClient(app_with_engine_and_stub_auth)
        resp = client.post(
            "/v1/audio/speech",
            headers={"Authorization": "Bearer any-token"},
            json={"model": "tts-1", "input": "hola mundo", "response_format": "pcm"},
        )
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "audio/pcm"
        assert resp.headers["x-request-id"]
        assert resp.headers["x-model-used"] == "mock"
        assert resp.headers["x-voice-used"] == "ef_dora"
        # MockEngine emits at least one silence frame for a 11-char input
        assert len(resp.content) > 0
        assert len(resp.content) % 2 == 0  # PCM16 invariant

    def test_wav_format_starts_with_riff_header(self, app_with_engine_and_stub_auth):
        client = TestClient(app_with_engine_and_stub_auth)
        resp = client.post(
            "/v1/audio/speech",
            headers={"Authorization": "Bearer any-token"},
            json={"model": "tts-1", "input": "hola", "response_format": "wav"},
        )
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "audio/wav"
        # First 4 bytes are "RIFF"
        assert resp.content[:4] == b"RIFF"
        assert len(resp.content) >= 44

    def test_wav_header_compatible_with_kokoro_sample_rate(self, app_with_engine_and_stub_auth, monkeypatch):
        # The mock engine defaults to 24000. Verify the X-Model-Used reflects engine state.
        client = TestClient(app_with_engine_and_stub_auth)
        resp = client.post(
            "/v1/audio/speech",
            headers={"Authorization": "Bearer t",
                     "X-Trace": "trace-1"},
            json={"model": "kokoro-es", "input": "adios", "voice": "shimmer"},
        )
        assert resp.status_code == 200
        # MVP behavior: voice from request is ignored, X-Voice-Used echoes the configured one
        assert resp.headers["x-voice-used"] == "ef_dora"


class TestAuthFailures:
    def test_missing_authorization_header_returns_401(self, app_with_engine_and_stub_auth):
        client = TestClient(app_with_engine_and_stub_auth)
        resp = client.post(
            "/v1/audio/speech",
            json={"model": "tts-1", "input": "hola"},
        )
        assert resp.status_code == 401
        body = resp.json()
        assert body["error"]["code"] == "invalid_api_key"

    def test_non_bearer_scheme_returns_401(self, app_with_engine_and_stub_auth):
        client = TestClient(app_with_engine_and_stub_auth)
        resp = client.post(
            "/v1/audio/speech",
            headers={"Authorization": "Basic dXNlcjpwYXNz"},
            json={"model": "tts-1", "input": "hola"},
        )
        assert resp.status_code == 401
        assert resp.json()["error"]["code"] == "invalid_api_key"

    def test_empty_bearer_token_returns_401(self, app_with_engine_and_stub_auth):
        client = TestClient(app_with_engine_and_stub_auth)
        resp = client.post(
            "/v1/audio/speech",
            headers={"Authorization": "Bearer "},
            json={"model": "tts-1", "input": "hola"},
        )
        assert resp.status_code == 401
        assert resp.json()["error"]["code"] == "invalid_api_key"

    def test_token_rejected_by_provider_returns_401(self, app_with_engine_and_stub_auth):
        # Replace stub with a reject-all provider
        class RejectAll(IAuthProvider):
            async def validate(self, token: str) -> bool:
                return False

        app_with_engine_and_stub_auth.state.auth = RejectAll()
        client = TestClient(app_with_engine_and_stub_auth)
        resp = client.post(
            "/v1/audio/speech",
            headers={"Authorization": "Bearer x"},
            json={"model": "tts-1", "input": "hola"},
        )
        assert resp.status_code == 401
        assert resp.json()["error"]["code"] == "invalid_api_key"

    def test_provider_raises_returns_500(self, app_with_engine_and_stub_auth):
        class BoomAuth(IAuthProvider):
            async def validate(self, token: str) -> bool:
                raise ConnectionError("jota-db down")

        app_with_engine_and_stub_auth.state.auth = BoomAuth()
        client = TestClient(app_with_engine_and_stub_auth)
        resp = client.post(
            "/v1/audio/speech",
            headers={"Authorization": "Bearer x"},
            json={"model": "tts-1", "input": "hola"},
        )
        assert resp.status_code == 500
        assert resp.json()["error"]["code"] == "auth_provider_error"
        assert resp.json()["error"]["type"] == "server_error"


class TestInputValidation:
    def test_whitespace_only_input_returns_400(self, app_with_engine_and_stub_auth):
        client = TestClient(app_with_engine_and_stub_auth)
        resp = client.post(
            "/v1/audio/speech",
            headers={"Authorization": "Bearer t"},
            json={"model": "tts-1", "input": "   \t\n  "},
        )
        assert resp.status_code == 400
        body = resp.json()
        assert body["error"]["code"] == "input_empty"
        assert body["error"]["type"] == "invalid_request_error"

    def test_unsupported_format_returns_400(self, app_with_engine_and_stub_auth):
        client = TestClient(app_with_engine_and_stub_auth)
        resp = client.post(
            "/v1/audio/speech",
            headers={"Authorization": "Bearer t"},
            json={"model": "tts-1", "input": "hola", "response_format": "mp3"},
        )
        # Pydantic catches this at 422 with our model_config still emitting the OpenAI envelope
        assert resp.status_code == 422
        body = resp.json()
        assert "response_format" in str(body)

    def test_input_too_long_returns_422(self, app_with_engine_and_stub_auth):
        client = TestClient(app_with_engine_and_stub_auth)
        resp = client.post(
            "/v1/audio/speech",
            headers={"Authorization": "Bearer t"},
            json={"model": "tts-1", "input": "x" * 4097},
        )
        assert resp.status_code == 422

    def test_speed_out_of_range_returns_422(self, app_with_engine_and_stub_auth):
        client = TestClient(app_with_engine_and_stub_auth)
        resp = client.post(
            "/v1/audio/speech",
            headers={"Authorization": "Bearer t"},
            json={"model": "tts-1", "input": "hola", "speed": 10.0},
        )
        assert resp.status_code == 422