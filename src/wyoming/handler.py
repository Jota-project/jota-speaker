import asyncio

from src.core.config import Settings
from src.core.logger import get_logger
from src.tts.interface import ITTSEngine
from src.wyoming.protocol import read_event, write_event

logger = get_logger(__name__)


class WyomingHandler:
    def __init__(self, engine: ITTSEngine, settings: Settings) -> None:
        self._engine = engine
        self._settings = settings

    async def handle(self, reader: asyncio.StreamReader, writer) -> None:
        peer = writer.get_extra_info("peername")
        logger.info("Wyoming connection from %s", peer)
        try:
            while True:
                event_type, data, _ = await read_event(reader)
                if not event_type:
                    break
                if event_type == "describe":
                    await self._describe(writer)
                elif event_type == "synthesize":
                    text = data.get("text", "")
                    if text:
                        try:
                            await self._synthesize(writer, text)
                        except Exception:
                            logger.exception("Synthesis error for text=%r", text)
        except (asyncio.IncompleteReadError, ConnectionResetError):
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
            logger.info("Wyoming connection closed %s", peer)

    async def _describe(self, writer) -> None:
        await write_event(writer, "info", {
            "tts": [{
                "name": "jota-speaker",
                "attribution": {"name": "jota-speaker", "url": ""},
                "installed": True,
                "languages": [self._settings.kokoro_lang],
            }]
        })

    async def _synthesize(self, writer, text: str) -> None:
        rate = self._engine.sample_rate
        audio_info = {"rate": rate, "width": 2, "channels": 1}
        await write_event(writer, "audio-start", audio_info)
        async for chunk in self._engine.synthesize(text):
            await write_event(writer, "audio-chunk", audio_info, payload=chunk)
        await write_event(writer, "audio-stop", {"timestamp": 0})
