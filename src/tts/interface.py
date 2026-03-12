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
