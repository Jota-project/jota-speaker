"""Service layer for POST /v1/audio/speech.

Orchestrates: Bearer extraction → IAuthProvider.validate → input validation
→ normalization → engine.synthesize → encoder wrapping → StreamingResponse.

Reads from app.state (set by FastAPI lifespan): engine_registry, auth,
normalizer. Logs each request with a 12-char hex request_id for correlation
with the X-Request-Id response header.

Public surface:
- extract_bearer_token(authorization) -> Optional[str]
- handle_speech_request(request_body, http_request) -> StreamingResponse
- handle_list_voices(http_request) -> VoicesListResponse

Errors are raised as exceptions (defined in protocol.py) and translated
to JSON envelopes by FastAPI exception handlers in routes.py.
"""

from __future__ import annotations

import re
import uuid
from typing import AsyncIterator, Optional

from fastapi import Request
from fastapi.responses import Response, StreamingResponse

from src.core.logger import get_logger
from src.openai.encoder import (
    aac_stream,
    build_wav_header,
    flac_stream,
    mp3_stream,
    opus_stream,
    pcm_stream,
    wav_stream,
)
from src.openai.protocol import (
    OpenAIAuthError,
    OpenAIBadRequestError,
    OpenAIEngineError,
    SpeechRequest,
    VoiceObject,
    VoicesListResponse,
)


_log = get_logger(__name__)

_FFMPEG_ENCODERS = {
    "mp3": ("audio/mpeg", mp3_stream),
    "opus": ("audio/ogg", opus_stream),
    "aac": ("audio/aac", aac_stream),
    "flac": ("audio/flac", flac_stream),
}


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


async def _authenticate(http_request: Request) -> None:
    """Extract and validate the Bearer token via app.state.auth.

    Raises OpenAIAuthError (401) or OpenAIEngineError (auth_provider_error)
    on failure; returns None on success.
    """
    state = http_request.app.state
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


# ── Main service entry point ────────────────────────────────────────────────


async def handle_speech_request(
    request_body: SpeechRequest,
    http_request: Request,
) -> StreamingResponse | Response:
    """Process POST /v1/audio/speech and return an audio response.

    Lifecycle:
    1. Extract and validate Bearer token via IAuthProvider (app.state.auth).
    2. Check `input` for whitespace-only (Pydantic min_length=1 already
       covers the empty case as 422).
    3. Generate a request_id, log start.
    4. Normalize the input via app.state.normalizer (best-effort).
    5. Call engine.synthesize(normalized) → AsyncIterator[bytes].
    6. Wrap with pcm_stream, wav_stream, or one of the ffmpeg-based
       encoders (mp3/opus/aac/flac) per response_format.
    7. If `request_body.stream` (default True): return StreamingResponse,
       chunked, with X-Request-Id/X-Model-Used/X-Voice-Used/X-Speed-Used.
       Otherwise buffer the full response — for `wav`, this also swaps the
       streaming 0xFFFFFFFF header sentinel for the exact byte-accurate
       size — and return a plain Response with a real Content-Length.

    Raises:
        OpenAIAuthError: 401 invalid_api_key.
        OpenAIBadRequestError: 400 invalid_request_error.
        OpenAIEngineError: 500 server_error.
    """
    state = http_request.app.state

    # ── 1. Auth ──────────────────────────────────────────────────────────────
    await _authenticate(http_request)

    # ── 2. Post-Pydantic validation ──────────────────────────────────────────
    if not request_body.input.strip():
        raise OpenAIBadRequestError(
            code="input_empty",
            message="input cannot be empty",
        )

    # ── 3. Resolve model/voice ────────────────────────────────────────────
    registry = getattr(state, "engine_registry", None)
    if registry is None:
        raise OpenAIEngineError(
            code="engine_unavailable",
            message="TTS engine not initialized",
            status_code=503,
        )
    model_id, engine = registry.resolve(request_body.model)
    voice_used = engine.resolve_voice(request_body.voice)
    speed_used = engine.resolve_speed(request_body.speed)
    request_id = _new_request_id()

    _log.info(
        "speech_start request_id=%s model=%s voice=%s speed=%s chars=%d format=%s",
        request_id,
        model_id,
        voice_used,
        speed_used,
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
    fmt = request_body.response_format
    if fmt in _FFMPEG_ENCODERS and not getattr(state, "ffmpeg_available", False):
        raise OpenAIEngineError(
            code="audio_format_unavailable",
            message=f"{fmt} encoding not available (ffmpeg not installed)",
            status_code=503,
        )

    try:
        engine_stream: AsyncIterator[bytes] = engine.synthesize(
            normalized, voice=voice_used, speed=speed_used
        )
    except Exception as exc:
        raise OpenAIEngineError(
            code="engine_error",
            message=f"TTS engine error before stream start: {exc}",
        )

    # ── 6. Wrap with encoder ────────────────────────────────────────────────
    output_stream: AsyncIterator[bytes] | None
    if fmt in _FFMPEG_ENCODERS:
        media_type, encoder_fn = _FFMPEG_ENCODERS[fmt]
        output_stream = encoder_fn(engine_stream, engine.sample_rate)
    elif fmt == "wav":
        media_type = "audio/wav"
        # Buffered mode needs the real PCM size before it can build a
        # byte-accurate header, so it bypasses wav_stream's sentinel header
        # entirely (see the buffering branch below).
        output_stream = None if not request_body.stream else wav_stream(
            engine_stream, engine.sample_rate
        )
    else:
        output_stream = pcm_stream(engine_stream)
        media_type = "audio/pcm"

    # ── 7. Build response ───────────────────────────────────────────────────
    headers = {
        "X-Request-Id": request_id,
        "X-Model-Used": model_id,
        "X-Voice-Used": voice_used,
        "X-Speed-Used": str(speed_used),
        "Cache-Control": "no-store",
    }

    if not request_body.stream:
        if fmt == "wav":
            pcm = bytearray()
            async for chunk in engine_stream:
                pcm.extend(chunk)
            body = build_wav_header(engine.sample_rate, data_size=len(pcm)) + bytes(pcm)
        else:
            buffered = bytearray()
            async for chunk in output_stream:
                buffered.extend(chunk)
            body = bytes(buffered)
        return Response(content=body, media_type=media_type, headers=headers)

    return StreamingResponse(
        output_stream,
        media_type=media_type,
        headers=headers,
    )


async def handle_list_voices(http_request: Request) -> VoicesListResponse:
    """Process GET /v1/voices: list voices loaded on the default model.

    All discovered Kokoro model variants share the same voices file
    (see EngineRegistry), so the default model's catalog is authoritative.

    Raises:
        OpenAIAuthError: 401 invalid_api_key.
        OpenAIEngineError: 503 engine_unavailable.
    """
    await _authenticate(http_request)

    state = http_request.app.state
    registry = getattr(state, "engine_registry", None)
    if registry is None:
        raise OpenAIEngineError(
            code="engine_unavailable",
            message="TTS engine not initialized",
            status_code=503,
        )
    _, engine = registry.resolve(None)
    voices = engine.available_voices() or []
    return VoicesListResponse(
        voices=[VoiceObject(voice_id=v, name=v) for v in voices]
    )
