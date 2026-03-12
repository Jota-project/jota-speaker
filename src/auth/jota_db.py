import httpx

from src.core.config import Settings
from src.core.logger import get_logger
from .interface import IAuthProvider

logger = get_logger(__name__)


class JotaDbAuthProvider(IAuthProvider):
    """Validates tokens by calling jota-db via HTTP POST."""

    def __init__(self, settings: Settings) -> None:
        self._url = settings.jota_db_url.rstrip("/") + settings.jota_db_auth_path
        self._timeout = settings.jota_db_timeout

    async def validate(self, token: str) -> bool:
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.post(self._url, json={"token": token})

        if response.status_code == 200:
            return True
        if response.status_code in (401, 403):
            return False
        # Any other status (5xx etc.) → fail closed
        logger.error(
            "jota-db auth endpoint returned unexpected status %d", response.status_code
        )
        raise RuntimeError(
            f"jota-db auth returned unexpected status {response.status_code}"
        )
