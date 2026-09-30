"""Page geometry: find the grades table, fix orientation / skew, and describe where the
columns and rows are.

Everything else on the transcript (header fields, average box, issue date, secret code,
serial code) is located *relative to the table*, which is far more robust than absolute
page coordinates or OCR of the Arabic labels.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import cv2
import numpy as np

from .preprocessing import (binarize, crop_to_document, estimate_skew_degrees, ink_gray,
                            rotate90, rotate_bound)

# Column layout of the table, as fractions of the table width, right-to-left order is
# (subject, grade, coefficient, total). Used only when the vertical rules are not found.
DEFAULT_COL_FRACTIONS = [0.0, 0.173, 0.343, 0.485, 1.0]  # left -> right borders


@dataclass
class TableGeometry:
    x0: int
    x1: int
    hlines: list[int]            # y of every horizontal rule of the table, top -> bottom
    col_x: list[int]             # 5 x positions, left -> right: total|coef|grade|subject
    header_bottom: int
    total_top: int
    y_top: int
    y_bottom: int
    cols_from_rules: bool = True
    notes: list[str] = field(default_factory=list)

    @property
    def width(self) -> int:
        return self.x1 - self.x0

    # named column spans (x0, x1) -------------------------------------------------
    @property
    def col_total(self):
        return self.col_x[0], self.col_x[1]

    @property
    def col_coef(self):
        return self.col_x[1], self.col_x[2]

    @property
    def col_grade(self):
        return self.col_x[2], self.col_x[3]

    @property
    def col_subject(self):
        return self.col_x[3], self.col_x[4]


@dataclass
class PageLayout:
    image: np.ndarray            # upright, deskewed, rescaled colour image
    gray: np.ndarray             # ink_gray(image)
    bw: np.ndarray               # binary ink mask (255 = ink)
    table: TableGeometry
    scale: float
    rotation_applied: int        # quarter turns clockwise applied
    skew_applied: float
    notes: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Rule (line) detection
# ---------------------------------------------------------------------------
def _segments(mask: np.ndarray, horizontal: bool, min_len: int, max_thick: int):
    """Connected components of a line mask -> [(pos, start, end)]."""
    n, _, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    segs = []
    for i in range(1, n):
        x, y, w, h, _ = stats[i]
        if horizontal and w >= min_len and h <= max_thick:
            segs.append((y + h / 2.0, x, x + w))
        elif not horizontal and h >= min_len and w <= max_thick:
            segs.append((x + w / 2.0, y, y + h))
    return segs


def _line_masks(bw: np.ndarray, h_len: int, v_len: int):
    hk = cv2.getStructuringElement(cv2.MORPH_RECT, (h_len, 1))
    vk = cv2.getStructuringElement(cv2.MORPH_RECT, (1, v_len))
    hmask = cv2.morphologyEx(bw, cv2.MORPH_OPEN, hk)
    vmask = cv2.morphologyEx(bw, cv2.MORPH_OPEN, vk)
    return hmask, vmask


def _best_rule_group(hsegs, w: int, h: int, tol: float):
    best = None
    for pos0, s0, e0 in hsegs:
        group = [(p, s, e) for p, s, e in hsegs if abs(s - s0) < tol and abs(e - e0) < tol]
        group.sort()
        # de-duplicate segments that are the same rule broken in two pieces
        merged = []
        for p, s, e in group:
            if merged and p - merged[-1][0] < h * 0.006:
                continue
            merged.append((p, s, e))
        if len(merged) < 3:
            continue
        span = merged[-1][0] - merged[0][0]
        length = float(np.median([e - s for _, s, e in merged]))
        # the table is wide and comparatively short; the page frame is not
        if span > 0.6 * length or span < 0.10 * length:
            continue
        score = len(merged) * 10 + length / w
        if best is None or score > best[0]:
            best = (score, merged)
    return best


def detect_table(bw: np.ndarray) -> TableGeometry | None:
    """Find the grades table in an *upright* binary page.

    The table's horizontal rules (top, under header, above total, bottom) share exactly
    the same left/right extents and sit close together; the ornamental page frame gives
    only two such lines far apart. Grouping by shared extents separates them reliably.
    """
    h, w = bw.shape[:2]
    hmask, vmask = _line_masks(bw, max(30, w // 14), max(30, h // 40))
    hsegs = _segments(hmask, True, int(w * 0.45), max(6, h // 120))
    if len(hsegs) < 3:
        return None

    best = None
    for tol_frac in (0.03, 0.07):  # strict first; relaxed for faded / warped scans
        best = _best_rule_group(hsegs, w, h, w * tol_frac)
        if best is not None:
            break
    if best is None:
        return None

    lines = best[1]
    x0 = int(min(s for _, s, _ in lines))
    x1 = int(max(e for _, _, e in lines))
    ys = [int(round(p)) for p, _, _ in lines]
    if x1 - x0 < w * 0.45:
        return None

    notes: list[str] = []
    y_top, y_bottom = ys[0], ys[-1]
    if len(ys) >= 4:
        header_bottom, total_top = ys[1], ys[-2]
    else:  # a rule was missed: infer from typical proportions
        notes.append("table: fewer than 4 horizontal rules, row bands inferred")
        tw = x1 - x0
        header_bottom = ys[1] if len(ys) == 3 and (ys[1] - y_top) < 0.06 * tw else y_top + int(0.035 * tw)
        total_top = ys[1] if len(ys) == 3 and (y_bottom - ys[1]) < 0.06 * tw else y_bottom - int(0.035 * tw)

    # vertical rules inside the body band
    body_v = vmask[header_bottom:total_top, x0 - 8:x1 + 9]
    col_x, from_rules = _column_positions(body_v, x0, x1, notes)
    return TableGeometry(x0=x0, x1=x1, hlines=ys, col_x=col_x, header_bottom=header_bottom,
                         total_top=total_top, y_top=y_top, y_bottom=y_bottom,
                         cols_from_rules=from_rules, notes=notes)


def _column_positions(body_v: np.ndarray, x0: int, x1: int, notes: list[str]):
    """x positions (left->right) of the 5 vertical rules of the table."""
    fallback = [int(x0 + f * (x1 - x0)) for f in DEFAULT_COL_FRACTIONS]
    if body_v.size == 0:
        notes.append("table: column rules not found, using default proportions")
        return fallback, False
    profile = (body_v > 0).sum(axis=0).astype(float)
    if profile.max() <= 0:
        notes.append("table: column rules not found, using default proportions")
        return fallback, False
    cols = np.where(profile >= 0.55 * body_v.shape[0])[0]
    if len(cols) == 0:
        notes.append("table: column rules not found, using default proportions")
        return fallback, False
    # group adjacent pixel columns into rules
    groups, cur = [], [cols[0]]
    for c in cols[1:]:
        if c - cur[-1] <= 4:
            cur.append(c)
        else:
            groups.append(cur)
            cur = [c]
    groups.append(cur)
    xs = [int(np.mean(g)) + x0 - 8 for g in groups]
    # keep the outer borders as x0 / x1, dividers in between
    inner = [x for x in xs if x0 + 0.08 * (x1 - x0) < x < x1 - 0.08 * (x1 - x0)]
    if len(inner) == 3:
        return [x0] + sorted(inner) + [x1], True
    # try to repair: pick the inner rules closest to the expected proportions
    expected = fallback[1:4]
    if len(inner) > 3:
        chosen = [min(inner, key=lambda x, e=e: abs(x - e)) for e in expected]
        if len(set(chosen)) == 3:
            notes.append("table: extra vertical rules ignored")
            return [x0] + sorted(chosen) + [x1], True
    notes.append(f"table: found {len(inner)} inner column rules (expected 3), using default proportions")
    return fallback, False


# ---------------------------------------------------------------------------
# Orientation
# ---------------------------------------------------------------------------
def _resize_long_side(img: np.ndarray, target: int) -> tuple[np.ndarray, float]:
    h, w = img.shape[:2]
    s = target / float(max(h, w))
    if abs(s - 1) < 0.02:
        return img, 1.0
    return cv2.resize(img, None, fx=s, fy=s, interpolation=cv2.INTER_AREA if s < 1 else cv2.INTER_CUBIC), s


def _average_box_below(bw: np.ndarray, t: TableGeometry) -> bool:
    """Upright transcripts have a small boxed 'المعدل العام' just under the table's
    bottom-left corner. Used to tell 0° from 180°."""
    h, w = bw.shape[:2]
    tw = t.width
    y_lo, y_hi = t.y_bottom + 4, min(h, t.y_bottom + int(0.14 * tw))
    if y_hi <= y_lo:
        return False
    band = bw[y_lo:y_hi, max(0, t.x0 - 10):min(w, t.x1 + 10)]
    hk = cv2.getStructuringElement(cv2.MORPH_RECT, (max(20, int(tw * 0.12)), 1))
    lines = cv2.morphologyEx(band, cv2.MORPH_OPEN, hk)
    for _, s, e in _segments(lines, True, int(tw * 0.15), 12):
        length = e - s
        left_gap = s - 10
        if 0.15 * tw < length < 0.5 * tw and left_gap < 0.06 * tw:
            return True
    return False


def normalize_page(img: np.ndarray, working_width: int = 2000) -> PageLayout | None:
    """Return an upright, deskewed, rescaled page plus table geometry, or None if no
    transcript table could be found. Detection is retried at several working sizes because
    thin, faded rules sit right at the threshold of the line detector."""
    img = crop_to_document(img)
    for long_side in (1600, 1300, 2000, 1000):
        layout = _normalize_at(img, working_width, long_side)
        if layout is not None:
            return layout
    return None


def _normalize_at(img: np.ndarray, working_width: int, long_side: int) -> PageLayout | None:
    notes: list[str] = []
    small, _ = _resize_long_side(img, long_side)
    bw_small = binarize(ink_gray(small))
    skew = estimate_skew_degrees(bw_small)
    work = rotate_bound(small, skew)
    if abs(skew) > 0.2:
        notes.append(f"deskewed by {skew:.2f} degrees")

    found = None
    for quarter in (0, 1):
        cand = rotate90(work, quarter)
        bw = binarize(ink_gray(cand))
        t = detect_table(bw)
        if t is not None and t.width > 1.4 * (t.y_bottom - t.y_top):
            found = (quarter, cand, bw, t)
            break
    if found is None:
        return None
    quarter, cand, bw, t = found

    # 0 vs 180 (or 90 vs 270): pick the orientation where the average box is below-left
    flip_quarter = quarter
    if not _average_box_below(bw, t):
        cand180 = rotate90(cand, 2)
        bw180 = binarize(ink_gray(cand180))
        t180 = detect_table(bw180)
        if t180 is not None and _average_box_below(bw180, t180):
            flip_quarter = quarter + 2
            notes.append("page was upside down, rotated 180 degrees")
    total_quarter = flip_quarter % 4
    if quarter:
        notes.append("page was sideways, rotated 90 degrees")

    # Re-apply the whole chain on the full-resolution image, then rescale so that the
    # table is `working_width` wide (a stable scale for every downstream crop).
    full = rotate90(rotate_bound(img, skew), total_quarter)
    full, s0 = _resize_long_side(full, 2200)
    probe = detect_table(binarize(ink_gray(full)))
    if probe is None:
        return None
    s = working_width / float(probe.width)
    final = cv2.resize(full, None, fx=s, fy=s,
                       interpolation=cv2.INTER_CUBIC if s > 1 else cv2.INTER_AREA)
    gray = ink_gray(final)
    final_bw = binarize(gray)
    final_t = detect_table(final_bw)
    if final_t is None:
        return None
    return PageLayout(image=final, gray=gray, bw=final_bw, table=final_t, scale=s * s0,
                      rotation_applied=total_quarter, skew_applied=skew,
                      notes=notes + final_t.notes)
