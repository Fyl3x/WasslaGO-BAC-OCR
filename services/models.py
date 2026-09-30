"""Plain dataclasses describing an extraction result. `to_dict()` output is what the JSON
export and the HTML templates consume."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

# field statuses
OK = "ok"                # read directly and passed its sanity checks
DERIVED = "derived"      # recomputed / repaired from other fields
LOW = "low_confidence"   # read, but failed a check or OCR confidence is poor
MISSING = "missing"      # not found


@dataclass
class Field:
    key: str
    label_ar: str
    label_en: str
    value: Any = None
    raw: str | None = None
    status: str = MISSING
    confidence: float = 0.0   # 0.0 - 1.0
    note: str | None = None

    def set(self, value, status=OK, confidence=0.9, raw=None, note=None) -> "Field":
        self.value, self.status, self.confidence = value, status, confidence
        if raw is not None:
            self.raw = raw
        if note:
            self.note = note
        return self


@dataclass
class GradeRow:
    index: int
    subject: str | None
    subject_raw: str
    subject_score: float
    grade: float | None
    coefficient: int | None
    total: float | None
    status: str = OK            # ok | derived | not_taken | conflict | unreadable
    notes: list[str] = field(default_factory=list)


@dataclass
class Check:
    name: str
    passed: bool | None          # None = could not be evaluated
    detail: str = ""


@dataclass
class TranscriptResult:
    page: int
    source: str                              # "scan" | "text-layer"
    fields: dict[str, Field] = field(default_factory=dict)
    rows: list[GradeRow] = field(default_factory=list)
    checks: list[Check] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    raw_text: str = ""
    debug_images: dict[str, str] = field(default_factory=dict)   # name -> relative file path
    elapsed_s: float = 0.0
    error: str | None = None

    # -------------------------------------------------------------------------
    @property
    def missing_fields(self) -> list[str]:
        return [f.key for f in self.fields.values() if f.status == MISSING]

    @property
    def completeness(self) -> float:
        if not self.fields:
            return 0.0
        return sum(1 for f in self.fields.values() if f.status != MISSING) / len(self.fields)

    @property
    def overall_confidence(self) -> float:
        if not self.fields:
            return 0.0
        return sum(f.confidence for f in self.fields.values()) / len(self.fields)

    def to_dict(self) -> dict:
        return {
            "page": self.page,
            "source": self.source,
            "fields": {k: asdict(v) for k, v in self.fields.items()},
            "grades": [asdict(r) for r in self.rows],
            "checks": [asdict(c) for c in self.checks],
            "missing_fields": self.missing_fields,
            "completeness": round(self.completeness, 3),
            "overall_confidence": round(self.overall_confidence, 3),
            "notes": self.notes,
            "elapsed_s": round(self.elapsed_s, 2),
            "error": self.error,
        }

    def simple_dict(self) -> dict:
        """Compact form used for evaluation against expected fixtures."""
        out: dict[str, Any] = {k: f.value for k, f in self.fields.items()}
        out["grades"] = [{"subject": r.subject, "grade": r.grade, "coefficient": r.coefficient,
                          "total": r.total} for r in self.rows]
        return out
