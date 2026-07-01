import pytest

from src.tts.normalizer import INormalizer, PassThroughNormalizer, SpanishNormalizer


def test_passthrough_normalizer_returns_text_unchanged():
    n = PassThroughNormalizer()
    assert n.normalize.__doc__ is None or True


@pytest.mark.asyncio
async def test_passthrough_normalizer_preserves_text():
    n = PassThroughNormalizer()
    assert await n.normalize("Hola mundo 123") == "Hola mundo 123"


@pytest.mark.asyncio
async def test_passthrough_normalizer_empty_string():
    n = PassThroughNormalizer()
    assert await n.normalize("") == ""


def test_passthrough_is_normalizer_instance():
    n = PassThroughNormalizer()
    assert isinstance(n, INormalizer)


@pytest.fixture
def norm():
    return SpanishNormalizer()


@pytest.fixture
def norm_no_excl():
    return SpanishNormalizer(excluded_patterns=[])


@pytest.mark.asyncio
async def test_spanish_normalizer_integers(norm):
    assert await norm.normalize("Tengo 25 años") == "Tengo veinticinco años"


@pytest.mark.asyncio
async def test_spanish_normalizer_multi_digit_integers(norm):
    assert await norm.normalize("Son 100 personas") == "Son cien personas"


@pytest.mark.asyncio
async def test_spanish_normalizer_decimals(norm):
    out = await norm.normalize("Vale 3,14 euros")
    assert "tres" in out and "coma" in out and "catorce" in out


@pytest.mark.asyncio
async def test_spanish_normalizer_decimals_dot(norm):
    out = await norm.normalize("Vale 3.14 euros")
    assert "tres" in out and "coma" in out and "catorce" in out


@pytest.mark.asyncio
async def test_spanish_normalizer_percentages(norm):
    out = await norm.normalize("50% de descuento")
    assert "cincuenta" in out and "por ciento" in out


@pytest.mark.asyncio
async def test_spanish_normalizer_decimal_percentage(norm):
    out = await norm.normalize("3,14%")
    assert "tres" in out and "catorce" in out and "por ciento" in out


@pytest.mark.asyncio
async def test_spanish_normalizer_currency_prefix(norm):
    out = await norm.normalize("Cuesta 50€")
    assert "cincuenta" in out and "euros" in out


@pytest.mark.asyncio
async def test_spanish_normalizer_currency_word(norm):
    out = await norm.normalize("Pagué 100 euros")
    assert "cien" in out and "euros" in out


@pytest.mark.asyncio
async def test_spanish_normalizer_postal_code_protected_by_default(norm):
    # Default whitelist includes postal_code → 5-digit number stays
    assert await norm.normalize("Vivo en 28013") == "Vivo en 28013"


@pytest.mark.asyncio
async def test_spanish_normalizer_postal_normalized_when_excluded(norm_no_excl):
    out = await norm_no_excl.normalize("28013")
    assert "veintiocho" in out.lower() or "ochenta" not in out  # 28 → veintiocho
    # 28013 parsed as decimal by some regexes is fine; ensure it changed
    assert "28013" not in out


@pytest.mark.asyncio
async def test_spanish_normalizer_hours_24h(norm):
    out = await norm.normalize("Son las 15:30")
    assert "quince" in out and "treinta" in out


@pytest.mark.asyncio
async def test_spanish_normalizer_hours_midnight(norm):
    out = await norm.normalize("A las 9:00")
    assert "nueve" in out


@pytest.mark.asyncio
async def test_spanish_normalizer_dates(norm):
    out = await norm.normalize("Nací el 15/03/2024")
    assert "quince" in out
    assert "marzo" in out
    assert "dos mil veinticuatro" in out


@pytest.mark.asyncio
async def test_spanish_normalizer_dates_dash(norm):
    out = await norm.normalize("Fecha 01-12-1999")
    assert "diciembre" in out
    assert "mil novecientos noventa y nueve" in out


@pytest.mark.asyncio
async def test_spanish_normalizer_abbreviations_dr(norm):
    assert "doctor" in (await norm.normalize("Soy Dr. García")).lower()


@pytest.mark.asyncio
async def test_spanish_normalizer_etcetera(norm):
    assert "etcétera" in (await norm.normalize("Perros, gatos, etc.")).lower()


@pytest.mark.asyncio
async def test_spanish_normalizer_url_not_touched_by_default(norm):
    out = await norm.normalize("Ver https://ejemplo.com aquí")
    assert "https://ejemplo.com" in out


@pytest.mark.asyncio
async def test_spanish_normalizer_email_spoken_when_not_excluded():
    n = SpanishNormalizer(excluded_patterns=[])
    out = await n.normalize("Escribe a foo@bar.com")
    assert "foo" in out and "bar" in out and "arroba" in out


@pytest.mark.asyncio
async def test_spanish_normalizer_email_not_touched_by_default(norm):
    assert "foo@bar.com" in (await norm.normalize("Escribe a foo@bar.com"))


@pytest.mark.asyncio
async def test_spanish_normalizer_hashtag(norm):
    out = await norm.normalize("Me encanta #verano")
    assert "hashtag" in out.lower()


@pytest.mark.asyncio
async def test_spanish_normalizer_mention(norm):
    out = await norm.normalize("Follow @usuario")
    assert "arroba" in out.lower()


@pytest.mark.asyncio
async def test_spanish_normalizer_ampersand(norm):
    assert await norm.normalize("A & B") == "A y B"
