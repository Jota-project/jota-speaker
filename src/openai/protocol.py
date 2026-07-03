"""OpenAI-compatible TTS API: request schema and error envelope.

Two Pydantic models live here:

- `SpeechRequest`: the request body for POST /v1/audio/speech.
- `ErrorResponse`: the OpenAI-style error envelope used for every non-2xx.

Three exception classes (caught by routes.py exception handlers):

- `OpenAIAuthError`: maps to 401 invalid_api_key.
- `OpenAIBadRequestError`: maps to 400 invalid_request_error.
- `OpenAIEngineError`: maps to 500 server_error (or 503 engine_unavailable).
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


# ── Request ──────────────────────────────────────────────────────────────────


class SpeechRequest(BaseModel):
    """Body of POST /v1/audio/speech.

    Matches the OpenAI TTS request shape at the wire level. Fields the MVP
    ignores (model, voice, speed, instructions) are still validated so that
    clients sending arbitrary values do not get a 4xx — only nonsense
    values (empty strings, out-of-range speed) are rejected.
    """

    model_config = ConfigDict(extra="ignore")

    model: str
    input: str = Field(min_length=1, max_length=4096)
    voice: str = "alloy"
    response_format: Literal["pcm", "wav"] = "wav"
    speed: float = 1.0
    instructions: str | None = None

    @field_validator("model", "voice")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("must be a non-empty string")
        return v

    @field_validator("speed")
    @classmethod
    def _speed_range(cls, v: float) -> float:
        if not (0.25 <= v <= 4.0):
            raise ValueError("must be between 0.25 and 4.0")
        return v


# ── Error envelope ───────────────────────────────────────────────────────────


class ErrorDetail(BaseModel):
    message: str
    type: str
    param: str | None = None
    code: str | None = None


class ErrorResponse(BaseModel):
    error: ErrorDetail


# ── Exception classes (caught by FastAPI handlers in routes.py) ──────────────


class OpenAIAuthError(Exception):
    """401 invalid_api_key."""

    def __init__(self, message: str = "Invalid API key") -> None:
        super().__init__(message)
        self.message = message


class OpenAIBadRequestError(Exception):
    """400 invalid_request_error."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class OpenAIEngineError(Exception):
    """500 server_error (or 503 engine_unavailable for unavailable engine)."""

    def __init__(self, code: str, message: str, status_code: int = 500) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
