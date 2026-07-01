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
        self._re_time = re.compile(r"\b(\d{1,2}):(\d{2})\s*(am|pm|AM|PM)?\b")
        self._re_date = re.compile(r"\b(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})\b")
        self._re_url = re.compile(r"https?://\S+")
        self._re_email = re.compile(r"[\w.+-]+@[\w.-]+\.\w+")
        self._re_hashtag = re.compile(r"#(\w+)")
        self._re_mention = re.compile(r"(?<!\w)@(\w+)")
        self._re_ampersand = re.compile(r"\s+&\s+")
        self._re_handle_safe_marker = re.compile(r"§§H:([^§]+)§§")

    async def normalize(self, text: str) -> str:
        try:
            t = text

            # 1. Protect excluded patterns with placeholder chunks that contain
            # no @, #, no digits, no special chars that other regexes care about.
            # Use § as marker (no @ inside), and lowercase letter prefix to identify type.
            protected: dict[str, str] = {}
            counter = [0]

            def _protect(m: re.Match) -> str:
                counter[0] += 1
                key = f"§§X{counter[0]}§§"
                protected[key] = m.group(0)
                return key

            if "url" in self.excluded:
                t = self._re_url.sub(_protect, t)
            if "email" in self.excluded:
                t = self._re_email.sub(_protect, t)
            if "hash" in self.excluded:
                t = re.sub(r"\b([0-9a-fA-F]{8,})\b", _protect, t)
            if "postal_code" in self.excluded:
                t = re.sub(r"(?<![\w.,])(\d{5})(?![\w.])", _protect, t)

            # 2. Numeric/date/time categories.
            t = self._apply_dates(t)
            t = self._apply_times(t)
            t = self._apply_percentages(t)
            t = self._apply_decimals(t)
            t = self._apply_currency(t)
            t = self._apply_integers(t)

            # 3. Email pronunciation only when not excluded.
            if "email" not in self.excluded:
                t = self._apply_emails(t)

            # 4. Handles.
            t = self._apply_handles(t)

            # 5. Abbreviations and symbols.
            t = self._apply_abbreviations(t)
            t = self._apply_ampersand(t)

            # 6. Restore protected sections.
            for key, original in protected.items():
                t = t.replace(key, original)
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

    def _apply_times(self, t: str) -> str:
        def _to_words(m: re.Match) -> str:
            hh, mm, meridiem = m.group(1), m.group(2), (m.group(3) or "").lower()
            try:
                h_int = int(hh)
                m_int = int(mm)
            except ValueError:
                return m.group(0)
            if self.hour_format == "12h" and meridiem in ("am", "pm"):
                period = "de la mañana" if meridiem == "am" else "de la tarde"
                if m_int == 0:
                    return f"{num2words(h_int, lang='es')} en punto {period}"
                if m_int == 30:
                    return f"{num2words(h_int, lang='es')} y media {period}"
                return (
                    f"{num2words(h_int, lang='es')} "
                    f"y {num2words(m_int, lang='es')} {period}"
                )
            if m_int == 0:
                base = num2words(h_int, lang="es")
                return f"{base} en punto"
            base = num2words(h_int, lang="es")
            mm_w = num2words(m_int, lang="es")
            return f"{base} y {mm_w}"
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

    def _apply_emails(self, t: str) -> str:
        def _to_words(m: re.Match) -> str:
            full = m.group(0)
            local, _, domain = full.partition("@")
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
            # Use (?<!\w) to avoid matching @ inside markers or words.
            t = self._re_mention.sub(lambda m: f"arroba {m.group(1)}", t)
            return t
        except Exception:
            return t

    def _apply_abbreviations(self, t: str) -> str:
        keys = sorted(_ABBREVIATIONS.keys(), key=len, reverse=True)
        out = t
        for abbr in keys:
            expansion = _ABBREVIATIONS[abbr]
            pattern = re.compile(re.escape(abbr) + r"\.", re.IGNORECASE)
            out = pattern.sub(expansion, out)
        return out

    def _apply_ampersand(self, t: str) -> str:
        try:
            return self._re_ampersand.sub(" y ", t)
        except Exception:
            return t
