"""Integration tests: real OCR on synthetic fixtures, plus the Flask app."""
import io
import json
import time

import pytest

from tests.conftest import needs_tesseract

pytestmark = needs_tesseract


def test_accuracy_target_on_fixtures(fixtures_dir):
    from scripts.evaluate import evaluate
    report = evaluate(fixtures_dir, verbose=False)
    s = report["summary"]
    assert s["overall_accuracy"] >= 0.90, s
    assert s["cell_accuracy"] >= 0.90, s


def test_scan_produces_validated_table(fixtures_dir):
    from services.pipeline import process_file
    res = process_file(fixtures_dir / "synthetic_math.jpg")[0]
    assert res.error is None and res.source == "scan"
    assert all(c.passed for c in res.checks if c.passed is not None), [c for c in res.checks if not c.passed]
    assert len(res.rows) == 11 and res.fields["overall_average"].status == "ok"


def test_text_layer_pdf_needs_no_ocr(fixtures_dir):
    from services.pipeline import process_file
    res = process_file(fixtures_dir / "synthetic_textlayer.pdf")[0]
    assert res.source == "text-layer"
    exp = json.loads((fixtures_dir / "synthetic_textlayer.expected.json").read_text(encoding="utf-8"))
    assert res.fields["registration_number"].value == exp["registration_number"]
    assert res.fields["overall_total"].value == exp["overall_total"]
    assert len(res.rows) == len(exp["grades"])


def test_missing_table_reports_error_instead_of_crashing(tmp_path):
    import numpy as np
    from PIL import Image
    from services.pipeline import process_file
    Image.fromarray(np.full((900, 700, 3), 255, np.uint8)).save(tmp_path / "blank.png")
    res = process_file(tmp_path / "blank.png")[0]
    assert res.error and "table" in res.error.lower()
    assert set(res.missing_fields) == set(res.fields)      # every field flagged as missing


# ---------------------------------------------------------------- web app
@pytest.fixture()
def client(tmp_path, monkeypatch):
    from config import Config
    monkeypatch.setattr(Config, "UPLOAD_DIR", tmp_path)
    import app as app_module
    return app_module.create_app().test_client()


def test_upload_flow_renders_html_table_and_json(client, fixtures_dir):
    data = {"file": ((fixtures_dir / "synthetic_experimental.png").open("rb"), "t.png")}
    r = client.post("/upload", data=data, content_type="multipart/form-data")
    assert r.status_code == 302
    job = r.headers["Location"].rsplit("/", 1)[1]
    for _ in range(90):
        st = client.get(f"/job/{job}/status").get_json()
        if st["state"] in ("done", "error"):
            break
        time.sleep(1)
    assert st["state"] == "done", st
    html = client.get(f"/result/{job}").get_data(as_text=True)
    assert '<table class="grades"' in html and html.count('<tr class="g-') >= 10
    js = client.get(f"/result/{job}.json?download=1")
    assert "attachment" in js.headers["Content-Disposition"]
    assert json.loads(js.get_data(as_text=True))["pages"][0]["fields"]["registration_number"]["value"]


def test_rejects_bad_extension_and_bad_ids(client):
    r = client.post("/upload", data={"file": (io.BytesIO(b"x"), "a.txt")}, content_type="multipart/form-data")
    assert r.status_code == 400
    assert client.get("/result/../../etc/passwd").status_code == 404
    assert client.get("/files/zzzz/x.png").status_code == 404
