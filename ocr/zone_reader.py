"""Read everything on the transcript that is *not* the grades table.

All zones are located relative to the table (see `layout.py`):

    above the table   : year, branch, intro sentence, name, birth line, institution, "passed" line
                        (+ registration number at the far left)
    just below        : boxed average, issue-date line
    lower part        : secret code (right), serial code (bottom, contains USEPWD_BC)

Each zone is OCR'd as a tight, cleaned crop. Numbers are re-read with a digits-only
engine because that is far more reliable than Arabic OCR for digits.
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field

import cv2
import numpy as np

from . import ocr_service as ocr
from .layout import PageLayout
from .preprocessing import binarize, prepare_for_ocr, remove_border_lines

HEADER_LINES = ["year", "branch", "intro", "name", "birth", "institution", "passed"]
# Fallback vertical centres (fraction of table width, relative to the table top) used when
# text-line segmentation fails. Measured on real transcripts.
FALLBACK_CENTRES = [-0.289, -0.255, -0.2075, -0.1675, -0.123, -0.082, -0.0345]


@dataclass
class LineRead:
    zone: str
    box: tuple[int, int, int, int]
    text: str = ""
    conf: float = -1.0
    numbers: list[str] = field(default_factory=list)   # digit groups, reading order
    digits_line: str = ""                               # whole-line digits-only OCR
    crop: np.ndarray | None = None


@dataclass
class RawZones:
    lines: dict[str, LineRead] = field(default_factory=dict)
    reg_candidates: list[str] = field(default_factory=list)
    avg_candidates: list[str] = field(default_factory=list)
    secret_candidates: list[str] = field(default_factory=list)
    serial_candidates: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    debug: dict[str, np.ndarray] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# band segmentation helpers
# ---------------------------------------------------------------------------
def text_bands(bw: np.ndarray, thr: float, min_h: int, mingap: int = 6) -> list[list[int]]:
    """Horizontal ink bands of a binary crop -> [[y0, y1], ...] (crop coordinates)."""
    prof = (bw > 0).sum(axis=1).astype(float)
    on = prof > thr
    out, s = [], None
    for i, v in enumerate(on):
        if v and s is None:
            s = i
        elif not v and s is not None:
            out.append([s, i])
            s = None
    if s is not None:
        out.append([s, len(on)])
    merged: list[list[int]] = []
    for b in out:
        if merged and b[0] - merged[-1][1] < mingap:
            merged[-1][1] = b[1]
        else:
            merged.append(b)
    return [b for b in merged if b[1] - b[0] >= min_h]


def _split_tall(bw: np.ndarray, bands: list[list[int]], typical: float) -> list[list[int]]:
    """Cut bands that are ~2 lines tall at their thinnest row (touching / tilted lines)."""
    prof = cv2.GaussianBlur((bw > 0).sum(axis=1).astype(np.float32).reshape(-1, 1), (1, 0), 3).ravel()
    out: list[list[int]] = []
    for y0, y1 in bands:
        h = y1 - y0
        if h > 1.7 * typical:
            k = int(round(h / (1.15 * typical)))
            k = max(2, min(k, 3))
            cuts = [y0]
            for j in range(1, k):
                centre = y0 + int(h * j / k)
                win = int(0.25 * h / k)
                seg = prof[centre - win:centre + win + 1]
                cuts.append(centre - win + int(np.argmin(seg)))
            cuts.append(y1)
            out.extend([[a, b] for a, b in zip(cuts[:-1], cuts[1:]) if b - a > 12])
        else:
            out.append([y0, y1])
    return out


def _bw_variants(layout: PageLayout):
    yield "otsu", layout.bw
    for strength in (0.5, 0.42, 0.36):
        yield f"strict{strength}", binarize(layout.gray, strength)


def _hrule_mask(bw: np.ndarray, min_frac: float = 0.35) -> np.ndarray:
    """Mask of long *horizontal* rules only (text strokes are never that wide)."""
    h, w = bw.shape
    if w < 40:
        return np.zeros_like(bw)
    k = cv2.getStructuringElement(cv2.MORPH_RECT, (max(30, int(w * min_frac)), 1))
    return cv2.dilate(cv2.morphologyEx(bw, cv2.MORPH_OPEN, k), np.ones((5, 1), np.uint8))


def _tight_crop(layout: PageLayout, x0: int, y0: int, x1: int, y1: int, bw=None, pad=6,
                hrules: bool = False):
    """Gray crop of the ink bounding box of a region. With `hrules`, horizontal ruled
    lines (e.g. the border of the average box) are painted out first."""
    h, w = layout.gray.shape
    x0, y0, x1, y1 = max(0, x0), max(0, y0), min(w, x1), min(h, y1)
    gray = layout.gray[y0:y1, x0:x1]
    b = (bw if bw is not None else layout.bw)[y0:y1, x0:x1].copy()
    if gray.size == 0:
        return gray
    if hrules:
        rules = _hrule_mask(b)
        b[rules > 0] = 0
        gray = gray.copy()
        gray[rules > 0] = int(np.percentile(gray, 90))
    n, labels, stats, _ = cv2.connectedComponentsWithStats(b, connectivity=8)
    keep = np.zeros_like(b)
    for i in range(1, n):
        if stats[i, cv2.CC_STAT_AREA] >= 14:
            keep[labels == i] = 255
    ys, xs = np.where(keep > 0)
    if len(ys) == 0:
        return gray
    return gray[max(0, ys.min() - pad):ys.max() + pad + 1, max(0, xs.min() - pad):xs.max() + pad + 1]


# ---------------------------------------------------------------------------
# header
# ---------------------------------------------------------------------------
LABEL_WORDS = {
    "year": ["بكالوريا", "دورة"], "branch": ["شعبة"], "intro": ["بناء", "محضر", "لجنة"],
    "name": ["السيد", "السيدة"], "birth": ["المزداد", "المزدادة"], "institution": ["المؤسسة"],
    "passed": ["نجح", "حصوله"],
}


def _header_band_variants(layout: PageLayout):
    """Yield (variant name, [(y0, y1), ...]) for the text bands above the table, ordered
    top -> bottom. Stricter binarisations are only tried when needed."""
    t = layout.table
    W = t.width
    ya = max(0, t.y_top - int(0.56 * W))
    yb = t.y_top - 6
    xa, xb = t.x0 + int(0.28 * W), t.x1
    for name, bw in _bw_variants(layout):
        region = bw[ya:yb, xa:xb]
        peak = float((region > 0).sum(axis=1).max()) if region.size else 0
        bands = text_bands(region, thr=max(3.0, 0.025 * peak), min_h=int(0.010 * W))
        if len(bands) >= 4:
            typical = float(np.median(sorted(b[1] - b[0] for b in bands[-7:])))
            bands = _split_tall(region, bands, typical)
        if len(bands) >= 5:
            yield name, [(ya + a, ya + b) for a, b in bands]


def _label_scores(text: str) -> dict[str, float]:
    from rapidfuzz import fuzz
    from parsers.normalize import normalize_ar
    toks = [normalize_ar(t) for t in text.split()][:5]
    out = {}
    for zone, words in LABEL_WORDS.items():
        best = 0.0
        for tok in toks:
            for w in words:
                best = max(best, fuzz.ratio(tok, normalize_ar(w)))
        out[zone] = best
    return out


def _classify_bands(layout: PageLayout, bands: list[tuple[int, int]]) -> dict[str, int]:
    """Assign header zones to band indices by reading each band's right-hand end, where
    the printed label sits. Returns {zone: band_index}."""
    t = layout.table
    W = t.width
    scores: list[dict[str, float]] = []
    for y0, y1 in bands:
        crop = _tight_crop(layout, t.x1 - int(0.26 * W), y0 - 6, t.x1 + 10, y1 + 6)
        if crop.size == 0:
            scores.append({})
            continue
        words = ocr.read_words(prepare_for_ocr(crop, 80), "ara", psm=7)
        scores.append(_label_scores(" ".join(w.text for w in words)))
    pairs = sorted(((sc, zone, i) for i, d in enumerate(scores) for zone, sc in d.items() if sc >= 66),
                   reverse=True)
    assigned: dict[str, int] = {}
    used: set[int] = set()
    for sc, zone, i in pairs:
        if zone not in assigned and i not in used:
            assigned[zone] = i
            used.add(i)
    # fill unrecognised zones from their neighbours in the canonical order
    known = {HEADER_LINES.index(z): i for z, i in assigned.items()}
    for pos, zone in enumerate(HEADER_LINES):
        if zone in assigned or not known:
            continue
        q = min(known, key=lambda k: abs(k - pos))
        j = known[q] + (pos - q)
        if 0 <= j < len(bands) and j not in used:
            assigned[zone] = j
            used.add(j)
    return assigned


def _header_bands(layout: PageLayout, notes: list[str]) -> dict[str, tuple[int, int]]:
    """{zone: (y0, y1)} for the seven header lines."""
    t = layout.table
    W = t.width
    best: tuple[int, str, dict[str, tuple[int, int]]] | None = None
    for name, bands in _header_band_variants(layout):
        window = bands[-10:]
        assigned = _classify_bands(layout, window)
        core = sum(1 for z in ("year", "branch", "name", "birth", "institution") if z in assigned)
        mapping = {z: window[i] for z, i in assigned.items()}
        if best is None or core > best[0]:
            best = (core, name, mapping)
        if core >= 5:
            break
    if best and best[0] >= 4:
        if best[1] != "otsu":
            notes.append(f"header lines needed stricter binarisation ({best[1]})")
        return best[2]
    notes.append("header text lines could not be identified: using default proportions")
    half = int(0.026 * W)
    return {z: (t.y_top + int(c * W) - half, t.y_top + int(c * W) + half)
            for z, c in zip(HEADER_LINES, FALLBACK_CENTRES)}


# (tesseract lang, target height, binarise, psm). Arabic-only first: the French model injects
# Latin noise into short Arabic words. Mixed ara+fra is kept as a fallback for lines that
# really contain French.
_LINE_ATTEMPTS = [("ara", 88, True, 7), ("ara", 72, True, 7), ("ara", 104, True, 7),
                  ("ara+fra", 88, True, 7), ("ara", 88, True, 13), ("ara", 120, False, 7)]
_MIN_WORD_CONF = 25


def _ocr_line_best(crop: np.ndarray):
    """OCR a text line with several lang / scale / threshold / PSM recipes and keep the most
    confident reading. Stops early as soon as a clean result is found."""
    best = None
    for lang, height, binar, psm in _LINE_ATTEMPTS:
        prepared = prepare_for_ocr(crop, target_height=height, binarize_crop=binar)
        words = [w for w in ocr.read_words(prepared, lang, psm=psm) if w.conf >= _MIN_WORD_CONF]
        confs = [w.conf for w in words]
        conf = float(np.mean(confs)) if confs else -1.0
        # words weigh in so an empty / one-token reading never beats a full line
        score = conf + 4.0 * min(len(words), 6) if words else -1.0
        if best is None or score > best[0]:
            best = (score, words, prepared, conf)
        if words and conf >= 86 and len(words) >= 2:
            break
    return best[1], best[2], best[3]


def _read_line(layout: PageLayout, zone: str, y0: int, y1: int, x0: int, x1: int,
               bw=None) -> LineRead:
    pad_y = 8
    crop = _tight_crop(layout, x0, y0 - pad_y, x1, y1 + pad_y, bw=bw)
    lr = LineRead(zone=zone, box=(x0, y0, x1, y1), crop=crop)
    if crop.size == 0:
        return lr
    words, prepared, conf = _ocr_line_best(crop)
    lr.text = " ".join(w.text for w in words)
    lr.conf = conf
    # targeted digit re-reads
    for w in words:
        if sum(ch.isdigit() for ch in w.text) >= 2 or re.search(r"[٠-٩۰-۹]{2,}", w.text):
            bx0, by0, bx1, by1 = w.box
            sub = prepared[max(0, by0 - 8):by1 + 8, max(0, bx0 - 8):bx1 + 8]
            if sub.size:
                sub = cv2.copyMakeBorder(sub, 12, 12, 16, 16, cv2.BORDER_CONSTANT, value=255)
                txt = ocr.read_digits(sub, psm=7, whitelist="0123456789-/.").text
                lr.numbers.append(txt.strip())
    lr.digits_line = ocr.read_digits(prepared, psm=7, whitelist="0123456789-/. ").text
    if zone in ("year", "birth", "institution", "issue") and not lr.numbers:
        # the Arabic model did not emit digit words: ask the digits-only engine for them
        for w in ocr.read_words(prepared, "eng", psm=7, extra="-c tessedit_char_whitelist=0123456789-/."):
            if sum(ch.isdigit() for ch in w.text) >= 2:
                lr.numbers.append(w.text)
    return lr


def read_header(layout: PageLayout, zones: RawZones) -> None:
    t = layout.table
    W = t.width
    bands = _header_bands(layout, zones.notes)
    for name in ("year", "branch", "name", "birth", "institution"):
        if name not in bands:
            zones.notes.append(f"header line '{name}' not located")
            continue
        y0, y1 = bands[name]
        # year/branch are short right-aligned lines: skip the registration label on the left
        x0 = t.x0 + int(0.30 * W) if name in ("year", "branch") else t.x0 - 10
        zones.lines[name] = _read_line(layout, name, y0, y1, x0, t.x1 + 12)
    zones.debug["header_bands"] = np.array([bands[z] for z in HEADER_LINES if z in bands])


def read_registration(layout: PageLayout, zones: RawZones, header_bands) -> None:
    """8-digit registration number: left of the year/branch lines, under 'رقم التسجيل'."""
    t = layout.table
    W = t.width
    ya = t.y_top - int(0.60 * W)
    yb = t.y_top - int(0.20 * W)
    xa, xb = t.x0 - 10, t.x0 + int(0.34 * W)
    for name, bw in _bw_variants(layout):
        region = bw[ya:yb, xa:xb]
        clean = region.copy()
        clean[_hrule_mask(region, 0.5) > 0] = 0
        bands = text_bands(clean, thr=3, min_h=int(0.012 * W))
        cands: list[str] = []
        for b0, b1 in bands:
            crop = _tight_crop(layout, xa, ya + b0 - 6, xb, ya + b1 + 6, bw=bw)
            if crop.size == 0:
                continue
            aspect = crop.shape[1] / max(1, crop.shape[0])
            if aspect < 2.5:      # a number is a wide, short blob
                continue
            for height in (64, 96):
                for psm in (7, 13):
                    txt = re.sub(r"\D", "", ocr.read_digits(prepare_for_ocr(crop, height), psm=psm,
                                                            whitelist="0123456789").text)
                    if 6 <= len(txt) <= 10:
                        cands.append(txt)
        if cands:
            zones.reg_candidates = cands
            return


# ---------------------------------------------------------------------------
# below the table
# ---------------------------------------------------------------------------
def _below_left_bands(layout: PageLayout, bw):
    t = layout.table
    W = t.width
    ya = t.y_bottom + 6
    yb = min(layout.gray.shape[0], t.y_bottom + int(0.30 * W))
    xa, xb = t.x0, t.x0 + int(0.55 * W)
    region = bw[ya:yb, xa:xb].copy()
    region[_hrule_mask(region, 0.5) > 0] = 0
    bands = text_bands(region, thr=4, min_h=int(0.008 * W))
    return [(ya + a, ya + b) for a, b in bands]


def read_average_and_issue(layout: PageLayout, zones: RawZones) -> None:
    """Average box (first band) + issue-date line (second band). A binarisation variant is
    accepted once the issue line really contains a year."""
    t = layout.table
    W = t.width
    fallback = None
    for vname, bw in _bw_variants(layout):
        bands = _below_left_bands(layout, bw)
        if len(bands) < 2:
            continue
        (ay0, ay1), (iy0, iy1) = bands[0], bands[1]
        issue = _read_line(layout, "issue", iy0, iy1, t.x0 - 10, t.x0 + int(0.62 * W), bw=bw)
        has_year = re.search(r"(?<!\d)(?:19|20)\d{2}(?!\d)", issue.text + " " + issue.digits_line
                             + " ".join(issue.numbers))
        result = (vname, (ay0, ay1), issue, bw)
        if has_year:
            fallback = result
            break
        fallback = fallback or result
    if fallback is None:
        zones.notes.append("could not locate the average box / issue date line")
        return
    vname, (ay0, ay1), issue, bw = fallback
    if vname != "otsu":
        zones.notes.append(f"lines under the table needed stricter binarisation ({vname})")
    avg_crop = _tight_crop(layout, t.x0 + 14, ay0 - 4, t.x0 + int(0.20 * W), ay1 + 4, bw=bw, hrules=True)
    cands = []
    if avg_crop.size:
        for height in (64, 96):
            for psm in (7, 13):
                cands.append(ocr.read_digits(prepare_for_ocr(avg_crop, height), psm=psm,
                                             whitelist="0123456789.").text.strip())
    zones.avg_candidates = [c for c in cands if c]
    zones.lines["issue"] = issue


# ---------------------------------------------------------------------------
# secret code and serial code
# ---------------------------------------------------------------------------
def _latin_variants(crop: np.ndarray, whitelist: str | None = None) -> list[str]:
    """OCR one crop of case-sensitive alphanumerics several ways (scale x binarisation x
    page-segmentation mode). The caller votes across the returned strings."""
    outs: list[str] = []
    for height in (72, 100):
        for binar in (True, False):
            prep = prepare_for_ocr(crop, height, binarize_crop=binar)
            for psm in (7, 13):
                res = ocr.read_latin(prep, psm=psm, whitelist=whitelist or ocr.CODE_WHITELIST)
                txt = res.text.replace(" ", "").replace("\n", "")
                if txt:
                    outs.append(txt)
    return outs


def _word_clusters(strip_bw: np.ndarray, gap: int) -> list[tuple[int, int]]:
    """Horizontal ink clusters separated by gaps wider than `gap` -> [(x0, x1)]."""
    cols = strip_bw.any(axis=0)
    xs = np.where(cols)[0]
    if len(xs) == 0:
        return []
    clusters, start, prev = [], xs[0], xs[0]
    for x in xs[1:]:
        if x - prev > gap:
            clusters.append((int(start), int(prev)))
            start = x
        prev = x
    clusters.append((int(start), int(prev)))
    return clusters


def _code_bands(zone_bw: np.ndarray, W: int):
    """Text-height bands wide enough to hold a code: [(y0, y1, clusters, width)]."""
    cand = []
    for b0, b1 in text_bands(zone_bw, thr=6, min_h=int(0.007 * W)):
        bh = b1 - b0
        if bh > 0.05 * W or bh < 0.006 * W:          # QR code / page frame / dust
            continue
        clusters = _word_clusters(zone_bw[b0:b1], gap=int(0.010 * W))
        if clusters and clusters[-1][1] - clusters[0][0] > 0.06 * W:
            cand.append((b0, b1, clusters, clusters[-1][1] - clusters[0][0]))
    return cand


def read_codes(layout: PageLayout, zones: RawZones) -> None:
    """Serial = the band with the most digits; secret code = the band right above it.
    Binarisation variants are tried until a band that really contains a serial is found
    (watermarked scans need a stricter threshold)."""
    t = layout.table
    W = t.width
    h, w = layout.gray.shape
    ya = t.y_bottom + int(0.20 * W)
    yb = min(h, t.y_bottom + int(0.85 * W))
    xa, xb = t.x0 + int(0.44 * W), min(w, t.x1 + int(0.004 * W))  # right of the seal, inside the frame
    zone_gray = layout.gray[ya:yb, xa:xb]
    if zone_gray.size == 0:
        return

    def crop_of(item, pad=8, xpad=24):
        b0, b1, clusters, _ = item
        return zone_gray[max(0, b0 - pad):b1 + pad, max(0, clusters[0][0] - xpad):clusters[-1][1] + xpad + 1]

    def digit_count(item) -> int:
        txt = ocr.read_latin(prepare_for_ocr(crop_of(item), 64), psm=7).text
        return sum(ch.isdigit() for ch in txt) + (30 if "PWD" in txt.upper() else 0)

    chosen = None
    for vname, bwv in _bw_variants(layout):
        cand = _code_bands(bwv[ya:yb, xa:xb], W)
        if len(cand) < 2:
            continue
        scores = [digit_count(c) for c in cand[:6]]
        best = max(range(len(scores)), key=lambda i: (scores[i], cand[i][3]))
        if scores[best] >= 12:
            chosen = (cand, best)
            if vname != "otsu":
                zones.notes.append(f"code lines needed stricter binarisation ({vname})")
            break
    if chosen is None:
        zones.notes.append("serial / secret code lines not found")
        return
    cand, serial_i = chosen
    zones.serial_candidates = _latin_variants(crop_of(cand[serial_i]))
    if serial_i > 0:
        b0, b1, clusters, _ = cand[serial_i - 1]
        no_sym = ocr.CODE_WHITELIST.replace("_", "").replace("/", "").replace("-", "")
        for x0c, x1c in clusters:                     # left-most plausible cluster
            if 0.05 * W <= x1c - x0c <= 0.17 * W:
                crop = zone_gray[max(0, b0 - 8):b1 + 8, max(0, x0c - 8):x1c + 9]
                zones.secret_candidates = _latin_variants(crop, no_sym)
                break


def read_zones(layout: PageLayout) -> RawZones:
    zones = RawZones()
    read_header(layout, zones)
    read_registration(layout, zones, zones.debug.get("header_bands"))
    read_average_and_issue(layout, zones)
    read_codes(layout, zones)
    return zones
