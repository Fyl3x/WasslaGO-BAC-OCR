"""Subject vocabulary: fuzzy-match noisy OCR text to the canonical BAC subject names."""
from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from rapidfuzz import fuzz

from config import Config
from .normalize import arabic_only, normalize_ar, skeleton_ar


@dataclass
class Subject:
    id: str
    name_ar: str
    name_fr: str
    forms: list[str]  # normalised strings to compare against


@dataclass
class Branch:
    id: str
    name_ar: str
    forms: list[str]
    order: list[str]


@dataclass
class Vocabulary:
    subjects: dict[str, Subject]
    branches: list[Branch]


@lru_cache(maxsize=4)
def load_vocabulary(path: str | None = None) -> Vocabulary:
    data = json.loads(Path(path or Config.SUBJECTS_FILE).read_text(encoding="utf-8"))
    subjects = {}
    for s in data["subjects"]:
        forms = [normalize_ar(x) for x in [s["name_ar"], *s.get("aliases", [])]]
        subjects[s["id"]] = Subject(s["id"], s["name_ar"], s.get("name_fr", ""), [f for f in forms if f])
    branches = [Branch(b["id"], b["name_ar"], [normalize_ar(x) for x in [b["name_ar"], *b.get("aliases", [])]],
                       b.get("order", [])) for b in data["branches"]]
    return Vocabulary(subjects, branches)


def _score(a: str, forms: list[str]) -> float:
    best = 0.0
    a_sk = skeleton_ar(a)
    for f in forms:
        s = max(fuzz.ratio(a, f), 0.9 * fuzz.partial_ratio(a, f) if len(a) >= 6 else 0,
                fuzz.token_sort_ratio(a, f),
                0.96 * fuzz.ratio(a_sk, skeleton_ar(f)),
                0.9 * fuzz.partial_ratio(a_sk, skeleton_ar(f)) if len(a) >= 6 else 0)
        best = max(best, s)
    return best


def match_subject(ocr_text: str, vocab: Vocabulary | None = None, candidates: list[str] | None = None,
                  expected_id: str | None = None) -> tuple[Subject | None, float]:
    """Best vocabulary entry for `ocr_text` (score 0-100). `candidates` restricts the
    search to a set of subject ids; `expected_id` (branch order prior) gets a small
    bonus so that it wins ties between similar-looking names."""
    vocab = vocab or load_vocabulary()
    text = normalize_ar(arabic_only(ocr_text))
    if len(text.replace(" ", "")) < 3:
        return None, 0.0
    best: tuple[Subject | None, float] = (None, 0.0)
    ids = candidates if candidates is not None else list(vocab.subjects)
    for sid in ids:
        sub = vocab.subjects[sid]
        score = _score(text, sub.forms) + (6.0 if sid == expected_id else 0.0)
        if score > best[1]:
            best = (sub, score)
    return best[0], min(best[1], 100.0)


def match_branch(ocr_text: str, vocab: Vocabulary | None = None) -> tuple[Branch | None, float]:
    vocab = vocab or load_vocabulary()
    text = normalize_ar(arabic_only(ocr_text))
    if not text:
        return None, 0.0
    best: tuple[Branch | None, float] = (None, 0.0)
    for b in vocab.branches:
        score = _score(text, b.forms)
        if score > best[1]:
            best = (b, score)
    return best
