"""
Smoke test: simula un LLM enviando tokens por WS mientras se reciben
frames PCM16 de forma simultánea (productor y consumidor async en paralelo).

Uso:
    python3 scripts/ws_smoke_test.py [--url ws://localhost:8002/ws] [--token mytoken]
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

INTER_TOKEN_DELAY = 0.05   # segundos entre tokens (simula cadencia LLM)


# ── Estado compartido ─────────────────────────────────────────────────────────

@dataclass
class Stats:
    tokens_sent: int = 0
    text_messages: list[dict] = field(default_factory=list)
    audio_chunks: list[dict] = field(default_factory=list)  # {chunk_id, bytes}
    total_audio_bytes: int = 0
    first_audio_ts: float | None = None
    done_ts: float | None = None
    start_ts: float = field(default_factory=time.monotonic)

    def time_to_first_audio(self) -> float | None:
        if self.first_audio_ts:
            return self.first_audio_ts - self.start_ts
        return None

    def total_time(self) -> float | None:
        if self.done_ts:
            return self.done_ts - self.start_ts
        return None

    def audio_duration_ms(self) -> float:
        # PCM16 mono 24 kHz → 2 bytes/sample → 24000 samples/s
        return self.total_audio_bytes / 2 / 24000 * 1000


# ── Productor: envía tokens ───────────────────────────────────────────────────

async def token_producer(ws, stats: Stats) -> None:
    for token in TOKEN_STREAM:
        await ws.send(json.dumps({"type": "token", "text": token}))
        stats.tokens_sent += 1
        print(f"  → token [{stats.tokens_sent:02d}/{len(TOKEN_STREAM)}]: {token!r}")
        await asyncio.sleep(INTER_TOKEN_DELAY)

    await ws.send(json.dumps({"type": "end"}))
    print(f"  → end  (all {len(TOKEN_STREAM)} tokens sent)")


# ── Consumidor: recibe frames y mensajes de control ───────────────────────────

async def frame_consumer(ws, stats: Stats) -> None:
    current_chunk: dict | None = None

    async for message in ws:
        if isinstance(message, bytes):
            # Frame PCM16
            if stats.first_audio_ts is None:
                stats.first_audio_ts = time.monotonic()
            stats.total_audio_bytes += len(message)
            if current_chunk:
                current_chunk["bytes"] += len(message)
            print(f"  ← audio frame  {len(message):>6} bytes  "
                  f"(total: {stats.total_audio_bytes // 1024} KB)")

        else:
            msg = json.loads(message)
            stats.text_messages.append(msg)
            t = msg["type"]

            if t == "auth_ok":
                print("  ← auth_ok")

            elif t == "auth_error":
                print(f"  ✗ auth_error: {msg.get('reason')}")
                return

            elif t == "audio_start":
                current_chunk = {"chunk_id": msg["chunk_id"], "bytes": 0}
                stats.audio_chunks.append(current_chunk)
                print(f"  ← audio_start  chunk={msg['chunk_id']}  "
                      f"rate={msg['sample_rate']} Hz")

            elif t == "audio_end":
                if current_chunk:
                    print(f"  ← audio_end    chunk={msg['chunk_id']}  "
                          f"({current_chunk['bytes'] // 1024} KB in chunk)")
                current_chunk = None

            elif t == "done":
                stats.done_ts = time.monotonic()
                print("  ← done")
                return

            elif t == "error":
                print(f"  ✗ error [{msg.get('code')}]: {msg.get('message')}")
                return


# ── Entry point ───────────────────────────────────────────────────────────────

async def run(url: str, token: str) -> None:
    print(f"\nConnecting to {url} …\n")
    stats = Stats()

    async with websockets.connect(url) as ws:
        # Auth (síncrono, antes de arrancar las tareas paralelas)
        await ws.send(json.dumps({"type": "auth", "token": token}))

        # Lanzamos productor y consumidor en paralelo
        try:
            await asyncio.gather(
                token_producer(ws, stats),
                frame_consumer(ws, stats),
            )
        except ConnectionClosed as exc:
            print(f"\n  WS closed: {exc}")

    # ── Resumen ───────────────────────────────────────────────────────────────
    print("\n" + "─" * 50)
    print("RESUMEN")
    print("─" * 50)
    print(f"  Tokens enviados     : {stats.tokens_sent}")
    print(f"  Segmentos de audio  : {len(stats.audio_chunks)}")
    print(f"  Audio total         : {stats.total_audio_bytes:,} bytes "
          f"({stats.audio_duration_ms():.0f} ms)")
    if (ttfa := stats.time_to_first_audio()) is not None:
        print(f"  Time-to-first-audio : {ttfa * 1000:.0f} ms")
    if (tt := stats.total_time()) is not None:
        print(f"  Tiempo total        : {tt * 1000:.0f} ms")
    print("─" * 50)


def main() -> None:
    parser = argparse.ArgumentParser(description="jota-speaker WS smoke test")
    parser.add_argument("--url", default="ws://localhost:8002/ws")
    parser.add_argument("--token", default="smoke-test")
    args = parser.parse_args()
    asyncio.run(run(args.url, args.token))


if __name__ == "__main__":
    main()
