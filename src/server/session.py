import asyncio
from typing import Any

from fastapi.websockets import WebSocketState
from pydantic import ValidationError
from starlette.websockets import WebSocket

from src.auth.interface import IAuthProvider
from src.core.logger import get_logger
from src.tts.interface import ITTSEngine
from .accumulator import TokenAccumulator
from .protocol import (
    AudioEndMessage,
    AudioStartMessage,
    AuthErrorMessage,
    AuthOkMessage,
    DoneMessage,
    EndMessage,
    ErrorMessage,
    FlushMessage,
    TokenMessage,
    parse_client_message,
    serialize_server_message,
)

logger = get_logger(__name__)

# Sentinel pushed into the queue to signal end-of-stream
_SENTINEL = object()


class SpeakerSession:
    def __init__(
        self,
        ws: WebSocket,
        engine: ITTSEngine,
        auth: IAuthProvider,
        min_flush_chars: int = 80,
    ) -> None:
        self._ws = ws
        self._engine = engine
        self._auth = auth
        self._accumulator = TokenAccumulator(min_flush_chars=min_flush_chars)
        self._queue: asyncio.Queue[str | object] = asyncio.Queue()
        self._chunk_counter = 0

    # ── public entry point ────────────────────────────────────────────────────

    async def run(self) -> None:
        await self._ws.accept()
        if not await self._authenticate():
            return
        tts_task = asyncio.create_task(self._tts_worker())
        try:
            await self._receive_loop()
        finally:
            await tts_task

    # ── authentication ────────────────────────────────────────────────────────

    async def _authenticate(self) -> bool:
        try:
            raw = await self._ws.receive_text()
            msg = parse_client_message(raw)
        except (ValidationError, Exception) as exc:
            await self._send(AuthErrorMessage(reason=f"Bad first message: {exc}"))
            await self._ws.close(code=1008)
            return False

        if msg.type != "auth":
            await self._send(AuthErrorMessage(reason="First message must be auth"))
            await self._ws.close(code=1008)
            return False

        try:
            valid = await self._auth.validate(msg.token)
        except Exception as exc:
            logger.error("Auth provider error: %s", exc)
            await self._send(ErrorMessage(code="auth_error", message="Auth service unavailable"))
            await self._ws.close(code=1011)
            return False

        if not valid:
            await self._send(AuthErrorMessage(reason="Invalid token"))
            await self._ws.close(code=1008)
            return False

        await self._send(AuthOkMessage())
        return True

    # ── receive loop ──────────────────────────────────────────────────────────

    async def _receive_loop(self) -> None:
        try:
            while True:
                raw = await self._ws.receive_text()
                try:
                    msg = parse_client_message(raw)
                except (ValidationError, Exception) as exc:
                    await self._send(ErrorMessage(code="parse_error", message=str(exc)))
                    continue

                if isinstance(msg, TokenMessage):
                    segments = self._accumulator.add(msg.text)
                    for seg in segments:
                        await self._queue.put(seg)

                elif isinstance(msg, FlushMessage):
                    for seg in self._accumulator.flush():
                        await self._queue.put(seg)

                elif isinstance(msg, EndMessage):
                    for seg in self._accumulator.flush():
                        await self._queue.put(seg)
                    await self._queue.put(_SENTINEL)
                    return

        except Exception as exc:
            logger.warning("Receive loop ended: %s", exc)
            await self._queue.put(_SENTINEL)

    # ── TTS worker ────────────────────────────────────────────────────────────

    async def _tts_worker(self) -> None:
        while True:
            item = await self._queue.get()
            if item is _SENTINEL:
                break
            assert isinstance(item, str)
            await self._synthesize_segment(item)

        if self._ws.client_state == WebSocketState.CONNECTED:
            await self._send(DoneMessage())

    async def _synthesize_segment(self, text: str) -> None:
        chunk_id = self._chunk_counter
        self._chunk_counter += 1
        await self._send(
            AudioStartMessage(
                chunk_id=chunk_id,
                sample_rate=self._engine.sample_rate,
            )
        )
        async for frame in self._engine.synthesize(text):
            if self._ws.client_state != WebSocketState.CONNECTED:
                return
            await self._ws.send_bytes(frame)

        await self._send(AudioEndMessage(chunk_id=chunk_id))

    # ── helpers ───────────────────────────────────────────────────────────────

    async def _send(self, msg: Any) -> None:
        if self._ws.client_state == WebSocketState.CONNECTED:
            await self._ws.send_text(serialize_server_message(msg))
