import asyncio
import logging
import uuid
from typing import Any

from fastapi.websockets import WebSocketState
from pydantic import ValidationError
from starlette.websockets import WebSocket, WebSocketDisconnect

from src.auth.interface import IAuthProvider
from src.core.logger import get_logger
from src.tts.interface import ITTSEngine
from src.tts.normalizer import INormalizer
from .accumulator import TokenAccumulator
from .protocol import (
    AudioEndMessage,
    AudioStartMessage,
    AuthErrorMessage,
    AuthOkMessage,
    ChunkAbortedMessage,
    DoneMessage,
    EndMessage,
    ErrorMessage,
    FlushMessage,
    TokenMessage,
    parse_client_message,
    serialize_server_message,
)

_base_logger = get_logger(__name__)

# Sentinel pushed into the queue to signal end-of-stream
_SENTINEL = object()


class _SidAdapter(logging.LoggerAdapter):
    def process(self, msg: str, kwargs: Any) -> tuple[str, Any]:
        return f"[sid={self.extra['sid']}] {msg}", kwargs


class SpeakerSession:
    def __init__(
        self,
        ws: WebSocket,
        engine: ITTSEngine,
        auth: IAuthProvider,
        normalizer: INormalizer,
        min_flush_chars: int = 80,
        queue_maxsize: int = 100,
        session_timeout: float = 300.0,
    ) -> None:
        self._ws = ws
        self._engine = engine
        self._auth = auth
        self._normalizer = normalizer
        self._accumulator = TokenAccumulator(min_flush_chars=min_flush_chars)
        self._queue: asyncio.Queue[str | object] = asyncio.Queue(maxsize=queue_maxsize)
        self._chunk_counter = 0
        self._session_timeout = session_timeout
        self._tts_task: asyncio.Task | None = None
        self._id = uuid.uuid4().hex[:8]
        self._log = _SidAdapter(_base_logger, {"sid": self._id})

    # ── public entry point ────────────────────────────────────────────────────

    async def run(self) -> None:
        await self._ws.accept()
        self._log.info("Session started")
        if not await self._authenticate():
            return
        self._tts_task = asyncio.create_task(self._tts_worker())
        try:
            await asyncio.wait_for(self._receive_loop(), timeout=self._session_timeout)
        except asyncio.TimeoutError:
            self._log.warning("Session timeout after %.0fs", self._session_timeout)
            await self._send(ErrorMessage(code="session_timeout", message="Session timed out"))
            if self._tts_task and not self._tts_task.done():
                self._tts_task.cancel()
        finally:
            if self._tts_task is not None:
                try:
                    await self._tts_task
                except asyncio.CancelledError:
                    pass
                except Exception as exc:
                    self._log.warning("TTS worker ended with error: %s", exc)
            try:
                await self._engine.aclose()
            except Exception as exc:
                self._log.warning("Engine aclose failed: %s", exc)
        self._log.info("Session ended")

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
            self._log.error("Auth provider error: %s", exc)
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
                    for seg in self._accumulator.add(msg.text):
                        if not await self._put(seg):
                            return

                elif isinstance(msg, FlushMessage):
                    for seg in self._accumulator.flush():
                        if not await self._put(seg):
                            return

                elif isinstance(msg, EndMessage):
                    for seg in self._accumulator.flush():
                        if not await self._put(seg):
                            return
                    await self._queue.put(_SENTINEL)
                    return

        except Exception as exc:
            self._log.warning("Receive loop ended: %s", exc)
            await self._queue.put(_SENTINEL)

    # ── TTS worker ────────────────────────────────────────────────────────────

    async def _tts_worker(self) -> None:
        try:
            while True:
                item = await self._queue.get()
                if item is _SENTINEL:
                    break
                assert isinstance(item, str)
                try:
                    await self._synthesize_segment(item)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self._log.error("Synthesis failed: %s", exc, exc_info=True)
                    await self._send(
                        ErrorMessage(
                            code="synthesis_error",
                            message=f"TTS engine error: {exc}",
                        )
                    )
                    break
        finally:
            if self._ws.client_state == WebSocketState.CONNECTED:
                try:
                    await self._send(DoneMessage())
                except Exception:
                    pass

    async def _synthesize_segment(self, text: str) -> None:
        chunk_id = self._chunk_counter
        self._chunk_counter += 1
        await self._send(
            AudioStartMessage(
                chunk_id=chunk_id,
                sample_rate=self._engine.sample_rate,
            )
        )
        # Normalize BEFORE synthesis (best-effort: never raises).
        normalized = await self._normalizer.normalize(text)
        try:
            async for frame in self._engine.synthesize(normalized):
                try:
                    await self._ws.send_bytes(frame)
                except WebSocketDisconnect:
                    await self._send(ChunkAbortedMessage(chunk_id=chunk_id))
                    return
        except WebSocketDisconnect:
            await self._send(ChunkAbortedMessage(chunk_id=chunk_id))
            return

        await self._send(AudioEndMessage(chunk_id=chunk_id))

    # ── helpers ───────────────────────────────────────────────────────────────

    async def _put(self, seg: str) -> bool:
        """Enqueue a segment. Returns False and aborts session if queue is full."""
        try:
            self._queue.put_nowait(seg)
            return True
        except asyncio.QueueFull:
            self._log.error("Synthesis queue full — aborting session")
            await self._send(ErrorMessage(code="queue_full", message="Synthesis queue full"))
            if self._tts_task and not self._tts_task.done():
                self._tts_task.cancel()
                try:
                    await self._tts_task
                except (asyncio.CancelledError, Exception):
                    pass
            return False

    async def _send(self, msg: Any) -> None:
        if self._ws.client_state == WebSocketState.CONNECTED:
            await self._ws.send_text(serialize_server_message(msg))
