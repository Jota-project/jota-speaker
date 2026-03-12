FLUSH_CHARS = frozenset(".!?\n")


class TokenAccumulator:
    """Pure logic (no I/O) that accumulates tokens and flushes on boundaries."""

    def __init__(self, min_flush_chars: int = 80) -> None:
        self._buffer = ""
        self._min_flush_chars = min_flush_chars

    def add(self, token: str) -> list[str]:
        """Add a token. Returns a list of segments ready for synthesis."""
        self._buffer += token
        return self._extract_segments()

    def flush(self) -> list[str]:
        """Force-flush whatever is in the buffer."""
        if not self._buffer.strip():
            self._buffer = ""
            return []
        segment = self._buffer.strip()
        self._buffer = ""
        return [segment]

    # ── internal ─────────────────────────────────────────────────────────────

    def _extract_segments(self) -> list[str]:
        segments: list[str] = []
        while True:
            segment = self._try_extract_one()
            if segment is None:
                break
            segments.append(segment)
        return segments

    def _try_extract_one(self) -> str | None:
        # Flush on sentence boundary
        for i, ch in enumerate(self._buffer):
            if ch in FLUSH_CHARS:
                segment = self._buffer[: i + 1].strip()
                self._buffer = self._buffer[i + 1 :]
                if segment:
                    return segment
                # boundary produced empty segment (e.g. leading whitespace) → keep going
                continue

        # Flush when buffer is long enough (no boundary found yet)
        if len(self._buffer) >= self._min_flush_chars:
            # Split at last space to avoid cutting mid-word
            split_at = self._buffer.rfind(" ")
            if split_at == -1:
                split_at = self._min_flush_chars
            segment = self._buffer[:split_at].strip()
            self._buffer = self._buffer[split_at:]
            if segment:
                return segment

        return None
