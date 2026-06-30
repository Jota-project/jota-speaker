from abc import ABC, abstractmethod

from src.core.logger import get_logger

logger = get_logger(__name__)


class INormalizer(ABC):
    """Normalize text to its spoken form before TTS synthesis."""

    @abstractmethod
    async def normalize(self, text: str) -> str:
        """Return the spoken-form equivalent of `text`. Must never raise."""
        ...


class PassThroughNormalizer(INormalizer):
    """No-op normalizer. Used when settings.normalizer == 'none'."""

    async def normalize(self, text: str) -> str:
        return text
