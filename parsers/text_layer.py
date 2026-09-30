"""Extraction for PDFs that already contain a text layer (no OCR needed).

Header fields use the same line parsers as the scanned path. The grades table is rebuilt
from word coordinates: numeric tokens are grouped into rows by y, and the subject is the
Arabic text on the same row.
"""
from __future__ import annotations

import re

from ocr.table_reader import RawCell, RawRow, RawTable
from ocr.zone_reader import LineRead, RawZones
from .bac_parser import _orient
from .normalize import clean_line, normalize_ar, strip_marks, to_ascii_digits

_NUM = re.compile(r"^\d{1,3}\.\d{2}$")
_INT = re.compile(r"^\d{1,2}$")
_DASH = re.compile(r"^[-–—]{1,3}$")
_SERIAL = re.compile(r"\d{10,18}\s*/\s*\S*?USEPWD_?BC-*\d{6,14}", re.I)

LABELS = {
    "year": "بكالوريا", "branch": "شعبة", "name": "السيد", "birth": "المزداد",
    "institution": "المؤسسة", "issue": "حرر",
}


def _norm_words(words: list) -> list[tuple[float, float, float, float, str]]:
    out = []
    for w in words:
        txt = clean_line(str(w[4]))
        if txt:
            out.append((w[0], w[1], w[2], w[3], txt))
    return out


def zones_from_text(text: str) -> RawZones:
    zones = RawZones()
    lines = _orient([clean_line(ln) for ln in text.splitlines() if ln.strip()])
    for i, ln in enumerate(lines):
        n = normalize_ar(ln)
        for key, label in LABELS.items():
            if key not in zones.lines and label in n:
                zones.lines[key] = LineRead(zone=key, box=(0, 0, 0, 0), text=ln, conf=99.0,
                                            numbers=re.findall(r"\d[\d\-/]*\d", to_ascii_digits(ln)),
                                            digits_line=to_ascii_digits(ln))
        m = _SERIAL.search(ln.replace(" ", ""))
        if m:
            zones.serial_candidates.append(m.group(0))
        if "الرقم السري" in n or ("السري" in n and "الرقم" in n):
            toks = re.findall(r"[A-Za-z0-9]{6,10}", ln) or (re.findall(r"[A-Za-z0-9]{6,10}", lines[i + 1])
                                                            if i + 1 < len(lines) else [])
            zones.secret_candidates += toks
        if not zones.reg_candidates and re.fullmatch(r"\d{8}", ln.strip()):
            zones.reg_candidates.append(ln.strip())
    if not zones.reg_candidates:
        m = re.search(r"(?<!\d)(\d{8})(?!\d)", to_ascii_digits(text))
        if m:
            zones.reg_candidates.append(m.group(1))
    return zones


def table_from_words(words: list) -> tuple[RawTable, list[str]]:
    """Rebuild RawTable (candidates = the exact text, so downstream logic is unchanged)."""
    ws = _norm_words(words)
    numeric = [w for w in ws if _NUM.match(w[4]) or _INT.match(w[4]) or _DASH.match(w[4])]
    # find grade rows: 3 numeric tokens on one baseline, ordered left->right = total, coef, grade
    ws.sort(key=lambda w: (round((w[1] + w[3]) / 2 / 3), w[0]))
    rows_by_y: dict[int, list] = {}
    for w in numeric:
        rows_by_y.setdefault(round((w[1] + w[3]) / 2 / 3), []).append(w)
    raw_rows: list[RawRow] = []
    total_row = {"total": RawCell(), "coef": RawCell()}
    notes: list[str] = []
    for key in sorted(rows_by_y):
        toks = sorted(rows_by_y[key], key=lambda w: w[0])
        vals = [t[4] for t in toks]
        y0, y1 = min(t[1] for t in toks), max(t[3] for t in toks)
        subj = " ".join(w[4] for w in sorted(
            [w for w in ws if abs((w[1] + w[3]) / 2 - (y0 + y1) / 2) < 4 and w[0] > toks[-1][2]
             and re.search(r"[ء-ي]", w[4])], key=lambda w: -w[0]))
        if len(vals) == 3:
            row = RawRow(index=len(raw_rows), y0=int(y0), y1=int(y1), subject_text=subj)
            for name, v in zip(("total", "coef", "grade"), vals):
                cell = RawCell(candidates=[] if _DASH.match(v) else [v], dash=bool(_DASH.match(v)))
                setattr(row, "grade" if name == "grade" else name, cell)
            raw_rows.append(row)
    # the totals row: two numbers (total, coefficient sum) and no subject, after all rows
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


def average_from_words(words: list) -> list[str]:
    """The average is the lone dd.dd number on the line below the totals row."""
    cands = []
    for w in _norm_words(words):
        if re.fullmatch(r"\d{1,2}\.\d{2}", w[4]) and float(w[4]) <= 20:
            cands.append((w[1], w[4]))
    return [c[1] for c in sorted(cands)[-1:]]
