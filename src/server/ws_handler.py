from fastapi import APIRouter
from starlette.websockets import WebSocket

from src.core.logger import get_logger
from src.observability.metrics import session_ended, session_started
from .session import SpeakerSession

logger = get_logger(__name__)

router = APIRouter()


@router.websocket("/ws")
async def websocket_endpoint(ws: WebSocket) -> None:
    # Dependencies are resolved via app state set during lifespan
    state = ws.app.state
    session = SpeakerSession(
        ws=ws,
        registry=state.engine_registry,
        auth=state.auth,
        normalizer=state.normalizer,
        min_flush_chars=state.settings.min_flush_chars,
        queue_maxsize=state.settings.queue_maxsize,
        session_timeout=state.settings.session_timeout,
    )
    session_started("ws")
    result = "ok"
    try:
        await session.run()
    except Exception as exc:
        result = "error"
        logger.error("Session error: %s", exc, exc_info=True)
    finally:
        session_ended("ws", result)
