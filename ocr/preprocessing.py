"""Image preprocessing helpers.

The transcript is printed on a coloured, watermarked background with a red stamp and a
blue signature. The trick that matters most is `ink_gray`: taking the *maximum* colour
channel makes black text dark while red / blue / yellow decorations become bright, so
they disappear before any thresholding.
"""
from __future__ import annotations

import cv2
import numpy as np


def ink_gray(img: np.ndarray) -> np.ndarray:
    """Grayscale where only dark (black-ish) ink stays dark."""
    if img.ndim == 2:
        return img
    return img.max(axis=2)


def normalize_background(gray: np.ndarray) -> np.ndarray:
    """Divide by an estimate of the paper/watermark background (removes shadows and
    the pale diagonal watermark that covers the whole page)."""
    k = max(15, (min(gray.shape[:2]) // 60) | 1)
    bg = cv2.dilate(gray, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k)))
    bg = cv2.medianBlur(bg, k | 1)
    norm = cv2.divide(gray, bg, scale=255)
    return norm


def binarize(gray: np.ndarray, strength: float | None = None) -> np.ndarray:
    """Ink = 255, paper = 0. A pixel is ink when it is darker than `strength` x the local
    background. By default the threshold comes from Otsu on the background-normalised
    image (clamped), which copes with both crisp scans and washed-out photos."""
    norm = normalize_background(gray)
    norm = cv2.GaussianBlur(norm, (3, 3), 0)
    if strength is None:
        otsu, _ = cv2.threshold(norm, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        thr = int(min(max(otsu, 255 * 0.5), 255 * 0.86))
    else:
        thr = int(255 * strength)
    _, bw = cv2.threshold(norm, thr, 255, cv2.THRESH_BINARY_INV)
    return bw


def crop_to_document(img: np.ndarray, margin: float = 0.01) -> np.ndarray:
    """Crop away everything outside the transcript's red/pink ornamental frame (phone
    screenshot chrome, black bars, desk). Falls back to the full image."""
    h, w = img.shape[:2]
    s = 800.0 / max(h, w)
    small = cv2.resize(img, None, fx=s, fy=s, interpolation=cv2.INTER_AREA)
    hsv = cv2.cvtColor(small, cv2.COLOR_BGR2HSV)
    hue, sat, val = hsv[..., 0], hsv[..., 1], hsv[..., 2]
    mask = (((hue < 12) | (hue > 160)) & (sat > 45) & (val > 90)).astype(np.uint8) * 255
    k = max(5, int(min(small.shape[:2]) * 0.04)) | 1
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_RECT, (k, k)))
    n, _, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    if n <= 1:
        return img
    i = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
    x, y, bw_, bh_, _ = stats[i]
    if bw_ * bh_ < 0.12 * small.shape[0] * small.shape[1] or bw_ < 0.2 * small.shape[1]:
        return img
    mx, my = int(margin * small.shape[1]) + 2, int(margin * small.shape[0]) + 2
    x0, y0 = max(0, x - mx), max(0, y - my)
    x1, y1 = min(small.shape[1], x + bw_ + mx), min(small.shape[0], y + bh_ + my)
    return img[int(y0 / s):int(y1 / s), int(x0 / s):int(x1 / s)]


def estimate_skew_degrees(bw: np.ndarray) -> float:
    """Dominant angle (degrees, folded into [-45, 45)) of the long straight lines
    (table borders, page frame). Positive = lines run down to the right."""
    h, w = bw.shape[:2]
    scale = 1000.0 / max(h, w)
    small = cv2.resize(bw, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    small = (small > 60).astype(np.uint8) * 255
    min_len = int(0.28 * min(small.shape[:2]))
    lines = cv2.HoughLinesP(small, 1, np.pi / 720, threshold=80, minLineLength=min_len, maxLineGap=8)
    if lines is None:
        return 0.0
    angles, weights = [], []
    for x1, y1, x2, y2 in lines.reshape(-1, 4):
        length = float(np.hypot(x2 - x1, y2 - y1))
        ang = np.degrees(np.arctan2(y2 - y1, x2 - x1))
        ang = (ang + 45.0) % 90.0 - 45.0  # fold: horizontal and vertical lines agree
        angles.append(ang)
        weights.append(length)
    angles, weights = np.array(angles), np.array(weights)
    order = np.argsort(angles)
    cum = np.cumsum(weights[order])
    return float(angles[order][np.searchsorted(cum, cum[-1] / 2.0)])


def rotate_bound(img: np.ndarray, angle_deg: float, border_value=None) -> np.ndarray:
    """Rotate around the centre, enlarging the canvas so nothing is cut off."""
    if abs(angle_deg) < 0.05:
        return img
    h, w = img.shape[:2]
    m = cv2.getRotationMatrix2D((w / 2, h / 2), angle_deg, 1.0)
    cos, sin = abs(m[0, 0]), abs(m[0, 1])
    nw, nh = int(h * sin + w * cos), int(h * cos + w * sin)
    m[0, 2] += nw / 2 - w / 2
    m[1, 2] += nh / 2 - h / 2
    if border_value is None:
        border_value = (255, 255, 255) if img.ndim == 3 else 255
    return cv2.warpAffine(img, m, (nw, nh), flags=cv2.INTER_CUBIC,
                          borderMode=cv2.BORDER_CONSTANT, borderValue=border_value)


def rotate90(img: np.ndarray, quarter_turns_cw: int) -> np.ndarray:
    k = quarter_turns_cw % 4
    if k == 0:
        return img
    return np.ascontiguousarray(np.rot90(img, k=-k))  # np.rot90 is counter-clockwise


def prepare_for_ocr(gray_crop: np.ndarray, target_height: int = 96, pad: int = 14,
                    binarize_crop: bool = True) -> np.ndarray:
    """Turn a cropped text line / cell (ink-gray) into a clean OCR input: upscale so the
    text is ~target_height tall, normalise contrast, threshold, add a white margin."""
    h, w = gray_crop.shape[:2]
    if h == 0 or w == 0:
        return np.full((target_height, target_height), 255, np.uint8)
    scale = target_height / float(h)
    scale = min(max(scale, 0.6), 4.0)
    g = cv2.resize(gray_crop, None, fx=scale, fy=scale,
                   interpolation=cv2.INTER_CUBIC if scale > 1 else cv2.INTER_AREA)
    g = cv2.normalize(g, None, 0, 255, cv2.NORM_MINMAX)
    if binarize_crop:
        _, g = cv2.threshold(cv2.GaussianBlur(g, (3, 3), 0), 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    return cv2.copyMakeBorder(g, pad, pad, pad, pad, cv2.BORDER_CONSTANT, value=255)


def remove_border_lines(bw: np.ndarray, min_frac: float = 0.6) -> np.ndarray:
    """Erase long straight rules (table borders) from a binary crop so they do not fuse
    with the text. Only lines spanning `min_frac` of the crop are removed."""
    h, w = bw.shape[:2]
    out = bw.copy()
    if w >= 20:
        hk = cv2.getStructuringElement(cv2.MORPH_RECT, (max(10, int(w * min_frac)), 1))
        out[cv2.dilate(cv2.morphologyEx(bw, cv2.MORPH_OPEN, hk), np.ones((3, 1), np.uint8)) > 0] = 0
    if h >= 20:
        vk = cv2.getStructuringElement(cv2.MORPH_RECT, (1, max(10, int(h * min_frac))))
        out[cv2.dilate(cv2.morphologyEx(bw, cv2.MORPH_OPEN, vk), np.ones((1, 3), np.uint8)) > 0] = 0
    return out
