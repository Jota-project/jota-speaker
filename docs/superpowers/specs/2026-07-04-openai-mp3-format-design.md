# OpenAI-compatible mp3 support — Design Spec

**Date:** 2026-07-04
**Branch:** main (post PR #8, MVP `POST /v1/audio/speech`)
**Status:** 📝 Design

## Context

PR #8 shipped a streaming, OpenAI-compatible `POST /v1/audio/speech` MVP supporting `response_format: pcm | wav`. The real OpenAI TTS API also supports `mp3`, `opus`, `aac`, and `flac`. This spec covers **mp3 only** — the most commonly requested format — to validate the ffmpeg-subprocess pattern before repeating it for the other three formats in a follow-up.

Two adjacent questions came up during brainstorming and were deliberately pushed out of scope:

- **Buffered (Content-Length) response mode**: real OpenAI has no `stream` field on this endpoint — the wire behavior is always chunked; a client choosing to buffer is a client-side decision, not a server mode. Since jota-speaker's existing chunked implementation already matches that behavior, no work is needed to "match OpenAI" here. Tracked for future reconsideration (e.g. if a strict player needs an exact `Content-Length`) in [issue #9](https://github.com/Jota-project/jota-speaker/issues/9).
- **`opus`/`flac`/`aac`, `GET /v1/models`, honoring `voice`/`speed`, SSE realtime**: separate gaps from the PR #8 "Out of MVP" list, each to get its own spec.

## Goal

A client sending `response_format: "mp3"` gets a valid, streamed MP3 (64 kbps CBR, mono) with the same low-latency-to-first-byte property the wav/pcm paths already have, encoded via an `ffmpeg` subprocess piped from the engine's PCM output.

## Non-goals

- `opus`, `flac`, `aac` — same pattern, separate follow-up PRs.
- Configurable bitrate — fixed at 64 kbps CBR for this iteration.
- Recovering a mid-stream ffmpeg crash into a clean JSON error — once the 200 is committed, a failure truncates the stream (matches the existing documented limitation for wav/pcm).
- Buffered/Content-Length response mode (see [issue #9](https://github.com/Jota-project/jota-speaker/issues/9)).

## Architecture

```
POST /v1/audio/speech {response_format: "mp3"}
        ↓
handle_speech_request (service.py) — auth, input validation (unchanged)
        ↓
if fmt == "mp3" and not state.mp3_available:
        → OpenAIEngineError(code="mp3_unavailable", status_code=503)
        ↓ (else)
engine.synthesize(text) → AsyncIterator[bytes]  (unchanged, still raw PCM16 LE mono)
        ↓
mp3_stream(engine_stream, sample_rate)  [encoder.py, new]
        ↓
   ┌───────────────────────────────────────────────────────────┐
   │  ffmpeg -f s16le -ar {sample_rate} -ac 1 -i pipe:0         │
   │         -f mp3 -b:a 64k pipe:1                             │
   │                                                            │
   │  task A: write each PCM chunk from engine_stream to stdin, │
   │          close stdin when engine_stream is exhausted       │
   │  task B (drain): read stderr, log on non-zero exit         │
   │  generator: read stdout chunks, yield each as it arrives   │
   └───────────────────────────────────────────────────────────┘
        ↓
StreamingResponse(media_type="audio/mpeg", headers=...)
```

### Components

#### New

- **`mp3_stream(source: AsyncIterator[bytes], sample_rate: int) -> AsyncIterator[bytes]`** in `src/openai/encoder.py`. Spawns the ffmpeg subprocess via `asyncio.create_subprocess_exec`, runs the stdin-writer and stderr-drain as background `asyncio.Task`s, yields stdout chunks. On generator close/exception, kills the process and awaits it in a `finally` to avoid zombies.
- **ffmpeg availability probe** in `src/main.py` lifespan: `shutil.which("ffmpeg")` (or a cheap `ffmpeg -version` subprocess call), stored as `app.state.mp3_available: bool`. Logs a warning if not found; does **not** block startup.
- **`tests/openai/test_encoder.py`** additions: real-ffmpeg tests for `mp3_stream`, skipped via `pytest.mark.skipif(shutil.which("ffmpeg") is None, ...)` — mirrors how Kokoro-model-dependent tests are already skipped when the model isn't present.

#### Modified

- **`src/openai/protocol.py`**: `response_format: Literal["pcm", "wav", "mp3"]`.
- **`src/openai/service.py`**: branch on `fmt == "mp3"` — check `state.mp3_available` (503 `mp3_unavailable` if missing, same envelope shape as the existing `engine_unavailable` path), else wrap with `mp3_stream(...)`, `media_type="audio/mpeg"`.
- **`Dockerfile`**: add `ffmpeg` to the `apt-get install` line alongside `espeak-ng`.
- **`.github/workflows/test.yml`**: add an explicit `ffmpeg` install step so the real-ffmpeg encoder tests never silently skip in CI.
- **`README.md`**: document `mp3` in the `response_format` table (64 kbps CBR mono, via ffmpeg).

## Data flow & error handling

1. Client validation (auth, input) is unchanged.
2. `mp3_available` is checked **before** touching the engine — same early-exit shape as the existing `engine is None` → 503 check.
3. `engine.synthesize(text)` is untouched: it still only ever produces raw PCM. All format-specific work stays in the encoder layer.
4. Once the subprocess is spawned, the async generator IS what `StreamingResponse` iterates — headers (`X-Request-Id`, `X-Model-Used`, `X-Voice-Used`, `Cache-Control`) are unchanged, only `media_type` differs per format.
5. **Mid-stream failure** (ffmpeg crashes, engine raises after the first chunk): once the 200 is committed there's no way to turn it into a JSON error — this matches the existing documented behavior for wav/pcm. The only addition is process hygiene: `proc.kill()` + `await proc.wait()` in a `finally` so a crashed/abandoned ffmpeg process doesn't linger.
6. **Backpressure**: if the client reads slowly, ffmpeg's stdout pipe fills and blocks the writer task — accepted for this iteration, same as a normal OS pipe; no extra buffering added.

## Testing

- `tests/openai/test_encoder.py`: `mp3_stream` against real ffmpeg (skipped if ffmpeg absent) — output starts with a valid MP3 frame sync / ID3 header and round-trips through `ffprobe` (same verification style already used for the WAV header).
- `tests/openai/test_routes.py`:
  - e2e `response_format=mp3` → 200, `content-type: audio/mpeg`.
  - `state.mp3_available = False` → 503, `error.code == "mp3_unavailable"`.
- `tests/openai/test_service.py`: unit test of the 503 path via monkeypatched `state.mp3_available`, no real ffmpeg needed.
- CI: explicit `ffmpeg` install step (see Components) so the skip-if-absent guard never masks a real regression.
