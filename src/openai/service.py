"""Service layer for POST /v1/audio/speech.

Orchestrates: Bearer extraction → IAuthProvider.validate → input validation
→ normalization → engine.synthesize → encoder wrapping → StreamingResponse.

Reads from app.state (set by FastAPI lifespan): engine, auth, normalizer,
settings. Logs each request with a 12-char hex request_id for correlation
with the X-Request-Id response header.

Public surface:
- extract_bearer_token(authorization) -> Optional[str]
- handle_speech_request(request_body, http_request) -> StreamingResponse

Errors are raised as exceptions (defined in protocol.py) and translated
to JSON envelopes by FastAPI exception handlers in routes.py.
"""

from __future__ import annotations

import re
import uuid
from typing import AsyncIterator, Optional

from fastapi import Request
from fastapi.responses import StreamingResponse

from src.core.logger import get_logger
from src.openai.encoder import pcm_stream, wav_stream
from src.openai.protocol import (
    OpenAIAuthError,
    OpenAIBadRequestError,
    OpenAIEngineError,
    SpeechRequest,
)


_log = get_logger(__name__)


# ── Helpers ──────────────────────────────────────────────────────────────────


_BEARER_RE = re.compile(r"^Bearer\s+(.+)$", re.IGNORECASE)


def extract_bearer_token(authorization: Optional[str]) -> Optional[str]:
    """Extract the token from an `Authorization: Bearer <token>` header.

    Returns the trimmed token if the scheme is `Bearer` (case-insensitive)
    and a non-empty token follows. Returns None in any other case:
    missing header, empty string, no scheme, wrong scheme, no token,
    or whitespace-only token.
    """
    if not authorization:
        return None
    m = _BEARER_RE.match(authorization.strip())
    if not m:
        return None
    token = m.group(1).strip()
    return token if token else None


def _auth_header(http_request: Request) -> str | None:
    """Read the Authorization header.

    Starlette's Headers are case-insensitive — a single .get() covers any casing.
    """
    return http_request.headers.get("authorization")


def _new_request_id() -> str:
    return uuid.uuid4().hex[:12]


def _x_model_used(settings_engine: str) -> str:
    """Map the runtime engine name to a model identifier in the response header."""
    return "mock" if settings_engine == "mock" else "kokoro-es"


# ── Main service entry point ────────────────────────────────────────────────


async def handle_speech_request(
    request_body: SpeechRequest,
    http_request: Request,
) -> StreamingResponse:
    """Process POST /v1/audio/speech and return a streaming audio response.

    Lifecycle:
    1. Extract and validate Bearer token via IAuthProvider (app.state.auth).
    2. Check `input` for whitespace-only (Pydantic min_length=1 already
       covers the empty case as 422).
    3. Generate a request_id, log start.
    4. Normalize the input via app.state.normalizer (best-effort).
    5. Call engine.synthesize(normalized) → AsyncIterator[bytes].
    6. Wrap with pcm_stream or wav_stream per response_format.
    7. Return StreamingResponse with X-Request-Id, X-Model-Used, X-Voice-Used.

    Raises:
        OpenAIAuthError: 401 invalid_api_key.
        OpenAIBadRequestError: 400 invalid_request_error.
        OpenAIEngineError: 500 server_error.
    """
    state = http_request.app.state

    # ── 1. Auth ──────────────────────────────────────────────────────────────
    token = extract_bearer_token(_auth_header(http_request))
    if token is None:
        raise OpenAIAuthError("Missing or malformed Authorization header")

    try:
        valid = await state.auth.validate(token)
    except Exception as exc:  # network / provider failure
        _log.error("auth provider raised: %s", exc)
        raise OpenAIEngineError(
            code="auth_provider_error",
            message="Auth provider unavailable",
        )

    if not valid:
        raise OpenAIAuthError("Invalid API key")

    # ── 2. Post-Pydantic validation ──────────────────────────────────────────
    if not request_body.input.strip():
        raise OpenAIBadRequestError(
            code="input_empty",
            message="input cannot be empty",
        )

    # ── 3. Setup ────────────────────────────────────────────────────────────
    settings = state.settings
    request_id = _new_request_id()
    model_used = _x_model_used(settings.engine)
    voice_used = settings.kokoro_voice

    _log.info(
        "speech_start request_id=%s model=%s voice=%s chars=%d format=%s",
        request_id,
        model_used,
        voice_used,
        len(request_body.input),
        request_body.response_format,
    )

    # ── 4. Normalize (best-effort) ──────────────────────────────────────────
    try:
        normalized = await state.normalizer.normalize(request_body.input)
    except Exception as exc:
        _log.warning("normalizer raised for request_id=%s: %s", request_id, exc)
        normalized = request_body.input

    # ── 5. Synthesize (the streaming source) ───────────────────────────────
    engine = getattr(state, "engine", None)
    if engine is None or not hasattr(engine, "synthesize"):
        raise OpenAIEngineError(
            code="engine_unavailable",
            message="TTS engine not initialized",
            status_code=503,
        )
    try:
        engine_stream: AsyncIterator[bytes] = engine.synthesize(normalized)
    except Exception as exc:
        raise OpenAIEngineError(
            code="engine_error",
            message=f"TTS engine error before stream start: {exc}",
        )

    # ── 6. Wrap with encoder ────────────────────────────────────────────────
    fmt = request_body.response_format
    if fmt == "wav":
        output_stream = wav_stream(engine_stream, engine.sample_rate)
        media_type = "audio/wav"
    else:
        output_stream = pcm_stream(engine_stream)
        media_type = "audio/pcm"

    # ── 7. Build response ───────────────────────────────────────────────────
    headers = {
        "X-Request-Id": request_id,
        "X-Model-Used": model_used,
        "X-Voice-Used": voice_used,
        "Cache-Control": "no-store",
    }

    return StreamingResponse(
        output_stream,
        media_type=media_type,
        headers=headers,
    )
