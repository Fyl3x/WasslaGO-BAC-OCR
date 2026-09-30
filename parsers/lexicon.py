"""Spelling repair against small closed vocabularies (wilayas, institution words)."""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from rapidfuzz import fuzz

from config import Config
from .normalize import normalize_ar, skeleton_ar


@lru_cache(maxsize=1)
def _lex() -> dict:
    path = Path(Config.SUBJECTS_FILE).with_name("lexicon.json")
    return json.loads(path.read_text(encoding="utf-8"))


def _best(word: str, vocab: list[str], threshold: float, max_len_diff: int = 1) -> str | None:
    """Closest vocabulary entry, but only when the word is a plausible OCR corruption of it:
    similar length and high similarity (skeleton form ignores dot-only differences)."""
    w, ws = normalize_ar(word), skeleton_ar(word)
    if len(w) < 3:
        return None
    best, score = None, 0.0
    for cand in vocab:
        c = normalize_ar(cand)
        if abs(len(c) - len(w)) > max_len_diff:
            continue
        s = max(fuzz.ratio(w, c), 0.97 * fuzz.ratio(ws, skeleton_ar(cand)))
        if s > score:
            best, score = cand, s
    return best if score >= threshold else None


def fix_wilaya(piece: str, threshold: float = 84) -> str:
    """Snap a place-name piece to a wilaya name when it is clearly a misspelling of one."""
    piece = piece.strip()
    return _best(piece, _lex()["wilayas"], threshold) or piece


def fix_words(text: str, threshold: float = 88) -> str:
    """Repair common institution words token by token (never invents words)."""
    vocab = _lex()["institution_words"]
    out = []
    for tok in text.split():
        out.append(tok if tok == "-" else (_best(tok, vocab, threshold, max_len_diff=0) or tok))
    return " ".join(out)


def fix_specialty(text: str, threshold: float = 66) -> str | None:
    """'هندسة كيربالية' -> 'هندسة كهربائية' for technical branches."""
    return _best(text, _lex()["technical_specialties"], threshold)
