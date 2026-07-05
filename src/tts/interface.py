from abc import ABC, abstractmethod
from typing import AsyncIterator


class ITTSEngine(ABC):
    @abstractmethod
    async def synthesize(self, text: str, voice: str | None = None) -> AsyncIterator[bytes]:
        """Yield PCM16 LE mono audio frames for the given text."""
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

    def resolve_voice(self, requested: str | None) -> str:
        """Return `requested` if usable, else `default_voice`. Never raises."""
        available = self.available_voices()
        if requested and (available is None or requested in available):
            return requested
        return self.default_voice

    @abstractmethod
    async def aclose(self) -> None:
        """Release engine resources (thread pools, native handles)."""
        ...

    # Optional: engines may set this to bound blocking inference calls.
    # None means no timeout. The session will use this to wrap run_in_executor.
    synthesize_timeout: float | None = None
