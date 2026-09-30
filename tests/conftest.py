import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

FIXTURES = ROOT / "tests" / "fixtures"


def _tesseract_ready() -> bool:
    if not shutil.which("tesseract"):
        return False
    try:
        from ocr.ocr_service import check_tesseract
        return bool(check_tesseract().get("ok"))
    except Exception:
        return False


needs_tesseract = pytest.mark.skipif(not _tesseract_ready(),
                                     reason="Tesseract with Arabic language data is not installed")


@pytest.fixture(scope="session")
def fixtures_dir() -> Path:
    if not any(FIXTURES.glob("*.expected.json")):
        pytest.skip("run `python scripts/make_fixtures.py` first")
    return FIXTURES
