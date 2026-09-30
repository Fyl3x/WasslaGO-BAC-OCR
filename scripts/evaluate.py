"""Measure extraction accuracy against ground truth.

    python scripts/evaluate.py                       # tests/fixtures (synthetic)
    python scripts/evaluate.py samples/private       # your own labelled transcripts
    python scripts/evaluate.py DIR --min 0.9 --json report.json

For every `<name>.(pdf|png|jpg|jpeg)` that has a `<name>.expected.json` next to it, the
document is processed and compared with the ground truth. The expected file has the same
schema as `TranscriptResult.simple_dict()` (see tests/fixtures/*.expected.json). For a
multi-page PDF, `expected.json` may be a list with one object per page.

Scoring
  * field accuracy  = correct scalar fields / expected scalar fields   (15 fields)
  * cell accuracy   = correct table cells / expected table cells       (subject, grade, coef, total)
  * overall         = (correct fields + correct cells) / (fields + cells)
  * "strict" additionally requires the row count to match.
The process exits with status 1 when `overall` < --min (default 0.90).
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from parsers.normalize import normalize_ar, to_ascii_digits  # noqa: E402
from services.pipeline import process_file  # noqa: E402

SCALAR_FIELDS = ["registration_number", "session_year", "branch", "full_name", "birth_date", "birth_place",
                 "institution_name", "institution_code", "overall_total", "coefficient_total",
                 "overall_average", "issue_place", "issue_date", "secret_code", "serial_code"]
CASE_SENSITIVE = {"secret_code", "serial_code"}
NUMERIC = {"overall_total", "overall_average"}


def _norm(key: str, v):
    if v is None:
        return None
    if key in CASE_SENSITIVE:
        return str(v).strip()
    if key in NUMERIC:
        try:
            return round(float(v), 2)
        except (TypeError, ValueError):
            return None
    if key in {"session_year", "coefficient_total"}:
        try:
            return int(v)
        except (TypeError, ValueError):
            return None
    if isinstance(v, str):
        return normalize_ar(to_ascii_digits(v)) if any("؀" <= c <= "ۿ" for c in v) else to_ascii_digits(v).strip()
    return v


def compare_fields(pred: dict, exp: dict):
    ok, per = 0, {}
    for k in SCALAR_FIELDS:
        if k not in exp:
            continue
        good = _norm(k, pred.get(k)) == _norm(k, exp[k])
        per[k] = good
        ok += good
    return ok, len(per), per


def compare_table(pred_rows: list[dict], exp_rows: list[dict]):
    """Rows are compared in order; a cell counts when its normalised value matches."""
    cells_ok = cells_total = 0
    detail = []
    for i, e in enumerate(exp_rows):
        p = pred_rows[i] if i < len(pred_rows) else {}
        row = {}
        for k in ("subject", "grade", "coefficient", "total"):
            cells_total += 1
            pv, ev = p.get(k), e.get(k)
            if k == "subject":
                good = normalize_ar(pv or "") == normalize_ar(ev or "")
            else:
                good = (pv is None and ev is None) or (pv is not None and ev is not None and abs(float(pv) - float(ev)) < 0.006)
            row[k] = good
            cells_ok += good
        detail.append(row)
    strict = len(pred_rows) == len(exp_rows)
    return cells_ok, cells_total, strict, detail


def evaluate(folder: Path, verbose: bool = True) -> dict:
    exts = {".pdf", ".png", ".jpg", ".jpeg"}
    files = sorted(p for p in folder.iterdir()
                   if p.suffix.lower() in exts and (folder / f"{p.stem}.expected.json").exists())
    if not files:
        raise SystemExit(f"No documents with a matching <name>.expected.json in {folder}")
    totals = defaultdict(int)
    per_field = defaultdict(lambda: [0, 0])
    report = {"documents": []}
    for f in files:
        exp = json.loads((folder / f"{f.stem}.expected.json").read_text(encoding="utf-8"))
        expected_pages = exp if isinstance(exp, list) else [exp]
        t0 = time.time()
        results = process_file(f)
        dt = time.time() - t0
        for pi, e in enumerate(expected_pages):
            res = results[pi] if pi < len(results) else None
            pred = res.simple_dict() if res else {}
            fok, ftotal, per = compare_fields(pred, e)
            cok, ctotal, strict, _ = compare_table(pred.get("grades", []), e.get("grades", []))
            totals["fok"] += fok; totals["ftotal"] += ftotal
            totals["cok"] += cok; totals["ctotal"] += ctotal
            totals["strict_ok"] += int(strict and cok == ctotal and fok == ftotal)
            totals["docs"] += 1
            for k, g in per.items():
                per_field[k][0] += g
                per_field[k][1] += 1
            entry = {"file": f.name, "page": pi + 1, "fields": f"{fok}/{ftotal}", "cells": f"{cok}/{ctotal}",
                     "rows_match": strict, "seconds": round(dt / max(1, len(expected_pages)), 1),
                     "wrong_fields": [k for k, g in per.items() if not g]}
            report["documents"].append(entry)
            if verbose:
                print(f"{f.name:34s} p{pi + 1}  fields {fok:2d}/{ftotal:<2d}  table cells {cok:3d}/{ctotal:<3d}  "
                      f"rows {'ok' if strict else 'MISMATCH'}  {entry['seconds']:5.1f}s"
                      + (f"  wrong: {', '.join(entry['wrong_fields'])}" if entry["wrong_fields"] else ""))
    overall = (totals["fok"] + totals["cok"]) / max(1, totals["ftotal"] + totals["ctotal"])
    report["summary"] = {
        "documents": totals["docs"],
        "field_accuracy": round(totals["fok"] / max(1, totals["ftotal"]), 4),
        "cell_accuracy": round(totals["cok"] / max(1, totals["ctotal"]), 4),
        "overall_accuracy": round(overall, 4),
        "documents_fully_correct": totals["strict_ok"],
        "per_field_accuracy": {k: round(v[0] / v[1], 3) for k, v in per_field.items()},
    }
    return report


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("folder", nargs="?", default=str(ROOT / "tests" / "fixtures"))
    ap.add_argument("--min", type=float, default=0.90, help="required overall accuracy (default 0.90)")
    ap.add_argument("--json", help="write the full report to this file")
    args = ap.parse_args()
    report = evaluate(Path(args.folder))
    s = report["summary"]
    print("\n" + "=" * 72)
    print(f"documents           : {s['documents']}  (fully correct: {s['documents_fully_correct']})")
    print(f"field accuracy      : {s['field_accuracy']:.1%}")
    print(f"table cell accuracy : {s['cell_accuracy']:.1%}")
    print(f"OVERALL accuracy    : {s['overall_accuracy']:.1%}   (target >= {args.min:.0%})")
    weak = {k: v for k, v in s["per_field_accuracy"].items() if v < 1.0}
    if weak:
        print("fields below 100%   : " + ", ".join(f"{k} {v:.0%}" for k, v in sorted(weak.items(), key=lambda kv: kv[1])))
    if args.json:
        Path(args.json).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if s["overall_accuracy"] >= args.min else 1


if __name__ == "__main__":
    raise SystemExit(main())
