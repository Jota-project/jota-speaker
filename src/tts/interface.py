from abc import ABC, abstractmethod
from typing import AsyncIterator


class ITTSEngine(ABC):
    @abstractmethod
    async def synthesize(
        self, text: str, *, voice: str | None = None, speed: float | None = None
    ) -> AsyncIterator[bytes]:
        """Yield PCM16 LE mono audio frames for the given text.

        Args:
            text: Text to synthesize.
            voice: Override the default engine voice for this request. None = use engine default.
            speed: Synthesis speed multiplier. None = use engine default (1.0).
        """
        ...

    @property
    @abstractmethod
    def sample_rate(self) -> int: ...

    @abstractmethod
    async def aclose(self) -> None:
        """Release engine resources (thread pools, native handles)."""
        ...

    @abstractmethod
    def list_voices(self) -> list[str]:
        """List available voice names from the voices pack."""
        ...

    # Optional: engines may set this to bound blocking inference calls.
    # None means no timeout. The session will use this to wrap run_in_executor.
    synthesize_timeout: float | None = None