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
        self.excluded = set(
            excluded_patterns if excluded_patterns is not None else self.DEFAULT_EXCLUDED
        )
        self.hour_format = hour_format
        self._re_int = re.compile(r"(?<![\w.,])(\d{1,21})(?![\w.])")
        self._re_decimal = re.compile(r"(\d{1,15})([.,])(\d{1,15})(?!\d)")
        self._re_percent = re.compile(r"(\d+(?:[.,]\d+)?)\s*%")
        self._re_currency = re.compile(r"(\d{1,15}(?:[.,]\d+)?)\s*(?:€|euros?|EUR)")
        self._re_currency_prefix = re.compile(r"€\s*(\d{1,15}(?:[.,]\d+)?)")

    async def normalize(self, text: str) -> str:
        try:
            t = text
            if "postal_code" in self.excluded:
                t = re.sub(r"(?<![\w.,])(\d{5})(?![\w.])", r"__POSTAL_\1__", t)
            t = self._apply_percentages(t)
            t = self._apply_decimals(t)
            t = self._apply_currency(t)
            t = self._apply_integers(t)
            if "postal_code" in self.excluded:
                t = re.sub(r"__POSTAL_(\d{5})__", r"\1", t)
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
            if "," in num or "." in num:
                sep_idx = max(num.find(","), num.find("."))
                whole = num[:sep_idx]
                frac = num[sep_idx + 1:]
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

        def _prefix_to_words(m: re.Match) -> str:
            num = m.group(1)
            try:
                w = num2words(int(num.replace(",", "").replace(".", "")), lang="es")
            except Exception:
                return m.group(0)
            return f"{w} euros"
        t = self._re_currency_prefix.sub(_prefix_to_words, t)
        return t
