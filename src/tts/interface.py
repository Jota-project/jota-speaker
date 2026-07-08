from abc import ABC, abstractmethod
from typing import AsyncIterator


class ITTSEngine(ABC):
    @abstractmethod
    async def synthesize(
        self, text: str, voice: str | None = None, speed: float | None = None
    ) -> AsyncIterator[bytes]:
        """Yield PCM16 LE mono audio frames for the given text.

        `speed` is assumed already resolved via `resolve_speed()` — this
        method does not re-validate or clamp it.
        """
        ...

    @property
    @abstractmethod
    def sample_rate(self) -> int: ...

    @property
    def default_voice(self) -> str:
        """Voice used when no override is requested, or the override isn't usable."""
        return ""

    def available_voices(self) -> list[str] | None:
        """Voice ids valid for `synthesize(voice=...)`. None means unrestricted."""
        return None

    @property
    def is_ready(self) -> bool:
        """Whether this engine can currently serve synthesize()/available_voices().

        False while the model is still loading, or if it was torn down
        (e.g. mid-shutdown). Used by GET /ready.
        """
        return True

    def resolve_voice(self, requested: str | None) -> str:
        """Return `requested` if usable, else `default_voice`. Never raises."""
        available = self.available_voices()
        if requested and (available is None or requested in available):
            return requested
        return self.default_voice

    def resolve_speed(self, requested: float | None) -> float:
        """Return `requested` clamped to whatever range this engine supports.

        Base implementation is unrestricted (no clamping) — 1.0 when
        `requested` is None. Never raises.
        """
        return requested if requested is not None else 1.0

    @abstractmethod
    async def aclose(self) -> None:
        """Release engine resources (thread pools, native handles)."""
        ...

    # Optional: engines may set this to bound blocking inference calls.
    # None means no timeout. The session will use this to wrap run_in_executor.
    synthesize_timeout: float | None = None
