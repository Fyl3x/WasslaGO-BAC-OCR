# BAC Transcript OCR (كشف نقاط البكالوريا)

A local web app that reads an **Algerian BAC score transcript** (scan, phone photo or PDF) and returns
structured data: the header fields, the codes, and the subject/grade table as a real HTML table,
with per-field status and arithmetic validation. Everything runs on your machine (Flask + Tesseract +
OpenCV); no cloud service, no database.

> Scope is deliberately limited to this one document type. Other documents are out of scope.

```
upload PDF/JPG/PNG ─► orient + deskew + find the table ─► OCR each zone/cell ─► reconcile with the
arithmetic of the table ─► fields + HTML table + validation + JSON
```

* How the document is structured and why the pipeline works this way: [docs/ANALYSIS.md](docs/ANALYSIS.md)

## Extracted fields

| Key | Arabic label | Notes |
|---|---|---|
| `registration_number` | رقم التسجيل | 8 digits |
| `session_year` | بكالوريا دورة | cross-checked with the issue date |
| `branch` | شعبة | canonicalised (technical branches keep the specialty) |
| `full_name` | السيد (ة) | Arabic free text |
| `birth_date` / `birth_place` | المزداد (ة) في / بـ | ISO date; `commune - wilaya` |
| `institution_name` / `institution_code` | المؤسسة | code has 4-8 digits |
| `grades[]` | المادة / النقطة / المعامل / المجموع | subject, grade, coefficient, row total, status |
| `overall_total`, `coefficient_total` | المجموع العام | |
| `overall_average` | المعدل العام | |
| `issue_place` / `issue_date` | حرر بالجزائر في | ISO date (handles `17 جويلية 2025` and `2019/07/16`) |
| `secret_code` | الرقم السري | case-sensitive |
| `serial_code` | the long code containing `USEPWD_BC` | `<prefix>/<id>USEPWD_BC---<digits>` |

Every field carries `status` (`ok`, `derived`, `low_confidence`, `missing`), `confidence` (0-1), the raw OCR
text and an optional note. Missing fields are listed at the top of the result page.

## Project layout

```
app.py                     Flask app (upload, background job, result, JSON download, /health)
config.py                  all settings (env vars / .env)
ocr/
  loader.py                PDF (text layer or 300 dpi raster) / image loading, EXIF orientation
  preprocessing.py         ink map (max channel), background normalisation, deskew, crop helpers
  layout.py                document crop, table detection, orientation, column geometry
  zone_reader.py           header lines, registration no., average, issue date, secret + serial codes
  table_reader.py          12-row grid, per-cell cleaning and multi-pass numeric OCR
  ocr_service.py           Tesseract wrapper (digits / Arabic / Latin-code recipes)
parsers/
  bac_parser.py            field parsers, voting, cross-checks, FIELD_DEFS
  table_parser.py          row solver (grade x coef = total), reconciliation with totals
  subjects.py, lexicon.py  fuzzy vocab matching / spelling repair (data/*.json)
  normalize.py             Arabic normalisation, number repair
  text_layer.py            extraction for PDFs that already have a text layer
services/
  pipeline.py              file -> [TranscriptResult]      table_service.py  extraction with escalation
  models.py                dataclasses + JSON shape
data/subjects.json         subjects, branches (+ usual row order)     data/lexicon.json  wilayas, words
templates/, static/        HTML (index / processing / result) and CSS/JS
scripts/make_fixtures.py   generate synthetic transcripts + ground truth
scripts/evaluate.py        accuracy report against ground truth
tests/                     unit tests + integration tests (+ fixtures)
docs/ANALYSIS.md           document analysis and extraction strategy
```

## 1. System dependencies

You need **Python 3.10+** and **Tesseract 5** with the **Arabic** (and English/French) language data.
Nothing else outside pip (PDF handling uses PyMuPDF, no Poppler needed).

| OS | Command |
|---|---|
| Debian / Ubuntu | `sudo apt-get update && sudo apt-get install -y tesseract-ocr tesseract-ocr-ara tesseract-ocr-fra tesseract-ocr-eng` |
| macOS (Homebrew) | `brew install tesseract tesseract-lang` |
| Windows | install from <https://github.com/UB-Mannheim/tesseract/wiki>, tick **Arabic** (and French) in the installer, then set `TESSERACT_CMD="C:\Program Files\Tesseract-OCR\tesseract.exe"` in `.env` |

Check: `tesseract --list-langs` must list `ara` and `eng`. The home page and `GET /health` also show this.

*Optional, recommended for better Arabic:* download `ara.traineddata` from
[tessdata_best](https://github.com/tesseract-ocr/tessdata_best), put it in a folder together with
`eng.traineddata`, and set `TESSDATA_DIR` to that folder.

## 2. Install (virtual environment)

```bash
git clone <this repo> && cd WasslaGO-BAC-OCR
python3 -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install --upgrade pip
pip install -r requirements.txt
cp .env.example .env                 # optional; every value has a default
```

`requirements.txt`: Flask, PyMuPDF, opencv-python-headless, numpy, Pillow, pytesseract, rapidfuzz,
python-dotenv, pytest. (`requirements-dev.txt` adds `arabic-reshaper` and `python-bidi`, needed only to
regenerate the synthetic fixtures.)

## 3. Run

```bash
python app.py                        # http://127.0.0.1:5000
# or
flask --app app run --port 5000
```

Open the page, choose a transcript (PDF / JPG / JPEG / PNG), press **Extract**. A scanned page takes
roughly 10-25 seconds (the page updates by itself). A PDF may contain several transcripts (one per page,
first `MAX_PAGES`=10); each one gets its own section.

Expected console output:

```
 * Serving Flask app 'app'
 * Running on http://127.0.0.1:5000
```

Expected result page: summary tiles (fields found, mean confidence, missing, checks passed), a fields table
with status badges, the **grades table as an HTML `<table>`** (subject | grade | coefficient | total | status,
with totals and average in the footer), a validation list

```
✔ row_total = grade x coefficient     all rows consistent
✔ sum(row totals) = overall total     rows sum to 407.42, overall total 407.42 (confirmed)
✔ sum(coefficients) = coefficient total   coefficients sum to 30, printed 30 (confirmed)
✔ overall average = total / coefficients  407.42 / 30 = 13.581, printed 13.58
```

and a collapsible debug section (raw OCR of each zone, the normalised page with the detected table, crops).
**Download JSON** returns:

```json
{
  "job_id": "3f9c1a7b2d4e", "filename": "transcript.pdf",
  "pages": [{
    "page": 1, "source": "scan", "completeness": 1.0, "overall_confidence": 0.94,
    "fields": {
      "registration_number": {"value": "37631706", "status": "ok", "confidence": 0.93, "raw": "..."},
      "session_year": {"value": 2023, "status": "ok", "confidence": 0.97},
      "branch": {"value": "علوم تجريبية", "status": "ok"},
      "birth_date": {"value": "2005-03-14", "status": "ok"},
      "overall_total": {"value": "407.42", "status": "ok"},
      "overall_average": {"value": "13.58", "status": "ok"},
      "serial_code": {"value": "1391500080636083/4FJPrzrXUSEPWD_BC---1374068124", "status": "ok"}
    },
    "grades": [
      {"index": 2, "subject": "الرياضيات", "grade": 19.5, "coefficient": 5, "total": 97.5, "status": "ok"},
      {"index": 8, "subject": "اللغة الأمازيغية", "grade": null, "coefficient": 2, "total": null, "status": "not_taken"}
    ],
    "checks": [{"name": "row_total = grade x coefficient", "passed": true, "detail": "all rows consistent"}],
    "missing_fields": [], "notes": []
  }]
}
```

(abridged; the real file lists all fields.) Uploads and results are stored under `uploads/<job id>/`.

## 4. Configuration

All optional, via environment variables or `.env` (see `.env.example`):

| Variable | Default | Meaning |
|---|---|---|
| `HOST`, `PORT` | `127.0.0.1`, `5000` | bind address |
| `MAX_UPLOAD_MB` | `25` | upload limit |
| `MAX_PAGES` | `10` | PDF pages processed |
| `TESSERACT_CMD` | (PATH) | tesseract binary |
| `TESSDATA_DIR` | (default) | extra traineddata folder |
| `OCR_WORKERS` | `4` | parallel Tesseract processes |
| `PDF_RENDER_DPI` | `300` | rasterisation resolution |
| `SAVE_DEBUG_IMAGES` | `true` | write crops next to the upload |
| `SUBJECT_MATCH_THRESHOLD` | `62` | fuzzy score to accept a subject name |
| `SUBJECTS_FILE` | `data/subjects.json` | subject / branch vocabulary |

Rules are data, not code: add subjects, aliases and branch row orders in `data/subjects.json`, and
wilayas / common institution words in `data/lexicon.json`.

## Docker / hosting

```bash
cp .env.example .env     # set SECRET_KEY, BASIC_AUTH_USER / BASIC_AUTH_PASSWORD, DOMAIN
docker compose up -d --build
```

The image bundles Tesseract (Arabic) and runs gunicorn behind a Caddy proxy with automatic HTTPS.
Step-by-step Hostinger VPS guide: [docs/DEPLOY.md](docs/DEPLOY.md).

## 5. Accuracy and validation

**How correctness is checked (built into every result):** each row must satisfy `grade x coefficient = total`;
the rows must add up to the printed overall total and coefficient total; the average must equal
`total / coefficients`; the session year must agree with the issue date; the registration number has 8
digits; the average box must agree with the computed average. Failures are shown and lower a field's status.
Where the redundancy allows, values are **repaired** (a lost decimal point, a dropped digit, a missing grade)
and marked `derived`.

**Measured results**

* *Real sample (10 pages: scans, phone photos, one screenshot, one page rotated 90°, years 2017-2026,
  5 branches).* All 10 tables reconcile arithmetically (rows, totals, coefficients, average). I read
  the printed values by eye on 7 of those pages and scored the output: **93 % field accuracy (94 of
  101 fields)**. Clean scans reach 100 %; the errors are on the low-resolution photo/screenshot pages -
  Arabic names / institution names, one birth date, one case-sensitive code. 10 of the 150 fields were
  flagged `low_confidence`/`derived` for review, but 4 of the 7 wrong fields were still marked `ok`, so
  the flags do not catch every error. This is a small sample and **not** a guarantee; measure on your own
  documents (below).
* *Synthetic fixtures* (`tests/fixtures`, invented data, includes a rotated/blurred/JPEG one and a
  text-layer PDF): **≈ 98-99 % overall**, 100 % of table cells.

**Reproduce / measure on your own data**

```bash
python scripts/make_fixtures.py          # (re)creates tests/fixtures/*  (needs requirements-dev.txt)
python scripts/evaluate.py               # synthetic fixtures; exits 1 below --min (default 0.90)

# your own documents: put  name.pdf|png|jpg  next to  name.expected.json  (same schema as the fixtures;
# for a multi-page PDF the JSON is a list, one object per page)
python scripts/evaluate.py samples/private --json report.json
```

`samples/private/` is git-ignored: keep real people's transcripts out of the repository.

**Tests**

```bash
pytest -q                                # 25 unit tests + integration tests (skipped without Tesseract)
```

## 6. Known limitations

* **Names, places, institutions** are free Arabic text; accuracy depends on Tesseract's Arabic model
  and image quality. Low-resolution photos/screenshots lose letters. The default `ara` model is the
  "fast" one - `tessdata_best` helps noticeably.
* **Case-sensitive random codes** (secret code, serial) can still contain look-alike errors
  (`0/O`, `1/l`, `2/z`). They are voted over many OCR variants and flagged when the variants disagree; verify
  them if they matter.
* The **text-layer PDF** path was only validated on a synthetic PDF; real digital originals may lay out
  Arabic differently.
* Very tilted (> ~10°) photos with perspective distortion, transcripts cut off at the frame, or a
  different template/year with another layout can fail; the result then reports "table not found".
* Only the first `MAX_PAGES` pages of a PDF are processed; one transcript per page is assumed.
* The subject/branch vocabulary comes from the samples (`data/subjects.json`): unseen branches still
  extract, but subject names are only matched against the known list.
* Processing is CPU-bound (~10-25 s per page, one document at a time).

## 7. Next improvements

1. Fine-tune / swap in a stronger Arabic recogniser for names (e.g. `tessdata_best`, or a second engine
   voting with Tesseract) - the biggest remaining source of error.
2. Perspective (homography) correction for photos taken at an angle.
3. Human-in-the-loop correction UI: edit flagged fields, re-validate, export.
4. Ground-truth set from real consented documents to tune thresholds per branch/year.
5. Batch upload + CSV export, and a queue for concurrent users.
6. Coefficient tables per branch/year to validate coefficients against the official scheme.

## Troubleshooting

* *"Tesseract binary not found"* - install it and/or set `TESSERACT_CMD`.
* *"Missing Tesseract language pack(s): ara"* - install `tesseract-ocr-ara`.
* *"Could not find the grades table"* - the whole transcript, including the table borders, must be visible.
* Slow or timing out - lower `OCR_WORKERS` to your core count, or raise `OCR_TIMEOUT_S`.
