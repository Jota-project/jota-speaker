from abc import ABC, abstractmethod


class IAuthProvider(ABC):
    @abstractmethod
    async def validate(self, token: str) -> bool:
        """Return True if the token is valid, False if invalid.
        Raises on network / infrastructure errors (fail-closed)."""
        ...
