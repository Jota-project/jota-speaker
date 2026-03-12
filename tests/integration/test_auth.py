import pytest
import respx
import httpx

from src.auth.stub import StubAuthProvider
from src.auth.jota_db import JotaDbAuthProvider
from src.core.config import Settings


# ── StubAuthProvider ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_stub_accepts_any_token():
    auth = StubAuthProvider()
    assert await auth.validate("anything") is True


@pytest.mark.asyncio
async def test_stub_accepts_empty_token():
    auth = StubAuthProvider()
    assert await auth.validate("") is True


# ── JotaDbAuthProvider ────────────────────────────────────────────────────────

def make_settings(**kwargs) -> Settings:
    defaults = dict(
        engine="mock",
        auth_provider="jota_db",
        jota_db_url="http://jota-db.test",
        jota_db_auth_path="/auth/validate",
        jota_db_timeout=5.0,
    )
    defaults.update(kwargs)
    return Settings(**defaults)


@pytest.mark.asyncio
async def test_jota_db_200_returns_true():
    settings = make_settings()
    auth = JotaDbAuthProvider(settings)
    with respx.mock:
        respx.post("http://jota-db.test/auth/validate").mock(
            return_value=httpx.Response(200)
        )
        result = await auth.validate("valid-token")
    assert result is True


@pytest.mark.asyncio
async def test_jota_db_401_returns_false():
    settings = make_settings()
    auth = JotaDbAuthProvider(settings)
    with respx.mock:
        respx.post("http://jota-db.test/auth/validate").mock(
            return_value=httpx.Response(401)
        )
        result = await auth.validate("bad-token")
    assert result is False


@pytest.mark.asyncio
async def test_jota_db_403_returns_false():
    settings = make_settings()
    auth = JotaDbAuthProvider(settings)
    with respx.mock:
        respx.post("http://jota-db.test/auth/validate").mock(
            return_value=httpx.Response(403)
        )
        result = await auth.validate("forbidden-token")
    assert result is False


@pytest.mark.asyncio
async def test_jota_db_500_raises():
    settings = make_settings()
    auth = JotaDbAuthProvider(settings)
    with respx.mock:
        respx.post("http://jota-db.test/auth/validate").mock(
            return_value=httpx.Response(500)
        )
        with pytest.raises(RuntimeError):
            await auth.validate("any")


@pytest.mark.asyncio
async def test_jota_db_timeout_raises():
    settings = make_settings(jota_db_timeout=0.001)
    auth = JotaDbAuthProvider(settings)
    with respx.mock:
        respx.post("http://jota-db.test/auth/validate").mock(
            side_effect=httpx.TimeoutException("timeout")
        )
        with pytest.raises(httpx.TimeoutException):
            await auth.validate("any")


@pytest.mark.asyncio
async def test_jota_db_sends_correct_payload():
    settings = make_settings()
    auth = JotaDbAuthProvider(settings)
    with respx.mock:
        route = respx.post("http://jota-db.test/auth/validate").mock(
            return_value=httpx.Response(200)
        )
        await auth.validate("my-secret-token")
        request = route.calls[0].request
        import json
        body = json.loads(request.content)
        assert body == {"token": "my-secret-token"}


@pytest.mark.asyncio
async def test_jota_db_uses_correct_url():
    settings = make_settings(
        jota_db_url="http://custom-host:9000",
        jota_db_auth_path="/v2/auth/check",
    )
    auth = JotaDbAuthProvider(settings)
    with respx.mock:
        route = respx.post("http://custom-host:9000/v2/auth/check").mock(
            return_value=httpx.Response(200)
        )
        await auth.validate("tok")
        assert route.called
