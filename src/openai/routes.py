"""FastAPI router for POST /v1/audio/speech.

Single endpoint. Three exception handlers translate the OpenAI-style
exception classes (defined in src.openai.protocol) to JSON envelopes.

Stream-end cleanup is intentionally not done in handlers — by the time
a 200 response is committed, any subsequent failure only truncates the
stream. Middleware that observes end-of-stream is Phase 2.
"""

from __future__ import annotations

from fastapi import APIRouter, FastAPI, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from src.openai.protocol import (
    ErrorDetail,
    ErrorResponse,
    OpenAIAuthError,
    OpenAIBadRequestError,
    OpenAIEngineError,
    SpeechRequest,
    VoiceObject,
    VoicesListResponse,
)
from src.openai.service import handle_speech_request


router = APIRouter()


@router.get("/v1/voices")
async def list_voices(http_request: Request) -> VoicesListResponse:
    """List all available voices.

    Returns a list of voice objects with name, voice_id, and model.
    """
    engine = getattr(http_request.app.state, "engine", None)
    if engine is not None and hasattr(engine, "list_voices"):
        all_voices = engine.list_voices()
    else:
        all_voices = []

    # Filter to the 3 Spanish voices we support
    spanish_voices = ["ef_dora", "em_alex", "em_santa"]
    available = [v for v in spanish_voices if v in all_voices]

    return VoicesListResponse(
        voices=[
            VoiceObject(name=name, voice_id=name)
            for name in available
        ]
    )


@router.post("/v1/audio/speech")
async def audio_speech(
    request_body: SpeechRequest,
    http_request: Request,
) -> object:  # StreamingResponse (avoid circular import for type hints)
    return await handle_speech_request(request_body, http_request)


# ── Exception handlers ───────────────────────────────────────────────────────
#
# FastAPI's APIRouter does not support `.exception_handler()` (it is a method
# only on `FastAPI` app instances). We expose the handlers as plain functions
# and a `register_exception_handlers(app)` helper that `main.py` calls once
# after constructing the app. This keeps the surface clean while staying
# compatible with FastAPI's API.


async def _on_auth_error(_request: Request, exc: OpenAIAuthError) -> JSONResponse:
    return JSONResponse(
        status_code=401,
        content=ErrorResponse(
            error=ErrorDetail(
                message=exc.message,
                type="invalid_request_error",
                param=None,
                code="invalid_api_key",
            )
        ).model_dump(),
    )


async def _on_bad_request(_request: Request, exc: OpenAIBadRequestError) -> JSONResponse:
    return JSONResponse(
        status_code=400,
        content=ErrorResponse(
            error=ErrorDetail(
                message=exc.message,
                type="invalid_request_error",
                param=None,
                code=exc.code,
            )
        ).model_dump(),
    )


async def _on_engine_error(_request: Request, exc: OpenAIEngineError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content=ErrorResponse(
            error=ErrorDetail(
                message=exc.message,
                type="server_error",
                param=None,
                code=exc.code,
            )
        ).model_dump(),
    )


async def _on_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    """Translate SpeechRequest field violations to the OpenAI envelope.

    Pydantic rejects some invalid requests (bad `response_format`, out-of-range
    `speed`, `input` too long/short) before `handle_speech_request` ever runs,
    so they never go through OpenAIBadRequestError. Only field-level errors
    (loc has a field name past "body") get the 400 envelope here — structural
    errors like a missing body or non-JSON content type fall back to FastAPI's
    default 422, since there's no single field to attribute them to.
    """
    for error in exc.errors():
        loc = error.get("loc", ())
        if len(loc) >= 2 and loc[0] == "body":
            field = str(loc[-1])
            return JSONResponse(
                status_code=400,
                content=ErrorResponse(
                    error=ErrorDetail(
                        message=error["msg"],
                        type="invalid_request_error",
                        param=field,
                        code=f"invalid_{field}",
                    )
                ).model_dump(),
            )
    return await request_validation_exception_handler(request, exc)


def register_exception_handlers(app: FastAPI) -> None:
    """Register the OpenAI exception handlers on a FastAPI app.

    Call this after constructing the app, before including the router.
    Mirrors what `@app.exception_handler(...)` decorators would do inline.
    """
    app.add_exception_handler(OpenAIAuthError, _on_auth_error)
    app.add_exception_handler(OpenAIBadRequestError, _on_bad_request)
    app.add_exception_handler(OpenAIEngineError, _on_engine_error)
    app.add_exception_handler(RequestValidationError, _on_validation_error)
