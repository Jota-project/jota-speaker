from contextlib import asynccontextmanager

from fastapi import FastAPI

from src.auth import create_auth_provider
from src.core.config import Settings, get_settings
from src.core.engine_factory import create_engine
from src.core.logger import get_logger

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings: Settings = get_settings()
    logger.info(
        "Starting jota-speaker (engine=%s, auth=%s)",
        settings.engine,
        settings.auth_provider,
    )
    app.state.settings = settings
    app.state.engine = create_engine(settings)
    app.state.auth = create_auth_provider(settings)
    yield
    logger.info("Shutting down jota-speaker")


app = FastAPI(title="jota-speaker", lifespan=lifespan)

from src.server.ws_handler import router  # noqa: E402

app.include_router(router)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("src.main:app", host="0.0.0.0", port=8005, reload=False)
