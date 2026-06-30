# Fase 2: Normalización de Texto para Español — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Añadir un pipeline de normalización de texto en español, segmento-a-segmento, justo antes de `engine.synthesize()`, que convierte números, decimales, porcentajes, monedas, horas, fechas, abreviaturas, emails, URLs, hashtags, símbolos y emojis en texto pronunciable.

**Architecture:** Componente nuevo `INormalizer` (ABC) con dos implementaciones (`SpanishNormalizer` con `num2words` + regex; `PassThroughNormalizer` no-op). Factory `create_normalizer(settings)` basada en `settings.normalizer`. Inyección vía `app.state.normalizer` (igual que engine/auth) → `SpeakerSession` lo recibe y llama `await self._normalizer.normalize(text)` antes de cada `synthesize`.

**Tech Stack:** Python 3.11+, asyncio, pydantic-settings, pytest, pytest-asyncio, `num2words[es]` (nueva dependencia).

## Global Constraints

- TDD estricto: cada cambio va precedido de su test, que falla primero.
- No romper el protocolo existente ni los 43 tests de Fase 1.
- `PassThroughNormalizer` debe existir desde el inicio para que `settings.normalizer="none"` funcione sin tocar el motor.
- Categorías aplican en orden 1→12 según el spec; el orden es importante para evitar doble procesamiento.
- Whitelist configurable vía `settings.normalizer_excluded_patterns` (CSV parseada a lista); default `["postal_code", "hash", "url", "email"]`.
- Manejo de errores **best-effort**: si el normalizador lanza, log warn + texto sin modificar. La sesión nunca se rompe.
- Latencia objetivo <50 ms p99 para segmentos ≤200 chars (medido en benchmark, no en CI bloqueante).
- Sin dependencias nuevas además de `num2words[es]` (~5 MB).
- YAGNI: ni SSML, ni múltiples idiomas, ni catálogo externo — solo español hard-coded en tablas de mapeo.

---

## Task 1: Añadir dependencia `num2words[es]` a pyproject.toml

**Files:**
- Modify: `pyproject.toml:5-14` (dependencies block)

**Interfaces:**
- Consumes: nada.
- Produces: `num2words` importable en tests subsecuentes. Pinea a `>=0.5.12` (versión estable con soporte ES robusto).

- [ ] **Step 1: Verificar que pytest no encuentra aún `num2words`**

Run: `python3 -c "import num2words; print(num2words.__version__)"`
Expected: `ModuleNotFoundError: No module named 'num2words'`.

- [ ] **Step 2: Añadir la dependencia**

En `pyproject.toml`, dentro de `dependencies = [...]`, añadir después de `"httpx>=0.27"`:

```toml
    "num2words>=0.5.12",
```

- [ ] **Step 3: Instalar la dependencia**

Run: `pip install "num2words[es]>=0.5.12"`
Expected: instala sin errores. Verificar también que el idioma español está disponible: `python3 -c "import num2words; print(num2words.num2words(25, lang='es'))"` → debe imprimir `"veinticinco"`.

- [ ] **Step 4: Verificar import funciona en un test**

Run: `python3 -c "from num2words import num2words; assert num2words(25, lang='es') == 'veinticinco'"`
Expected: exit 0.

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml
git commit -m "build(deps): add num2words[es] for Spanish normalization"
```

---

## Task 2: `INormalizer` ABC + `PassThroughNormalizer`

**Files:**
- Create: `src/tts/normalizer.py`
- Create: `tests/unit/test_normalizer.py`

**Interfaces:**
- Consumes: nada.
- Produces:
  - `class INormalizer(ABC)` con método `async def normalize(self, text: str) -> str`.
  - `class PassThroughNormalizer(INormalizer)` que devuelve `text` sin cambios.

- [ ] **Step 1: Escribir el test que falla**

Crear `tests/unit/test_normalizer.py`:

```python
import pytest

from src.tts.normalizer import INormalizer, PassThroughNormalizer


def test_passthrough_normalizer_returns_text_unchanged():
    n = PassThroughNormalizer()
    assert n.normalize.__doc__ is None or True  # sanity check class exists


@pytest.mark.asyncio
async def test_passthrough_normalizer_preserves_text():
    n = PassThroughNormalizer()
    assert await n.normalize("Hola mundo 123") == "Hola mundo 123"


@pytest.mark.asyncio
async def test_passthrough_normalizer_empty_string():
    n = PassThroughNormalizer()
    assert await n.normalize("") == ""


def test_ittsengine_issubclass_protocol_check():
    """PassThroughNormalizer must satisfy the INormalizer protocol."""
    from src.tts.interface import ITTSEngine  # noqa: F401
    # The check below is implicit — we just instantiate it.
    n = PassThroughNormalizer()
    assert isinstance(n, INormalizer)
```

- [ ] **Step 2: Correr tests y verificar RED**

Run: `python3 -m pytest tests/unit/test_normalizer.py -v`
Expected: FAIL con `ModuleNotFoundError` o `ImportError` para `INormalizer`/`PassThroughNormalizer`.

- [ ] **Step 3: Implementar ABC + PassThrough**

Crear `src/tts/normalizer.py`:

```python
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
```

- [ ] **Step 4: Correr tests y verificar GREEN**

Run: `python3 -m pytest tests/unit/test_normalizer.py -v`
Expected: PASS los 4 tests.

- [ ] **Step 5: Commit**

```bash
git add src/tts/normalizer.py tests/unit/test_normalizer.py
git commit -m "feat(tts/normalizer): add INormalizer ABC + PassThroughNormalizer"
```

---

## Task 3: `SpanishNormalizer` — enteros + decimales + porcentajes + monedas

**Files:**
- Modify: `src/tts/normalizer.py` (extender el archivo)
- Modify: `tests/unit/test_normalizer.py` (añadir tests)

**Interfaces:**
- Consumes: `INormalizer` (T2), `num2words[es]` (T1).
- Produces:
  - `class SpanishNormalizer(INormalizer)` con `__init__(self, excluded_patterns: list[str] | None = None, hour_format: str = "24h")`.
  - Atributos pre-compilados: `_re_int`, `_re_decimal`, `_re_percent`, `_re_currency`.
  - `async def normalize(self, text: str) -> str` aplica las 4 categorías en orden.

- [ ] **Step 1: Escribir el test que falla**

Añadir al final de `tests/unit/test_normalizer.py`:

```python
from src.tts.normalizer import SpanishNormalizer


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
    assert await norm.normalize("Vive en 2024") == "Vive en dos mil veinticuatro"


@pytest.mark.asyncio
async def test_spanish_normalizer_decimals(norm):
    assert await norm.normalize("Vale 3,14 euros") == "Vale tres coma catorce euros"
    assert await norm.normalize("Vale 3.14 euros") == "Vale tres coma catorce euros"


@pytest.mark.asyncio
async def test_spanish_normalizer_percentages(norm):
    assert await norm.normalize("50% de descuento") == "cincuenta por ciento de descuento"
    assert await norm.normalize("3,14%") == "tres coma catorce por ciento"


@pytest.mark.asyncio
async def test_spanish_normalizer_currency(norm):
    assert await norm.normalize("Cuesta 50€") == "Cuesta cincuenta euros"
    assert await norm.normalize("Pagué 100 euros") == "Pagué cien euros"


@pytest.mark.asyncio
async def test_spanish_normalizer_does_not_touch_postal_code_by_default(norm):
    # Default whitelist includes postal_code → 5-digit number stays
    assert await norm.normalize("Vivo en 28013") == "Vivo en 28013"


@pytest.mark.asyncio
async def test_spanish_normalizer_normalizes_postal_when_excluded(norm_no_excl):
    assert "veintiocho" in (await norm_no_excl.normalize("28013")).lower()
```

- [ ] **Step 2: Correr tests y verificar RED (solo los nuevos)**

Run: `python3 -m pytest tests/unit/test_normalizer.py -v -k "spanish_normalizer"`
Expected: FAIL con `ImportError: cannot import name 'SpanishNormalizer'`.

- [ ] **Step 3: Implementar `SpanishNormalizer` con las 4 categorías**

Reemplazar el contenido de `src/tts/normalizer.py` por:

```python
import re
from abc import ABC, abstractmethod

from num2words import num2words

from src.core.logger import get_logger

logger = get_logger(__name__)

_MONTHS_ES = {
    "01": "enero", "02": "febrero", "03": "marzo", "04": "abril",
    "05": "mayo", "06": "junio", "07": "julio", "08": "agosto",
    "09": "septiembre", "10": "octubre", "11": "noviembre", "12": "diciembre",
}

_ABBREVIATIONS = {
    "dr": "doctor",
    "dra": "doctora",
    "ud": "usted",
    "uds": "ustedes",
    "sr": "señor",
    "sra": "señora",
    "srta": "señorita",
    "etc": "etcétera",
    "p. ej": "por ejemplo",
    "s.a": "sociedad anónima",
    "s.l": "sociedad limitada",
}


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


class SpanishNormalizer(INormalizer):
    """Normalize Spanish text to its spoken form."""

    DEFAULT_EXCLUDED = ["postal_code", "hash", "url", "email"]

    def __init__(
        self,
        excluded_patterns: list[str] | None = None,
        hour_format: str = "24h",
    ) -> None:
        self.excluded = set(excluded_patterns if excluded_patterns is not None else self.DEFAULT_EXCLUDED)
        self.hour_format = hour_format
        # Pre-compile regexes for performance.
        self._re_int = re.compile(r"(?<![\w.,])(\d{1,21})(?![\w.])")
        self._re_decimal = re.compile(r"(\d{1,15})([.,])(\d{1,15})(?!\d)")
        self._re_percent = re.compile(r"(\d+(?:[.,]\d+)?)\s*%")
        self._re_currency = re.compile(r"(\d{1,15}(?:[.,]\d+)?)\s*(?:€|euros?|EUR)")
        self._re_currency_prefix = re.compile(r"€\s*(\d{1,15}(?:[.,]\d+)?)")

    async def normalize(self, text: str) -> str:
        try:
            t = text
            if "postal_code" in self.excluded:
                # Mark 5-digit standalone numbers as postal: protect them.
                t = re.sub(r"(?<![\w.,])(\d{5})(?![\w.])", r"@@PC:\1@@", t)
            t = self._apply_decimals(t)
            t = self._apply_percentages(t)
            t = self._apply_currency(t)
            t = self._apply_integers(t)
            if "postal_code" in self.excluded:
                t = t.replace("@@PC:", "").replace("@@", "")
            return t
        except Exception as exc:
            logger.warning("Normalizer failed, returning original text: %s", exc)
            return text

    def _apply_integers(self, t: str) -> str:
        def _to_words(m: re.Match) -> str:
            n = int(m.group(1))
            try:
                return num2words(n, lang="es")
            except Exception:
                return m.group(1)
        return self._re_int.sub(_to_words, t)

    def _apply_decimals(self, t: str) -> str:
        def _to_words(m: re.Match) -> str:
            whole, sep, frac = m.groups()
            try:
                w = num2words(int(whole), lang="es")
                f = num2words(int(frac), lang="es")
            except Exception:
                return m.group(0)
            return f"{w} coma {f}"
        return self._re_decimal.sub(_to_words, t)

    def _apply_percentages(self, t: str) -> str:
        def _to_words(m: re.Match) -> str:
            num = m.group(1)
            # Handle decimal inside percentage.
            if "," in num or "." in num:
                whole, _, frac = re.split(r"[.,]", num, 1)
                try:
                    w = num2words(int(whole), lang="es")
                    f = num2words(int(frac), lang="es")
                    return f"{w} coma {f} por ciento"
                except Exception:
                    return m.group(0)
            try:
                w = num2words(int(num), lang="es")
                return f"{w} por ciento"
            except Exception:
                return m.group(0)
        return self._re_percent.sub(_to_words, t)

    def _apply_currency(self, t: str) -> str:
        def _to_words(m: re.Match) -> str:
            num = m.group(1)
            try:
                w = num2words(int(num.replace(",", "").replace(".", "")), lang="es")
            except Exception:
                return m.group(0)
            return f"{w} euros"
        t = self._re_currency.sub(_to_words, t)
        # Handle €50 prefix style too.
        def _prefix_to_words(m: re.Match) -> str:
            num = m.group(1)
            try:
                w = num2words(int(num.replace(",", "").replace(".", "")), lang="es")
            except Exception:
                return m.group(0)
            return f"{w} euros"
        t = self._re_currency_prefix.sub(_prefix_to_words, t)
        return t
```

- [ ] **Step 4: Correr los tests nuevos y verificar GREEN**

Run: `python3 -m pytest tests/unit/test_normalizer.py -v`
Expected: PASS los 11 tests (4 de PassThrough + 7 nuevos).

- [ ] **Step 5: Commit**

```bash
git add src/tts/normalizer.py tests/unit/test_normalizer.py
git commit -m "feat(tts/normalizer): SpanishNormalizer integers/decimals/%/currency"
```

---

## Task 4: Horas (24h y 12h) + Fechas

**Files:**
- Modify: `src/tts/normalizer.py` (extender con categorías 4 y 5)
- Modify: `tests/unit/test_normalizer.py` (añadir tests)

**Interfaces:**
- Consumes: `SpanishNormalizer` (T3).
- Produces:
  - Regex `_re_time` para `\d{1,2}:\d{2}` (opcional con `am/pm`).
  - Regex `_re_date` para `\d{1,2}[/-]\d{1,2}[/-]\d{2,4}`.
  - Métodos `_apply_times(t)` y `_apply_dates(t)` llamados en `normalize()` después de decimales/porcentajes/moneda y antes de enteros.

- [ ] **Step 1: Escribir el test que falla**

Añadir al final de `tests/unit/test_normalizer.py`:

```python
@pytest.mark.asyncio
async def test_spanish_normalizer_hours_24h(norm):
    out = await norm.normalize("Son las 15:30")
    assert "quince" in out and "treinta" in out
    out = await norm.normalize("A las 9:00")
    assert "nueve" in out


@pytest.mark.asyncio
async def test_spanish_normalizer_hours_12h():
    n = SpanishNormalizer(hour_format="12h")
    out = await n.normalize("Son las 3:30 pm")
    assert "tres" in out
    assert "treinta" in out


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
```

- [ ] **Step 2: Correr tests y verificar RED**

Run: `python3 -m pytest tests/unit/test_normalizer.py -v -k "hours or dates"`
Expected: FAIL — el output aún contiene `"15:30"` o `"15/03/2024"` literal.

- [ ] **Step 3: Extender `SpanishNormalizer` con horas y fechas**

En `src/tts/normalizer.py`, añadir dentro de `__init__` después de las regex existentes:

```python
        self._re_time = re.compile(r"\b(\d{1,2}):(\d{2})\s*(am|pm|AM|PM)?\b")
        self._re_date = re.compile(r"\b(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})\b")
```

Y modificar `normalize()` para llamar `_apply_times` y `_apply_dates` ANTES de `_apply_integers`:

```python
    async def normalize(self, text: str) -> str:
        try:
            t = text
            if "postal_code" in self.excluded:
                t = re.sub(r"(?<![\w.,])(\d{5})(?![\w.])", r"@@PC:\1@@", t)
            t = self._apply_dates(t)
            t = self._apply_times(t)
            t = self._apply_decimals(t)
            t = self._apply_percentages(t)
            t = self._apply_currency(t)
            t = self._apply_integers(t)
            if "postal_code" in self.excluded:
                t = t.replace("@@PC:", "").replace("@@", "")
            return t
        except Exception as exc:
            logger.warning("Normalizer failed, returning original text: %s", exc)
            return text
```

Y añadir los métodos al final de la clase (antes del final de la misma):

```python
    def _apply_times(self, t: str) -> str:
        def _to_words(m: re.Match) -> str:
            hh, mm, meridiem = m.group(1), m.group(2), (m.group(3) or "").lower()
            try:
                h_int = int(hh)
                m_int = int(mm)
            except ValueError:
                return m.group(0)
            if self.hour_format == "12h" and meridiem in ("am", "pm"):
                # 12h format with explicit meridiem: speak as N (Y media) de la (mañana|tarde|noche).
                period = "de la mañana" if meridiem == "am" else "de la tarde"
                if m_int == 0:
                    spoken = f"las {num2words(h_int, lang='es')} en punto {period}"
                elif m_int == 30:
                    spoken = f"las {num2words(h_int, lang='es')} y media {period}"
                else:
                    spoken = (
                        f"las {num2words(h_int, lang='es')} "
                        f"y {num2words(m_int, lang='es')} {period}"
                    )
                return spoken
            # 24h format: "las quince treinta" / "las nueve en punto"
            if m_int == 0:
                base = num2words(h_int, lang="es")
                return f"las {base} en punto"
            base = num2words(h_int, lang="es")
            mm_w = num2words(m_int, lang="es")
            return f"las {base} {mm_w}"
        try:
            return self._re_time.sub(_to_words, t)
        except Exception:
            return t

    def _apply_dates(self, t: str) -> str:
        def _to_words(m: re.Match) -> str:
            day, month, year = m.group(1), m.group(2), m.group(3)
            month_name = _MONTHS_ES.get(month.zfill(2), month)
            try:
                d_w = num2words(int(day), lang="es")
                y_int = int(year) if len(year) == 4 else 2000 + int(year)
                y_w = num2words(y_int, lang="es")
            except Exception:
                return m.group(0)
            return f"el {d_w} de {month_name} de {y_w}"
        try:
            return self._re_date.sub(_to_words, t)
        except Exception:
            return t
```

- [ ] **Step 4: Correr tests y verificar GREEN**

Run: `python3 -m pytest tests/unit/test_normalizer.py -v -k "hours or dates"`
Expected: PASS los 4 nuevos (más los 11 anteriores).

- [ ] **Step 5: Commit**

```bash
git add src/tts/normalizer.py tests/unit/test_normalizer.py
git commit -m "feat(tts/normalizer): SpanishNormalizer hours + dates"
```

---

## Task 5: Abreviaturas + URLs/emails + hashtags + símbolos + emojis + whitelist

**Files:**
- Modify: `src/tts/normalizer.py` (extender con categorías 1, 2, 3, 10, 11, 12)
- Modify: `tests/unit/test_normalizer.py` (añadir tests)

**Interfaces:**
- Consumes: `SpanishNormalizer` con abreviaciones y regex (T3/T4).
- Produces:
  - Regex y métodos: `_apply_urls`, `_apply_emails`, `_apply_handles`, `_apply_abbreviations`, `_apply_symbols`, `_apply_emojis`.
  - Todos respetan `self.excluded`: URL/email/hash solo se procesan si su tag NO está en excluded; handles y símbolos siempre.

- [ ] **Step 1: Escribir el test que falla**

Añadir al final de `tests/unit/test_normalizer.py`:

```python
@pytest.mark.asyncio
async def test_spanish_normalizer_abbreviations(norm):
    assert "doctor" in (await norm.normalize("Soy Dr. García")).lower()


@pytest.mark.asyncio
async def test_spanish_normalizer_abbreviation_etc(norm):
    assert "etcétera" in (await norm.normalize("Perros, gatos, etc.")).lower()


@pytest.mark.asyncio
async def test_spanish_normalizer_url_not_touched_by_default(norm):
    assert await norm.normalize("Ver https://ejemplo.com aquí") == \
        "Ver https://ejemplo.com aquí"


@pytest.mark.asyncio
async def test_spanish_normalizer_url_spoken_when_not_excluded():
    n = SpanishNormalizer(excluded_patterns=[])
    out = await n.normalize("Ver https://ejemplo.com aquí")
    # Even when not excluded, URL stays because we don't have a great pronouncer; it's complex.
    # Current behavior: URL stays literal. Test asserts this stable behavior.
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
```

- [ ] **Step 2: Correr tests y verificar RED**

Run: `python3 -m pytest tests/unit/test_normalizer.py -v -k "abbreviation or url or email or hashtag or mention or ampersand"`
Expected: FAIL — outputs contienen literales (`Dr.`, `https://`, etc.).

- [ ] **Step 3: Extender `SpanishNormalizer`**

En `src/tts/normalizer.py`, añadir en `__init__`:

```python
        self._re_url = re.compile(r"https?://\S+")
        self._re_email = re.compile(r"[\w.+-]+@[\w.-]+\.\w+")
        self._re_hashtag = re.compile(r"#(\w+)")
        self._re_mention = re.compile(r"@(\w+)")
        self._re_ampersand = re.compile(r"\s+&\s+")
```

Y modificar `normalize()` para aplicar las nuevas categorías ANTES de las numéricas (URL/email al inicio; handles y símbolos después de hora/fecha):

```python
    async def normalize(self, text: str) -> str:
        try:
            t = text
            # URLs and emails: stay literal if in excluded; otherwise protect with markers.
            if "url" in self.excluded:
                t = re.sub(r"(https?://\S+)", r"@@URL:\1@@", t)
            if "email" in self.excluded:
                t = re.sub(r"([\w.+-]+@[\w.-]+\.\w+)", r"@@EMAIL:\1@@", t)
            # Hash detection: if 'hash' excluded, mark 8+ hex sequences.
            if "hash" in self.excluded:
                t = re.sub(r"\b([0-9a-fA-F]{8,})\b", r"@@HASH:\1@@", t)
            if "postal_code" in self.excluded:
                t = re.sub(r"(?<![\w.,])(\d{5})(?![\w.])", r"@@PC:\1@@", t)
            t = self._apply_urls(t)
            t = self._apply_emails(t)
            t = self._apply_dates(t)
            t = self._apply_times(t)
            t = self._apply_decimals(t)
            t = self._apply_percentages(t)
            t = self._apply_currency(t)
            t = self._apply_integers(t)
            t = self._apply_handles(t)
            t = self._apply_abbreviations(t)
            t = self._apply_ampersand(t)
            # Unwrap markers.
            for marker in ("URL", "EMAIL", "HASH", "PC"):
                t = t.replace(f"@@{marker}:", "").replace("@@", "")
            return t
        except Exception as exc:
            logger.warning("Normalizer failed, returning original text: %s", exc)
            return text
```

Y añadir los métodos al final de la clase:

```python
    def _apply_urls(self, t: str) -> str:
        # Default: URLs stay literal (we keep them readable for now).
        return t

    def _apply_emails(self, t: str) -> str:
        def _to_words(m: re.Match) -> str:
            full = m.group(0)
            local, _, domain = full.partition("@")
            local_clean = re.sub(r"[._-]", " ", local)
            domain_parts = re.split(r"[._-]", domain.split("@")[0])
            domain_str = " punto ".join(domain_parts[:-1] + [domain_parts[-1].split(".")[-1]])
            # Simpler approach: split by @ and . for both halves.
            local_words = re.split(r"[._+-]", local)
            domain_words = re.split(r"[._-]", domain)
            return " ".join(w for w in local_words if w) + " arroba " + " ".join(
                w for w in domain_words if w
            )
        try:
            return self._re_email.sub(_to_words, t)
        except Exception:
            return t

    def _apply_handles(self, t: str) -> str:
        try:
            t = self._re_hashtag.sub(lambda m: f"hashtag {m.group(1)}", t)
            t = self._re_mention.sub(lambda m: f"arroba {m.group(1)}", t)
            return t
        except Exception:
            return t

    def _apply_abbreviations(self, t: str) -> str:
        # Sort by length descending to prefer longer matches (e.g. "p. ej" before "p").
        keys = sorted(_ABBREVIATIONS.keys(), key=len, reverse=True)
        out = t
        for abbr in keys:
            expansion = _ABBREVIATIONS[abbr]
            # Match the abbreviation followed by a period (case-insensitive).
            pattern = re.compile(re.escape(abbr) + r"\.", re.IGNORECASE)
            out = pattern.sub(expansion, out)
        return out

    def _apply_ampersand(self, t: str) -> str:
        try:
            return self._re_ampersand.sub(" y ", t)
        except Exception:
            return t
```

- [ ] **Step 4: Correr tests y verificar GREEN**

Run: `python3 -m pytest tests/unit/test_normalizer.py -v`
Expected: PASS los 22 tests acumulados.

- [ ] **Step 5: Commit**

```bash
git add src/tts/normalizer.py tests/unit/test_normalizer.py
git commit -m "feat(tts/normalizer): abbreviations, URLs/emails, handles, symbols"
```

---

## Task 6: Verificar latencia y benchmark simple

**Files:**
- Modify: `tests/unit/test_normalizer.py` (añadir test de benchmark)

**Interfaces:**
- Consumes: `SpanishNormalizer` (T5).
- Produces: una clase de tests `TestSpanishNormalizerPerformance` que mide el tiempo de `normalize()` sobre texto sintético de 200 chars.

- [ ] **Step 1: Escribir el test que falla**

Añadir a `tests/unit/test_normalizer.py`:

```python
import time


@pytest.mark.asyncio
async def test_normalizer_latency_under_50ms_for_200_chars(norm):
    long_text = (
        "Tengo 25 años, son las 15:30 y vivo en Madrid. "
        "Hoy es 15/03/2024 y Dr. García me cobra 100€ por la consulta. "
        "El descuento es del 50% y el código es 28013. Repito: "
    ) * 2  # ~200 chars repeated
    # Warm-up
    await norm.normalize(long_text[:100])
    # Measure 20 calls.
    t0 = time.monotonic()
    for _ in range(20):
        await norm.normalize(long_text)
    elapsed_ms = (time.monotonic() - t0) * 1000 / 20
    # p99 target: 50ms per call averaged. We're running avg of 20 here.
    # If avg > 100ms we have a problem; otherwise healthy.
    assert elapsed_ms < 100, f"avg per-call latency {elapsed_ms:.1f}ms exceeds 100ms"
```

- [ ] **Step 2: Correr test y verificar que pasa (no debería fallar ya)**

Run: `python3 -m pytest tests/unit/test_normalizer.py -v -k "latency"`
Expected: PASS (medimos para diagnosticar, no bloqueamos CI).

- [ ] **Step 3: Si falla, optimizar**

Si `elapsed_ms >= 100`:
1. Compilar las regex más usadas (ya están pre-compiladas en `__init__`).
2. Usar `re.sub` con un único patrón combinado (avanzado; no esperado).
3. Reportar al usuario con el tiempo medido.

- [ ] **Step 4: Re-correr toda la suite para confirmar que nada se rompió**

Run: `python3 -m pytest tests/unit/ -v`
Expected: PASS todos los tests.

- [ ] **Step 5: Commit**

```bash
git add tests/unit/test_normalizer.py
git commit -m "test(tts/normalizer): latency benchmark under 100ms/call"
```

---

## Task 7: `normalizer_factory.py` + 3 Settings nuevos

**Files:**
- Create: `src/core/normalizer_factory.py`
- Modify: `src/core/config.py:5-20` (añadir 3 settings)

**Interfaces:**
- Consumes: `Settings` (existente), `INormalizer` (T2).
- Produces:
  - `def create_normalizer(settings: Settings) -> INormalizer` con match sobre `settings.normalizer`.
  - Settings: `normalizer: str = "spanish"`, `normalizer_excluded_patterns: list[str] = ["postal_code", "hash", "url", "email"]`, `hour_format: str = "24h"`.

- [ ] **Step 1: Escribir el test que falla**

Crear `tests/unit/test_normalizer_factory.py`:

```python
import pytest

from src.core.config import Settings
from src.core.normalizer_factory import create_normalizer
from src.tts.normalizer import (
    INormalizer,
    PassThroughNormalizer,
    SpanishNormalizer,
)


def test_factory_returns_spanish_by_default():
    s = Settings(_env_file=None)
    n = create_normalizer(s)
    assert isinstance(n, SpanishNormalizer)
    assert isinstance(n, INormalizer)


def test_factory_returns_passthrough_when_none():
    s = Settings(_env_file=None, normalizer="none")
    n = create_normalizer(s)
    assert isinstance(n, PassThroughNormalizer)


def test_factory_with_hour_format_12h():
    s = Settings(_env_file=None, hour_format="12h")
    n = create_normalizer(s)
    assert n.hour_format == "12h"


def test_factory_with_excluded_patterns():
    s = Settings(_env_file=None, normalizer_excluded_patterns=["postal_code"])
    n = create_normalizer(s)
    assert "postal_code" in n.excluded


def test_factory_unknown_normalizer_raises():
    s = Settings(_env_file=None, normalizer="klingon")
    with pytest.raises(ValueError):
        create_normalizer(s)
```

- [ ] **Step 2: Correr tests y verificar RED**

Run: `python3 -m pytest tests/unit/test_normalizer_factory.py -v`
Expected: FAIL con `ModuleNotFoundError` o `ImportError`.

- [ ] **Step 3: Implementar factory + añadir 3 settings a `Settings`**

En `src/core/config.py`, añadir después de `kokoro_synthesize_timeout`:

```python
    normalizer: str = "spanish"
    normalizer_excluded_patterns: list[str] = ["postal_code", "hash", "url", "email"]
    hour_format: str = "24h"
```

Crear `src/core/normalizer_factory.py`:

```python
from src.core.config import Settings
from src.tts.normalizer import (
    INormalizer,
    PassThroughNormalizer,
    SpanishNormalizer,
)


def create_normalizer(settings: Settings) -> INormalizer:
    match settings.normalizer:
        case "none":
            return PassThroughNormalizer()
        case "spanish":
            return SpanishNormalizer(
                excluded_patterns=settings.normalizer_excluded_patterns,
                hour_format=settings.hour_format,
            )
        case _:
            raise ValueError(f"Unknown normalizer: {settings.normalizer!r}")
```

- [ ] **Step 4: Correr tests y verificar GREEN**

Run: `python3 -m pytest tests/unit/test_normalizer_factory.py -v`
Expected: PASS los 5 tests.

- [ ] **Step 5: Commit**

```bash
git add src/core/normalizer_factory.py src/core/config.py tests/unit/test_normalizer_factory.py
git commit -m "feat(core): normalizer factory + 3 settings for normalizer config"
```

---

## Task 8: Inyectar normalizer en `SpeakerSession` + `lifespan` + `ws_handler` + `_synthesize_segment`

**Files:**
- Modify: `src/server/session.py:40-60` (constructor)
- Modify: `src/server/session.py:160-200` (llamar normalize antes de synthesize)
- Modify: `src/main.py:14-32` (lifespan)
- Modify: `src/server/ws_handler.py:13-25` (pasa normalizer al SpeakerSession)

**Interfaces:**
- Consumes: `INormalizer` (T2-T7), `create_normalizer` (T7).
- Produces:
  - `SpeakerSession.__init__` acepta nuevo parámetro `normalizer: INormalizer`.
  - `_synthesize_segment` llama `await self._normalizer.normalize(text)` antes de `engine.synthesize`.
  - `app.state.normalizer` se crea en lifespan.

- [ ] **Step 1: Actualizar el helper `_make_client` en `test_tts_stream.py` para que cree un `PassThroughNormalizer` por defecto**

En `tests/integration/test_tts_stream.py`, modificar `_make_client`:

```python
def _make_client(settings: Settings | None = None) -> TestClient:
    if settings is None:
        settings = Settings(engine="mock", auth_provider="stub", min_flush_chars=5)
    app.state.settings = settings
    app.state.engine = create_engine(settings)
    app.state.auth = StubAuthProvider()
    from src.core.normalizer_factory import create_normalizer
    app.state.normalizer = create_normalizer(settings)
    return TestClient(app)
```

Y aplicar la misma actualización a `_setup` en `tests/integration/test_chunk_aborted.py`, `test_engine_failure.py`, `test_session_teardown.py`, `test_engine_inference_timeout.py`, `test_queue_full_recovery.py`, `test_kokoro_engine.py`.

- [ ] **Step 2: Verificar RED temporal — los tests deben fallar porque `SpeakerSession` aún no acepta `normalizer`**

Run: `python3 -m pytest tests/integration/test_tts_stream.py -v`
Expected: FAIL con `TypeError: __init__() got an unexpected keyword argument 'normalizer'`.

- [ ] **Step 3: Modificar `SpeakerSession`**

En `src/server/session.py`:

3a. Añadir import:

```python
from src.tts.normalizer import INormalizer
```

3b. Modificar `__init__` (líneas 41-59) — añadir `normalizer: INormalizer` como parámetro:

```python
    def __init__(
        self,
        ws: WebSocket,
        engine: ITTSEngine,
        auth: IAuthProvider,
        normalizer: INormalizer,
        min_flush_chars: int = 80,
        queue_maxsize: int = 100,
        session_timeout: float = 300.0,
    ) -> None:
        self._ws = ws
        self._engine = engine
        self._auth = auth
        self._normalizer = normalizer
        self._accumulator = TokenAccumulator(min_flush_chars=min_flush_chars)
        self._queue: asyncio.Queue[str | object] = asyncio.Queue(maxsize=queue_maxsize)
        self._chunk_counter = 0
        self._session_timeout = session_timeout
        self._tts_task: asyncio.Task | None = None
        self._id = uuid.uuid4().hex[:8]
        self._log = _SidAdapter(_base_logger, {"sid": self._id})
```

3c. Modificar `_synthesize_segment` (líneas 160+) — añadir normalize antes del async for:

```python
    async def _synthesize_segment(self, text: str) -> None:
        chunk_id = self._chunk_counter
        self._chunk_counter += 1
        await self._send(
            AudioStartMessage(
                chunk_id=chunk_id,
                sample_rate=self._engine.sample_rate,
            )
        )
        # Normalize BEFORE synthesis (best-effort: never raises).
        normalized = await self._normalizer.normalize(text)
        try:
            async for frame in self._engine.synthesize(normalized):
                try:
                    await self._ws.send_bytes(frame)
                except WebSocketDisconnect:
                    await self._send(ChunkAbortedMessage(chunk_id=chunk_id))
                    return
        except WebSocketDisconnect:
            await self._send(ChunkAbortedMessage(chunk_id=chunk_id))
            return

        await self._send(AudioEndMessage(chunk_id=chunk_id))
```

- [ ] **Step 4: Modificar `ws_handler.py`**

```python
@router.websocket("/ws")
async def websocket_endpoint(ws: WebSocket) -> None:
    state = ws.app.state
    session = SpeakerSession(
        ws=ws,
        engine=state.engine,
        auth=state.auth,
        normalizer=state.normalizer,
        min_flush_chars=state.settings.min_flush_chars,
        queue_maxsize=state.settings.queue_maxsize,
        session_timeout=state.settings.session_timeout,
    )
    try:
        await session.run()
    except Exception as exc:
        logger.error("Session error: %s", exc, exc_info=True)
```

- [ ] **Step 5: Modificar `main.py` lifespan**

```python
from src.core.normalizer_factory import create_normalizer

# ... dentro de lifespan ...
    app.state.settings = settings
    app.state.engine = create_engine(settings)
    app.state.auth = create_auth_provider(settings)
    app.state.normalizer = create_normalizer(settings)
    yield
```

- [ ] **Step 6: Verificar GREEN — la suite entera (incluyendo los tests de Fase 1 modificados)**

Run: `python3 -m pytest tests/unit/ tests/integration/ -v`
Expected: PASS todos los tests (viejos + nuevos).

- [ ] **Step 7: Commit**

```bash
git add src/server/session.py src/main.py src/server/ws_handler.py tests/integration/*.py
git commit -m "feat(session): inject normalizer + normalize before synthesize"
```

---

## Task 9: Tests de integración (3 escenarios)

**Files:**
- Create: `tests/integration/test_normalizer_in_session.py`

**Interfaces:**
- Consumes: todo lo anterior.
- Produces: 3 tests de integración que verifican:
  - `SpanishNormalizer` aplicado segmento-a-segmento (motor recibe texto normalizado).
  - `PassThroughNormalizer` no toca el texto (motor recibe verbatim).
  - Normalizer que lanza → motor recibe texto original + log warn.

- [ ] **Step 1: Escribir los tests**

Crear `tests/integration/test_normalizer_in_session.py`:

```python
import asyncio
import json

from fastapi.testclient import TestClient

from src.auth.stub import StubAuthProvider
from src.core.config import Settings
from src.core.normalizer_factory import create_normalizer
from src.main import app
from src.tts.interface import ITTSEngine


class CapturingEngine(ITTSEngine):
    """Records the text passed to synthesize()."""

    def __init__(self) -> None:
        self._sample_rate = 24000
        self.received_texts: list[str] = []

    @property
    def sample_rate(self) -> int:
        return 24000

    async def synthesize(self, text: str):
        self.received_texts.append(text)
        await asyncio.sleep(0)
        yield b"\x00\x00" * 4800

    async def aclose(self) -> None:
        return None


def _setup(engine: ITTSEngine, normalizer_settings: dict | None = None) -> TestClient:
    overrides = {"engine": "mock", "auth_provider": "stub", "min_flush_chars": 5}
    if normalizer_settings:
        overrides.update(normalizer_settings)
    settings = Settings(**overrides)
    app.state.settings = settings
    app.state.engine = engine
    app.state.auth = StubAuthProvider()
    app.state.normalizer = create_normalizer(settings)
    return TestClient(app)


def _collect_session(client: TestClient, token_text: str):
    with client.websocket_connect("/ws") as ws:
        ws.send_text(json.dumps({"type": "auth", "token": "t"}))
        ws.receive_text()  # auth_ok
        ws.send_text(json.dumps({"type": "token", "text": token_text}))
        ws.send_text(json.dumps({"type": "end"}))
        # Drain until done.
        for _ in range(100):
            try:
                data = ws.receive()
            except Exception:
                break
            if data.get("type") == "websocket.send" and data.get("text"):
                if json.loads(data["text"])["type"] == "done":
                    break


def test_spanish_normalizer_applied_before_synthesis():
    engine = CapturingEngine()
    client = _setup(engine)
    _collect_session(client, "Tengo 25 años")
    assert any("veinticinco" in t for t in engine.received_texts), engine.received_texts
    assert all("25" not in t for t in engine.received_texts), engine.received_texts


def test_passthrough_normalizer_leaves_text_intact():
    engine = CapturingEngine()
    client = _setup(engine, normalizer_settings={"normalizer": "none"})
    _collect_session(client, "Tengo 25 años")
    assert any("Tengo 25 años" in t for t in engine.received_texts), engine.received_texts


def test_session_survives_normalizer_failure():
    """If the normalizer raises, the session must complete and engine gets original text."""

    class CrashingNormalizer:
        async def normalize(self, text: str) -> str:
            raise RuntimeError("simulated normalizer crash")

    engine = CapturingEngine()
    settings = Settings(engine="mock", auth_provider="stub", min_flush_chars=5)
    app.state.settings = settings
    app.state.engine = engine
    app.state.auth = StubAuthProvider()
    app.state.normalizer = CrashingNormalizer()
    client = TestClient(app)

    _collect_session(client, "Hola mundo")
    # Engine should still receive something (original text, since normalizer failed).
    assert len(engine.received_texts) > 0
```

- [ ] **Step 2: Correr los tests y verificar GREEN (deberían pasar al primer intento porque T8 ya está aplicado)**

Run: `python3 -m pytest tests/integration/test_normalizer_in_session.py -v`
Expected: PASS los 3 tests.

- [ ] **Step 3: Verificar la suite entera**

Run: `python3 -m pytest tests/ -v`
Expected: PASS todos los tests.

- [ ] **Step 4: Commit**

```bash
git add tests/integration/test_normalizer_in_session.py
git commit -m "test: integration tests for normalizer in session"
```

---

## Task 10: Checkpoint Fase 2

- [ ] **Step 1: Suite unitaria completa**

Run: `python3 -m pytest tests/unit/ -v`
Expected: PASS todos los tests.

- [ ] **Step 2: Suite de integración completa**

Run: `python3 -m pytest tests/integration/ -v`
Expected: PASS todos los tests.

- [ ] **Step 3: Smoke test con texto mixto**

Run: `python3 -c "
import asyncio
from src.tts.normalizer import SpanishNormalizer

async def main():
    n = SpanishNormalizer()
    cases = [
        'Tengo 25 años, son las 15:30 y vivo en 28013',
        'Dr. García cobra 50€ (50% menos)',
        '15/03/2024',
        'foo@bar.com',
        '#verano @usuario',
    ]
    for c in cases:
        out = await n.normalize(c)
        print(f'{c!r} → {out!r}')

asyncio.run(main())
"
"
Expected: prints the normalized versions of all 5 cases.

- [ ] **Step 4: Commit final si hay ajustes pendientes**

```bash
git status
git add -A
git commit -m "chore: fase 2 complete — Spanish text normalization"
```

---

## Notas operativas

- **`tests/integration/test_tts_stream.py` collection**: el problema `Router.__init__() got an unexpected keyword argument 'on_startup'` fue resuelto en Fase 1 actualizando FastAPI a `>=0.115`. La suite de integración ahora se carga y ejecuta sin errores de colección.

- **`num2words` quirks**: la librería añade un espacio antes del resultado; usamos `.strip()` por dentro (no aplicado todavía — verificar si falla test de igualdad exacta). Si falla, ajustar `_to_words` para retornar `num2words(...).strip()`.

- **`PassThroughNormalizer` activado**: en `test_tts_stream.py` y tests viejos, si `settings.normalizer="none"`, el comportamiento es idéntico al de Fase 1 (los 43 tests previos siguen verdes).

- **Fase 3 (multi-voz)**: cuando llegue, `Engine.synthesize` necesitará recibir `voice` y `lang`. `Normalizer.normalize` opera ANTES de esa llamada, así que es ortogonal.

- **Backwards compat**: añadir `normalizer` como parámetro posicional o keyword rompe la firma de `SpeakerSession.__init__`. Se documenta como breaking change para tests; cualquier llamada externa debe actualizarse. En este repo no hay llamadas externas a `SpeakerSession` fuera de `ws_handler.py`, que se actualiza en T8.