"""FastAPI router for POST /v1/audio/speech.

Single endpoint. Three exception handlers translate the OpenAI-style
exception classes (defined in src.openai.protocol) to JSON envelopes.

Stream-end cleanup is intentionally not done in handlers — by the time
a 200 response is committed, any subsequent failure only truncates the
stream. Middleware that observes end-of-stream is Phase 2.
"""

from __future__ import annotations

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse

from src.openai.protocol import (
    ErrorDetail,
    ErrorResponse,
    OpenAIAuthError,
    OpenAIBadRequestError,
    OpenAIEngineError,
    SpeechRequest,
)
from src.openai.service import handle_speech_request


router = APIRouter()


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


def register_exception_handlers(app: FastAPI) -> None:
    """Register the OpenAI exception handlers on a FastAPI app.

    Call this after constructing the app, before including the router.
    Mirrors what `@app.exception_handler(...)` decorators would do inline.
    """
    app.add_exception_handler(OpenAIAuthError, _on_auth_error)
    app.add_exception_handler(OpenAIBadRequestError, _on_bad_request)
    app.add_exception_handler(OpenAIEngineError, _on_engine_error)
