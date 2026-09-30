"""Read the grades table cell by cell.

The body of the table is a uniform grid (12 rows on every transcript we have seen). We
estimate the row pitch, snap the grid onto the ink, clean every cell (remove the ruled
lines and specks that bleed in from neighbours) and OCR each cell several ways. The
candidates are later reconciled arithmetically by `parsers.table_parser`.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

import cv2
import numpy as np

from config import Config
from . import ocr_service as ocr
from .layout import PageLayout
from .preprocessing import binarize, prepare_for_ocr, remove_border_lines


@dataclass
class RawCell:
    candidates: list[str] = field(default_factory=list)
    empty: bool = False   # no ink at all in the cell
    dash: bool = False    # only a short horizontal stroke ('-' / '--')


@dataclass
class RawRow:
    index: int
    y0: int
    y1: int
    subject_text: str = ""
    subject_conf: float = -1.0
    grade: RawCell = field(default_factory=RawCell)
    coef: RawCell = field(default_factory=RawCell)
    total: RawCell = field(default_factory=RawCell)


@dataclass
class RawTable:
    rows: list[RawRow]
    total_row: dict[str, RawCell]       # {"total": cell, "coef": cell}
    pitch: float
    n_rows: int
    debug_images: dict[str, np.ndarray] = field(default_factory=dict)


# ---------------------------------------------------------------------------
def _estimate_grid(layout: PageLayout) -> tuple[int, float, float, np.ndarray]:
    """Return (n_rows, pitch, offset_of_first_boundary, ink profile of the numeric columns)."""
    t = layout.table
    y0, y1 = t.header_bottom, t.total_top
    height = y1 - y0
    x0, x1 = t.col_x[0] + 10, t.col_x[3] - 10
    reg = remove_border_lines(layout.bw[y0 + 4:y1 - 4, x0:x1], 0.5)
    raw0 = (reg.sum(axis=1) / 255.0).astype(np.float32)
    prof = cv2.GaussianBlur(raw0.reshape(-1, 1), (1, 0), 4).ravel()
    p = prof - prof.mean()
    if p.std() < 1e-6:
        return 12, height / 12.0, 0.0, raw0
    ac = np.correlate(p, p, "full")[len(p) - 1:]
    lo, hi = int(0.022 * t.width), int(0.045 * t.width)
    lag = lo + int(np.argmax(ac[lo:hi]))
    n = int(round(height / lag))
    n = min(max(n, 8), 18)
    pitch = height / n

    # snap: shift the grid so that as little ink as possible sits on row boundaries
    raw = (reg.sum(axis=1) / 255.0).astype(np.float32)
    best, best_off = None, 0.0
    for off in np.arange(-0.25 * pitch, 0.25 * pitch + 1, 1.0):
        cost = 0.0
        for r in range(1, n):
            b = int(round(r * pitch + off)) - 4
            if 0 <= b < len(raw) - 8:
                cost += raw[b:b + 8].sum()
        if best is None or cost < best:
            best, best_off = cost, float(off)
    return n, pitch, best_off, raw0


def _binarize_cell(gray: np.ndarray, paper_frac: float = 0.62) -> np.ndarray:
    """Ink mask for a small crop. The threshold is Otsu, capped at a fraction of the local
    paper brightness so the pale watermark (much lighter than printed digits) never counts."""
    blur = cv2.GaussianBlur(gray, (3, 3), 0)
    paper = float(np.percentile(blur, 92))
    otsu, _ = cv2.threshold(blur, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    thr = min(otsu, paper_frac * paper)
    return (blur < thr).astype(np.uint8) * 255


def _clean_cell(gray_cell: np.ndarray, paper_frac: float = 0.62) -> tuple[np.ndarray, RawCell]:
    """Binarise a numeric cell and keep only the ink that belongs to it.

    * ruled lines (tall strokes on the side edges, flat slivers on the top / bottom edge)
      and ink of neighbouring rows are dropped;
    * digit-sized components are the 'main' ink; smaller components (decimal point) are
      kept only when they sit inside the horizontal span of the main ink;
    * a lone flat stroke means a dash ('-' / '--'); nothing at all means empty.
    """
    state = RawCell()
    if gray_cell.size == 0:
        state.empty = True
        return gray_cell, state
    bw = _binarize_cell(gray_cell, paper_frac)
    h, w = bw.shape
    n, labels, stats, _ = cv2.connectedComponentsWithStats(bw, connectivity=8)
    comps = []
    for i in range(1, n):
        x, y, cw, ch, area = (int(v) for v in stats[i])
        if area < 6:
            continue
        touches_side = x <= 1 or x + cw >= w - 1
        touches_top, touches_bot = y <= 1, y + ch >= h - 1
        if touches_side and ch > 0.5 * h and ch >= 3.0 * max(cw, 1):        # vertical rule (thin + tall)
            continue
        if (touches_top or touches_bot) and cw >= 2.5 * max(ch, 1) and cw > 0.15 * w:   # horizontal rule
            continue
        comps.append((i, x, y, cw, ch, area, touches_top, touches_bot))
    main = [c for c in comps if c[4] >= 0.35 * h and not ((c[6] or c[7]) and c[4] < 0.6 * h)]
    keep = np.zeros_like(bw)
    if main:
        sx0 = min(c[1] for c in main) - 0.04 * w
        sx1 = max(c[1] + c[3] for c in main) + 0.04 * w
        ly0, ly1 = min(c[2] for c in main), max(c[2] + c[4] for c in main)
        main_ids = {c[0] for c in main}
        for i, x, y, cw, ch, area, top, bot in comps:
            inside = sx0 <= x and x + cw <= sx1
            compact_low = ch <= 0.25 * h and cw <= 1.6 * ch and y + ch > 0.6 * h and not top
            # fragments of a glyph whose strokes broke under the ink threshold: same text line,
            # right next to the main ink
            fragment = (area >= 10 and ch >= 0.12 * h and ly0 - 3 <= y and y + ch <= ly1 + 3
                        and sx0 - 0.07 * w <= x and x + cw <= sx1 + 0.07 * w)
            if i in main_ids or (inside and compact_low) or fragment:
                keep[labels == i] = 255
        return 255 - keep, state
    # no digit-sized ink: dash or empty
    flats = [c for c in comps if c[3] >= 2 * max(c[4], 1) and 0.25 * h <= c[2] + c[4] / 2 <= 0.75 * h]
    if flats:
        state.dash = True
    else:
        state.empty = True
    return np.full((h, w), 255, np.uint8), state


def _cell_candidates(gray_cell: np.ndarray, kind: str, paper_frac: float = 0.62) -> RawCell:
    cleaned, state = _clean_cell(gray_cell, paper_frac)
    if state.empty or state.dash:
        return state
    ink_rows = np.where((cleaned < 128).any(axis=1))[0]
    ink_cols = np.where((cleaned < 128).any(axis=0))[0]
    y0, y1 = int(ink_rows.min()), int(ink_rows.max()) + 1
    x0, x1 = int(ink_cols.min()), int(ink_cols.max()) + 1
    tight = cleaned[max(0, y0 - 3):y1 + 3, max(0, x0 - 3):x1 + 3]
    tight = cv2.copyMakeBorder(tight, 18, 18, 24, 24, cv2.BORDER_CONSTANT, value=255)
    img = cv2.resize(tight, None, fx=80.0 / tight.shape[0], fy=80.0 / tight.shape[0],
                     interpolation=cv2.INTER_CUBIC)
    outs: list[str] = []
    wl = "0123456789." if kind != "coef" else "0123456789"
    psms = (7, 8, 13) if kind != "coef" else (10, 8, 7)
    for psm in psms:
        txt = ocr.read_digits(img, psm=psm, whitelist=wl).text.replace("\n", " ").strip()
        if txt:
            outs.append(txt)
    state.candidates = outs
    return state


def _erase_rules(gray_cell: np.ndarray) -> np.ndarray:
    """For Arabic text cells: keep the grayscale (dots and diacritics matter) but paint
    over ruled lines / neighbour ink that touches the crop edge."""
    if gray_cell.size == 0:
        return gray_cell
    bw = binarize(gray_cell)
    h, w = bw.shape
    n, labels, stats, _ = cv2.connectedComponentsWithStats(bw, connectivity=8)
    out = gray_cell.copy()
    paper = int(np.percentile(gray_cell, 90))
    for i in range(1, n):
        x, y, cw, ch, _ = stats[i]
        if (x <= 1 or x + cw >= w - 1) and ch > 0.5 * h and cw < 0.12 * w:
            out[labels == i] = paper
    return out


def _read_subject(gray_cell: np.ndarray) -> tuple[str, float]:
    cleaned, state = _clean_cell(gray_cell)
    if state.empty or state.dash:
        return "", -1.0
    best: tuple[float, str, float] | None = None
    prepared = prepare_for_ocr(_erase_rules(gray_cell), target_height=84)
    for psm, mixed in ((7, False), (13, False), (7, True)):
        res = ocr.read_arabic(prepared, psm=psm, mixed=mixed)
        arabic_letters = sum(1 for ch in res.text if "\u0621" <= ch <= "\u064A")
        score = res.confidence + min(arabic_letters, 20)      # prefer readings that contain Arabic
        if best is None or score > best[0]:
            best = (score, res.text, res.confidence)
        if res.confidence >= 80 and arabic_letters >= 5:
            break
    return best[1].replace("\n", " ").strip(), best[2]


def _row_trusted(row: RawRow, row_options) -> bool:
    """A row is trusted when its best reading is a fully consistent triple that the three
    cells agree on (each cell read several times)."""
    opts = row_options(row)
    return bool(opts) and opts[0].status == "ok" and opts[0].score >= 5.5


def reread_all(layout: PageLayout, raw: RawTable, fracs=(0.85, 1.6)) -> None:
    """Widen the candidate pool of every numeric cell with looser ink thresholds. Called
    when the global totals do not reconcile, so the sums can choose among alternatives."""
    t = layout.table
    gray = layout.gray
    inset = 12
    jobs = []
    for r in raw.rows:
        for kind, (xa, xb) in (("grade", t.col_grade), ("coef", t.col_coef), ("total", t.col_total)):
            for frac in fracs:
                jobs.append((r, kind, (xa + inset, r.y0, xb - inset, r.y1), frac))
    for name, (xa, xb) in (("total", t.col_total), ("coef", t.col_coef)):
        for frac in fracs:
            jobs.append((None, name, (xa + inset, t.total_top + 4, xb - inset, t.y_bottom - 3), frac))

    def run(job):
        r, kind, (xa, ya, xb, yb), frac = job
        k = kind if r is not None or kind == "total" else "grade"
        return job, _cell_candidates(gray[ya:yb, xa:xb], k, frac)

    with ThreadPoolExecutor(max_workers=Config.OCR_WORKERS) as pool:
        for (r, kind, _, _), fresh in pool.map(run, jobs):
            cell = getattr(r, kind) if r is not None else raw.total_row[kind]
            cell.candidates += fresh.candidates
            if fresh.candidates:
                cell.empty = cell.dash = False


def read_table(layout: PageLayout) -> RawTable:
    t = layout.table
    n, pitch, off, prof = _estimate_grid(layout)
    gray = layout.gray
    inset = 12
    jobs: list[tuple[str, int, tuple[int, int, int, int]]] = []
    rows: list[RawRow] = []
    for r in range(n):
        centre = (r + 0.5) * pitch + off                    # relative to header_bottom (+4 crop offset)
        lo, hi = int(centre - 0.42 * pitch) - 4, int(centre + 0.42 * pitch) - 4
        seg = prof[max(0, lo):max(0, hi)]
        if seg.size and seg.sum() > 25:                     # snap the row onto its own digits
            idx = np.arange(max(0, lo), max(0, lo) + len(seg))
            shift = float((idx * seg).sum() / seg.sum()) + 4 - centre
            centre += max(-0.22 * pitch, min(0.22 * pitch, shift))
        ya = int(round(t.header_bottom + centre - 0.5 * pitch)) + 3
        yb = int(round(t.header_bottom + centre + 0.5 * pitch)) - 3
        ya, yb = max(ya, t.header_bottom + 2), min(yb, t.total_top - 2)
        rows.append(RawRow(index=r, y0=ya, y1=yb))
        for kind, (xa, xb) in (("grade", t.col_grade), ("coef", t.col_coef),
                               ("total", t.col_total), ("subject", t.col_subject)):
            jobs.append((kind, r, (xa + inset, ya, xb - inset, yb)))

    def run(job):
        kind, r, (xa, ya, xb, yb) = job
        crop = gray[ya:yb, xa:xb]
        if kind == "subject":
            return job, _read_subject(crop)
        return job, _cell_candidates(crop, kind)

    with ThreadPoolExecutor(max_workers=Config.OCR_WORKERS) as pool:
        for (kind, r, _), out in pool.map(run, jobs):
            if kind == "subject":
                rows[r].subject_text, rows[r].subject_conf = out
            else:
                setattr(rows[r], kind, out)

    from parsers.table_parser import is_blank, is_not_taken, row_options

    # An optional subject that was not sat prints only its coefficient (grade / total are
    # '--'). Nothing cross-checks that digit, so read it at every threshold and let them vote.
    for r in rows:
        if is_not_taken(r):
            xa, xb = t.col_coef
            for frac in (0.85, 1.6):
                fresh = _cell_candidates(gray[r.y0:r.y1, xa + inset:xb - inset], "coef", frac)
                r.coef.candidates += fresh.candidates

    # Escalation: rows whose numbers do not reconcile are re-read with looser ink thresholds
    # (faint photos lose thin strokes under the strict threshold) and the candidates merged.
    for frac in (0.85, 1.6):
        bad = [r for r in rows if not (is_blank(r) or is_not_taken(r))
               and not _row_trusted(r, row_options)]
        if not bad:
            break
        for r in bad:
            for kind, (xa, xb) in (("grade", t.col_grade), ("coef", t.col_coef), ("total", t.col_total)):
                fresh = _cell_candidates(gray[r.y0:r.y1, xa + inset:xb - inset], kind, frac)
                cell = getattr(r, kind)
                cell.candidates += fresh.candidates
                if fresh.candidates:
                    cell.empty = cell.dash = False

    # totals row (below the last rule): overall total + sum of coefficients
    ya, yb = t.total_top + 4, t.y_bottom - 3
    total_cells = {}
    for name, (xa, xb) in (("total", t.col_total), ("coef", t.col_coef)):
        kind = "total" if name == "total" else "grade"
        crop = gray[ya:yb, xa + inset:xb - inset]
        cell = _cell_candidates(crop, kind)
        for frac in (0.85, 1.6):
            if cell.candidates:
                break
            cell = _cell_candidates(crop, kind, frac)
        total_cells[name] = cell
    return RawTable(rows=rows, total_row=total_cells, pitch=pitch, n_rows=n)
