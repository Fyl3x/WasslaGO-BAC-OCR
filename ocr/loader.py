"""Load an uploaded file (PDF / JPG / PNG) into pages the pipeline can process."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np
import pymupdf
from PIL import Image, ImageOps

from config import Config


@dataclass
class PageInput:
    index: int                      # 1-based page number
    image: np.ndarray | None        # BGR raster (None only for text-layer pages we never rasterise)
    text: str | None = None         # embedded text layer, if any
    words: list | None = None       # PyMuPDF word tuples (x0, y0, x1, y1, word, ...) for text pages
    source: str = "raster"          # "raster" | "text-layer"
    page_size: tuple[float, float] | None = None


def _render(page: "pymupdf.Page", dpi: int) -> np.ndarray:
    # Cap the pixel count so a huge page cannot exhaust memory.
    scale = dpi / 72.0
    rect = page.rect
    longest = max(rect.width, rect.height) * scale
    if longest > 5200:
        scale *= 5200 / longest
    pix = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), colorspace=pymupdf.csRGB, alpha=False)
    arr = np.frombuffer(pix.samples, np.uint8).reshape(pix.h, pix.w, 3)
    return cv2.cvtColor(arr, cv2.COLOR_RGB2BGR)


def load_pages(path: str | Path, max_pages: int | None = None) -> list[PageInput]:
    path = Path(path)
    max_pages = max_pages or Config.MAX_PAGES
    ext = path.suffix.lower().lstrip(".")
    if ext == "pdf":
        return _load_pdf(path, max_pages)
    if ext in {"jpg", "jpeg", "png"}:
        return [_load_image(path)]
    raise ValueError(f"Unsupported file type: .{ext}")


def _load_image(path: Path) -> PageInput:
    with Image.open(path) as im:
        im = ImageOps.exif_transpose(im)          # honour phone-camera orientation
        if im.mode in ("RGBA", "LA", "P"):
            bg = Image.new("RGB", im.size, (255, 255, 255))
            rgba = im.convert("RGBA")
            bg.paste(rgba, mask=rgba.split()[-1])
            im = bg
        arr = np.array(im.convert("RGB"))
    img = cv2.cvtColor(arr, cv2.COLOR_RGB2BGR)
    h, w = img.shape[:2]
    if max(h, w) < 1800:                          # low-resolution photo: upscale for OCR
        s = 2200 / max(h, w)
        img = cv2.resize(img, None, fx=s, fy=s, interpolation=cv2.INTER_CUBIC)
    elif max(h, w) > 5200:
        s = 5200 / max(h, w)
        img = cv2.resize(img, None, fx=s, fy=s, interpolation=cv2.INTER_AREA)
    return PageInput(index=1, image=img)


def _load_pdf(path: Path, max_pages: int) -> list[PageInput]:
    pages: list[PageInput] = []
    with pymupdf.open(path) as doc:
        if doc.needs_pass:
            raise ValueError("The PDF is password protected.")
        for i, page in enumerate(doc):
            if i >= max_pages:
                break
            text = page.get_text("text") or ""
            from parsers.bac_parser import looks_like_transcript_text
            if len(text.strip()) > 80 and looks_like_transcript_text(text):
                pages.append(PageInput(index=i + 1, image=None, text=text, words=page.get_text("words"),
                                       source="text-layer", page_size=(page.rect.width, page.rect.height)))
            else:
                pages.append(PageInput(index=i + 1, image=_render(page, Config.PDF_RENDER_DPI)))
    if not pages:
        raise ValueError("The PDF has no pages.")
    return pages
