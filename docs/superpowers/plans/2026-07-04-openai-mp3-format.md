# OpenAI mp3 response format — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `response_format: "mp3"` support to `POST /v1/audio/speech`, streamed via a piped `ffmpeg` subprocess, matching the design in `docs/superpowers/specs/2026-07-04-openai-mp3-format-design.md`.

**Architecture:** The engine keeps producing raw PCM16 LE mono, unchanged. A new `mp3_stream()` encoder in `src/openai/encoder.py` pipes those PCM chunks into an `ffmpeg` subprocess (`-f s16le ... -f mp3 -b:a 64k`) and yields its stdout as the encoded chunks arrive — same shape as the existing `wav_stream`/`pcm_stream`. `service.py` checks a new `app.state.mp3_available` flag (set once at startup by probing `shutil.which("ffmpeg")`) before touching the engine, returning a 503 `mp3_unavailable` envelope if ffmpeg is missing instead of failing per-request.

**Tech Stack:** Python 3.11, FastAPI, `asyncio.subprocess`, `ffmpeg` CLI (new runtime/CI dependency), pytest + pytest-asyncio.

## Global Constraints

- MP3 bitrate is fixed at 64 kbps CBR, mono — not configurable in this iteration.
- Only `mp3` is added. `opus`/`flac`/`aac` are explicitly out of scope (separate follow-up plans).
- A missing `ffmpeg` binary must never crash startup or break `pcm`/`wav` requests — only `mp3` requests are affected (503).
- Once a 200 response is committed, a mid-stream ffmpeg failure just truncates the stream (no JSON error) — matches existing wav/pcm behavior, do not try to change this.
- All new/changed Python code follows the existing style in `src/openai/`: `from __future__ import annotations` where the file already has it, module-level `_log = get_logger(__name__)` for logging, no comments explaining *what* the code does.

---

### Task 1: ffmpeg availability probe + Docker/CI wiring

**Files:**
- Modify: `src/openai/encoder.py`
- Modify: `Dockerfile`
- Modify: `.github/workflows/test.yml`
- Test: `tests/openai/test_encoder.py`

**Interfaces:**
- Produces: `ffmpeg_available() -> bool` in `src/openai/encoder.py` — `True` iff the `ffmpeg` binary is on `PATH`. Used by Task 3 to populate `app.state.mp3_available` at startup.

- [ ] **Step 1: Write the failing test for `ffmpeg_available()`**

Add to `tests/openai/test_encoder.py` (after the existing imports, which already include `shutil`):

```python
from src.openai.encoder import ffmpeg_available


class TestFfmpegAvailable:
    def test_true_when_binary_on_path(self, monkeypatch):
        monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/ffmpeg")
        assert ffmpeg_available() is True

    def test_false_when_binary_missing(self, monkeypatch):
        monkeypatch.setattr(shutil, "which", lambda name: None)
        assert ffmpeg_available() is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/openai/test_encoder.py::TestFfmpegAvailable -v`
Expected: FAIL with `ImportError: cannot import name 'ffmpeg_available'`

- [ ] **Step 3: Implement `ffmpeg_available()`**

In `src/openai/encoder.py`, the module currently starts with:

```python
from __future__ import annotations

import struct
from typing import AsyncIterator
```

Replace that import block with:

```python
from __future__ import annotations

import shutil
import struct
from typing import AsyncIterator
```

Then add, anywhere after the imports (e.g. right before `def build_wav_header`):

```python
def ffmpeg_available() -> bool:
    """Check whether the ffmpeg binary is on PATH."""
    return shutil.which("ffmpeg") is not None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/openai/test_encoder.py::TestFfmpegAvailable -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Add ffmpeg to the Docker runtime image**

In `Dockerfile`, the runtime stage currently has:

```dockerfile
# espeak-ng required by kokoro-onnx phonemizer
RUN apt-get update && apt-get install -y --no-install-recommends espeak-ng && \
    rm -rf /var/lib/apt/lists/*
```

Replace with:

```dockerfile
# espeak-ng required by kokoro-onnx phonemizer; ffmpeg required for mp3 encoding
RUN apt-get update && apt-get install -y --no-install-recommends espeak-ng ffmpeg && \
    rm -rf /var/lib/apt/lists/*
```

- [ ] **Step 6: Add ffmpeg to the CI workflow**

In `.github/workflows/test.yml`, currently:

```yaml
      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.11"
          cache: pip

      - name: Install dependencies
        run: pip install -e ".[dev]"
```

Replace with:

```yaml
      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.11"
          cache: pip

      - name: Install ffmpeg
        run: sudo apt-get update && sudo apt-get install -y --no-install-recommends ffmpeg

      - name: Install dependencies
        run: pip install -e ".[dev]"
```

- [ ] **Step 7: Verify the Docker image builds and has ffmpeg**

Run: `docker compose build jota-speaker && docker compose run --rm jota-speaker ffmpeg -version`
Expected: prints an `ffmpeg version ...` banner (exit code 0), confirming the binary is on `PATH` inside the image.

- [ ] **Step 8: Commit**

```bash
git add src/openai/encoder.py tests/openai/test_encoder.py Dockerfile .github/workflows/test.yml
git commit -m "feat(openai): add ffmpeg_available() probe + install ffmpeg in Docker/CI"
```

---

### Task 2: `mp3_stream` encoder

**Files:**
- Modify: `src/openai/encoder.py`
- Test: `tests/openai/test_encoder.py`

**Interfaces:**
- Consumes: `ffmpeg_available() -> bool` (Task 1, used only in tests here to decide skip).
- Produces: `mp3_stream(source: AsyncIterator[bytes], sample_rate: int) -> AsyncIterator[bytes]` in `src/openai/encoder.py`. Used by Task 3's `service.py` change.

- [ ] **Step 1: Write the failing tests**

Add to `tests/openai/test_encoder.py`:

```python
import subprocess

from src.openai.encoder import mp3_stream


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
class TestMp3Stream:
    @pytest.mark.asyncio
    async def test_produces_valid_mp3(self, tmp_path):
        # 1 second of silence at 24kHz mono PCM16, split into 200ms chunks
        # to mirror how KokoroEngine/MockEngine actually yield audio.
        silence = b"\x00" * (24000 * 2)
        chunk_size = 4800 * 2
        chunks = [silence[i : i + chunk_size] for i in range(0, len(silence), chunk_size)]

        result = bytearray()
        async for chunk in mp3_stream(_async_iter(chunks), 24000):
            result.extend(chunk)
        assert len(result) > 0

        mp3_path = tmp_path / "out.mp3"
        mp3_path.write_bytes(bytes(result))
        probe = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=format_name",
                "-of", "csv=p=0",
                str(mp3_path),
            ],
            capture_output=True,
            text=True,
        )
        assert probe.returncode == 0, f"ffprobe rejected output: {probe.stderr}"
        assert "mp3" in probe.stdout.lower()

    @pytest.mark.asyncio
    async def test_empty_input_does_not_hang(self):
        result = []
        async for chunk in mp3_stream(_async_iter([]), 24000):
            result.append(chunk)
        assert isinstance(result, list)  # completes without hanging/raising
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/openai/test_encoder.py::TestMp3Stream -v`
Expected: FAIL with `ImportError: cannot import name 'mp3_stream'`

- [ ] **Step 3: Implement `mp3_stream`**

In `src/openai/encoder.py`, update the imports to add `asyncio` and the logger:

```python
from __future__ import annotations

import asyncio
import shutil
import struct
from typing import AsyncIterator

from src.core.logger import get_logger

_log = get_logger(__name__)
```

Then add, at the end of the file (after `wav_stream`):

```python
_MP3_BITRATE = "64k"
_READ_CHUNK_SIZE = 4096


async def mp3_stream(
    source: AsyncIterator[bytes],
    sample_rate: int,
) -> AsyncIterator[bytes]:
    """Encode a PCM16 LE mono stream to MP3 (64 kbps CBR) via a piped ffmpeg subprocess.

    A background task feeds `source` chunks into ffmpeg's stdin and closes it
    once `source` is exhausted; this generator yields stdout chunks as ffmpeg
    produces them, so the first MP3 bytes can reach the client before the
    whole input has been synthesized.
    """
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg",
        "-loglevel", "error",
        "-f", "s16le",
        "-ar", str(sample_rate),
        "-ac", "1",
        "-i", "pipe:0",
        "-f", "mp3",
        "-b:a", _MP3_BITRATE,
        "pipe:1",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    async def _feed_stdin() -> None:
        try:
            async for chunk in source:
                proc.stdin.write(chunk)
                await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:
            _log.warning("mp3_stream: source raised while feeding ffmpeg: %s", exc)
        finally:
            if not proc.stdin.is_closing():
                proc.stdin.close()

    async def _drain_stderr() -> None:
        stderr = await proc.stderr.read()
        if stderr:
            _log.warning("ffmpeg stderr: %s", stderr.decode(errors="replace"))

    feed_task = asyncio.create_task(_feed_stdin())
    stderr_task = asyncio.create_task(_drain_stderr())

    try:
        while True:
            chunk = await proc.stdout.read(_READ_CHUNK_SIZE)
            if not chunk:
                break
            yield chunk
    finally:
        if proc.returncode is None:
            proc.kill()
        await asyncio.gather(feed_task, stderr_task, proc.wait(), return_exceptions=True)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/openai/test_encoder.py -v`
Expected: PASS (all tests in the file, including the pre-existing WAV/PCM ones)

- [ ] **Step 5: Commit**

```bash
git add src/openai/encoder.py tests/openai/test_encoder.py
git commit -m "feat(openai): add mp3_stream encoder (ffmpeg subprocess, 64kbps CBR)"
```

---

### Task 3: Wire `mp3` end-to-end (protocol, service, startup probe, docs)

**Files:**
- Modify: `src/openai/protocol.py`
- Modify: `src/openai/service.py`
- Modify: `src/main.py`
- Modify: `README.md`
- Test: `tests/openai/test_protocol.py`
- Test: `tests/openai/test_service.py`
- Test: `tests/openai/test_routes.py`

**Interfaces:**
- Consumes: `ffmpeg_available()` and `mp3_stream()` from `src/openai/encoder.py` (Tasks 1 & 2).
- Produces: `app.state.mp3_available: bool` (set in `src/main.py` lifespan) — read by `src/openai/service.py`.

- [ ] **Step 1: Update `SpeechRequest` to accept `"mp3"`**

In `src/openai/protocol.py`, currently:

```python
    response_format: Literal["pcm", "wav"] = "wav"
```

Replace with:

```python
    response_format: Literal["pcm", "wav", "mp3"] = "wav"
```

- [ ] **Step 2: Update `test_protocol.py` for the new accepted value**

In `tests/openai/test_protocol.py`, the happy-path test currently has this stale comment:

```python
        assert r.response_format == "wav"  # default (MVP: not "mp3")
```

Replace with:

```python
        assert r.response_format == "wav"  # default
```

Then replace the now-incorrect rejection test:

```python
    def test_response_format_mp3_rejected(self):
        with pytest.raises(ValidationError):
            SpeechRequest.model_validate({
                "model": "tts-1",
                "input": "hola",
                "response_format": "mp3",
            })
```

with an acceptance test in `TestSpeechRequestHappyPath` (move it there — it no longer belongs in `TestSpeechRequestRejections`):

```python
    def test_response_format_mp3_accepted(self):
        r = SpeechRequest.model_validate({
            "model": "tts-1",
            "input": "hola",
            "response_format": "mp3",
        })
        assert r.response_format == "mp3"
```

`test_response_format_opus_rejected` stays unchanged — `opus` is still out of scope.

- [ ] **Step 3: Run protocol tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/openai/test_protocol.py -v`
Expected: PASS (all tests, including the new `test_response_format_mp3_accepted`)

- [ ] **Step 4: Probe ffmpeg at startup**

In `src/main.py`, currently:

```python
    app.state.settings = settings
    app.state.engine = create_engine(settings)
    app.state.auth = create_auth_provider(settings)
```

Replace with:

```python
    app.state.settings = settings
    app.state.engine = create_engine(settings)
    app.state.auth = create_auth_provider(settings)

    from src.openai.encoder import ffmpeg_available

    app.state.mp3_available = ffmpeg_available()
    if not app.state.mp3_available:
        logger.warning("ffmpeg not found on PATH — POST /v1/audio/speech with response_format=mp3 will return 503")
```

- [ ] **Step 5: Write the failing service-layer tests**

In `tests/openai/test_service.py`, update the shared fixture to default `mp3_available` to `True` (mirrors how it already wires a real `engine`). Currently:

```python
    class _FakeSettings:
        engine = "mock"
        kokoro_voice = "ef_dora"
        kokoro_lang = "es"
        sample_rate = 24000

    app.state.settings = _FakeSettings()
```

Replace with:

```python
    class _FakeSettings:
        engine = "mock"
        kokoro_voice = "ef_dora"
        kokoro_lang = "es"
        sample_rate = 24000

    app.state.settings = _FakeSettings()
    app.state.mp3_available = True
```

Then add a new test class (anywhere after `TestEngineFailures`/before `TestInputValidation`, matching the file's existing class-per-concern layout):

```python
class TestMp3Format:
    def test_mp3_unavailable_returns_503(self, app_with_engine_and_stub_auth):
        app_with_engine_and_stub_auth.state.mp3_available = False
        client = TestClient(app_with_engine_and_stub_auth)
        resp = client.post(
            "/v1/audio/speech",
            headers={"Authorization": "Bearer t"},
            json={"model": "tts-1", "input": "hola", "response_format": "mp3"},
        )
        assert resp.status_code == 503
        assert resp.json()["error"]["code"] == "mp3_unavailable"
        assert resp.json()["error"]["type"] == "server_error"
```

- [ ] **Step 6: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/openai/test_service.py::TestMp3Format -v`
Expected: FAIL — `response_format: mp3` is now accepted by Pydantic (Step 1) and reaches `handle_speech_request`, which doesn't know about `mp3` yet, so it falls into the `pcm_stream` else-branch and returns 200 instead of 503.

- [ ] **Step 7: Wire the mp3 branch into `handle_speech_request`**

In `src/openai/service.py`, add the import:

```python
from src.openai.encoder import mp3_stream, pcm_stream, wav_stream
```

(replacing the current `from src.openai.encoder import pcm_stream, wav_stream`).

Then, currently:

```python
    # ── 5. Synthesize (the streaming source) ───────────────────────────────
    engine = getattr(state, "engine", None)
    if engine is None or not hasattr(engine, "synthesize"):
        raise OpenAIEngineError(
            code="engine_unavailable",
            message="TTS engine not initialized",
            status_code=503,
        )
    try:
        engine_stream: AsyncIterator[bytes] = engine.synthesize(normalized)
    except Exception as exc:
        raise OpenAIEngineError(
            code="engine_error",
            message=f"TTS engine error before stream start: {exc}",
        )

    # ── 6. Wrap with encoder ────────────────────────────────────────────────
    fmt = request_body.response_format
    if fmt == "wav":
        output_stream = wav_stream(engine_stream, engine.sample_rate)
        media_type = "audio/wav"
    else:
        output_stream = pcm_stream(engine_stream)
        media_type = "audio/pcm"
```

Replace with:

```python
    # ── 5. Synthesize (the streaming source) ───────────────────────────────
    fmt = request_body.response_format
    if fmt == "mp3" and not getattr(state, "mp3_available", False):
        raise OpenAIEngineError(
            code="mp3_unavailable",
            message="mp3 encoding not available (ffmpeg not installed)",
            status_code=503,
        )

    engine = getattr(state, "engine", None)
    if engine is None or not hasattr(engine, "synthesize"):
        raise OpenAIEngineError(
            code="engine_unavailable",
            message="TTS engine not initialized",
            status_code=503,
        )
    try:
        engine_stream: AsyncIterator[bytes] = engine.synthesize(normalized)
    except Exception as exc:
        raise OpenAIEngineError(
            code="engine_error",
            message=f"TTS engine error before stream start: {exc}",
        )

    # ── 6. Wrap with encoder ────────────────────────────────────────────────
    if fmt == "mp3":
        output_stream = mp3_stream(engine_stream, engine.sample_rate)
        media_type = "audio/mpeg"
    elif fmt == "wav":
        output_stream = wav_stream(engine_stream, engine.sample_rate)
        media_type = "audio/wav"
    else:
        output_stream = pcm_stream(engine_stream)
        media_type = "audio/pcm"
```

Note the docstring's numbered lifecycle comment above `handle_speech_request` (steps 1-7) does not need renumbering — the mp3 check is folded into step 5, matching how it reads a check before synthesis just like the existing `engine is None` check.

- [ ] **Step 8: Run the service test to verify it passes**

Run: `.venv/bin/python -m pytest tests/openai/test_service.py -v`
Expected: PASS (all tests, including `TestMp3Format::test_mp3_unavailable_returns_503`)

- [ ] **Step 9: Add e2e route tests for the mp3 happy path**

In `tests/openai/test_routes.py`, update the `app` fixture the same way as Task 3 Step 5 — find:

```python
    class _Settings:
        engine = "mock"
        kokoro_voice = "ef_dora"
        kokoro_lang = "es"
        sample_rate = 24000

    app.state.settings = _Settings()
```

Replace with:

```python
    class _Settings:
        engine = "mock"
        kokoro_voice = "ef_dora"
        kokoro_lang = "es"
        sample_rate = 24000

    app.state.settings = _Settings()
    app.state.mp3_available = True
```

Then add, at the end of the file, imports for `shutil` at the top of the file (add `import shutil` next to the existing `import pytest`), and a new test class:

```python
class TestMp3Format:
    @pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
    def test_mp3_format_returns_200_audio_mpeg(self, app):
        client = TestClient(app)
        resp = client.post(
            "/v1/audio/speech",
            headers={"Authorization": "Bearer t"},
            json={"model": "tts-1", "input": "hola", "response_format": "mp3"},
        )
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "audio/mpeg"
        assert len(resp.content) > 0

    def test_mp3_unavailable_returns_503(self, app):
        app.state.mp3_available = False
        client = TestClient(app)
        resp = client.post(
            "/v1/audio/speech",
            headers={"Authorization": "Bearer t"},
            json={"model": "tts-1", "input": "hola", "response_format": "mp3"},
        )
        assert resp.status_code == 503
        assert resp.json()["error"]["code"] == "mp3_unavailable"
```

- [ ] **Step 10: Run the full openai test suite to verify everything passes**

Run: `.venv/bin/python -m pytest tests/openai/ -v`
Expected: PASS (all tests, including the two new `TestMp3Format` classes)

- [ ] **Step 11: Run the entire project test suite for regressions**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: PASS, same count as before plus the new mp3 tests, no failures elsewhere

- [ ] **Step 12: Update README documentation**

In `README.md`, currently:

```markdown
| `response_format` | string | no | `"wav"` | MVP supports `"pcm"` and `"wav"` only. Other values return 400. |
```

Replace with:

```markdown
| `response_format` | string | no | `"wav"` | Supports `"pcm"`, `"wav"`, and `"mp3"` (64 kbps CBR mono, via ffmpeg). Other values return 400. |
```

And currently:

```markdown
**Response:** 200 OK with the audio streamed chunked:
- `Content-Type: audio/pcm` for raw PCM16, or `audio/wav` for RIFF/WAVE.
- `X-Request-Id`, `X-Model-Used`, `X-Voice-Used` headers.
- WAV header has `0xFFFFFFFF` length sentinels (standard streaming practice; all modern players handle it).
```

Replace with:

```markdown
**Response:** 200 OK with the audio streamed chunked:
- `Content-Type: audio/pcm` for raw PCM16, `audio/wav` for RIFF/WAVE, or `audio/mpeg` for MP3 (64 kbps CBR mono).
- `X-Request-Id`, `X-Model-Used`, `X-Voice-Used` headers.
- WAV header has `0xFFFFFFFF` length sentinels (standard streaming practice; all modern players handle it).
- If `response_format=mp3` is requested but ffmpeg isn't installed on the host, returns `503` with error code `mp3_unavailable` instead of failing to start up.
```

- [ ] **Step 13: Rebuild and smoke-test the running container**

Run: `docker compose build jota-speaker && docker compose up -d jota-speaker`

Then, once healthy (`docker ps` shows `(healthy)`):

```bash
curl -X POST http://localhost:8005/v1/audio/speech \
  -H "Authorization: Bearer test-token" \
  -H "Content-Type: application/json" \
  -d '{"model":"tts-1","input":"Hola mundo","response_format":"mp3"}' \
  --output /tmp/output.mp3 -D - -w "\nHTTP_STATUS:%{http_code}\n"
file /tmp/output.mp3
```

Expected: `HTTP_STATUS:200`, `content-type: audio/mpeg`, and `file` reports `MPEG ADTS, layer III` (or similar MP3 identification).

- [ ] **Step 14: Commit**

```bash
git add src/openai/protocol.py src/openai/service.py src/main.py README.md \
        tests/openai/test_protocol.py tests/openai/test_service.py tests/openai/test_routes.py
git commit -m "feat(openai): wire mp3 response_format end-to-end (protocol, service, startup probe, docs)"
```

---

## Self-Review Notes

- **Spec coverage:** ffmpeg probe (Task 1) ✓, mp3_stream encoder (Task 2) ✓, protocol Literal (Task 3.1-3) ✓, service branch + 503 (Task 3.4-8) ✓, Docker/CI (Task 1.5-7) ✓, README (Task 3.12) ✓, e2e tests (Task 3.9-11) ✓. Buffered/Content-Length mode and opus/flac/aac are explicitly out of scope per the spec and are not tasked here.
- **Type consistency:** `mp3_stream(source: AsyncIterator[bytes], sample_rate: int) -> AsyncIterator[bytes]` (Task 2) matches the call site in `service.py` (Task 3 Step 7): `mp3_stream(engine_stream, engine.sample_rate)`. `ffmpeg_available() -> bool` (Task 1) matches its use in `main.py` (Task 3 Step 4) and in tests (Task 1 Step 1).
- **Placeholder scan:** none found — every step has literal code, exact file paths, and runnable commands.
