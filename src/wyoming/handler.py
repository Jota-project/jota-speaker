import asyncio
import time

from src.core.config import Settings
from src.core.logger import get_logger
from src.observability.metrics import error_occurred, observe_ttfb_ms, session_ended, session_started
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
        session_started("wyoming")
        result = "ok"
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
                        voice_field = data.get("voice")
                        requested_voice = voice_field.get("name") if isinstance(voice_field, dict) else None
                        try:
                            await self._synthesize(writer, text, requested_voice)
                        except Exception:
                            logger.exception("Synthesis error for text=%r", text)
                            error_occurred("synthesis_error")
                            result = "error"
        except (asyncio.IncompleteReadError, ConnectionResetError):
            result = "client_close"
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
            session_ended("wyoming", result)
            logger.info("Wyoming connection closed %s", peer)

    async def _describe(self, writer) -> None:
        voices = self._engine.available_voices() or [self._settings.kokoro_voice]
        await write_event(writer, "info", {
            "asr": [],
            "tts": [{
                "name": "jota-speaker",
                "description": "jota-speaker TTS",
                "attribution": {"name": "jota-speaker", "url": ""},
                "installed": True,
                "languages": [self._settings.kokoro_lang],
                "voices": [{
                    "name": v,
                    "description": v,
                    "attribution": {"name": "jota-speaker", "url": ""},
                    "installed": True,
                    "languages": [self._settings.kokoro_lang],
                    "speakers": [],
                } for v in voices],
            }],
            "wake": [],
            "handle": [],
            "intent": [],
        })

    async def _synthesize(self, writer, text: str, requested_voice: str | None = None) -> None:
        voice = self._engine.resolve_voice(requested_voice)
        rate = self._engine.sample_rate
        audio_info = {"rate": rate, "width": 2, "channels": 1}
        await write_event(writer, "audio-start", audio_info)
        synth_start = time.monotonic()
        first_chunk = True
        async for chunk in self._engine.synthesize(text, voice=voice):
            if first_chunk:
                first_chunk = False
                elapsed_ms = (time.monotonic() - synth_start) * 1000
                observe_ttfb_ms(
                    elapsed_ms,
                    session_type="wyoming",
                    engine=self._engine.__class__.__name__.lower(),
                )
            await write_event(writer, "audio-chunk", audio_info, payload=chunk)
        await write_event(writer, "audio-stop", {"timestamp": 0})
