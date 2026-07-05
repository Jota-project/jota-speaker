"""Unit tests for src.openai.protocol — Pydantic SpeechRequest and exception classes."""

import pytest
from pydantic import ValidationError

from src.openai.protocol import (
    ErrorResponse,
    OpenAIAuthError,
    OpenAIBadRequestError,
    OpenAIEngineError,
    SpeechRequest,
)


class TestSpeechRequestHappyPath:
    def test_minimal_valid_request(self):
        r = SpeechRequest.model_validate({"model": "tts-1", "input": "hola"})
        assert r.model == "tts-1"
        assert r.input == "hola"
        assert r.voice == "alloy"  # default
        assert r.response_format == "wav"  # default
        assert r.speed == 1.0  # default
        assert r.instructions is None  # default

    def test_all_fields_explicit(self):
        r = SpeechRequest.model_validate({
            "model": "kokoro-es",
            "input": "texto",
            "voice": "ef_dora",
            "response_format": "pcm",
            "speed": 2.0,
            "instructions": "habla despacio",
        })
        assert r.voice == "ef_dora"
        assert r.response_format == "pcm"
        assert r.speed == 2.0
        assert r.instructions == "habla despacio"

    def test_response_format_mp3_accepted(self):
        r = SpeechRequest.model_validate({
            "model": "tts-1",
            "input": "hola",
            "response_format": "mp3",
        })
        assert r.response_format == "mp3"

    def test_response_format_opus_accepted(self):
        r = SpeechRequest.model_validate({
            "model": "tts-1",
            "input": "hola",
            "response_format": "opus",
        })
        assert r.response_format == "opus"

    def test_response_format_aac_accepted(self):
        r = SpeechRequest.model_validate({
            "model": "tts-1",
            "input": "hola",
            "response_format": "aac",
        })
        assert r.response_format == "aac"

    def test_response_format_flac_accepted(self):
        r = SpeechRequest.model_validate({
            "model": "tts-1",
            "input": "hola",
            "response_format": "flac",
        })
        assert r.response_format == "flac"

    def test_extra_fields_are_ignored(self):
        """OpenAI clients send fields like stream, logprobs, n. They must be
        silently dropped, not raise."""
        r = SpeechRequest.model_validate({
            "model": "tts-1",
            "input": "hola",
            "stream": True,
            "logprobs": 5,
            "n": 1,
            "unknown_future_field": "x",
        })
        assert r.model == "tts-1"


class TestSpeechRequestRejections:
    def test_missing_model(self):
        with pytest.raises(ValidationError):
            SpeechRequest.model_validate({"input": "hola"})

    def test_missing_input(self):
        with pytest.raises(ValidationError):
            SpeechRequest.model_validate({"model": "tts-1"})

    def test_empty_input(self):
        with pytest.raises(ValidationError):
            SpeechRequest.model_validate({"model": "tts-1", "input": ""})

    def test_input_too_long(self):
        with pytest.raises(ValidationError):
            SpeechRequest.model_validate({"model": "tts-1", "input": "x" * 4097})

    def test_input_at_max_length(self):
        r = SpeechRequest.model_validate({"model": "tts-1", "input": "x" * 4096})
        assert len(r.input) == 4096

    def test_response_format_aiff_rejected(self):
        with pytest.raises(ValidationError):
            SpeechRequest.model_validate({
                "model": "tts-1",
                "input": "hola",
                "response_format": "aiff",
            })

    def test_speed_below_minimum_rejected(self):
        with pytest.raises(ValidationError):
            SpeechRequest.model_validate({
                "model": "tts-1",
                "input": "hola",
                "speed": 0.1,
            })

    def test_speed_above_maximum_rejected(self):
        with pytest.raises(ValidationError):
            SpeechRequest.model_validate({
                "model": "tts-1",
                "input": "hola",
                "speed": 5.0,
            })

    def test_speed_at_boundaries(self):
        for v in (0.25, 4.0):
            r = SpeechRequest.model_validate({
                "model": "tts-1",
                "input": "hola",
                "speed": v,
            })
            assert r.speed == v

    def test_empty_model_rejected(self):
        with pytest.raises(ValidationError):
            SpeechRequest.model_validate({"model": "", "input": "hola"})

    def test_whitespace_model_rejected(self):
        with pytest.raises(ValidationError):
            SpeechRequest.model_validate({"model": "   ", "input": "hola"})

    def test_empty_voice_rejected(self):
        with pytest.raises(ValidationError):
            SpeechRequest.model_validate({
                "model": "tts-1",
                "input": "hola",
                "voice": "",
            })


class TestExceptionClasses:
    def test_auth_error_default_message(self):
        e = OpenAIAuthError()
        assert e.message == "Invalid API key"

    def test_bad_request_error_carries_code(self):
        e = OpenAIBadRequestError(code="input_empty", message="input cannot be empty")
        assert e.code == "input_empty"
        assert e.message == "input cannot be empty"

    def test_engine_error_default_status(self):
        e = OpenAIEngineError(code="engine_error", message="boom")
        assert e.status_code == 500

    def test_engine_error_explicit_status(self):
        e = OpenAIEngineError(code="engine_unavailable", message="not loaded", status_code=503)
        assert e.status_code == 503


class TestErrorEnvelope:
    def test_error_response_shape(self):
        r = ErrorResponse.model_validate({
            "error": {
                "message": "Invalid API key",
                "type": "invalid_request_error",
                "param": None,
                "code": "invalid_api_key",
            }
        })
        assert r.error.code == "invalid_api_key"
        assert r.error.message == "Invalid API key"
        assert r.error.type == "invalid_request_error"
