"""Thin, configurable wrapper around Tesseract (via pytesseract).

Three recipes are used by the pipeline:
  * `read_digits`  – numbers only (grades, coefficients, dates, registration number)
  * `read_arabic`  – Arabic / mixed Arabic-French text line
  * `read_latin`   – case-sensitive alphanumeric codes (secret code, serial code)
"""
from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass

import numpy as np
import pytesseract
from pytesseract import Output

from config import Config

DIGITS_WHITELIST = "0123456789.-/"
CODE_WHITELIST = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_/-"


class OcrUnavailable(RuntimeError):
    pass


@dataclass
class OcrResult:
    text: str
    confidence: float  # 0-100, mean word confidence (-1 when unknown)


@dataclass
class Word:
    text: str
    x: int
    y: int
    w: int
    h: int
    conf: float

    @property
    def box(self) -> tuple[int, int, int, int]:
        return self.x, self.y, self.x + self.w, self.y + self.h


def configure() -> None:
    # We parallelise across cells ourselves; letting every tesseract process also spawn
    # OpenMP threads oversubscribes the CPU and can make single calls time out.
    os.environ.setdefault("OMP_THREAD_LIMIT", "1")
    if Config.TESSDATA_DIR:
        # Environment variable instead of --tessdata-dir: no quoting / backslash problems on Windows.
        os.environ["TESSDATA_PREFIX"] = str(Config.TESSDATA_DIR).strip().strip('"').rstrip("\\/")
    if Config.TESSERACT_CMD:
        pytesseract.pytesseract.tesseract_cmd = Config.TESSERACT_CMD


def check_tesseract() -> dict:
    """Return diagnostics about the local Tesseract install (used by /health and the UI)."""
    configure()
    cmd = pytesseract.pytesseract.tesseract_cmd
    if not (shutil.which(cmd) or os.path.exists(cmd)):
        return {"ok": False, "error": f"Tesseract binary not found ('{cmd}'). See README."}
    try:
        langs = set(pytesseract.get_languages())
        version = str(pytesseract.get_tesseract_version())
    except Exception as exc:  # pragma: no cover - environment specific
        return {"ok": False, "error": str(exc)}
    needed = {"ara", "eng"}
    missing = sorted(needed - langs)
    return {"ok": not missing, "version": version, "languages": sorted(langs),
            "error": f"Missing Tesseract language pack(s): {', '.join(missing)}" if missing else None}


def _tessdata_flag() -> str:
    return ""   # the folder is passed through TESSDATA_PREFIX (see configure())


def _run(img: np.ndarray, lang: str, psm: int, extra: str = "") -> OcrResult:
    configure()
    cfg = f"--oem 1 --psm {psm} -c preserve_interword_spaces=1 {extra} {_tessdata_flag()}".strip()
    try:
        data = pytesseract.image_to_data(img, lang=lang, config=cfg, output_type=Output.DICT,
                                         timeout=Config.OCR_TIMEOUT_S)
    except pytesseract.TesseractNotFoundError as exc:
        raise OcrUnavailable("Tesseract is not installed or not on PATH. See README.") from exc
    words, confs, last_line = [], [], None
    for txt, conf, blk, par, ln in zip(data["text"], data["conf"], data["block_num"],
                                       data["par_num"], data["line_num"]):
        if not str(txt).strip():
            continue
        key = (blk, par, ln)
        if last_line is not None and key != last_line:
            words.append("\n")
        last_line = key
        words.append(str(txt))
        try:
            c = float(conf)
        except ValueError:
            c = -1
        if c >= 0:
            confs.append(c)
    text = " ".join(words).replace(" \n ", "\n").strip()
    return OcrResult(text=text, confidence=float(np.mean(confs)) if confs else -1.0)


def read_words(img: np.ndarray, lang: str, psm: int = 7, extra: str = "") -> list[Word]:
    """Word boxes in *reading order* (Tesseract emits Arabic right-to-left)."""
    configure()
    cfg = f"--oem 1 --psm {psm} {extra} {_tessdata_flag()}".strip()
    try:
        data = pytesseract.image_to_data(img, lang=lang, config=cfg, output_type=Output.DICT,
                                         timeout=Config.OCR_TIMEOUT_S)
    except pytesseract.TesseractNotFoundError as exc:
        raise OcrUnavailable("Tesseract is not installed or not on PATH. See README.") from exc
    words = []
    for i, txt in enumerate(data["text"]):
        if not str(txt).strip():
            continue
        try:
            conf = float(data["conf"][i])
        except ValueError:
            conf = -1.0
        words.append(Word(str(txt).strip(), int(data["left"][i]), int(data["top"][i]),
                          int(data["width"][i]), int(data["height"][i]), conf))
    return words


def read_digits(img: np.ndarray, psm: int = 7, whitelist: str = DIGITS_WHITELIST) -> OcrResult:
    return _run(img, Config.OCR_LANG_LATIN, psm,
                f"-c tessedit_char_whitelist={whitelist}")


def read_arabic(img: np.ndarray, psm: int = 7, mixed: bool = False) -> OcrResult:
    """Arabic text. The Arabic-only model is the default because the French model injects
    Latin noise into short Arabic words; `mixed=True` adds French for genuinely bilingual text."""
    return _run(img, Config.OCR_LANG_MIXED if mixed else Config.OCR_LANG_ARABIC, psm)


def read_latin(img: np.ndarray, psm: int = 7, whitelist: str = CODE_WHITELIST) -> OcrResult:
    return _run(img, Config.OCR_LANG_LATIN, psm, f"-c tessedit_char_whitelist={whitelist}")


def read_page_text(img: np.ndarray, psm: int = 4) -> OcrResult:
    """Full-page OCR, used for the raw-text debug view and as a fallback."""
    return _run(img, Config.OCR_LANG_MIXED, psm)


_NON_ALNUM = re.compile(r"[^0-9A-Za-z]")


def strip_non_alnum(s: str) -> str:
    return _NON_ALNUM.sub("", s)
