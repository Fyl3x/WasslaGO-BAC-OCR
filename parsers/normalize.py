"""Text / number normalisation shared by all parsers."""
from __future__ import annotations

import re
import unicodedata

_ARABIC_INDIC = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")
_DIACRITICS = re.compile(r"[ؐ-ًؚ-ٰٟۖ-ۭـ‎‏‪-‮]")
_ARABIC_LETTERS = re.compile(r"[ء-ي]")
_ARABIC_RUN = re.compile(r"[؀-ۿ]+")

MONTHS_AR = {  # Algerian month names -> number
    "جانفي": 1, "فيفري": 2, "مارس": 3, "أفريل": 4, "ماي": 5, "جوان": 6,
    "جويلية": 7, "أوت": 8, "سبتمبر": 9, "أكتوبر": 10, "نوفمبر": 11, "ديسمبر": 12,
}


def to_ascii_digits(s: str) -> str:
    return s.translate(_ARABIC_INDIC)


def strip_marks(s: str) -> str:
    """Remove diacritics, tatweel and invisible bidi control characters."""
    return _DIACRITICS.sub("", unicodedata.normalize("NFKC", s))


def normalize_ar(s: str) -> str:
    """Aggressive normal form used for fuzzy comparison of Arabic strings."""
    s = strip_marks(s)
    s = (s.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا").replace("ٱ", "ا")
          .replace("ى", "ي").replace("ة", "ه").replace("ؤ", "و").replace("ئ", "ي"))
    s = re.sub(r"[^ء-ي ]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


_SKELETON = str.maketrans({
    "ت": "ب", "ث": "ب", "ن": "ب", "ي": "ب", "ج": "ح", "خ": "ح", "ذ": "د", "ز": "ر", "ش": "س",
    "ص": "س", "ض": "س", "ظ": "ط", "غ": "ع", "ق": "ف",
})


def skeleton_ar(s: str) -> str:
    """Fold letters that differ only by their dots (OCR confuses them constantly)."""
    return normalize_ar(s).translate(_SKELETON)


def arabic_only(s: str) -> str:
    """Keep Arabic words only (drops Latin noise / digits / punctuation)."""
    return " ".join(_ARABIC_RUN.findall(strip_marks(s)))


def arabic_ratio(s: str) -> float:
    letters = [c for c in s if c.isalpha()]
    if not letters:
        return 0.0
    return sum(1 for c in letters if _ARABIC_LETTERS.match(c)) / len(letters)


def clean_line(s: str) -> str:
    s = to_ascii_digits(strip_marks(s))
    return re.sub(r"\s+", " ", s).strip()


# ---------------------------------------------------------------------------
# numbers
# ---------------------------------------------------------------------------
def quarter_ok(x: float) -> bool:
    return abs(x * 4 - round(x * 4)) < 1e-6


def _numeric_forms(text: str, allow_repair: bool = True) -> list[tuple[float, float]]:
    """All plausible float readings of an OCR digit string as (value, weight).
    Handles a lost decimal point ('1950' -> 19.50) and leading garbage digits that
    bled in from a neighbouring ruled line ('1136.50' -> 136.50)."""
    t = to_ascii_digits(text).replace(",", ".").replace(" ", "")
    t = re.sub(r"[^0-9.]", "", t)
    t = t.strip(".")
    if not t:
        return []
    out: list[tuple[float, float]] = []
    if "." in t:
        m = re.fullmatch(r"(\d+)\.(\d{1,2})\.?", t)
        if m:
            out.append((float(f"{m.group(1)}.{m.group(2).ljust(2, '0')}"), 1.0))
        else:  # several dots: keep the last well-formed number
            found = re.findall(r"\d+\.\d{2}", t)
            if found:
                out.append((float(found[-1]), 0.8))
    elif allow_repair and len(t) >= 3:
        out.append((float(f"{t[:-2]}.{t[-2:]}"), 0.7))
    # strip up to 2 leading digits (ruled-line bleed)
    if allow_repair:
        for v, w in list(out):
            s = f"{v:.2f}"
            for k in (1, 2):
                if len(s.split(".")[0]) > k:
                    out.append((float(s[k:]), w * 0.55))
    return out


def _soft_quarter(vals: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Most grades are multiples of 0.25, but PE is an average of several tests
    (17.67, 19.42 ...). So off-grid values are down-weighted, not rejected."""
    return [(v, w if quarter_ok(v) else w * 0.88) for v, w in vals]


def grade_values(text: str) -> list[tuple[float, float]]:
    return _soft_quarter([(v, w) for v, w in _numeric_forms(text) if 0 <= v <= 20])


def total_values(text: str) -> list[tuple[float, float]]:
    return _soft_quarter([(v, w) for v, w in _numeric_forms(text) if 0 <= v <= 200])


def overall_total_values(text: str) -> list[tuple[float, float]]:
    return _soft_quarter([(v, w) for v, w in _numeric_forms(text) if 0 <= v <= 1000])


def coef_values(text: str) -> list[tuple[float, float]]:
    d = re.sub(r"\D", "", to_ascii_digits(text))
    out = []
    if d:
        # a single digit is expected; take each digit as a weaker alternative
        if len(d) == 1 and 1 <= int(d) <= 9:
            out.append((float(int(d)), 1.0))
        else:
            for ch in d:
                if 1 <= int(ch) <= 9:
                    out.append((float(int(ch)), 0.4))
    return out


def coefsum_values(text: str) -> list[tuple[float, float]]:
    d = re.sub(r"\D", "", to_ascii_digits(text))
    if not d:
        return []
    out = []
    if 1 <= len(d) <= 3 and 1 <= int(d) <= 99:
        out.append((float(int(d)), 1.0))
    if len(d) > 2:
        out.append((float(int(d[-2:])), 0.5))
    return out


def fmt2(x: float | None) -> str | None:
    return None if x is None else f"{x:.2f}"
