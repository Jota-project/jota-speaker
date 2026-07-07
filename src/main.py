from contextlib import asynccontextmanager

from fastapi import FastAPI, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from src.auth import create_auth_provider
from src.core.config import Settings, get_settings
from src.core.engine_factory import create_engine_registry
from src.core.logger import get_logger
from src.core.normalizer_factory import create_normalizer

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings: Settings = get_settings()
    logger.info(
        "Starting jota-speaker (engine=%s, auth=%s, wyoming=%s)",
        settings.engine,
        settings.auth_provider,
        settings.wyoming_enabled,
    )
    app.state.settings = settings
    app.state.engine_registry = create_engine_registry(settings)
    app.state.auth = create_auth_provider(settings)

    from src.openai.encoder import ffmpeg_available

    app.state.ffmpeg_available = ffmpeg_available()
    if not app.state.ffmpeg_available:
        logger.warning("ffmpeg not found on PATH — POST /v1/audio/speech with response_format in {mp3,opus,aac,flac} will return 503")

    if settings.wyoming_enabled:
        from src.wyoming.server import WyomingServer

        _, default_engine = app.state.engine_registry.resolve(None)
        wyoming = WyomingServer(settings, default_engine)
        await wyoming.start()
        app.state.wyoming_server = wyoming

    app.state.normalizer = create_normalizer(settings)
    yield

    if hasattr(app.state, "wyoming_server"):
        await app.state.wyoming_server.stop()

    logger.info("Shutting down jota-speaker")
    try:
        await app.state.engine_registry.aclose()
    except Exception as exc:
        logger.warning("Engine registry aclose on shutdown failed: %s", exc)


app = FastAPI(title="jota-speaker", lifespan=lifespan)

from src.server.ws_handler import router  # noqa: E402
from src.openai.routes import register_exception_handlers, router as openai_router  # noqa: E402

register_exception_handlers(app)
app.include_router(router)
app.include_router(openai_router)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.get("/metrics")
async def metrics() -> Response:
    """Prometheus scrape endpoint. Returns metrics in text format. No auth."""
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("src.main:app", host="0.0.0.0", port=8005, reload=False)
