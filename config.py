"""Central configuration. Every value can be overridden with an environment variable
(or a `.env`-style export) so nothing has to be edited to change behaviour."""
from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent


def _env_bool(name: str, default: bool) -> bool:
    return os.environ.get(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


class Config:
    # --- Flask -----------------------------------------------------------
    SECRET_KEY = os.environ.get("SECRET_KEY", "dev-only-not-secret")
    MAX_CONTENT_LENGTH = int(os.environ.get("MAX_UPLOAD_MB", "25")) * 1024 * 1024
    UPLOAD_DIR = Path(os.environ.get("UPLOAD_DIR", BASE_DIR / "uploads"))
    ALLOWED_EXTENSIONS = {"pdf", "jpg", "jpeg", "png"}
    HOST = os.environ.get("HOST", "127.0.0.1")
    PORT = int(os.environ.get("PORT", "5000"))
    DEBUG = _env_bool("FLASK_DEBUG", False)

    # --- Tesseract -------------------------------------------------------
    # Path to the tesseract binary (leave empty to use PATH).
    TESSERACT_CMD = os.environ.get("TESSERACT_CMD", "")
    # Optional folder with extra traineddata (e.g. tessdata_best for better Arabic).
    TESSDATA_DIR = os.environ.get("TESSDATA_DIR", "")
    OCR_LANG_ARABIC = os.environ.get("OCR_LANG_ARABIC", "ara")
    OCR_LANG_MIXED = os.environ.get("OCR_LANG_MIXED", "ara+fra")
    OCR_LANG_LATIN = os.environ.get("OCR_LANG_LATIN", "eng")
    OCR_TIMEOUT_S = int(os.environ.get("OCR_TIMEOUT_S", "60"))
    OCR_WORKERS = int(os.environ.get("OCR_WORKERS", "4"))

    # --- Input handling --------------------------------------------------
    PDF_RENDER_DPI = int(os.environ.get("PDF_RENDER_DPI", "300"))
    # A PDF may contain several transcripts (one per page). Only the first
    # MAX_PAGES pages are processed.
    MAX_PAGES = int(os.environ.get("MAX_PAGES", "10"))
    # Working width (pixels) the table is normalised to before cell OCR.
    TABLE_WORKING_WIDTH = int(os.environ.get("TABLE_WORKING_WIDTH", "2000"))
    # Save intermediate crops next to the upload (useful to debug OCR problems).
    SAVE_DEBUG_IMAGES = _env_bool("SAVE_DEBUG_IMAGES", True)

    # --- Rules / data ----------------------------------------------------
    SUBJECTS_FILE = Path(os.environ.get("SUBJECTS_FILE", BASE_DIR / "data" / "subjects.json"))
    # Minimum fuzzy-match score (0-100) to accept a subject name from OCR.
    SUBJECT_MATCH_THRESHOLD = int(os.environ.get("SUBJECT_MATCH_THRESHOLD", "62"))
