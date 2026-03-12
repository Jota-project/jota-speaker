from .interface import IAuthProvider


class StubAuthProvider(IAuthProvider):
    """Accepts all tokens. Use for development and CI."""

    async def validate(self, token: str) -> bool:
        return True
