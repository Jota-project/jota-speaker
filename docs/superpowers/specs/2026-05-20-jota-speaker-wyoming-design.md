# SPEC: Wyoming TTS Server para jota-speaker

**Fecha:** 2026-05-20
**Proyecto:** jota-speaker
**Objetivo:** Añadir soporte para protocolo Wyoming como TTS provider en Home Assistant

---

## 1. Resumen

Se añade un servidor TCP asyncio que implementa el protocolo Wyoming para que jota-speaker pueda ser usado como TTS platform nativo en Home Assistant via la integración `wyoming`. El servidor comparte el motor TTS (Kokoro/Mock) existente y permite streaming de audio en tiempo real.

---

## 2. Arquitectura

```
jota-speaker
├── FastAPI (HTTP/WS)
│   ├── GET /health
│   ├── WS /ws  (streaming original)
│   └── POST /v1/audio/speech  (futuro)
│
└── Wyoming TCP Server (puerto configurable)
    └── :20424/tcp (default)
        └── Protocolo JSONL + binary payload

    Compartido:
    └── ITTSEngine (Kokoro/Mock)
```

---

## 3. Protocolo Wyoming

### Formato de eventos

Cada evento es:
1. Una línea JSON con campos:
   - `type` (string, requerido): nombre del evento
   - `data` (object, opcional): datos del evento
   - `payload_length` (int, opcional): bytes binarios que siguen
2. Si `payload_length > 0`: exactamente esos bytes siguen como payload binario

### Eventos TTS definidos

| Tipo | Dirección | Datos | Payload |
|------|-----------|-------|---------|
| `audio-start` | → HA | `{rate: int, width: int, channels: int}` | — |
| `audio-chunk` | → HA | `{rate: int, width: int, channels: int}` | PCM16 raw |
| `audio-stop` | → HA | `{timestamp: int}` | — |
| `synthesize` | ← HA | `{text: str}` | — |

### Flujo de síntesis

```
HA → jota-speaker:
  { "type": "synthesize", "data": { "text": "Hello world" } }

jota-speaker → HA:
  { "type": "audio-start", "data": { "rate": 24000, "width": 2, "channels": 1 } }
  { "type": "audio-chunk", "data": { "rate": 24000, "width": 2, "channels": 1 }, "payload_length": 9600 }
  <9600 bytes PCM16>
  { "type": "audio-chunk", ... }
  <... más chunks ...>
  { "type": "audio-stop", "data": { "timestamp": 0 } }
```

---

## 4. Configuración

### Variables de entorno nuevas

| Variable | Default | Descripción |
|----------|---------|-------------|
| `JOTA_WYOMING_ENABLED` | `true` | Activar servidor Wyoming |
| `JOTA_WYOMING_PORT` | `20424` | Puerto TCP para Wyoming |

### Variables existentes usadas

| Variable | Uso en Wyoming |
|----------|----------------|
| `JOTA_ENGINE` | moteur: `mock` o `kokoro` |
| `JOTA_KOKORO_VOICE` | voz seleccionada (`af_heart`) |
| `JOTA_SAMPLE_RATE` | rate en audio-start (24000) |

---

## 5. Server implementation

### File: `src/wyoming/protocol.py`

Define los tipos de evento Wyoming como dataclasses Pydantic:

```python
@dataclass
class AudioStart:
    rate: int
    width: int  # bytes por muestra (2 = 16bit)
    channels: int

@dataclass
class AudioChunk:
    rate: int
    width: int
    channels: int
    payload: bytes

@dataclass
class AudioStop:
    timestamp: int = 0

@dataclass
class Synthesize:
    text: str
```

### File: `src/wyoming/handler.py`

- `WyomingHandler`: maneja la conexión con HA
  - `handle()`: loop principal que lee eventos y responde
  - `handle_synthesize()`: llamada al TTS engine, envía audio chunks

### File: `src/wyoming/server.py`

- `WyomingServer`: servidor TCP asyncio
  - `start()` / `stop()` lifecycle
  - `create_server()`: usa `asyncio.start_server()`
  - Propaga `ITTSEngine` a los handlers

### File: `src/wyoming/__init__.py`

Módulo público vacío (exports solo para testing).

---

## 6. Cambios en archivos existentes

### `src/core/config.py`

Añadir:

```python
wyoming_enabled: bool = True
wyoming_port: int = 20424
```

### `src/main.py`

En el lifespan:
```python
if settings.wyoming_enabled:
    from src.wyoming.server import WyomingServer
    server = WyomingServer(settings, engine)
    await server.start()
    app.state.wyoming_server = server
```

En shutdown:
```python
if hasattr(app.state, 'wyoming_server'):
    await app.state.wyoming_server.stop()
```

---

## 7. Zeroconf / Auto-descubrimiento

El servidor **NO incluye** Zeroconf/mDNS directamente. Para auto-descubrimiento en HA:

**Opción A (recomendada para MVP):**
El usuario configura HA manualmente con IP:puerto de jota-speaker.

**Opción B (futuro):**
Añadir publishing Zeroconf con `python-avahi` o `zeroconf` library, publicando `_wyoming._tcp.local.` en puerto `JOTA_WYOMING_PORT`.

Por ahora: zeroconf disabled, configuración manual en HA.

---

## 8. Auth

El protocolo Wyoming no incluye mecanismo de autenticación — es para redes locales de confianza.

**Decisión:** El servidor Wyoming opera **sin auth**.
Si `JOTA_AUTH_PROVIDER` está configurado, el servidor WS lo usa pero el servidor Wyoming ignora auth.

---

## 9. Edge cases

| Situación | Comportamiento |
|-----------|----------------|
| Texto vacío en `synthesize` | Ignorar evento, no responder |
| Engine lanza exception | Enviar `error` event, cerrar conexión |
| HA cierra conexión | Limpiar recursos, no propagar error |
| Texto muy largo | El engine sintetiza todo (no hay límite en Kokoro) |

---

## 10. Testing

### Unit tests
- `tests/unit/test_wyoming_protocol.py`: parsing/serialization de eventos
- `tests/unit/test_wyoming_handler.py`: handler con mock engine

### Integration tests
- `tests/integration/test_wyoming_server.py`: iniciar server, conectar con socket, enviar synthesize, verificar audio chunks

---

## 11. Out of scope (para este spec)

- Endpoint HTTP `/v1/audio/speech` (estilo OpenAI)
- Streaming de chunks mientras se generan ( Wyoming envía cuando ready, no hay backpressure)
- Zeroconf auto-descubrimiento
- Múltiples voces (solo la configurada)
- Cache de audio

---

## 12. Dependencies

No se añaden nuevas dependencies. El servidor usa stdlib `asyncio`.