![Status: Maintained](https://img.shields.io/badge/status-Maintained-2ea44f)

> **Role in Jota ecosystem:** TTS streaming microservice. WebSocket receives LLM tokens and emits PCM16 audio; Wyoming TCP (port 20424) lets Home Assistant use it as native TTS. The gateway `jota-gateway` connects to this service per voice session.
>
> Part of [Jota-project](https://github.com/Jota-project). See [`ARCHITECTURE.md`](https://github.com/Jota-project/.github/blob/main/ARCHITECTURE.md) for the full system map.

---

# jota-speaker

TTS (Text-to-Speech) streaming microservice powered by [Kokoro](https://github.com/hexgrad/kokoro). Exposes two server surfaces:

- **WebSocket** (`/ws`) — receives LLM text tokens in real time and streams raw PCM16 audio frames back.
- **Wyoming TCP** (port `20424`) — implements the [Wyoming protocol](https://github.com/rhasspy/wyoming) so Home Assistant can use jota-speaker as a native TTS platform.

```
LLM token stream  →  [WebSocket /ws]      →  jota-speaker  →  PCM16 audio frames  →  client
HA assistant      →  [Wyoming TCP :20424]  →  jota-speaker  →  PCM16 audio chunks  →  HA
```

Default voice: **ef_dora** (Spanish, female). Default language: **es**.

---

## Table of contents

1. [Quick start](#quick-start)
2. [WebSocket protocol](#websocket-protocol)
   - [Connection](#1-connection)
   - [Authentication](#2-authentication)
   - [Sending tokens](#3-sending-tokens)
   - [Ending a session](#4-ending-a-session)
   - [Receiving audio](#5-receiving-audio)
   - [Interrupting playback (barge-in)](#6-interrupting-playback-barge-in)
   - [Error handling](#7-error-handling)
   - [Session limits](#8-session-limits)
3. [Message reference](#message-reference)
   - [Client → Server](#client--server)
   - [Server → Client](#server--client)
4. [Audio format](#audio-format)
5. [HTTP endpoints](#http-endpoints)
   - [`GET /health`](#get-health)
   - [`POST /v1/audio/speech`](#post-v1audiospeech)
   - [`GET /v1/voices`](#get-v1voices)
6. [Configuration](#configuration)
7. [Wyoming protocol (Home Assistant)](#wyoming-protocol-home-assistant)
8. [Running with Docker](#running-with-docker)
9. [Running tests](#running-tests)
10. [Status & roadmap](#status--roadmap)

---

## Quick start

```bash
# Install dependencies
pip install .

# Run with mock engine (no model files needed)
JOTA_ENGINE=mock uvicorn src.main:app --port 8005

# Run with Kokoro (production TTS)
JOTA_ENGINE=kokoro \
JOTA_KOKORO_MODEL=/models/kokoro-v1.0.int8.onnx \
JOTA_KOKORO_VOICES=/models/voices-v1.0.bin \
uvicorn src.main:app --host 0.0.0.0 --port 8005
```

Wyoming server starts automatically alongside FastAPI (set `JOTA_WYOMING_ENABLED=false` to disable it).

---

## WebSocket protocol

Endpoint: `ws://<host>:<port>/ws`

The protocol is **JSON over WebSocket text frames** for control messages, and **binary WebSocket frames** for audio data. All JSON fields use snake_case.

### Session lifecycle

```
Client                              Server
  │                                   │
  │──── WS connect ──────────────────►│
  │◄─── WS 101 Switching Protocols ───│
  │                                   │
  │──── {"type":"auth","token":"…"} ──►│  ← MUST be first message
  │◄─── {"type":"auth_ok"} ───────────│
  │                                   │
  │──── {"type":"token","text":"…"} ──►│  ← stream LLM tokens
  │──── {"type":"token","text":"…"} ──►│
  │         ...                       │
  │◄─── {"type":"audio_start",…} ─────│  ← synthesis begins
  │◄─── <binary PCM16 frame> ─────────│
  │◄─── <binary PCM16 frame> ─────────│
  │◄─── {"type":"audio_end",…} ───────│
  │         ...                       │
  │  (optional barge-in: see §6)      │
  │──── {"type":"interrupt"} ─────────►│  ← cut playback
  │◄─── {"type":"chunk_aborted",…} ───│
  │◄─── {"type":"interrupted",…} ─────│
  │──── {"type":"token","text":"…"} ──►│  ← new utterance
  │         ...                       │
  │──── {"type":"end"} ───────────────►│  ← signal no more tokens
  │◄─── {"type":"done"} ──────────────│  ← all synthesis complete
  │                                   │
  │──── WS close (1000) ─────────────►│
  │                                   │
```

---

### 1. Connection

Connect to the WebSocket endpoint. No query parameters or headers are required at the transport level.

```
ws://localhost:8005/ws
```

The server accepts the connection immediately. **The first message you send must be `auth`** — any other message will cause an `auth_error` and the connection will be closed.

---

### 2. Authentication

**Send** as the very first message:

```json
{"type": "auth", "token": "<your-token>"}
```

**Receive** on success:

```json
{"type": "auth_ok"}
```

**Receive** on failure (connection is closed by server with code 1008 after this):

```json
{"type": "auth_error", "reason": "Invalid token"}
```

> **Note:** In development/CI mode (`JOTA_AUTH_PROVIDER=stub`) any non-empty token is accepted. In production (`JOTA_AUTH_PROVIDER=jota_db`) the token is validated against jota-db.

---

### 3. Sending tokens

After a successful `auth_ok`, stream LLM output tokens one by one (or in small batches):

```json
{"type": "token", "text": "Hello"}
{"type": "token", "text": ", world"}
{"type": "token", "text": "."}
```

The server accumulates tokens internally and flushes them to the TTS engine on:
- **Sentence boundaries** — any of `.` `!` `?` `\n`
- **Buffer length** — when the buffer reaches `JOTA_MIN_FLUSH_CHARS` (default 80) characters without a boundary, it splits at the last word boundary

You can also trigger synthesis immediately at any point:

```json
{"type": "flush"}
```

Use `flush` when the LLM pauses mid-sentence but you want audio to start sooner (e.g., after a comma-heavy clause).

---

### 4. Ending a session

When the LLM finishes generating, send `end` to signal no more tokens:

```json
{"type": "end"}
```

The server will:
1. Flush any remaining buffered text to the TTS engine.
2. Synthesize all pending segments.
3. Send `{"type": "done"}` when all audio has been delivered.

After receiving `done`, **close the WebSocket normally** with code 1000:

```
Client sends: WS close frame (code 1000)
```

> Do **not** just drop the TCP connection — send a proper close frame so the server can clean up resources immediately.

---

### 5. Receiving audio

For each text segment synthesized, the server sends:

1. **`audio_start`** — signals a new audio chunk is beginning:
   ```json
   {"type": "audio_start", "chunk_id": 0, "sample_rate": 24000, "channels": 1, "encoding": "pcm16"}
   ```

2. **Binary frames** — raw PCM16 audio data (little-endian, 16-bit signed integers). Each frame may be any number of samples but is always an even number of bytes.

3. **`audio_end`** — signals the chunk is complete:
   ```json
   {"type": "audio_end", "chunk_id": 0}
   ```

Multiple chunks can be in flight sequentially. `chunk_id` is a monotonically increasing integer starting at 0 per session. Chunks are always delivered in order.

**Receiving loop pseudocode:**

```python
async for message in ws:
    if isinstance(message, str):
        msg = json.loads(message)
        if msg["type"] == "audio_start":
            current_chunk = msg["chunk_id"]
            sample_rate = msg["sample_rate"]
            # prepare audio buffer
        elif msg["type"] == "audio_end":
            # chunk is complete, play/forward buffer
        elif msg["type"] == "done":
            break  # session complete
        elif msg["type"] == "error":
            handle_error(msg["code"], msg["message"])
            break
    elif isinstance(message, bytes):
        # PCM16 audio — append to current chunk buffer
        audio_buffer.extend(message)
```

---

### 6. Interrupting playback (barge-in)

When the user starts speaking mid-playback, stop TTS **without** closing the WebSocket. The session stays open and you can keep sending `token` messages for the new utterance.

```
Current session:  auth → tokens → audio playing...
User speaks:      client sends {"type":"interrupt"}
                  ← chunk_aborted { chunk_id: N }
                  ← interrupted { chunk_id: N }
New tokens:       client sends {"type":"token", "text":"..."}
                  ← audio_start { chunk_id: N+1, ... }
```

1. **Stop playing** any audio buffered for the in-flight chunk.
2. **Send `{"type":"interrupt"}`** as a text frame.
3. The server cancels the in-flight chunk, discards the pending queue, resets the accumulator, and replies with `chunk_aborted` (the cut chunk) followed by `interrupted` (the chunk id that was cut, or `0` if no chunk was in flight).
4. **Send new `token` messages** for the new utterance — no re-auth needed.

Average `interrupt → interrupted` latency is well under 100 ms with the mock engine (measured by `test_interrupt_latency_under_100ms`).

> **Note:** The Kokoro inference thread is not hard-cancellable. A few extra audio frames for the cut chunk may arrive on the wire after `chunk_aborted`. The client should discard any audio received after `chunk_aborted` for that `chunk_id`.

---

### 7. Error handling

The server sends an `error` message before closing in any unexpected situation:

```json
{"type": "error", "code": "<code>", "message": "<human-readable description>"}
```

| `code` | Cause | Action |
|---|---|---|
| `auth_error` | Auth service unavailable | Retry later |
| `parse_error` | Malformed JSON or unknown message type | Fix client code |
| `session_timeout` | Session exceeded `JOTA_SESSION_TIMEOUT` | Reconnect |
| `queue_full` | TTS synthesis cannot keep up with token rate | Reconnect; slow down token emission |

After an `error` message the server closes the connection. The client should not attempt to send further messages.

---

### 8. Session limits

| Limit | Default | Config var |
|---|---|---|
| Session timeout | 300 s | `JOTA_SESSION_TIMEOUT` |
| Synthesis queue depth | 100 segments | `JOTA_QUEUE_MAXSIZE` |

**Session timeout:** if no `end` message is received within `JOTA_SESSION_TIMEOUT` seconds, the server sends `{"type":"error","code":"session_timeout",…}` and closes the connection.

**Queue depth:** the server buffers at most `JOTA_QUEUE_MAXSIZE` synthesized segments. If the client sends tokens faster than the TTS engine can process them and the queue fills up, the server sends `{"type":"error","code":"queue_full",…}` and closes. Under normal LLM output rates (100–300 tokens/s) this limit will not be reached.

---

## Message reference

### Client → Server

All messages are **JSON text frames**.

#### `auth`
Must be the first message sent after connecting.

| Field | Type | Required | Description |
|---|---|---|---|
| `type` | `"auth"` | yes | |
| `token` | string | yes | Bearer token for authentication |
| `model` | string | no | Kokoro model id to use for this session (see [Configuration](#configuration)). Falls back to the default model if unknown or omitted. |
| `voice` | string | no | Voice to use for this session. Falls back to the default voice if unknown or omitted. |

```json
{"type": "auth", "token": "sk-...", "model": "kokoro-v1.0.int8", "voice": "em_alex"}
```

#### `token`
Deliver a text token from the LLM.

| Field | Type | Required | Description |
|---|---|---|---|
| `type` | `"token"` | yes | |
| `text` | string | yes | One or more characters of LLM output |

```json
{"type": "token", "text": "Hello, world."}
```

#### `flush`
Force immediate synthesis of whatever text is currently buffered. Send after pauses or clause boundaries where you want audio to start sooner.

```json
{"type": "flush"}
```

#### `end`
Signal that the LLM has finished. No more `token` messages will follow. The server will synthesize remaining buffered text and send `done`.

```json
{"type": "end"}
```

#### `interrupt`
Cancel the in-flight chunk and discard any pending buffered text. The session stays open. See [Section 6](#6-interrupting-playback-barge-in) for the full flow.

```json
{"type":"interrupt"}
```

---

### Server → Client

Control messages are **JSON text frames**. Audio data is **binary frames**.

#### `auth_ok`
Authentication succeeded. The session is now active.

| Field | Type | Description |
|---|---|---|
| `type` | `"auth_ok"` | |
| `model_used` | string | The model id actually resolved for this session (may differ from the requested `model` if it wasn't loaded). |
| `voice_used` | string | The voice actually resolved for this session (may differ from the requested `voice` if it wasn't loaded). |

```json
{"type": "auth_ok", "model_used": "kokoro-v1.0.int8", "voice_used": "em_alex"}
```

#### `auth_error`
Authentication failed. The server closes the connection (code 1008) immediately after.

| Field | Type | Description |
|---|---|---|
| `type` | `"auth_error"` | |
| `reason` | string | Human-readable reason |

```json
{"type": "auth_error", "reason": "Invalid token"}
```

#### `audio_start`
A new audio chunk is beginning. Audio binary frames that follow belong to this chunk until `audio_end` with the same `chunk_id`.

| Field | Type | Description |
|---|---|---|
| `type` | `"audio_start"` | |
| `chunk_id` | integer | Zero-based chunk index, monotonically increasing |
| `sample_rate` | integer | Samples per second (e.g. `24000`) |
| `channels` | integer | Always `1` (mono) |
| `encoding` | `"pcm16"` | Always `"pcm16"` |

```json
{"type": "audio_start", "chunk_id": 0, "sample_rate": 24000, "channels": 1, "encoding": "pcm16"}
```

#### Audio binary frames
Raw PCM16 audio. **Little-endian, 16-bit signed integers, mono.** Always an even number of bytes. Multiple binary frames per chunk are normal; concatenate them in order.

#### `audio_end`
The current audio chunk is complete. All binary frames for `chunk_id` have been sent.

| Field | Type | Description |
|---|---|---|
| `type` | `"audio_end"` | |
| `chunk_id` | integer | Matches the preceding `audio_start` |

```json
{"type": "audio_end", "chunk_id": 0}
```

#### `chunk_aborted`
The audio chunk was cut short (client disconnected, or the client sent `interrupt`). The client should discard any audio buffered for `chunk_id`.

| Field | Type | Description |
|---|---|---|
| `type` | `"chunk_aborted"` | |
| `chunk_id` | integer | The chunk that was cut |

```json
{"type": "chunk_aborted", "chunk_id": 0}
```

#### `interrupted`
Confirmation that the server processed an `interrupt` message. Sent right after `chunk_aborted` when a chunk was in flight; sent alone with `chunk_id: 0` when no chunk was in flight.

| Field | Type | Description |
|---|---|---|
| `type` | `"interrupted"` | |
| `chunk_id` | integer | Chunk that was cut, or `0` if no chunk was in flight |

```json
{"type": "interrupted", "chunk_id": 0}
```

#### `done`
All synthesis is complete. Sent after all chunks have finished, in response to the client's `end` message. The client should close the WebSocket after receiving this.

```json
{"type": "done"}
```

#### `error`
An unrecoverable error occurred. The server closes the connection after this message.

| Field | Type | Description |
|---|---|---|
| `type` | `"error"` | |
| `code` | string | Machine-readable error code |
| `message` | string | Human-readable description |

```json
{"type": "error", "code": "session_timeout", "message": "Session timed out"}
```

---

## Audio format

| Property | Value |
|---|---|
| Format | Raw PCM (no WAV/MP3 header) |
| Encoding | Signed 16-bit integer |
| Byte order | Little-endian |
| Channels | 1 (mono) |
| Sample rate | 24000 Hz (configurable via `JOTA_SAMPLE_RATE`) |

To play with `ffplay` for debugging:
```bash
ffplay -f s16le -ar 24000 -ac 1 -
```

---

## HTTP endpoints

### `GET /health`

Returns `200 OK` with `{"status": "ok"}` when the service is running.

```bash
curl http://localhost:8005/health
# {"status":"ok"}
```

### `GET /metrics`

Prometheus scrape endpoint. Returns metrics in text format. No auth — same
network-filtering treatment as `/health`. See [Observability](#observability)
below for the full metric list.

```bash
curl http://localhost:8005/metrics
```

### `POST /v1/audio/speech`

OpenAI-compatible text-to-speech. Synthesizes the given text and returns
the audio stream. Matches the [OpenAI TTS API](https://platform.openai.com/docs/api-reference/audio/createSpeech)
request and response shape.

**Request body** (JSON):

| Field | Type | Required | Default | Notes |
|---|---|---|---|---|
| `model` | string | yes | — | Kokoro model id (see [Configuration](#configuration)). Falls back to the default model if unknown. |
| `input` | string | yes | — | 1–4096 chars. |
| `voice` | string | no | `"alloy"` | Falls back to the default voice if unknown or not loaded (e.g. OpenAI's own `"alloy"`/`"shimmer"` names won't match a Kokoro voice). |
| `response_format` | string | no | `"wav"` | Supports `"pcm"`, `"wav"`, `"mp3"`, `"opus"`, `"aac"`, and `"flac"` (64 kbps CBR mono for mp3/opus, 64 kbps VBR for aac, lossless for flac; all via ffmpeg). Other values return 400. |
| `speed` | float | no | `1.0` | Range `0.25..4.0` (Pydantic-validated). Clamped to whatever the resolved engine natively supports (Kokoro: `0.5..2.0`) via `resolve_speed()` — never rejected, just clamped. |
| `instructions` | string | no | `null` | Accepted, ignored in MVP. |
| `stream` | bool | no | `true` | jota-speaker extension, not part of the real OpenAI wire shape (see issue #9). `true` (default): chunked streaming, unchanged. `false`: buffers the full response and returns it with an exact `Content-Length` — for strict clients/players that handle chunked transfer or unknown-length WAV headers poorly. |

Unknown fields are silently dropped (clients sending `logprobs`, etc. work).

**Auth:** `Authorization: Bearer <token>` (same provider as WebSocket/Wyoming).

**Response:** 200 OK with the audio, either streamed chunked (`stream: true`, default) or fully buffered (`stream: false`):
- `Content-Type: audio/pcm` for raw PCM16, `audio/wav` for RIFF/WAVE, `audio/mpeg` for MP3, `audio/ogg` for Opus, `audio/aac` for AAC, or `audio/flac` for FLAC.
- `X-Request-Id`, `X-Model-Used`, `X-Voice-Used`, `X-Speed-Used` headers — the last three reflect the actually resolved/clamped values, not necessarily what was requested.
- `stream: true` (default): WAV header has `0xFFFFFFFF` length sentinels (standard streaming practice; all modern players handle it) — no `Content-Length` (chunked transfer).
- `stream: false`: exact `Content-Length`; WAV header carries the real, byte-accurate sizes instead of the sentinel.
- If `response_format` is `mp3`/`opus`/`aac`/`flac` but ffmpeg isn't installed on the host, returns `503` with error code `audio_format_unavailable` instead of failing to start up.

**Example:**

```bash
curl -X POST http://localhost:8005/v1/audio/speech \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"model":"tts-1","input":"Hola mundo","voice":"alloy","speed":1.2,"response_format":"wav"}' \
  --output output.wav

ffplay output.wav
```

### `GET /v1/voices`

Lists the voices loaded on the default model (ElevenLabs-style; all discovered
Kokoro model variants share the same voices file, so the default model's
catalog is authoritative — see [Configuration](#configuration)).

**Auth:** `Authorization: Bearer <token>` (same provider as `/v1/audio/speech`).

**Response:** `200 OK`:

```json
{"voices": [{"voice_id": "ef_dora", "name": "ef_dora"}, {"voice_id": "em_alex", "name": "em_alex"}, ...]}
```

**Example:**

```bash
curl -H "Authorization: Bearer $TOKEN" http://localhost:8005/v1/voices
```

**Errors:** JSON in OpenAI error envelope format:

```json
{"error": {"message": "...", "type": "invalid_request_error", "param": null, "code": "invalid_api_key"}}
```

See [the spec](docs/superpowers/specs/2026-07-03-endpoint-openai-compatible.md) for the full error code table.

---

## Configuration

All settings use the `JOTA_` prefix and can be set via environment variables or a `.env` file.

| Variable | Default | Description |
|---|---|---|
| `JOTA_ENGINE` | `mock` | TTS engine: `mock` (silence, for tests) or `kokoro` |
| `JOTA_KOKORO_MODEL` | `kokoro-v1.0.int8.onnx` | Path to the *default* Kokoro ONNX model file. All `.onnx` files in the same directory are auto-discovered and loaded at startup as selectable models (id = filename without extension). |
| `JOTA_KOKORO_VOICES` | `voices-v1.0.bin` | Path to Kokoro voices file |
| `JOTA_KOKORO_VOICE` | `ef_dora` | Kokoro voice (see voices list below) |
| `JOTA_KOKORO_LANG` | `es` | Kokoro language code |
| `JOTA_SAMPLE_RATE` | `24000` | Output sample rate (Hz) |
| `JOTA_MIN_FLUSH_CHARS` | `80` | Flush buffer to TTS after this many chars without a sentence boundary |
| `JOTA_AUTH_PROVIDER` | `stub` | Auth backend: `stub` (accept all) or `jota_db` |
| `JOTA_JOTA_DB_URL` | `http://localhost:8001` | jota-db base URL |
| `JOTA_JOTA_DB_AUTH_PATH` | `/auth/validate` | jota-db validation endpoint |
| `JOTA_JOTA_DB_TIMEOUT` | `5.0` | jota-db request timeout (seconds) |
| `JOTA_SESSION_TIMEOUT` | `300.0` | Max WebSocket session duration in seconds |
| `JOTA_QUEUE_MAXSIZE` | `100` | Max synthesis segments buffered per WebSocket session |
| `JOTA_WYOMING_ENABLED` | `true` | Start the Wyoming TCP server |
| `JOTA_WYOMING_PORT` | `20424` | Wyoming server port |

### Available Spanish voices (Kokoro)

| Voice | Style |
|---|---|
| `ef_dora` | Female (default) |
| `em_alex` | Male |
| `em_santa` | Male |

See `.env.example` for a ready-to-copy template.

---

## Wyoming protocol (Home Assistant)

> **Why this matters:** if you're integrating Jota with Home Assistant, this is the path you'll use. Wyoming is the protocol HA's voice pipeline speaks natively. jota-speaker implements both the WebSocket surface (for the gateway) and the Wyoming TCP surface (for HA) from a single process — no extra service needed.

jota-speaker exposes a [Wyoming](https://github.com/rhasspy/wyoming) TCP server so Home Assistant can use it as a native TTS platform via the **Wyoming integration** — no extra add-ons required.

### Setup in Home Assistant

1. Go to **Settings → Devices & Services → Add Integration → Wyoming Protocol**.
2. Enter the host/IP of the machine running jota-speaker and port `20424`.
3. Home Assistant will discover the voice (`ef_dora`, language `es`) and add jota-speaker as a TTS provider.
4. Assign it to your voice assistant pipeline under **Settings → Voice Assistants**.

### Wyoming flow

```
HA  →  { "type": "describe" }
       { "type": "info", "data": { "tts": [{ "name": "jota-speaker", "languages": ["es"], ... }] } }  ←  jota-speaker

HA  →  { "type": "synthesize", "data": { "text": "Hola mundo" } }
       { "type": "audio-start", "data": { "rate": 24000, "width": 2, "channels": 1 } }              ←  jota-speaker
       { "type": "audio-chunk", "data": { ... }, payload_length: N }  +  <N bytes PCM16>             ←  jota-speaker
       ...
       { "type": "audio-stop", "data": { "timestamp": 0 } }                                         ←  jota-speaker
```

The Wyoming server runs on the same process as FastAPI, started in the lifespan hook and stopped on shutdown.

---

## Observability

Prometheus metrics are exposed at `GET /metrics` on the same port as the
WebSocket (`8005`). No auth — same network-filtering treatment as
`/health`.

### Available metrics

| Name | Type | Labels |
|------|------|--------|
| `jota_speaker_ttfb_ms` | Histogram | `session_type`, `engine` |
| `jota_speaker_barge_in_latency_ms` | Histogram | `session_type` |
| `jota_speaker_sessions_active` | Gauge | `session_type` |
| `jota_speaker_engine_synthesis_in_flight` | Gauge | — |
| `jota_speaker_sessions_total` | Counter | `session_type`, `result` |
| `jota_speaker_errors_total` | Counter | `code` |
| `jota_speaker_chunks_total` | Counter | `result` |
| `jota_speaker_interrupts_total` | Counter | — |

`session_type` is `ws` or `wyoming`. TTFB is measured differently per
transport: for WS, from the first `token` message to the first audio byte;
for Wyoming, from the start of `_synthesize` to the first `audio-chunk`.

### Sample Grafana queries

```promql
# TTFB p95 by session type (5-minute window)
histogram_quantile(0.95,
  sum(rate(jota_speaker_ttfb_ms_bucket[5m])) by (le, session_type)
)

# Barge-in latency p99
histogram_quantile(0.99,
  sum(rate(jota_speaker_barge_in_latency_ms_bucket[5m])) by (le)
)

# Active sessions
jota_speaker_sessions_active

# Error rate by code
sum(rate(jota_speaker_errors_total[5m])) by (code)
```

### Disable

Set `JOTA_METRICS_ENABLED=false` to make all helpers no-op (the endpoint
still returns a 200 with no series — no behaviour change for scrapers).
Instrumentation is also fail-safe on its own: if the underlying registry
ever raises, helpers swallow the exception rather than crash the session.

---

## Running with Docker

```bash
# Development (mock engine, no models needed)
docker compose up

# Build only
docker compose build
```

The service exposes:
- Port `8005` — HTTP/WebSocket (FastAPI)
- Port `20424` — Wyoming TCP (Home Assistant)

Place Kokoro model files in `./models/` before starting with `JOTA_ENGINE=kokoro`:
- `kokoro-v1.0.int8.onnx`
- `voices-v1.0.bin`

**Nginx reverse proxy** — add these headers to your `location /ws` block:

```nginx
location /ws {
    proxy_pass http://jota-speaker:8005;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "Upgrade";
    proxy_set_header Host $host;
    proxy_read_timeout 310s;  # slightly larger than JOTA_SESSION_TIMEOUT
}
```

The Wyoming TCP port should be accessible directly (no HTTP proxy needed).

---

## Running tests

```bash
python3 -m pytest -v
```

134 tests, ~16 s. Uses `JOTA_ENGINE=mock` and `JOTA_AUTH_PROVIDER=stub` automatically — no model files required.

Tests are also run automatically via GitHub Actions on every push and pull request to `main` (see `.github/workflows/test.yml`).

---

## v1.0 — Final state (2026-07-06)

**Status: Stable.** This is the production-ready v1.0. Remaining issues are minor fixes and bug fixes from real usage. All core functionality is delivered.

### What's in v1.0

**Server surfaces:**
- **WebSocket** (`/ws`) — receives LLM tokens, streams PCM16 audio. Supports barge-in interrupt mid-stream.
- **Wyoming TCP** (port `20424`) — Home Assistant native TTS integration.
- **HTTP REST** (`POST /v1/audio/speech`, `GET /v1/voices`) — OpenAI-compatible TTS API.

**TTS engine:**
- **Kokoro ONNX** — fast, local inference. Spanish voice `ef_dora` by default. Three Spanish voices available: `ef_dora`, `em_alex`, `em_santa`.
- **Concurrency control** — `asyncio.Lock` serializes inference calls (one synthesis at a time).
- **Per-request voice** — override the default voice per request via `voice` param (HTTP, WebSocket `auth`, or Wyoming `synthesize`).
- **Per-request speed** — `speed` param (0.25–4.0, clamped to engine range) for pace control.

**Audio output:**
- Formats: PCM16, WAV, **MP3** (64 kbps CBR), **Opus** (64 kbps CBR via libopus), **AAC** (64 kbps, VBR — limitation accepted), **FLAC** (lossless).
- Two response modes: **streaming** (chunked transfer, default) and **buffered** (`stream: false`, exact `Content-Length`).
- Spanish text **normalization** — numbers, dates, hours, currency, emails, URLs, abbreviations.
- **Barge-in** — interrupt playback mid-stream in <100 ms.

**Authentication:** Bearer token validated against `jota-db` auth provider.

### Backlog

| # | Description | Notes |
|---|---|---|
| [#16](https://github.com/Jota-project/jota-speaker/issues/16) | SSE/Realtime endpoint | Covered by WebSocket + Wyoming. No immediate need. |
| [#17](https://github.com/Jota-project/jota-speaker/issues/17) | Configurable bitrate | Speculative — no known need yet. |
| [#21](https://github.com/Jota-project/jota-speaker/issues/21) | Chatterbox TTS engine | GPU-powered alternative to Kokoro. Benchmark pending. |

### Won't do

| # | Description | Reason |
|---|---|---|
| [#15](https://github.com/Jota-project/jota-speaker/issues/15) | Honor `instructions` field | Kokoro doesn't expose style/tone modifiers. Accepted but has no effect. |

### Changelog (selected PRs)

| PR | Description |
|---|---|
| [#23](https://github.com/Jota-project/jota-speaker/pull/23) | Buffered response mode (`stream: bool`) |
| [#20](https://github.com/Jota-project/jota-speaker/pull/20) | Fase 3 — per-request voice/speed, GET /v1/voices |
| [#11](https://github.com/Jota-project/jota-speaker/pull/11) | MP3, Opus, AAC, FLAC encoders (64 kbps CBR) |
| [#9](https://github.com/Jota-project/jota-speaker/pull/9) | OpenAI-compatible POST /v1/audio/speech |
| [#6](https://github.com/Jota-project/jota-speaker/pull/6) | Barge-in (<100 ms interrupt) |
| [#4](https://github.com/Jota-project/jota-speaker/pull/4) | Spanish normalizer, robustness & teardown |
