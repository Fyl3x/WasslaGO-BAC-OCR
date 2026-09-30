"""Extraction for PDFs that already contain a text layer (no OCR needed).

Header fields use the same line parsers as the scanned path. The grades table is rebuilt
from word coordinates: numeric tokens are grouped into rows by y, and the subject is the
Arabic text on the same row.

Some PDF producers store Arabic in *visual* order (characters reversed) and glue digits to
the neighbouring word. `detect_reversed` decides once per document, and `fix_line` /
`fix_word` undo it without touching digit runs.
"""
from __future__ import annotations

import re

from ocr.table_reader import RawCell, RawRow, RawTable
from ocr.zone_reader import LineRead, RawZones
from .normalize import clean_line, normalize_ar, strip_marks, to_ascii_digits

_NUM = re.compile(r"^\d{1,3}\.\d{2}$")
_INT = re.compile(r"^\d{1,3}$")
_DASH = re.compile(r"^[-–—]{1,3}$")
_SERIAL = re.compile(r"\d{10,18}\s*/\s*\S*?USEPWD_?BC-*\d{6,14}", re.I)
_AR = re.compile(r"[؀-ۿ]")
_TOKEN = re.compile(r"[\d.\-/]+|[^\d.\-/\s]+|\s+")

LABELS = {"year": "بكالوريا", "branch": "شعبة", "name": "السيد", "birth": "المزداد",
          "institution": "المؤسسة", "issue": "حرر"}
# words that make up each printed label (removed wherever they appear in the line)
LABEL_TOKENS = {
    "year": {"بكالوريا", "دورة"}, "branch": {"شعبة"}, "name": {"السيد", "السيدة", "(ة)", ")ة("},
    "birth": {"المزداد", "المزدادة", "(ة)", ")ة(", "في"}, "institution": {"المؤسسة"},
    "issue": {"حرر", "بالجزائر", "في:", "في"},
}


def _canonical(key: str, line: str) -> str:
    """'<value> : <label>' or '<label> : <value>' -> '<label> : <value>'."""
    drop = {normalize_ar(t) for t in LABEL_TOKENS[key]} | {t for t in LABEL_TOKENS[key]}
    kept = [tok for tok in line.replace(":", " ").split()
            if tok not in drop and normalize_ar(tok) not in drop]
    return f"{LABELS[key]} : " + " ".join(kept)
_KEY_WORDS = ("بكالوريا", "المجموع", "المعدل", "السيد", "المؤسسة", "شعبة", "كشف", "النقاط")


# ---------------------------------------------------------------------------
# visual-order repair
# ---------------------------------------------------------------------------
def _reverse_arabic_runs(line: str) -> str:
    """Reverse each run of consecutive Arabic words (word order + characters); digit and
    Latin tokens stay where they are and keep their internal order."""
    tokens = _TOKEN.findall(line)
    out: list[str] = []
    run: list[str] = []

    def flush():
        if run:
            words = [t for t in run if not t.isspace()]
            out.append(" ".join(w[::-1] for w in reversed(words)) + (" " if run[-1].isspace() else ""))
            run.clear()

    for tok in tokens:
        if _AR.search(tok):
            run.append(tok)
        elif tok.isspace() and run:
            run.append(tok)
        else:
            flush()
            out.append(tok)
    flush()
    return re.sub(r"[ ]{2,}", " ", "".join(out))


def _key_hits(lines: list[str]) -> int:
    j = normalize_ar(" ".join(lines))
    return sum(j.count(w) for w in _KEY_WORDS)


def detect_reversed(text: str) -> bool:
    lines = [clean_line(ln) for ln in text.splitlines() if ln.strip()]
    return _key_hits([_reverse_arabic_runs(ln) for ln in lines]) > _key_hits(lines)


def _unglue(line: str) -> str:
    """Put a space between Arabic letters and adjacent Latin letters / digits."""
    line = re.sub(r"([\u0600-\u06FF])([A-Za-z0-9])", r"\1 \2", line)
    return re.sub(r"([A-Za-z0-9])([\u0600-\u06FF])", r"\1 \2", line)


def fix_line(line: str, reversed_mode: bool) -> str:
    line = _unglue(clean_line(line))
    return _reverse_arabic_runs(line) if reversed_mode else line


def _split_word(w: tuple, reversed_mode: bool) -> list[tuple]:
    """Split '8.00ةايحلاو' into number + word (x split proportionally); reverse Arabic."""
    x0, y0, x1, y1, txt = w[0], w[1], w[2], w[3], clean_line(str(w[4]))
    parts = re.findall(r"[\d.\-]+|[^\d.\-]+", txt)
    if not parts:
        return []
    total = max(1, sum(len(p) for p in parts))
    out, cx = [], x0
    for p in parts:
        width = (x1 - x0) * len(p) / total
        piece = p[::-1] if (reversed_mode and _AR.search(p)) else p
        out.append((cx, y0, cx + width, y1, piece))
        cx += width
    return out


# ---------------------------------------------------------------------------
def zones_from_text(text: str, reversed_mode: bool | None = None) -> RawZones:
    zones = RawZones()
    rev = detect_reversed(text) if reversed_mode is None else reversed_mode
    if rev:
        zones.notes.append("text layer stores Arabic in visual order: reversed automatically")
    lines = [fix_line(ln, rev) for ln in text.splitlines() if ln.strip()]
    for i, ln in enumerate(lines):
        n = normalize_ar(ln)
        for key, label in LABELS.items():
            if key not in zones.lines and normalize_ar(label) in n:
                zones.lines[key] = LineRead(zone=key, box=(0, 0, 0, 0), text=_canonical(key, ln), conf=99.0,
                                            numbers=re.findall(r"\d[\d\-/]*\d", to_ascii_digits(ln)),
                                            digits_line=to_ascii_digits(ln))
        m = _SERIAL.search(ln.replace(" ", ""))
        if m:
            zones.serial_candidates.append(m.group(0))
        if "السري" in n and "الرقم" in n or "الرقم السري" in n:
            toks = re.findall(r"[A-Za-z0-9]{6,10}", ln) or (
                re.findall(r"[A-Za-z0-9]{6,10}", lines[i + 1]) if i + 1 < len(lines) else [])
            zones.secret_candidates += toks
        if not zones.reg_candidates and re.fullmatch(r"\d{8}", ln.strip()):
            zones.reg_candidates.append(ln.strip())
    if not zones.reg_candidates:
        m = re.search(r"(?<!\d)(\d{8})(?!\d)", to_ascii_digits(text))
        if m:
            zones.reg_candidates.append(m.group(1))
    return zones


def table_from_words(words: list, reversed_mode: bool) -> tuple[RawTable, list[str]]:
    """Rebuild a RawTable whose cell candidates are the exact text-layer strings, so the
    downstream reconciliation logic is shared with the OCR path."""
    ws: list[tuple] = []
    for w in words:
        ws.extend(_split_word(w, reversed_mode))
    numeric = [w for w in ws if _NUM.match(w[4]) or _INT.match(w[4]) or _DASH.match(w[4])]
    rows_by_y: dict[int, list] = {}
    for w in numeric:
        rows_by_y.setdefault(round((w[1] + w[3]) / 2 / 3), []).append(w)

    raw_rows: list[RawRow] = []
    total_row = {"total": RawCell(), "coef": RawCell()}
    notes: list[str] = []
    for key in sorted(rows_by_y):
        toks = sorted(rows_by_y[key], key=lambda w: w[0])       # left -> right: total, coef, grade
        y0, y1 = min(t[1] for t in toks), max(t[3] for t in toks)
        mid = (y0 + y1) / 2
        words_here = [w for w in ws if abs((w[1] + w[3]) / 2 - mid) < 4 and _AR.search(w[4])]
        boundary = min((w[0] for w in words_here), default=1e9)
        toks = [t for t in toks if t[2] <= boundary + 1]          # drop '-' etc. from the subject text
        vals = [t[4] for t in toks]
        subject = " ".join(w[4] for w in sorted(words_here, key=lambda w: -w[0]))
        if len(vals) == 3:
            row = RawRow(index=len(raw_rows), y0=int(y0), y1=int(y1), subject_text=subject)
            for name, v in zip(("total", "coef", "grade"), vals):
                cell = RawCell(candidates=[] if _DASH.match(v) else [v], dash=bool(_DASH.match(v)))
                setattr(row, name, cell)
            raw_rows.append(row)
    # totals row: [overall total, coefficient sum] and no subject cell before "المجموع العام"
    for key in reversed(sorted(rows_by_y)):
        toks = sorted(rows_by_y[key], key=lambda w: w[0])
        vals = [t[4] for t in toks]
        if len(vals) == 2 and _NUM.match(vals[0]) and re.fullmatch(r"\d{1,3}", vals[1]):
            total_row["total"] = RawCell(candidates=[vals[0]])
            total_row["coef"] = RawCell(candidates=[vals[1]])
            break
    if not raw_rows:
        notes.append("text layer present but no table rows could be rebuilt")
    return RawTable(rows=raw_rows, total_row=total_row, pitch=0, n_rows=len(raw_rows)), notes


def average_from_words(words: list, reversed_mode: bool = False) -> list[str]:
    """The average is the lowest lone dd.dd number (<= 20) on the page."""
    cands = []
    for w in words:
        for p in _split_word(w, reversed_mode):
            if re.fullmatch(r"\d{1,2}\.\d{2}", p[4]) and float(p[4]) <= 20:
                cands.append((p[1], p[4]))
    return [c[1] for c in sorted(cands)[-1:]]
