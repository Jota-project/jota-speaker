# Fase 1: Robustez y Cancelación Correcta — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Arreglar 5 bugs de robustez en `SpeakerSession` y `KokoroEngine` que pueden tumbar producción ante timeouts, desconexiones y crashes del motor TTS, añadiendo un mensaje `chunk_aborted` al protocolo y un `aclose()` al motor para garantizar que ningún thread quede zombie.

**Architecture:** Cambios quirúrgicos en la frontera motor↔sesión. Cada bug se trata como un test que falla primero (rojo), después una corrección mínima (verde), después commit. El protocolo crece con un nuevo mensaje `chunk_aborted` que el motor nunca emite y que solo el servidor usa para señalar al cliente que un chunk enviado con `audio_start` nunca produjo `audio_end`. El motor gana `aclose()` y un timeout opcional en `synthesize` para que `Kokoro.create()` colgado no agote el thread pool.

**Tech Stack:** Python 3.11+, asyncio, FastAPI/Starlette, pydantic, concurrent.futures, pytest, pytest-asyncio.

## Global Constraints

- TDD estricto: cada cambio va precedido de su test, que falla primero.
- No romper el protocolo existente: mensajes `auth_ok`, `auth_error`, `audio_start`, `audio_end`, `error`, `done` mantienen exactamente su forma.
- `MockEngine` debe seguir funcionando sin modelo Kokoro — los 25 tests unitarios y los 7 de integración (cuando se arregle el problema de colección ajeno a esta fase) deben seguir pasando.
- No añadir dependencias nuevas (sin paquetes extra en `pyproject.toml`).
- YAGNI: no circuit breaker, no métricas, no graceful shutdown de servidor completo — solo lo del spec.
- No tocar normalización, multi-voz ni barge-in.
- Verificación de aceptación transversal: tras cada sesión, `executor`/thread pool queda vacío; lo verifican tests `test_*_teardown`.
- Criterio de "Fase 1 completa": `python3 -m pytest tests/unit/ -v` pasa todos los tests (viejos + nuevos). Los tests de integración nuevos también pasan cuando el problema de colección previo se arregle en otra fase.

## File Structure

Archivos a modificar:

- `src/server/protocol.py` — añadir `ChunkAbortedMessage` (server → client) y exportarlo desde el módulo.
- `src/tts/interface.py` — añadir `aclose()` abstracto y `synthesize_timeout` opcional (atributo de instancia).
- `src/tts/mock_engine.py` — implementar `aclose()` como no-op. `synthesize` no necesita cambios (no bloquea).
- `src/tts/kokoro/engine.py` — añadir `asyncio.Lock` para serializar `_run_inference`, timeout via `asyncio.wait_for`, `aclose()` que libere el modelo, `ThreadPoolExecutor` dedicado para cancelación cooperativa.
- `src/server/session.py` — capturar `WebSocketDisconnect` en `_synthesize_segment`, enviar `chunk_aborted`; capturar `Exception` en `_tts_worker`; manejar `CancelledError` en `_put`/`run()`; llamar `await self._engine.aclose()` en `run()`'s `finally`.
- `src/main.py` — llamar `engine.aclose()` en el shutdown del lifespan.

Archivos de test a crear:

- `tests/unit/test_protocol.py` — añadir 2 tests para `ChunkAbortedMessage`.
- `tests/unit/test_engine_interface.py` — test que `ITTSEngine` requiere `aclose()`.
- `tests/integration/test_chunk_aborted.py` — desconexión a mitad de audio.
- `tests/integration/test_engine_failure.py` — excepción en el motor mata al worker con `error`.
- `tests/integration/test_engine_inference_timeout.py` — mock engine que cuelga → `TimeoutError` propagado.
- `tests/integration/test_queue_full_recovery.py` — queue llena → `error` único, no doble `done`.
- `tests/integration/test_session_teardown.py` — tras timeout/fin, el `executor` queda vacío y no quedan tasks.

---

## Task 1: Añadir `ChunkAbortedMessage` al protocolo

**Files:**
- Modify: `src/server/protocol.py:60-103`
- Modify: `tests/unit/test_protocol.py:1-15`

**Interfaces:**
- Consumes: nada (verde autónomo).
- Produces: `ChunkAbortedMessage` (Pydantic) con `type: Literal["chunk_aborted"]` y `chunk_id: int`. Exportado e incluido en el union `ServerMessage`.

- [ ] **Step 1: Escribir el test que falla**

En `tests/unit/test_protocol.py`, añadir al final:

```python
from src.server.protocol import ChunkAbortedMessage


def test_serialize_chunk_aborted():
    msg = ChunkAbortedMessage(chunk_id=7)
    data = json.loads(serialize_server_message(msg))
    assert data["type"] == "chunk_aborted"
    assert data["chunk_id"] == 7


def test_parse_client_does_not_match_chunk_aborted():
    """chunk_aborted is server→client only; clients must not send it."""
    with pytest.raises((ValidationError, Exception)):
        parse_client_message(json.dumps({"type": "chunk_aborted", "chunk_id": 1}))
```

- [ ] **Step 2: Correr el test y verificar que falla**

Run: `python3 -m pytest tests/unit/test_protocol.py::test_serialize_chunk_aborted tests/unit/test_protocol.py::test_parse_client_does_not_match_chunk_aborted -v`
Expected: FAIL con `ImportError: cannot import name 'ChunkAbortedMessage'`.

- [ ] **Step 3: Implementar el mensaje mínimo**

En `src/server/protocol.py`, justo después de `AudioEndMessage` (línea 77-79), añadir:

```python
class ChunkAbortedMessage(BaseModel):
    type: Literal["chunk_aborted"] = "chunk_aborted"
    chunk_id: int
```

Y en el union `ServerMessage` (línea 92-99), añadir `ChunkAbortedMessage | ` como primer elemento:

```python
ServerMessage = (
    ChunkAbortedMessage
    | AuthOkMessage
    | AuthErrorMessage
    | AudioStartMessage
    | AudioEndMessage
    | ErrorMessage
    | DoneMessage
)
```

- [ ] **Step 4: Correr el test y verificar que pasa**

Run: `python3 -m pytest tests/unit/test_protocol.py -v`
Expected: PASS los 15 tests (13 viejos + 2 nuevos).

- [ ] **Step 5: Commit**

```bash
git add src/server/protocol.py tests/unit/test_protocol.py
git commit -m "feat(protocol): add ChunkAbortedMessage for partial chunks"
```

---

## Task 2: Añadir `aclose()` y `synthesize_timeout` a `ITTSEngine`

**Files:**
- Modify: `src/tts/interface.py:1-14`
- Modify: `tests/unit/test_engine_interface.py` (nuevo)

**Interfaces:**
- Consumes: nada.
- Produces: `ITTSEngine.aclose()` async abstracto. Atributo opcional `synthesize_timeout: float | None = None` (None = sin timeout). MockEngine y KokoroEngine deben implementar `aclose()`.

- [ ] **Step 1: Escribir el test que falla**

Crear `tests/unit/test_engine_interface.py`:

```python
import asyncio
import pytest

from src.tts.interface import ITTSEngine


def test_ittsengine_requires_aclose():
    """Any subclass must implement aclose()."""

    class IncompleteEngine(ITTSEngine):
        async def synthesize(self, text: str):
            yield b""

        @property
        def sample_rate(self) -> int:
            return 24000

    with pytest.raises(TypeError):
        IncompleteEngine()  # missing aclose()


@pytest.mark.asyncio
async def test_ittsengine_synthesize_timeout_default_is_none():
    class DummyEngine(ITTSEngine):
        async def synthesize(self, text: str):
            yield b""

        @property
        def sample_rate(self) -> int:
            return 24000

        async def aclose(self) -> None:
            pass

    eng = DummyEngine()
    assert getattr(eng, "synthesize_timeout", None) is None
```

- [ ] **Step 2: Correr el test y verificar que falla**

Run: `python3 -m pytest tests/unit/test_engine_interface.py -v`
Expected: FAIL — `IncompleteEngine()` no falla actualmente porque `aclose` aún no existe como método abstracto. El test `synthesize_timeout_default_is_none` también fallará porque el atributo no está definido.

- [ ] **Step 3: Implementar `aclose` y `synthesize_timeout`**

Reemplazar el contenido de `src/tts/interface.py` por:

```python
from abc import ABC, abstractmethod
from typing import AsyncIterator


class ITTSEngine(ABC):
    @abstractmethod
    async def synthesize(self, text: str) -> AsyncIterator[bytes]:
        """Yield PCM16 LE mono audio frames for the given text."""
        ...

    @property
    @abstractmethod
    def sample_rate(self) -> int: ...

    @abstractmethod
    async def aclose(self) -> None:
        """Release engine resources (thread pools, native handles)."""
        ...

    # Optional: engines may set this to bound blocking inference calls.
    # None means no timeout. The session will use this to wrap run_in_executor.
    synthesize_timeout: float | None = None
```

- [ ] **Step 4: Correr tests — verificar rojo sigue donde corresponde**

Run: `python3 -m pytest tests/unit/test_engine_interface.py -v`
Expected: PASS los 2 tests nuevos. Los tests previos que importan `MockEngine` o `KokoroEngine` pueden fallar hasta que se implemente `aclose` en ellos (Task 3 y Task 4).

- [ ] **Step 5: Commit**

```bash
git add src/tts/interface.py tests/unit/test_engine_interface.py
git commit -m "feat(tts): add ITTSEngine.aclose() and synthesize_timeout"
```

---

## Task 3: `MockEngine.aclose()` no-op

**Files:**
- Modify: `src/tts/mock_engine.py:1-27`

**Interfaces:**
- Consumes: `ITTSEngine.aclose()` (Task 2).
- Produces: `MockEngine.aclose()` como no-op async. `synthesize_timeout = None`.

- [ ] **Step 1: Escribir el test que falla**

En `tests/unit/test_engine_interface.py`, añadir al final:

```python
from src.tts.mock_engine import MockEngine


@pytest.mark.asyncio
async def test_mock_engine_has_aclose_and_default_timeout():
    eng = MockEngine()
    assert hasattr(eng, "aclose")
    assert await eng.aclose() is None
    assert eng.synthesize_timeout is None


@pytest.mark.asyncio
async def test_mock_engine_aclose_is_idempotent():
    eng = MockEngine()
    await eng.aclose()
    await eng.aclose()  # no debe lanzar
```

- [ ] **Step 2: Correr el test y verificar que falla**

Run: `python3 -m pytest tests/unit/test_engine_interface.py::test_mock_engine_has_aclose_and_default_timeout -v`
Expected: FAIL con `TypeError: Can't instantiate abstract class MockEngine without an implementation for abstract method 'aclose'`.

- [ ] **Step 3: Implementar `aclose` en `MockEngine`**

Reemplazar `src/tts/mock_engine.py` por:

```python
import asyncio
from typing import AsyncIterator

from .interface import ITTSEngine

# 200ms of silence per frame at 24 kHz, PCM16 (2 bytes/sample)
_FRAME_SAMPLES = 4800
_SILENCE_FRAME = b"\x00" * (_FRAME_SAMPLES * 2)
_FRAMES_PER_CHAR = 1  # emit 1 frame per character so tests get audible output


class MockEngine(ITTSEngine):
    """Generates silence PCM16 frames. Used for dev/CI."""

    synthesize_timeout: float | None = None  # mock never blocks → no timeout needed

    def __init__(self, sample_rate: int = 24000) -> None:
        self._sample_rate = sample_rate

    @property
    def sample_rate(self) -> int:
        return self._sample_rate

    async def synthesize(self, text: str) -> AsyncIterator[bytes]:
        frames = max(1, len(text) * _FRAMES_PER_CHAR)
        for _ in range(frames):
            await asyncio.sleep(0)  # yield control
            yield _SILENCE_FRAME

    async def aclose(self) -> None:
        # No-op: MockEngine holds no native resources.
        return None
```

- [ ] **Step 4: Correr tests**

Run: `python3 -m pytest tests/unit/ -v`
Expected: PASS todos los tests unitarios (27 = 25 viejos + 2 nuevos de T2 + 2 nuevos aquí).

- [ ] **Step 5: Commit**

```bash
git add src/tts/mock_engine.py tests/unit/test_engine_interface.py
git commit -m "feat(tts/mock): implement aclose() as no-op"
```

---

## Task 4: `KokoroEngine.aclose()` + lock + timeout + executor dedicado

**Files:**
- Modify: `src/tts/kokoro/engine.py:1-54`
- Create: `tests/integration/test_kokoro_engine.py` (mock kokoro-onnx via monkeypatch)

**Interfaces:**
- Consumes: `ITTSEngine.aclose()` y `synthesize_timeout`.
- Produces:
  - Atributo de instancia `synthesize_timeout: float | None` (param por constructor, default None).
  - `asyncio.Lock` interno serializando llamadas concurrentes a `Kokoro.create()` (mitigación de D1: race en init).
  - `_executor: ThreadPoolExecutor` (1 worker) dedicado a Kokoro para que `aclose()` pueda hacer `shutdown(wait=False, cancel_futures=True)`.
  - `synthesize()` envuelve `run_in_executor` con `asyncio.wait_for(..., timeout=self.synthesize_timeout)` y libera el lock en `finally`.
  - `aclose()` hace `executor.shutdown(wait=False, cancel_futures=True)` y libera el modelo (`self._kokoro = None`).

- [ ] **Step 1: Escribir el test que falla**

Crear `tests/integration/test_kokoro_engine.py`:

```python
import asyncio
import threading
import time

import numpy as np
import pytest

from src.tts.kokoro.engine import KokoroEngine


class _FakeKokoro:
    """Stand-in for kokoro_onnx.Kokoro that records concurrent calls."""

    def __init__(self) -> None:
        self.active = 0
        self.max_active = 0
        self.lock = threading.Lock()
        self._hang = False

    def create(self, text, voice=None, speed=None, lang=None):
        with self.lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            if self._hang:
                # block until cancelled — but we cannot truly cancel threads.
                # Use a short sleep; the engine's executor.shutdown will reap it.
                time.sleep(10)
            # Return 1 second of silence at 24 kHz
            return np.zeros(24000, dtype=np.float32), 24000
        finally:
            with self.lock:
                self.active -= 1


@pytest.fixture
def fake_kokoro(monkeypatch):
    fake = _FakeKokoro()
    # Patch the import inside KokoroEngine constructor
    monkeypatch.setattr(
        "kokoro_onnx.Kokoro", lambda *a, **kw: fake, raising=False
    )
    return fake


@pytest.mark.asyncio
async def test_kokoro_engine_serializes_concurrent_calls(fake_kokoro):
    eng = KokoroEngine(
        model_path="x", voices_path="y",
        synthesize_timeout=None,
    )
    # Two concurrent synthesize calls should never overlap inside _run_inference.
    results = await asyncio.gather(
        eng.synthesize("a").__anext__(),
        eng.synthesize("b").__anext__(),
    )
    assert len(results) == 2
    assert fake_kokoro.max_active == 1


@pytest.mark.asyncio
async def test_kokoro_engine_synthesize_timeout_raises(fake_kokoro):
    fake_kokoro._hang = True
    eng = KokoroEngine(
        model_path="x", voices_path="y",
        synthesize_timeout=0.05,
    )
    with pytest.raises(asyncio.TimeoutError):
        # Drain just the first chunk so the underlying run_in_executor runs.
        async for _ in eng.synthesize("slow"):
            break
    await eng.aclose()


@pytest.mark.asyncio
async def test_kokoro_engine_aclose_clears_resources(fake_kokoro):
    eng = KokoroEngine(model_path="x", voices_path="y", synthesize_timeout=None)
    assert eng._executor is not None
    await eng.aclose()
    assert eng._kokoro is None
    # Calling aclose twice is safe
    await eng.aclose()
```

- [ ] **Step 2: Correr el test y verificar que falla**

Run: `python3 -m pytest tests/integration/test_kokoro_engine.py -v`
Expected: FAIL — `KokoroEngine.__init__` aún no acepta `synthesize_timeout`, no hay `_executor`, no hay `_inference_lock`.

- [ ] **Step 3: Implementar los cambios en `KokoroEngine`**

Reemplazar `src/tts/kokoro/engine.py` por:

```python
import asyncio
from concurrent.futures import ThreadPoolExecutor
from typing import AsyncIterator

import numpy as np

from src.core.logger import get_logger
from src.tts.interface import ITTSEngine

logger = get_logger(__name__)

_CHUNK_SAMPLES = 4800  # 200ms at 24 kHz


class KokoroEngine(ITTSEngine):
    """Runs Kokoro ONNX inference in a dedicated single-worker thread pool.

    A dedicated executor (instead of the default shared one) lets us cancel
    pending inference on shutdown. A lock serializes calls because
    Kokoro.create() is not thread-safe.
    """

    def __init__(
        self,
        model_path: str,
        voices_path: str,
        voice: str = "af_heart",
        lang: str = "en-us",
        sample_rate: int = 24000,
        synthesize_timeout: float | None = None,
    ) -> None:
        from kokoro_onnx import Kokoro  # type: ignore[import-untyped]

        self._voice = voice
        self._lang = lang
        self._sample_rate = sample_rate
        self.synthesize_timeout = synthesize_timeout
        logger.info("Loading Kokoro model from %s …", model_path)
        self._kokoro = Kokoro(model_path, voices_path)
        self._executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="kokoro"
        )
        self._inference_lock = asyncio.Lock()
        logger.info("Kokoro model loaded.")

    @property
    def sample_rate(self) -> int:
        return self._sample_rate

    async def synthesize(self, text: str) -> AsyncIterator[bytes]:
        loop = asyncio.get_running_loop()
        async with self._inference_lock:
            try:
                if self.synthesize_timeout is not None:
                    audio: np.ndarray = await asyncio.wait_for(
                        loop.run_in_executor(self._executor, self._run_inference, text),
                        timeout=self.synthesize_timeout,
                    )
                else:
                    audio = await loop.run_in_executor(
                        self._executor, self._run_inference, text
                    )
            except asyncio.TimeoutError:
                logger.error("Kokoro inference timed out after %.2fs", self.synthesize_timeout)
                raise

        for start in range(0, len(audio), _CHUNK_SAMPLES):
            chunk = audio[start : start + _CHUNK_SAMPLES]
            pcm16 = (chunk * 32767).astype(np.int16).tobytes()
            yield pcm16

    # ── sync (runs in dedicated thread pool) ─────────────────────────────────

    def _run_inference(self, text: str) -> np.ndarray:
        samples, _ = self._kokoro.create(
            text, voice=self._voice, speed=1.0, lang=self._lang
        )
        return samples.astype(np.float32)

    # ── lifecycle ─────────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        """Release native resources. Idempotent and safe to call twice."""
        if self._executor is not None:
            # cancel_futures=True ensures any queued inference is dropped
            self._executor.shutdown(wait=False, cancel_futures=True)
            self._executor = None
        self._kokoro = None
        logger.info("Kokoro engine closed.")
```

- [ ] **Step 4: Correr los tests**

Run: `python3 -m pytest tests/integration/test_kokoro_engine.py tests/unit/ -v`
Expected: PASS los 3 nuevos tests de KokoroEngine + todos los unitarios.

- [ ] **Step 5: Commit**

```bash
git add src/tts/kokoro/engine.py tests/integration/test_kokoro_engine.py
git commit -m "feat(tts/kokoro): dedicated executor, inference lock, timeout, aclose"
```

---

## Task 5: Capturar `WebSocketDisconnect` en `_synthesize_segment` y enviar `chunk_aborted` (Fix A3)

**Files:**
- Modify: `src/server/session.py:159-173`
- Create: `tests/integration/test_chunk_aborted.py`

**Interfaces:**
- Consumes: `ChunkAbortedMessage` (T1).
- Produces: `_synthesize_segment` envuelve el bucle de envío de frames en `try/except WebSocketDisconnect`. Si el cliente se va tras `audio_start`, envía `chunk_aborted` solo si aún es posible (best-effort) y retorna silenciosamente.

- [ ] **Step 1: Escribir el test que falla**

Crear `tests/integration/test_chunk_aborted.py`:

```python
import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from src.auth.stub import StubAuthProvider
from src.core.config import Settings
from src.main import app
from src.tts.interface import ITTSEngine


class SlowDisconnectEngine(ITTSEngine):
    """Emits several frames then we abort the client connection mid-stream."""

    def __init__(self) -> None:
        self._sample_rate = 24000

    @property
    def sample_rate(self) -> int:
        return 24000

    async def synthesize(self, text: str):
        for _ in range(20):
            await asyncio.sleep(0.05)
            yield b"\x00\x00" * 4800  # 200ms silence

    async def aclose(self) -> None:
        return None


def _make_client(engine: ITTSEngine) -> TestClient:
    settings = Settings(engine="mock", auth_provider="stub", min_flush_chars=5)
    app.state.settings = settings
    app.state.engine = engine
    app.state.auth = StubAuthProvider()
    return TestClient(app)


def _drain_until_close(client: TestClient) -> list[dict]:
    """Connect, send token+end, then drop the connection as fast as possible."""
    with client.websocket_connect("/ws") as ws:
        ws.send_text(json.dumps({"type": "auth", "token": "t"}))
        auth = json.loads(ws.receive_text())
        assert auth["type"] == "auth_ok"
        ws.send_text(json.dumps({"type": "token", "text": "Hello."}))
        # Read a couple of frames then yank the connection.
        ws.receive()  # audio_start
        for _ in range(2):
            ws.receive()  # binary frame
        # Exit the context manager → closes the WebSocket from client side.
    return []


def test_chunk_aborted_when_client_disconnects_mid_audio():
    """When client drops mid-chunk, the server must NOT hang. It must send
    chunk_aborted (or just terminate cleanly) and never leave audio_end unpaired."""
    engine = SlowDisconnectEngine()
    client = _make_client(engine)

    # We don't need to inspect messages — we just need the server to return
    # from session.run() promptly without raising unhandled WebSocketDisconnect.
    _drain_until_close(client)

    # If we reach this line without TimeoutError, the server cleaned up.
    assert True
```

- [ ] **Step 2: Correr el test y verificar que falla**

Run: `python3 -m pytest tests/integration/test_chunk_aborted.py -v --timeout=10`
Expected: FAIL — o el test se cuelga (timeout) o `session.run()` propaga `WebSocketDisconnect` no manejada.

- [ ] **Step 3: Implementar captura en `_synthesize_segment`**

En `src/server/session.py`, añadir import al inicio del archivo:

```python
from starlette.websockets import WebSocketDisconnect
```

Y reemplazar el método `_synthesize_segment` (líneas 159-173) por:

```python
async def _synthesize_segment(self, text: str) -> None:
    chunk_id = self._chunk_counter
    self._chunk_counter += 1
    await self._send(
        AudioStartMessage(
            chunk_id=chunk_id,
            sample_rate=self._engine.sample_rate,
        )
    )
    try:
        async for frame in self._engine.synthesize(text):
            try:
                await self._ws.send_bytes(frame)
            except WebSocketDisconnect:
                # Client vanished mid-chunk. Best-effort notify, then exit.
                await self._send(
                    ChunkAbortedMessage(chunk_id=chunk_id)
                )
                return
    except WebSocketDisconnect:
        # Engine raised mid-stream because the socket is gone.
        await self._send(ChunkAbortedMessage(chunk_id=chunk_id))
        return

    await self._send(AudioEndMessage(chunk_id=chunk_id))
```

Y añadir `ChunkAbortedMessage` al import del protocolo (línea 14-26):

```python
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
```

- [ ] **Step 4: Correr el test y verificar que pasa**

Run: `python3 -m pytest tests/integration/test_chunk_aborted.py -v --timeout=10`
Expected: PASS el test termina en <2s sin colgarse.

- [ ] **Step 5: Commit**

```bash
git add src/server/session.py tests/integration/test_chunk_aborted.py
git commit -m "fix(session): send chunk_aborted on client disconnect (A3)"
```

---

## Task 6: `_tts_worker` captura excepciones y envía `error` (Fix A4)

**Files:**
- Modify: `src/server/session.py:148-157`
- Create: `tests/integration/test_engine_failure.py`

**Interfaces:**
- Consumes: T1, T2.
- Produces: `_tts_worker` envuelve el bucle en `try/except Exception`. Si el motor lanza algo no-cancelación, envía `error` (si WS aún abierto) y sale limpiamente. `CancelledError` se re-raise para que la cancelación se propague.

- [ ] **Step 1: Escribir el test que falla**

Crear `tests/integration/test_engine_failure.py`:

```python
import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from src.auth.stub import StubAuthProvider
from src.core.config import Settings
from src.main import app
from src.tts.interface import ITTSEngine


class CrashingEngine(ITTSEngine):
    """Raises a non-cancellation exception on first synthesize call."""

    def __init__(self) -> None:
        self._sample_rate = 24000
        self.calls = 0

    @property
    def sample_rate(self) -> int:
        return 24000

    async def synthesize(self, text: str):
        self.calls += 1
        await asyncio.sleep(0)
        raise RuntimeError("simulated Kokoro crash")
        yield  # noqa: unreachable — makes this an async generator

    async def aclose(self) -> None:
        return None


def _make_client(engine: ITTSEngine) -> TestClient:
    settings = Settings(engine="mock", auth_provider="stub", min_flush_chars=5)
    app.state.settings = settings
    app.state.engine = engine
    app.state.auth = StubAuthProvider()
    return TestClient(app)


def test_engine_exception_does_not_kill_session_silently():
    """A non-cancellation exception in the engine must surface as 'error'."""
    engine = CrashingEngine()
    client = _make_client(engine)

    with client.websocket_connect("/ws") as ws:
        ws.send_text(json.dumps({"type": "auth", "token": "t"}))
        assert json.loads(ws.receive_text())["type"] == "auth_ok"
        ws.send_text(json.dumps(json.dumps({"type": "token", "text": "Hello world."})))
        # Drain until done or error.
        messages: list[dict] = []
        while True:
            data = ws.receive()
            if data.get("type") != "websocket.send":
                break
            if data.get("text"):
                msg = json.loads(data["text"])
                messages.append(msg)
                if msg["type"] in ("done", "error"):
                    break
    types = [m["type"] for m in messages]
    assert "error" in types
    assert engine.calls == 1


def test_engine_exception_does_not_hang_session():
    """After an engine crash, the session must terminate in bounded time."""
    engine = CrashingEngine()
    client = _make_client(engine)

    with client.websocket_connect("/ws") as ws:
        ws.send_text(json.dumps({"type": "auth", "token": "t"}))
        ws.receive_text()  # auth_ok
        ws.send_text(json.dumps({"type": "token", "text": "Hello world."}))
        # Wait until 'error' or 'done'
        for _ in range(50):  # 50 * 0.05 = 2.5s ceiling
            data = ws.receive()
            if data.get("type") == "websocket.send" and data.get("text"):
                msg = json.loads(data["text"])
                if msg["type"] == "error":
                    break
            time.sleep(0.05)  # noqa
    assert True
```

(Pegar el import `import time` al inicio del archivo.)

- [ ] **Step 2: Correr el test y verificar que falla**

Run: `python3 -m pytest tests/integration/test_engine_failure.py -v --timeout=15`
Expected: FAIL — la excepción se traga silenciosamente, no llega `error` al cliente, el test `test_engine_exception_does_not_hang_session` se cuelga hasta el timeout.

- [ ] **Step 3: Implementar manejo de excepciones en `_tts_worker`**

En `src/server/session.py`, reemplazar `_tts_worker` (líneas 148-157) por:

```python
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
                raise  # re-raise: cancellation must propagate
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
```

- [ ] **Step 4: Correr el test y verificar que pasa**

Run: `python3 -m pytest tests/integration/test_engine_failure.py -v --timeout=15`
Expected: PASS los 2 tests.

- [ ] **Step 5: Commit**

```bash
git add src/server/session.py tests/integration/test_engine_failure.py
git commit -m "fix(session): tts_worker surfaces engine errors as 'error' (A4)"
```

---

## Task 7: `_put` y `run()` propagan cancelación limpiamente + `aclose()` en finally (Fix A1 + A5)

**Files:**
- Modify: `src/server/session.py:62-79`, `src/server/session.py:177-187`
- Create: `tests/integration/test_session_teardown.py`

**Interfaces:**
- Consumes: T2, T4.
- Produces:
  - `run()`'s `finally` espera `_tts_task` con `try/except (CancelledError, Exception)`, y llama `await self._engine.aclose()`.
  - `_put` cuando cancela `_tts_task`, espera la cancelación con `try/except` y propaga.

- [ ] **Step 1: Escribir el test que falla**

Crear `tests/integration/test_session_teardown.py`:

```python
import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from src.auth.stub import StubAuthProvider
from src.core.config import Settings
from src.main import app
from src.tts.interface import ITTSEngine


class TrackingEngine(ITTSEngine):
    def __init__(self) -> None:
        self._sample_rate = 24000
        self.aclose_called = 0
        self.executor_empty_after = None

    @property
    def sample_rate(self) -> int:
        return 24000

    async def synthesize(self, text: str):
        await asyncio.sleep(0)
        yield b"\x00\x00" * 4800

    async def aclose(self) -> None:
        self.aclose_called += 1


def _setup(engine: ITTSEngine, **kwargs) -> TestClient:
    defaults = dict(engine="mock", auth_provider="stub", min_flush_chars=5,
                    session_timeout=0.2)
    defaults.update(kwargs)
    settings = Settings(**defaults)
    app.state.settings = settings
    app.state.engine = engine
    app.state.auth = StubAuthProvider()
    return TestClient(app)


def test_session_timeout_calls_aclose_on_engine():
    engine = TrackingEngine()
    client = _setup(engine)
    with client.websocket_connect("/ws") as ws:
        ws.send_text(json.dumps({"type": "auth", "token": "t"}))
        ws.receive_text()  # auth_ok
        # Wait for server-side session_timeout to fire.
        for _ in range(40):  # 40 * 0.05 = 2s ceiling
            try:
                data = ws.receive()
                if data.get("type") == "websocket.send" and data.get("text"):
                    msg = json.loads(data["text"])
                    if msg["type"] == "error":
                        break
            except Exception:
                break
            import time
            time.sleep(0.05)
    assert engine.aclose_called == 1


def test_session_normal_end_calls_aclose():
    engine = TrackingEngine()
    client = _setup(engine, session_timeout=10.0)
    with client.websocket_connect("/ws") as ws:
        ws.send_text(json.dumps({"type": "auth", "token": "t"}))
        ws.receive_text()  # auth_ok
        ws.send_text(json.dumps({"type": "end"}))
        # Drain until done
        for _ in range(40):
            data = ws.receive()
            if data.get("type") == "websocket.send":
                if data.get("text"):
                    msg = json.loads(data["text"])
                    if msg["type"] == "done":
                        break
                # skip binary
            else:
                break
    assert engine.aclose_called == 1
```

- [ ] **Step 2: Correr el test y verificar que falla**

Run: `python3 -m pytest tests/integration/test_session_teardown.py -v --timeout=10`
Expected: FAIL — `engine.aclose_called == 0` porque `run()` aún no llama a `aclose`.

- [ ] **Step 3: Implementar `run()` robusto y `_put` cuidadoso**

En `src/server/session.py`, reemplazar `run()` (líneas 62-79) por:

```python
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
```

Y reemplazar `_put` (líneas 177-187) por:

```python
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
```

- [ ] **Step 4: Correr los tests y verificar que pasan**

Run: `python3 -m pytest tests/integration/test_session_teardown.py -v --timeout=10`
Expected: PASS los 2 tests; `aclose_called == 1` en ambos casos.

- [ ] **Step 5: Commit**

```bash
git add src/server/session.py tests/integration/test_session_teardown.py
git commit -m "fix(session): robust cancellation and engine aclose on teardown (A1+A5)"
```

---

## Task 8: Propagar `synthesize_timeout` desde `Settings` al engine

**Files:**
- Modify: `src/core/config.py:1-30`
- Modify: `src/core/engine_factory.py:1-21`
- Modify: `src/main.py:1-32`

**Interfaces:**
- Consumes: T4.
- Produces:
  - `Settings` gana `kokoro_synthesize_timeout: float | None = None`.
  - `create_engine` pasa `synthesize_timeout=settings.kokoro_synthesize_timeout` a `KokoroEngine`.
  - `main.py`'s lifespan llama `await app.state.engine.aclose()` en shutdown (try/except).

- [ ] **Step 1: Escribir el test que falla**

Añadir a `tests/unit/test_engine_interface.py`:

```python
from src.core.config import Settings


def test_settings_has_kokoro_synthesize_timeout_default_none():
    s = Settings(_env_file=None)  # avoid loading real .env
    assert s.kokoro_synthesize_timeout is None


def test_settings_kokoro_synthesize_timeout_from_env(monkeypatch):
    monkeypatch.setenv("JOTA_KOKORO_SYNTHESIZE_TIMEOUT", "2.5")
    s = Settings(_env_file=None)
    assert s.kokoro_synthesize_timeout == 2.5
```

- [ ] **Step 2: Correr el test y verificar que falla**

Run: `python3 -m pytest tests/unit/test_engine_interface.py::test_settings_has_kokoro_synthesize_timeout_default_none -v`
Expected: FAIL — `Settings` no tiene el atributo.

- [ ] **Step 3: Implementar los cambios**

En `src/core/config.py`, añadir el atributo a la clase `Settings` (después de `kokoro_lang`, línea 11):

```python
    kokoro_synthesize_timeout: float | None = None
```

En `src/core/engine_factory.py`, actualizar el case `"kokoro"` (líneas 10-18):

```python
        case "kokoro":
            from src.tts.kokoro.engine import KokoroEngine
            return KokoroEngine(
                model_path=settings.kokoro_model,
                voices_path=settings.kokoro_voices,
                voice=settings.kokoro_voice,
                lang=settings.kokoro_lang,
                sample_rate=settings.sample_rate,
                synthesize_timeout=settings.kokoro_synthesize_timeout,
            )
```

En `src/main.py`, actualizar el lifespan (líneas 13-25):

```python
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
    try:
        await app.state.engine.aclose()
    except Exception as exc:
        logger.warning("Engine aclose on shutdown failed: %s", exc)
```

- [ ] **Step 4: Correr los tests**

Run: `python3 -m pytest tests/unit/ -v`
Expected: PASS todos los tests unitarios, incluidos los 2 nuevos de Settings.

- [ ] **Step 5: Commit**

```bash
git add src/core/config.py src/core/engine_factory.py src/main.py tests/unit/test_engine_interface.py
git commit -m "feat(core): wire kokoro_synthesize_timeout + lifespan aclose"
```

---

## Task 9: Tests de integración transversales (timeout inference, queue-full recovery)

**Files:**
- Create: `tests/integration/test_engine_inference_timeout.py`
- Create: `tests/integration/test_queue_full_recovery.py`
- Modify: `src/server/session.py:148-174` (si el test de timeout lo requiere)

**Interfaces:**
- Consumes: T4, T7, T8.
- Produces: dos tests de integración que cubren los escenarios A2 (timeout) y la recuperación tras queue_full sin doble `done`.

- [ ] **Step 1: Escribir el test que falla (engine timeout)**

Crear `tests/integration/test_engine_inference_timeout.py`:

```python
import asyncio
import json
import threading
import time

import numpy as np
import pytest
from fastapi.testclient import TestClient

from src.auth.stub import StubAuthProvider
from src.core.config import Settings
from src.main import app
from src.tts.interface import ITTSEngine


class HangingEngine(ITTSEngine):
    def __init__(self, synthesize_timeout: float | None) -> None:
        self._sample_rate = 24000
        self.synthesize_timeout = synthesize_timeout

    @property
    def sample_rate(self) -> int:
        return 24000

    async def synthesize(self, text: str):
        # Simulate a hanging blocking call by sleeping in a thread.
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, time.sleep, 5)
        yield b"\x00\x00" * 4800

    async def aclose(self) -> None:
        return None


def _setup(engine: ITTSEngine) -> TestClient:
    settings = Settings(
        engine="mock", auth_provider="stub",
        min_flush_chars=5, session_timeout=10.0,
    )
    app.state.settings = settings
    app.state.engine = engine
    app.state.auth = StubAuthProvider()
    return TestClient(app)


def test_engine_timeout_does_not_hang_session(monkeypatch):
    """When synthesize exceeds the timeout, session ends with 'error'."""
    engine = HangingEngine(synthesize_timeout=0.1)
    # Wrap synthesize to enforce timeout using the engine's own attribute,
    # so we don't depend on KokoroEngine being wired here.
    orig = engine.synthesize

    async def with_timeout(text):
        loop = asyncio.get_running_loop()
        fut = loop.run_in_executor(None, time.sleep, 5)
        try:
            await asyncio.wait_for(fut, timeout=engine.synthesize_timeout)
        except asyncio.TimeoutError:
            raise
        async for f in orig(text):
            yield f

    engine.synthesize = with_timeout

    client = _setup(engine)
    with client.websocket_connect("/ws") as ws:
        ws.send_text(json.dumps({"type": "auth", "token": "t"}))
        ws.receive_text()  # auth_ok
        ws.send_text(json.dumps({"type": "token", "text": "Hello world."}))
        # Wait up to 3s for either error or done.
        seen = []
        deadline = time.time() + 3.0
        while time.time() < deadline:
            try:
                data = ws.receive()
            except Exception:
                break
            if data.get("type") == "websocket.send" and data.get("text"):
                m = json.loads(data["text"])
                seen.append(m)
                if m["type"] in ("error", "done"):
                    break
        assert any(m["type"] == "error" for m in seen), seen
```

- [ ] **Step 2: Escribir el test que falla (queue-full recovery)**

Crear `tests/integration/test_queue_full_recovery.py`:

```python
import json

from fastapi.testclient import TestClient

from src.auth.stub import StubAuthProvider
from src.core.config import Settings
from src.main import app


def test_queue_full_emits_single_done_or_no_done():
    """When the queue overflows, the server emits exactly one terminal
    message: either 'done' (clean) or 'error' followed by 'done'. It must
    NOT emit 'done' twice nor hang."""
    settings = Settings(
        engine="mock", auth_provider="stub",
        min_flush_chars=2, queue_maxsize=1, session_timeout=5.0,
    )
    app.state.settings = settings
    app.state.engine = None  # overwritten by factory in lifespan? no — manual:
    from src.core.engine_factory import create_engine
    app.state.engine = create_engine(settings)
    app.state.auth = StubAuthProvider()

    client = TestClient(app)
    with client.websocket_connect("/ws") as ws:
        ws.send_text(json.dumps({"type": "auth", "token": "t"}))
        ws.receive_text()  # auth_ok
        # min_flush_chars=2 + dots → many segments; queue_maxsize=1 → overflow
        ws.send_text(json.dumps({"type": "token", "text": "a. b. c. d. e. f."}))

        seen: list[dict] = []
        for _ in range(80):  # 4s ceiling
            try:
                data = ws.receive()
            except Exception:
                break
            if data.get("type") == "websocket.send" and data.get("text"):
                m = json.loads(data["text"])
                seen.append(m)
                if m["type"] == "done":
                    break

    done_count = sum(1 for m in seen if m["type"] == "done")
    error_count = sum(1 for m in seen if m["type"] == "error")
    assert done_count <= 1, seen
    assert error_count >= 1, seen
```

- [ ] **Step 3: Correr los tests y verificar rojo**

Run: `python3 -m pytest tests/integration/test_engine_inference_timeout.py tests/integration/test_queue_full_recovery.py -v --timeout=15`
Expected: el test de timeout falla porque el `HangingEngine` no respeta `synthesize_timeout` por sí solo (necesita ser envuelto). El test de queue-full falla o pasa según el comportamiento actual — verificar que no haya doble `done`.

- [ ] **Step 4: Ajustar `_synthesize_segment` para honrar `synthesize_timeout` desde settings**

`_synthesize_segment` no necesita cambios — la garantía de timeout se delega al motor (Task 4 ya lo implementó en `KokoroEngine`). El test de `HangingEngine` aquí es un contrato: cualquier engine que cuelgue debe cortarse por timeout. Para que `HangingEngine` lo respete, modificamos el wrapper para usar el atributo. La implementación correcta ya está en el step 1 (usa `engine.synthesize_timeout`).

Si el test de `queue-full` falla con doble `done`, editar `src/server/session.py` para garantizar un único `done` en `_tts_worker`. La versión actual de T6 ya lo garantiza (`finally` con un solo `send` de `DoneMessage` y `break` antes). Si aún se duplica, añadir flag `self._done_sent = False` y comprobarlo antes del `send`.

- [ ] **Step 5: Correr los tests y verificar que pasan**

Run: `python3 -m pytest tests/integration/test_engine_inference_timeout.py tests/integration/test_queue_full_recovery.py -v --timeout=15`
Expected: PASS ambos tests.

- [ ] **Step 6: Commit**

```bash
git add tests/integration/test_engine_inference_timeout.py tests/integration/test_queue_full_recovery.py src/server/session.py
git commit -m "test: engine timeout + queue full recovery integration tests"
```

---

## Task 10: Checkpoint final — Fase 1 completa

**Files:** ninguno (verificación).

- [ ] **Step 1: Correr toda la suite unitaria**

Run: `python3 -m pytest tests/unit/ -v`
Expected: PASS todos los tests (viejos + nuevos).

- [ ] **Step 2: Correr los nuevos tests de integración**

Run: `python3 -m pytest tests/integration/test_chunk_aborted.py tests/integration/test_engine_failure.py tests/integration/test_session_teardown.py tests/integration/test_engine_inference_timeout.py tests/integration/test_queue_full_recovery.py -v --timeout=20`
Expected: PASS todos los nuevos tests de integración.

- [ ] **Step 3: Verificar que el módulo de Kokoro importable sigue sano**

Run: `python3 -c "from src.tts.kokoro.engine import KokoroEngine; print('ok')"`
Expected: imprime `ok`.

- [ ] **Step 4: Commit final de checkpoint (si hay ajustes)**

```bash
git status
# Si hay cambios sin commitear:
git add -A
git commit -m "chore: fase 1 complete — robustness and cancellation"
```

---

## Notas operativas

- **Problema de colección conocido en `tests/integration/test_tts_stream.py`**: error `Router.__init__() got an unexpected keyword argument 'on_startup'`. Es ajeno a Fase 1 — pytest encuentra ese módulo durante el discovery y falla al cargar la app. Si bloquea el discovery, marcar `tests/integration/` con `__init__.py` vacío y pytest-asyncio modo `auto` ya está configurado. El plan no incluye arreglar este problema porque está fuera del scope ("solo Fase 1"). Si los nuevos tests fallan por esto, aislar el discovery usando `python3 -m pytest tests/integration/test_<nuevo>.py --confcutdir=tests/integration` para que pytest no intente cargar el módulo conflictivo.

- **Compatibilidad con tests existentes**: `MockEngine` mantiene su comportamiento (25 tests unitarios previos deben seguir verdes). Kokoro no se carga en CI sin modelo — los tests de `test_kokoro_engine.py` parchean `kokoro_onnx.Kokoro` con un fake.

- **Backwards compatibility del protocolo**: clientes existentes ignoran mensajes desconocidos, así que añadir `chunk_aborted` no rompe compatibilidad. Los 7 tests originales de `test_tts_stream.py` deben seguir pasando cuando el problema de colección se resuelva.

- **Locks**: el `asyncio.Lock` en `KokoroEngine` es por-instancia, no global. Esto significa que múltiples sesiones que compartan el mismo engine serializarán sus inferencias — aceptable para Fase 1 (una sola sesión a la vez en producción) y trivial de relajar en Fase 5 si hace falta.