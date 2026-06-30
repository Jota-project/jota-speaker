# Fase 2 — Normalización de texto para español — Design Spec

**Date:** 2026-06-29
**Branch:** main (post-Fase 1)
**Status:** Approved by user (block-by-block review)

## Context

jota-speaker es un microservicio TTS streaming que hoy lee texto literalmente. Esto produce:

- `"Tengo 25 años"` → "tengo dos cinco años"
- `"Dr. García"` → frase rota en el punto
- `"Son las 15:30"` → "son las uno cinco tres cero"
- `"50€"` → "cincuenta euro" (o peor, deletreado)
- `"Escribe a foo@bar.com"` → deletreo del email

Tras Fase 1 el sistema es robusto. Ahora el objetivo es que el TTS suene natural en español sin que el cliente tenga que pre-procesar el texto.

## Goal

Pipeline de normalización de texto en español, aplicado segmento-a-segmento justo antes de la síntesis, que cubre números, decimales, porcentajes, monedas, horas, fechas, abreviaturas, URLs, emails, hashtags, menciones, símbolos y emojis.

## Non-goals

- Cambio de voz / idioma (eso es Fase 3).
- SSML / prosodia (no en scope).
- Multi-idioma (solo español; extensibilidad preparada para futuro `"english"`).
- Soporte de catalán/gallego/euskera.
- Procesar el texto token-a-token dentro del accumulator (decisión confirmada: segmento-a-segmento).

## Architecture

```
TokenAccumulator
       │
       ▼ (cuando flushea)
SpeakerSession._synthesize_segment
       │
       ▼
Normalizer.normalize(segment)   ← NUEVO
       │
       ▼
Engine.synthesize(segment)
```

### Components

#### Nuevos

- **`src/tts/normalizer.py`**: contiene `INormalizer` (ABC), `SpanishNormalizer` (implementación), `PassThroughNormalizer` (no-op).
- **`src/core/normalizer_factory.py`**: `create_normalizer(settings) -> INormalizer` con match contra `settings.normalizer`.
- **`tests/unit/test_normalizer.py`**: cobertura exhaustiva por categoría.
- **`tests/integration/test_normalizer_in_session.py`**: smoke end-to-end.

#### Modificados

- `src/core/config.py`: añadir `normalizer`, `normalizer_excluded_patterns`, `hour_format`.
- `src/main.py`: lifespan crea `app.state.normalizer`.
- `src/server/ws_handler.py`: pasa `state.normalizer` a `SpeakerSession`.
- `src/server/session.py`: `SpeakerSession.__init__` acepta `normalizer`; `_synthesize_segment` llama `await self._normalizer.normalize(text)` antes de `engine.synthesize`.
- `pyproject.toml`: añadir `num2words[es]` como dependencia.
- `tests/integration/test_tts_stream.py`: actualizar `_make_client` para inyectar normalizer (los tests existentes deben seguir verdes con `PassThroughNormalizer`).

## Normalization Categories

`SpanishNormalizer.normalize(text)` aplica reglas en este orden:

| # | Categoría | Patrón entrada | Salida | Notas |
|---|-----------|----------------|--------|-------|
| 1 | URLs | `https?://\S+` | (sin tocar si en whitelist) | whitelist: `url` por defecto |
| 2 | Emails | `[\w.+-]+@[\w.-]+\.\w+` | `palabra1 palabra2 ...` (pronunciado) | whitelist: `email` |
| 3 | Hash/Mentions | `#\w+`, `@\w+` | `hashtag palabra`, `arroba palabra` | pronunciado |
| 4 | Fechas | `\d{1,2}[/-]\d{1,2}[/-]\d{2,4}` | `quince de marzo de dos mil veinticuatro` | month lookup table ES |
| 5 | Horas | `\d{1,2}:\d{2}` | `las quince treinta` (24h) o `las tres y media de la tarde` (12h) | configurable |
| 6 | Monedas | `€\d+` o `\d+\s*(€\|euros?)` | `cincuenta euros` | num2words + literal |
| 7 | Porcentajes | `\d+([,.]\d+)?%` | `cincuenta por ciento` | num2words |
| 8 | Decimales | `\d+[,.]\d+` | `tres coma catorce` | num2words para parte entera |
| 9 | Enteros | `\b\d+\b` | `veinticinco` | num2words; no aplica si está dentro de whitelist |
| 10 | Abreviaturas | `\b(Dr\|Ud\|Sr\|Sra\|S\.A\|etc\|p\. ej)\.\b` | `doctor`, `usted`, ..., `por ejemplo` | tabla de mapeo |
| 11 | Símbolos sueltos | `&`, `@` (si no son parte de email/hash) | `y`, `arroba` | solo cuando van sueltos |
| 12 | Emojis | rangos Unicode emoji | descripción textual corta o se omiten | `:\)` → "sonriendo"; otros se omiten |

**Orden importa**: las regex se aplican en este orden para evitar matching parcial.

## Whitelist (exclusiones)

Patrones que NO se tocan si están en `normalizer_excluded_patterns` (lista configurable, default: `["postal_code", "hash", "url", "email"]`):

- `postal_code`: 5 dígitos solos (`28013` → `28013`)
- `hash`: hashes hexadecimales (`0x[a-f0-9]+` o secuencias de 8+ hex chars)
- `url`: URLs completas
- `email`: emails completos (se pronuncian solo si NO está en whitelist)

**Configuración**:
```bash
JOTA_NORMALIZER_EXCLUDED_PATTERNS=postal_code,hash    # desactiva URL y email de la whitelist
JOTA_NORMALIZER_EXCLUDED_PATTERNS=                    # desactiva todas (normaliza todo)
```

## Configuration

```python
# src/core/config.py (añadir)
normalizer: str = "spanish"             # "spanish" | "none"
normalizer_excluded_patterns: list[str] = ["postal_code", "hash", "url", "email"]
hour_format: str = "24h"                 # "24h" | "12h"
```

Env vars:
- `JOTA_NORMALIZER=spanish|none`
- `JOTA_NORMALIZER_EXCLUDED_PATTERNS=postal_code,hash,url,email` (CSV)
- `JOTA_HOUR_FORMAT=24h|12h`

## Error Handling

- `normalize()` es **best-effort**: si lanza excepción interna, log warn + devolver texto sin modificar. La sesión nunca se rompe por un fallo del normalizador.
- `num2words` excepciones (números negativos, fracciones grandes, scientific notation): captura + fallback regex manual + log warn.
- Latencia objetivo: **<50ms p99** para segmentos ≤200 chars. Si se excede → log warn (sin bloquear envío; las métricas formales son Fase 5).
- Si `JOTA_NORMALIZER=none`, `PassThroughNormalizer` no añade latencia y devuelve `text` inmediatamente.

## Testing

### Unit (`tests/unit/test_normalizer.py`)

Una clase de tests por categoría (12 categorías). Patrón `TestXxxNormalization` con:

- Casos positivos (5+ cada una): entrada → salida esperada
- Casos negativos (no false positives): texto que NO debe tocarse
- Exclusiones (whitelist activa)
- Exclusiones desactivadas (whitelist vacía)

Helpers:

```python
@pytest.fixture
def normalizer():
    return SpanishNormalizer()

@pytest.fixture
def disabled_exclusions_normalizer():
    return SpanishNormalizer(excluded_patterns=[])
```

Tests críticos:
- Enteros: `"25"` → `"veinticinco"`
- Decimales: `"3,14"` → `"tres coma catorce"`
- Porcentajes: `"50%"` → `"cincuenta por ciento"`
- Monedas: `"50€"` → `"cincuenta euros"`
- Horas 24h: `"15:30"` → `"las quince treinta"`
- Horas 12h: `"3:30 pm"` → `"las tres y media de la tarde"`
- Fechas: `"15/03/2024"` → `"quince de marzo de dos mil veinticuatro"`
- Abreviaturas: `"Dr. García"` → `"doctor García"` (con punto posterior)
- Whitelist postal: `"28013"` → `"28013"`
- Whitelist hash: `"a1b2c3d4e5f6"` → sin cambios
- URL en whitelist: `"https://ejemplo.com"` → sin cambios
- Email en whitelist: `"foo@bar.com"` → sin cambios (texto completo)
- Email sin whitelist: `"foo@bar.com"` → `"foo arroba bar punto com"`
- Combinado: `"Tengo 25 años, soy Dr. García y vivo en 28013"` → `"Tengo veinticinco años, soy doctor García y vivo en 28013"`

### Integration (`tests/integration/test_normalizer_in_session.py`)

- `test_session_normalizes_before_synthesis`: con `SpanishNormalizer`, el motor recibe el texto normalizado (verificable con un spy engine que captura el `text` recibido en `synthesize`).
- `test_session_passthrough_when_normalizer_disabled`: con `JOTA_NORMALIZER=none`, el motor recibe el texto original verbatim.
- `test_session_continues_on_normalizer_error`: normalizador que lanza → motor recibe texto original + sesión no rompe + log warn emitido.

### Regresión

- Los 43 tests de Fase 1 deben seguir verdes.
- `tests/integration/test_tts_stream.py`: actualizar helper `_make_client` para inyectar `PassThroughNormalizer()` por defecto.

## Acceptance Criteria

1. `python3 -m pytest tests/unit/ -v` pasa todos los tests (viejos + nuevos).
2. `python3 -m pytest tests/integration/test_normalizer_in_session.py -v` pasa los 3 nuevos.
3. `python3 -m pytest tests/integration/test_tts_stream.py -v` sigue pasando los 7 existentes con la inyección de normalizer.
4. Cobertura de `src/tts/normalizer.py` ≥ 90%.
5. Latencia p99 de `normalize()` < 50ms con texto sintético de 200 chars (medido con `time.monotonic()` en un test de benchmark simple, no en CI).

## Risks

- **`num2words` quirks**: algunas reglas (apocope, acentos) pueden variar entre versiones. Mitigación: pin a versión específica en `pyproject.toml`.
- **Falsos positivos en regex**: emojis pueden coincidir con otros caracteres Unicode. Mitigación: usar librería `emoji` (opcional) o rangos Unicode explícitos.
- **Orden de aplicación**: si una categoría toca texto que otra esperaba, resultado inesperado. Mitigación: tests de orden explícitos y whitelist global que cortocircuita al inicio.
- **Performance**: regex compiladas en cada llamada. Mitigación: compilar las regex una vez en `__init__` de `SpanishNormalizer`.

## Dependencies

Añade `num2words[es]` (≈5 MB) a `pyproject.toml`. Ninguna otra dependencia nueva.

## Out of Scope (Fases futuras)

- **Fase 3**: voz/idioma per-session, multi-idioma (incluye inglés normalizer).
- **Fase 5**: métricas de latencia del normalizer, circuit breaker.
- **Fase 4**: barge-in (no afectado por normalización).

## Files Summary

### New
- `src/tts/normalizer.py` (≈300 LOC)
- `src/core/normalizer_factory.py` (≈20 LOC)
- `tests/unit/test_normalizer.py` (≈250 LOC)
- `tests/integration/test_normalizer_in_session.py` (≈100 LOC)

### Modified
- `src/core/config.py` (+3 settings)
- `src/main.py` (+2 LOC en lifespan)
- `src/server/ws_handler.py` (+1 línea)
- `src/server/session.py` (+5 LOC: parámetro, llamada a normalize)
- `pyproject.toml` (+1 dependencia)
- `tests/integration/test_tts_stream.py` (helper updated)
