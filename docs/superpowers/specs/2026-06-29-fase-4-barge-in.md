# Fase 4 — Barge-in In-Session — Design Spec

**Date:** 2026-06-29
**Branch:** main (post-Fases 1+2+3, con Wyoming integration ya mergeada)
**Status:** Approved by user (block-by-block review)

## Context

jota-speaker es un microservicio TTS streaming para un asistente conversacional en tiempo real. Hoy, cuando el usuario interrumpe al asistente, el cliente solo puede **cerrar el WebSocket y reconectar** — esto añade 100-300 ms de latencia y rompe el estado de la sesión.

El objetivo de esta fase es soportar **barge-in en menos de 100 ms** sin cerrar la sesión: el cliente envía un mensaje `interrupt`, el servidor cancela el chunk en curso, descarta la cola pendiente, y la sesión sigue viva lista para nuevos tokens.

Esta fase se apoya en:
- **Fase 1** (`aclose`, `asyncio.Lock` en Kokoro, executor dedicado) — provee la cancelación cooperativa del motor.
- Mensaje `ChunkAbortedMessage` (Fase 1) — se reutiliza para marcar el corte del audio en vuelo.

## Goal

Soportar barge-in conversacional: el cliente envía `interrupt`, el servidor responde con `chunk_aborted` del chunk cortado y `interrupted` con el chunk_id, mantiene la sesión viva, y queda listo para procesar nuevos tokens sin reconectar.

## Non-goals

- Pause/resume (`interrupt` + `resume`) — no en scope.
- Multiparte barge-in (varios interrupts concurrentes) — el segundo se ignora silenciosamente.
- Cancelación dura del thread de Kokoro — el motor sigue corriendo hasta completar la inferencia (limitación de `run_in_executor`); el cliente descarta el audio que llegue tarde.
- Interrupciones dirigidas a un chunk específico — siempre interrumpe el chunk en curso.
- Métricas de TTFB post-interrupt (Fase 5).

## Architecture

```
Cliente envía interrupt
        ↓ (WebSocket)
SpeakerSession._receive_loop
        ↓ handler InterruptMessage
   ┌─────────────────────────────────────────────────────────┐
   │  1. self._interrupt_lock = True                          │
   │  2. aborted_id = self._current_chunk_id                  │
   │  3. self._tts_task.cancel() (worker + synthesize cancel) │
   │  4. await self._tts_task (propagate)                     │
   │  5. drain queue (count, discard)                          │
   │  6. self._accumulator.flush() (discard unflushed tokens)  │
   │  7. self._tts_task = asyncio.create_task(self._tts_worker) │
   │  8. if aborted_id is not None:                           │
   │       await self._send(ChunkAbortedMessage(chunk_id=…))  │
   │  9. await self._send(InterruptedMessage(chunk_id=aborted_id or 0))│
   │  10. self._interrupt_lock = False                        │
   └─────────────────────────────────────────────────────────┘
        ↓
Cliente puede enviar nuevos `token`s — sesión sigue viva
```

### Components

#### Nuevos

- **Mensaje `InterruptMessage`** (cliente → servidor) en `src/server/protocol.py`: `Literal["interrupt"]`, sin payload.
- **Mensaje `InterruptedMessage`** (servidor → cliente) en `src/server/protocol.py`: `Literal["interrupted"]`, `chunk_id: int` (0 si no había chunk en vuelo).
- **`tests/integration/test_barge_in.py`**: 5 tests de integración.

#### Modificados

- `src/server/protocol.py`: añadir `InterruptMessage` al union `ClientMessage` y `InterruptedMessage` al union `ServerMessage`.
- `src/server/session.py`:
  - Añadir `self._current_chunk_id: int | None = None` y `self._interrupt_lock: bool = False` en `__init__`.
  - `_synthesize_segment` setea `_current_chunk_id = chunk_id` al inicio, lo limpia al terminar normalmente.
  - Handler `InterruptMessage` en `_receive_loop`.
  - Nuevo método `_handle_interrupt()`.

## Protocol Changes

### Nuevo: `InterruptMessage` (cliente → servidor)

```json
{"type": "interrupt"}
```

Sin payload adicional. El cliente puede enviarlo en cualquier momento después del handshake de auth.

### Nuevo: `InterruptedMessage` (servidor → cliente)

```json
{"type": "interrupted", "chunk_id": 7}
```

`chunk_id`:
- ID del chunk que fue cortado (si había uno en vuelo).
- `0` si el interrupt llegó sin chunk activo.

Reutilizamos `ChunkAbortedMessage { chunk_id: 7 }` (de Fase 1) cuando había chunk en vuelo, para que el cliente pueda descartar el buffer parcial.

### Orden esperado de mensajes durante un interrupt mid-síntesis

```
servidor → cliente: ChunkAbortedMessage { chunk_id: N }
servidor → cliente: InterruptedMessage { chunk_id: N }
```

(servidor después acepta nuevos `token` y procesa normalmente).

## Implementation Details

### `SpeakerSession.__init__` additions

```python
self._current_chunk_id: int | None = None
self._interrupt_lock: bool = False
```

### `_synthesize_segment` modification

Set `self._current_chunk_id = chunk_id` al inicio. Clear al final del happy path (después de `audio_end`) y al retornar por error/disconnect. En el caso de `CancelledError`, no se limpia explícitamente — el handler de interrupt lo lee y resetea.

```python
async def _synthesize_segment(self, text: str) -> None:
    chunk_id = self._chunk_counter
    self._chunk_counter += 1
    self._current_chunk_id = chunk_id
    try:
        await self._send(AudioStartMessage(chunk_id=chunk_id, sample_rate=…))
        try:
            normalized = await self._normalizer.normalize(text)
        except Exception as exc:
            self._log.warning("Normalizer raised: %s", exc)
            normalized = text
        try:
            async for frame in self._engine.synthesize(normalized):
                try:
                    await self._ws.send_bytes(frame)
                except WebSocketDisconnect:
                    await self._send(ChunkAbortedMessage(chunk_id=chunk_id))
                    self._current_chunk_id = None
                    return
        except asyncio.CancelledError:
            raise  # propagate to be caught by _handle_interrupt
        except WebSocketDisconnect:
            await self._send(ChunkAbortedMessage(chunk_id=chunk_id))
            self._current_chunk_id = None
            return
        await self._send(AudioEndMessage(chunk_id=chunk_id))
        self._current_chunk_id = None
    except BaseException:
        self._current_chunk_id = None
        raise
```

### `_receive_loop` handler addition

```python
elif isinstance(msg, InterruptMessage):
    if self._interrupt_lock:
        continue  # ignore concurrent interrupts
    self._interrupt_lock = True
    await self._handle_interrupt()
    self._interrupt_lock = False
```

### `_handle_interrupt` (nuevo)

```python
async def _handle_interrupt(self) -> None:
    aborted_id = self._current_chunk_id
    # Cancel worker (raises CancelledError inside _synthesize_segment)
    if self._tts_task and not self._tts_task.done():
        self._tts_task.cancel()
        try:
            await self._tts_task
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            self._log.warning("Worker ended with error during interrupt: %s", exc)
    # Drain queue
    drained = 0
    while not self._queue.empty():
        try:
            self._queue.get_nowait()
            drained += 1
        except asyncio.QueueEmpty:
            break
    # Reset accumulator
    self._accumulator.flush()
    # Restart worker
    self._tts_task = asyncio.create_task(self._tts_worker())
    # Notify client
    if aborted_id is not None:
        try:
            await self._send(ChunkAbortedMessage(chunk_id=aborted_id))
        except Exception:
            pass
    try:
        await self._send(InterruptedMessage(chunk_id=aborted_id or 0))
    except Exception:
        pass
    self._log.info("Barge-in processed (aborted=%s, drained=%d)", aborted_id, drained)
```

## Edge Cases & Error Handling

| Escenario | Comportamiento |
|-----------|----------------|
| `interrupt` llega sin chunks enviados | `interrupted { chunk_id: 0 }`, sin `chunk_aborted` |
| `interrupt` durante envío de `audio_start` (antes del async for) | worker cancela antes de empezar síntesis; `interrupted { chunk_id: 0 }`, log warn |
| `interrupt` llega durante el async for del engine | `CancelledError` re-raise; `chunk_aborted { chunk_id: N }` + `interrupted { chunk_id: N }` |
| Cliente desconecta durante el handler interrupt | try/except alrededor de cada `_send`; el WS está cerrado, la sesión termina |
| Dos `interrupt` concurrentes | el segundo se ignora (`_interrupt_lock`) |
| Worker recreado se cancela inmediatamente (race) | `try/except` no es necesario porque `_handle_interrupt` se ejecuta antes de cualquier `await` que pueda bloquear; la creación del task no es cancelable |
| Thread de Kokoro sigue corriendo (limitación) | el cliente descarta audio que llegue tarde (después de `chunk_aborted`); documentado en código con comment |

## Testing

### Unit (`tests/unit/test_barge_in_protocol.py`)

- `InterruptMessage` parsea correctamente.
- `InterruptedMessage` serializa con `chunk_id` entero.
- Tipo desconocido `{"type": "interrupted"}` sin `chunk_id` lanza `ValidationError`.

### Integration (`tests/integration/test_barge_in.py`)

5 tests obligatorios:

1. **test_interrupt_during_synthesis_sends_aborted_and_interrupted**:
   - Enviar token "Hello world."
   - Esperar `audio_start`
   - Enviar `interrupt` (antes de recibir ningún audio_end)
   - Recibir `chunk_aborted` + `interrupted` con mismo `chunk_id`
   - Sesión sigue abierta

2. **test_interrupt_drains_pending_queue**:
   - Enviar "a. b. c. d. e." (min_flush_chars=2 → 5 segmentos)
   - Esperar al menos el primer `audio_start`
   - Enviar `interrupt`
   - Verificar: 1 audio_start, 0 audio_end futuros de los 5 segmentos originales
   - Recibir `interrupted`

3. **test_interrupt_resets_accumulator**:
   - min_flush_chars=80
   - Enviar token "Hello wo" (sin flushear)
   - Enviar `interrupt`
   - Enviar token "rld world."
   - Primer `audio_start` tras interrupt debe ser del segmento nuevo
   - Verificar audio_end normal

4. **test_interrupt_with_no_inflight_chunk**:
   - Auth OK
   - Enviar `interrupt` directo (sin tokens)
   - Recibir `interrupted { chunk_id: 0 }`
   - Sesión sigue abierta, nuevos tokens funcionan

5. **test_session_continues_after_interrupt**:
   - Token "Hello", esperar audio_end
   - `interrupt`
   - Recibir `interrupted`
   - Token "world again.", esperar audio_end del nuevo segmento
   - Recibir `done` tras `end`

## Acceptance Criteria

1. `python3 -m pytest tests/ -v` pasa los 113 tests previos + 8 nuevos = **121 tests** sin regresiones.
2. Latencia `interrupt → interrupted` medida en tests < 100 ms con MockEngine.
3. Sin memory leaks: tests usan `len(asyncio.all_tasks())` antes/después para verificar.
4. `WS` y `task` quedan en estado consistente tras interrupt (sin tasks zombies).

## Out of Scope (Fases futuras)

- **Fase 5**: métricas de TTFB, histogramas de barge-in latency.
- **Fase 3** (multi-voz): barge-in no necesita saber de voz; ortogonal.
- **Pause/resume**: feature separado, mismo protocolo requeriría `ResumeMessage`.

## Risks & Limitations

1. **Thread de Kokoro no cancelable**: el primer frame después del interrupt puede llegar al cliente hasta 200-500 ms tarde. Mitigación: cliente descarta todo audio recibido después de `chunk_aborted` con el mismo `chunk_id`.

2. **Lock `_interrupt_lock` vs concurrent token**: si llegan tokens durante `_handle_interrupt`, se encolan normalmente en `_queue` (porque `_receive_loop` no pausa). El worker nuevo los procesará cuando estén listos. **No hay starvation** porque `_handle_interrupt` es rápido (<10 ms sin Kokoro).

3. **Race condition en startup**: si el cliente envía `interrupt` antes de que el primer `_synthesize_segment` haya fijado `_current_chunk_id`, `aborted_id` será `None` → `chunk_id: 0` en `interrupted`. Esto es correcto y no requiere manejo especial.

## Files Modified

### Modified
- `src/server/protocol.py` (+8 LOC: 2 mensajes, 2 union slots)
- `src/server/session.py` (+60 LOC: 2 atributos, handler interrupt, _handle_interrupt, mod a _synthesize_segment)
- `tests/integration/test_tts_stream.py` (+2 LOC: `import InterruptMessage` si aplica)
- `tests/integration/test_normalizer_in_session.py` (helper update, +1 LOC)

### Created
- `tests/unit/test_barge_in_protocol.py` (~30 LOC, 3 tests)
- `tests/integration/test_barge_in.py` (~180 LOC, 5 tests)

**Total estimate**: ~280 LOC of changes + tests.
