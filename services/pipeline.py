"""End-to-end extraction: file -> list of TranscriptResult (one per page)."""
from __future__ import annotations

import time
from pathlib import Path

import cv2
import numpy as np

from config import Config
from ocr import ocr_service
from ocr.layout import normalize_page
from ocr.loader import PageInput, load_pages
from ocr.zone_reader import read_zones
from parsers import bac_parser
from parsers.normalize import fmt2
from parsers.subjects import load_vocabulary, match_branch
from parsers.table_parser import parse_table
from services.table_service import extract_table
from parsers import text_layer
from services.models import Check, TranscriptResult


def process_file(path: str | Path, out_dir: str | Path | None = None) -> list[TranscriptResult]:
    pages = load_pages(path)
    out = Path(out_dir) if out_dir else None
    if out:
        out.mkdir(parents=True, exist_ok=True)
    results = []
    for page in pages:
        t0 = time.time()
        try:
            res = process_text_page(page) if page.source == "text-layer" else process_scanned_page(page, out)
        except ocr_service.OcrUnavailable:
            raise
        except Exception as exc:  # one bad page must not lose the others
            res = TranscriptResult(page=page.index, source=page.source, fields=bac_parser.new_fields(),
                                   error=f"{type(exc).__name__}: {exc}")
        res.elapsed_s = time.time() - t0
        results.append(res)
    return results


# ---------------------------------------------------------------------------
def _finish(res: TranscriptResult, zones, rows, overall, coefsum, checks, notes) -> TranscriptResult:
    res.fields = bac_parser.build_fields_from_zones(zones, (rows, overall, coefsum))
    res.rows = rows
    res.checks = checks
    res.notes += notes + zones.notes
    grades = res.fields  # noqa: F841  (kept for readability)
    return res


def _branch_from(zones):
    if "branch" not in zones.lines:
        return None
    text = bac_parser.strip_label(zones.lines["branch"].text, "branch")
    branch, score = match_branch(text)
    return branch if branch and score >= 70 else None


def process_scanned_page(page: PageInput, out_dir: Path | None) -> TranscriptResult:
    res = TranscriptResult(page=page.index, source="scan")
    layout = normalize_page(page.image, Config.TABLE_WORKING_WIDTH)
    if layout is None:
        res.fields = bac_parser.new_fields()
        res.error = ("Could not find the grades table. Make sure the whole transcript is visible, "
                     "sharp and not heavily rotated / cropped.")
        return res
    res.notes += layout.notes
    zones = read_zones(layout)
    raw_table, rows, overall, coefsum, checks, tnotes = extract_table(layout, _branch_from(zones), load_vocabulary())
    _finish(res, zones, rows, overall, coefsum, checks, tnotes)
    _add_field_checks(res)
    res.raw_text = _raw_text(zones, raw_table)
    if out_dir and Config.SAVE_DEBUG_IMAGES:
        res.debug_images = _save_debug(out_dir, page.index, layout, zones)
    return res


def process_text_page(page: PageInput) -> TranscriptResult:
    res = TranscriptResult(page=page.index, source="text-layer", raw_text=page.text or "")
    rev = text_layer.detect_reversed(page.text or "")
    zones = text_layer.zones_from_text(page.text or "", rev)
    raw_table, notes = text_layer.table_from_words(page.words or [], rev)
    zones.avg_candidates = text_layer.average_from_words(page.words or [], rev)
    branch = _branch_from(zones)
    rows, overall, coefsum, checks, tnotes = parse_table(raw_table, branch, load_vocabulary())
    _finish(res, zones, rows, overall, coefsum, checks, tnotes + notes)
    _add_field_checks(res)
    return res


# ---------------------------------------------------------------------------
def _add_field_checks(res: TranscriptResult) -> None:
    f = res.fields
    avg, tot = f["overall_average"], f["overall_total"]
    cs = f["coefficient_total"]
    if avg.value and tot.value and cs.value:
        ok = abs(float(tot.value) / float(cs.value) - float(avg.value)) <= 0.0151
        res.checks.append(Check("overall average = total / coefficients", ok,
                                f"{tot.value} / {cs.value} = {float(tot.value) / float(cs.value):.3f}, printed {avg.value}"))
    else:
        res.checks.append(Check("overall average = total / coefficients", None, "not enough data"))
    reg = f["registration_number"]
    if reg.value:
        res.checks.append(Check("registration number has 8 digits", len(str(reg.value)) == 8, str(reg.value)))
    missing = res.missing_fields
    if missing:
        res.notes.append("Missing fields: " + ", ".join(missing))


def _raw_text(zones, raw_table) -> str:
    lines = []
    for k in ("year", "branch", "name", "birth", "institution", "issue"):
        if k in zones.lines:
            lines.append(f"[{k}] {zones.lines[k].text}   digits={zones.lines[k].numbers}")
    lines.append(f"[registration] {zones.reg_candidates[:3]}")
    lines.append("[table]")
    for r in raw_table.rows:
        def fmt(c):
            return "-" if c.dash else ("" if c.empty else "/".join(dict.fromkeys(c.candidates)))
        lines.append(f"  {r.index:2d} | {r.subject_text} | grade={fmt(r.grade)} | coef={fmt(r.coef)} | total={fmt(r.total)}")
    tr = raw_table.total_row
    lines.append(f"  TOTAL | {'/'.join(dict.fromkeys(tr['total'].candidates))} | coef sum {'/'.join(dict.fromkeys(tr['coef'].candidates))}")
    lines.append(f"[average] {zones.avg_candidates}")
    lines.append(f"[secret] {zones.secret_candidates[:4]}")
    lines.append(f"[serial] {zones.serial_candidates[:2]}")
    return "\n".join(lines)


def _save_debug(out_dir: Path, n: int, layout, zones) -> dict[str, str]:
    saved = {}
    t = layout.table
    vis = layout.image.copy()
    for y in t.hlines:
        cv2.line(vis, (t.x0, y), (t.x1, y), (0, 180, 0), 4)
    for x in t.col_x:
        cv2.line(vis, (x, t.y_top), (x, t.y_bottom), (255, 0, 0), 4)
    bands = zones.debug.get("header_bands")
    if bands is not None:
        for y0, y1 in np.asarray(bands).reshape(-1, 2):
            cv2.rectangle(vis, (t.x0, int(y0)), (t.x1, int(y1)), (0, 0, 255), 3)
    name = f"page{n}_normalized.jpg"
    cv2.imwrite(str(out_dir / name), cv2.resize(vis, None, fx=0.45, fy=0.45, interpolation=cv2.INTER_AREA),
                [cv2.IMWRITE_JPEG_QUALITY, 82])
    saved["Normalized page (detected table + header lines)"] = name
    for key, lr in zones.lines.items():
        if lr.crop is not None and lr.crop.size:
            fn = f"page{n}_line_{key}.png"
            cv2.imwrite(str(out_dir / fn), lr.crop)
            saved[f"Crop: {key}"] = fn
    return saved
