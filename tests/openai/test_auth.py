"""Unit tests for extract_bearer_token and IAuthProvider integration."""

import pytest

from src.openai.service import extract_bearer_token


class TestExtractBearerToken:
    @pytest.mark.parametrize(
        "header,expected",
        [
            ("Bearer abc", "abc"),
            ("bearer abc", "abc"),
            ("BEARER abc", "abc"),
            ("BeArEr abc", "abc"),
            ("Bearer ABC", "ABC"),
            ("Bearer  abc", "abc"),  # two spaces → strip
            ("Bearer\tabc", "abc"),  # tab is whitespace
            ("Bearer    abc    ", "abc"),  # trailing whitespace after token
            ("Bearer abc-def_123", "abc-def_123"),
            ("Bearer eyJhbGciOi.fake.jwt", "eyJhbGciOi.fake.jwt"),  # JWT shape
        ],
    )
    def test_valid_bearer(self, header, expected):
        assert extract_bearer_token(header) == expected

    @pytest.mark.parametrize(
        "header",
        [
            None,
            "",
            "Bearer",
            "Bearer ",
            " Basic abc",  # leading space → no scheme match
            "Basic abc",
            "Token abc",
            "Digest abc",
            "abc",  # no scheme
            "Bearer\t",  # tab-only token → stripped to empty
            "Bearer\n",
        ],
    )
    def test_invalid_returns_none(self, header):
        assert extract_bearer_token(header) is None


class TestStubAuthProviderAccepts:
    """The StubAuthProvider already accepts anything. The HTTP layer
    relies on extract_bearer_token to filter empty tokens, NOT on the
    provider to reject them. This is the back-compat rationale."""

    def test_stub_accepts_any_token(self):
        import asyncio
        from src.auth.stub import StubAuthProvider
        p = StubAuthProvider()
        assert asyncio.run(p.validate("any-token")) is True


class TestJotaDbAuthProviderInvalidates:
    """JotaDbAuthProvider (via respx mock) must return False for
    invalid tokens. We don't fully test the provider here — that's
    covered in tests/integration/test_auth.py. We just smoke-test
    that extract_bearer_token is the single boundary."""

    def test_extract_rejects_non_bearer(self):
        assert extract_bearer_token("Bearer") is None
        assert extract_bearer_token("Bearer ") is None
        assert extract_bearer_token(None) is None
        assert extract_bearer_token("Basic abc") is None
