from abc import ABC, abstractmethod
from typing import AsyncIterator


class ITTSEngine(ABC):
    @abstractmethod
    async def synthesize(self, text: str) -> AsyncIterator[bytes]:
        """Yield PCM16 LE mono audio frames for the given text."""
        ...

    @property
    @abstractmethod
    def sample_rate(self) -> int: ...

    @abstractmethod
    async def aclose(self) -> None:
        """Release engine resources (thread pools, native handles)."""
        ...

    # Optional: engines may set this to bound blocking inference calls.
    # None means no timeout. The session will use this to wrap run_in_executor.
    synthesize_timeout: float | None = None