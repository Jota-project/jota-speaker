# jota-speaker

TTS (Text-to-Speech) streaming microservice. Receives text tokens from an LLM over a WebSocket connection and returns raw PCM16 audio frames in real time.

```
LLM token stream  →  [WebSocket]  →  jota-speaker  →  PCM16 audio frames  →  client
```

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
6. [Configuration](#configuration)
7. [Running with Docker](#running-with-docker)
8. [Running tests](#running-tests)

---

## Quick start

```bash
# Install dependencies
pip install -r requirements.txt

# Run with mock engine (no model files needed)
JOTA_ENGINE=mock uvicorn src.main:app --port 8005

# Run with Kokoro (production TTS)
JOTA_ENGINE=kokoro \
JOTA_KOKORO_MODEL=/models/kokoro-v1.0.int8.onnx \
JOTA_KOKORO_VOICES=/models/voices-v1.0.bin \
uvicorn src.main:app --host 0.0.0.0 --port 8005
```

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

When the user starts speaking mid-playback, stop TTS immediately:

1. **Stop playing** audio on the client side.
2. **Close the WebSocket** with code 1000.
3. **Open a new WebSocket**, authenticate, and start the new TTS session.

```
Current session:  auth → tokens → audio playing...
User speaks:      client closes WS (1000)
New session:      auth → new tokens → audio
```

Reconnection on a LAN typically takes under 100 ms. Full `interrupt` message support (in-session cancellation) is planned for a future version.

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

```json
{"type": "auth", "token": "sk-..."}
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

---

### Server → Client

Control messages are **JSON text frames**. Audio data is **binary frames**.

#### `auth_ok`
Authentication succeeded. The session is now active.

```json
{"type": "auth_ok"}
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

---

## Configuration

All settings use the `JOTA_` prefix and can be set via environment variables or a `.env` file.

| Variable | Default | Description |
|---|---|---|
| `JOTA_ENGINE` | `mock` | TTS engine: `mock` (silence, for tests) or `kokoro` |
| `JOTA_KOKORO_MODEL` | `kokoro-v1.0.int8.onnx` | Path to Kokoro ONNX model file |
| `JOTA_KOKORO_VOICES` | `voices-v1.0.bin` | Path to Kokoro voices file |
| `JOTA_KOKORO_VOICE` | `af_heart` | Default voice |
| `JOTA_KOKORO_LANG` | `en-us` | Default language |
| `JOTA_SAMPLE_RATE` | `24000` | Output sample rate (Hz) |
| `JOTA_MIN_FLUSH_CHARS` | `80` | Flush buffer to TTS after this many chars without a sentence boundary |
| `JOTA_AUTH_PROVIDER` | `stub` | Auth backend: `stub` (accept all) or `jota_db` |
| `JOTA_JOTA_DB_URL` | `http://localhost:8001` | jota-db base URL |
| `JOTA_JOTA_DB_AUTH_PATH` | `/auth/validate` | jota-db validation endpoint |
| `JOTA_JOTA_DB_TIMEOUT` | `5.0` | jota-db request timeout (seconds) |
| `JOTA_SESSION_TIMEOUT` | `300.0` | Max session duration in seconds (0 = unlimited) |
| `JOTA_QUEUE_MAXSIZE` | `100` | Max synthesis segments buffered per session |

See `.env.example` for a ready-to-copy template.

---

## Running with Docker

```bash
# Development (mock engine)
docker compose up

# Build only
docker compose build
```

The service listens on port `8005` by default. See `docker-compose.yml` for volume mounts and environment overrides.

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

---

## Running tests

```bash
python3 -m pytest -v
```

43 tests, ~0.5 s. Uses `JOTA_ENGINE=mock` and `JOTA_AUTH_PROVIDER=stub` automatically — no model files required.

Tests are also run automatically via GitHub Actions on every push and pull request to `main` (see `.github/workflows/test.yml`).
