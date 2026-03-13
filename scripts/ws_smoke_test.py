"""
Smoke test: simula un LLM enviando tokens por WS mientras se reciben
y reproducen frames PCM16 de forma simultánea.

Tres tareas corren en paralelo vía asyncio.gather:
  - token_producer  → envía tokens al ritmo de un LLM
  - frame_consumer  → recibe frames/control y los encola para reproducción
  - audio_player    → drena la queue y escribe en sounddevice en tiempo real

Uso:
    python3 scripts/ws_smoke_test.py [--url ws://localhost:8005/ws] [--token TOKEN] [--no-play]
"""

import argparse
import asyncio
import json
import time
from dataclasses import dataclass, field

import websockets
from websockets.exceptions import ConnectionClosed

# ── Tokens de prueba (simulan salida de un LLM) ───────────────────────────────

TOKEN_STREAM = [
    "Hello", ",", " this", " is", " a", " streaming", " test",
    ".", " The", " quick", " brown", " fox", " jumps", " over",
    " the", " lazy", " dog", ".", " How", " are", " you",
    " doing", " today", "?",
]

INTER_TOKEN_DELAY = 0.05   # segundos entre tokens

_SENTINEL = object()        # señal de fin para la queue de audio


# ── Estado compartido ─────────────────────────────────────────────────────────

@dataclass
class Stats:
    tokens_sent: int = 0
    audio_chunks: list[dict] = field(default_factory=list)
    total_audio_bytes: int = 0
    first_audio_ts: float | None = None
    done_ts: float | None = None
    start_ts: float = field(default_factory=time.monotonic)

    def ttfa_ms(self) -> float | None:
        return (self.first_audio_ts - self.start_ts) * 1000 if self.first_audio_ts else None

    def total_ms(self) -> float | None:
        return (self.done_ts - self.start_ts) * 1000 if self.done_ts else None

    def audio_duration_ms(self) -> float:
        return self.total_audio_bytes / 2 / 24000 * 1000


# ── Tarea 1: envía tokens ─────────────────────────────────────────────────────

async def token_producer(ws, stats: Stats) -> None:
    for token in TOKEN_STREAM:
        await ws.send(json.dumps({"type": "token", "text": token}))
        stats.tokens_sent += 1
        print(f"  → token [{stats.tokens_sent:02d}/{len(TOKEN_STREAM)}]: {token!r}")
        await asyncio.sleep(INTER_TOKEN_DELAY)

    await ws.send(json.dumps({"type": "end"}))
    print(f"  → end  ({len(TOKEN_STREAM)} tokens enviados)")


# ── Tarea 2: recibe mensajes WS y encola frames ───────────────────────────────

async def frame_consumer(ws, stats: Stats, audio_queue: asyncio.Queue) -> None:
    current_chunk: dict | None = None

    async for message in ws:
        if isinstance(message, bytes):
            if stats.first_audio_ts is None:
                stats.first_audio_ts = time.monotonic()
            stats.total_audio_bytes += len(message)
            if current_chunk:
                current_chunk["bytes"] += len(message)
            print(f"  ← frame  {len(message):>6} B  "
                  f"(acum: {stats.total_audio_bytes // 1024} KB)")
            await audio_queue.put(message)

        else:
            msg = json.loads(message)
            t = msg["type"]

            if t == "auth_ok":
                print("  ← auth_ok")

            elif t == "auth_error":
                print(f"  ✗ auth_error: {msg.get('reason')}")
                await audio_queue.put(_SENTINEL)
                return

            elif t == "audio_start":
                current_chunk = {"chunk_id": msg["chunk_id"], "bytes": 0}
                stats.audio_chunks.append(current_chunk)
                print(f"  ← audio_start  chunk={msg['chunk_id']}  {msg['sample_rate']} Hz")

            elif t == "audio_end":
                kb = current_chunk["bytes"] // 1024 if current_chunk else 0
                print(f"  ← audio_end    chunk={msg['chunk_id']}  ({kb} KB)")
                current_chunk = None

            elif t == "done":
                stats.done_ts = time.monotonic()
                print("  ← done")
                await audio_queue.put(_SENTINEL)
                return

            elif t == "error":
                print(f"  ✗ error [{msg.get('code')}]: {msg.get('message')}")
                await audio_queue.put(_SENTINEL)
                return


# ── Tarea 3: reproduce frames en tiempo real ──────────────────────────────────

async def audio_player(audio_queue: asyncio.Queue, sample_rate: int, play: bool) -> None:
    if not play:
        # Drena la queue sin reproducir
        while True:
            item = await audio_queue.get()
            if item is _SENTINEL:
                return

    try:
        import sounddevice as sd
    except ImportError:
        print("  [aviso] sounddevice no instalado — audio no reproducido")
        while True:
            item = await audio_queue.get()
            if item is _SENTINEL:
                return

    loop = asyncio.get_running_loop()

    def write_loop(stream: "sd.RawOutputStream") -> None:
        """Corre en un hilo aparte para no bloquear el event loop."""
        while True:
            # get() bloqueante desde el hilo — usa run_coroutine_threadsafe
            future = asyncio.run_coroutine_threadsafe(audio_queue.get(), loop)
            item = future.result()
            if item is _SENTINEL:
                break
            stream.write(item)

    with sd.RawOutputStream(samplerate=sample_rate, channels=1, dtype="int16"):
        # Abrimos el stream, pero la escritura real la hacemos en un executor
        pass

    # Reabrimos correctamente (el with anterior solo comprueba que sd funciona)
    stream = sd.RawOutputStream(samplerate=sample_rate, channels=1, dtype="int16")
    stream.start()
    try:
        await loop.run_in_executor(None, write_loop, stream)
    finally:
        stream.stop()
        stream.close()


# ── Entry point ───────────────────────────────────────────────────────────────

async def run(url: str, token: str, play: bool, sample_rate: int) -> None:
    print(f"\nConectando a {url} …")
    print(f"Reproducción de audio: {'sí' if play else 'no'}\n")
    stats = Stats()
    audio_queue: asyncio.Queue = asyncio.Queue()

    async with websockets.connect(url) as ws:
        await ws.send(json.dumps({"type": "auth", "token": token}))

        try:
            await asyncio.gather(
                token_producer(ws, stats),
                frame_consumer(ws, stats, audio_queue),
                audio_player(audio_queue, sample_rate, play),
            )
        except ConnectionClosed as exc:
            print(f"\n  WS cerrado: {exc}")
            await audio_queue.put(_SENTINEL)

    print("\n" + "─" * 50)
    print("RESUMEN")
    print("─" * 50)
    print(f"  Tokens enviados     : {stats.tokens_sent}")
    print(f"  Segmentos de audio  : {len(stats.audio_chunks)}")
    print(f"  Audio total         : {stats.total_audio_bytes:,} B  "
          f"({stats.audio_duration_ms():.0f} ms)")
    if (ttfa := stats.ttfa_ms()) is not None:
        print(f"  Time-to-first-audio : {ttfa:.0f} ms")
    if (tt := stats.total_ms()) is not None:
        print(f"  Tiempo total        : {tt:.0f} ms")
    print("─" * 50)


def main() -> None:
    parser = argparse.ArgumentParser(description="jota-speaker WS smoke test")
    parser.add_argument("--url", default="ws://localhost:8005/ws")
    parser.add_argument("--token", default="smoke-test")
    parser.add_argument("--no-play", dest="play", action="store_false",
                        help="recibe audio pero no lo reproduce")
    parser.add_argument("--sample-rate", type=int, default=24000)
    args = parser.parse_args()
    asyncio.run(run(args.url, args.token, args.play, args.sample_rate))


if __name__ == "__main__":
    main()
