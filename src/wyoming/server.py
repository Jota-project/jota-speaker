import asyncio

from src.core.config import Settings
from src.core.logger import get_logger
from src.tts.interface import ITTSEngine
from src.wyoming.handler import WyomingHandler

logger = get_logger(__name__)


class WyomingServer:
    def __init__(self, settings: Settings, engine: ITTSEngine) -> None:
        self._settings = settings
        self._engine = engine
        self._server: asyncio.Server | None = None

    @property
    def port(self) -> int:
        if self._server is None:
            raise RuntimeError("Server not started")
        return self._server.sockets[0].getsockname()[1]

    async def start(self) -> None:
        if self._server is not None:
            raise RuntimeError("Server already started")
        handler = WyomingHandler(self._engine, self._settings)
        self._server = await asyncio.start_server(
            handler.handle,
            host="0.0.0.0",
            port=self._settings.wyoming_port,
        )
        logger.info("Wyoming server listening on port %d", self.port)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
            logger.info("Wyoming server stopped")
