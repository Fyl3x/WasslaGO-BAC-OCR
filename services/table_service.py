"""Table extraction with escalation: if the arithmetic checks fail, widen the OCR
candidate pool and try again, keeping whichever attempt validates best."""
from __future__ import annotations

from ocr.layout import PageLayout
from ocr.table_reader import RawTable, read_table, reread_all
from parsers.subjects import Branch, Vocabulary
from parsers.table_parser import parse_table


def _score(checks) -> int:
    return sum(1 for c in checks if c.passed)


def extract_table(layout: PageLayout, branch: Branch | None, vocab: Vocabulary):
    """-> (raw_table, rows, overall, coefsum, checks, notes)"""
    raw = read_table(layout)
    rows, overall, coefsum, checks, notes = parse_table(raw, branch, vocab)
    if all(c.passed for c in checks):
        return raw, rows, overall, coefsum, checks, notes
    first = (raw, rows, overall, coefsum, checks, notes)
    reread_all(layout, raw)
    rows2, overall2, coefsum2, checks2, notes2 = parse_table(raw, branch, vocab)
    if _score(checks2) >= _score(checks):
        notes2 = notes2 + ["table needed a second, looser OCR pass"]
        return raw, rows2, overall2, coefsum2, checks2, notes2
    return first
