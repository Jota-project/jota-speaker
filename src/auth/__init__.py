from src.auth.interface import IAuthProvider
from src.core.config import Settings


def create_auth_provider(settings: Settings) -> IAuthProvider:
    match settings.auth_provider:
        case "stub":
            from src.auth.stub import StubAuthProvider
            return StubAuthProvider()
        case "jota_db":
            from src.auth.jota_db import JotaDbAuthProvider
            return JotaDbAuthProvider(settings)
        case _:
            raise ValueError(f"Unknown auth provider: {settings.auth_provider!r}")
