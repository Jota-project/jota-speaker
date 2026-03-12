from fastapi import APIRouter
from starlette.websockets import WebSocket

from src.core.logger import get_logger
from .session import SpeakerSession

logger = get_logger(__name__)

router = APIRouter()


@router.websocket("/ws")
async def websocket_endpoint(ws: WebSocket) -> None:
    # Dependencies are resolved via app state set during lifespan
    state = ws.app.state
    session = SpeakerSession(
        ws=ws,
        engine=state.engine,
        auth=state.auth,
        min_flush_chars=state.settings.min_flush_chars,
    )
    try:
        await session.run()
    except Exception as exc:
        logger.error("Session error: %s", exc, exc_info=True)
