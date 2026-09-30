# BAC transcript analysis & extraction strategy

Scope: **only** the Algerian BAC score transcript, *كشف نقاط البكالوريا* (ONEC / وزارة التربية الوطنية).

## 1. What the document looks like

The layout was derived from a real 10-page sample (scans, phone photos, one phone screenshot,
one page rotated 90°, different years 2017-2026 and branches). It is a fixed RTL form:

```
┌───────────────────────── ornamental red/green frame ─────────────────────────┐
│  الجمهورية الجزائرية الديمقراطية الشعبية        وزارة التربية الوطنية          │
│  رقم التسجيل  ← 8 digits (left)                 كشف النقاط  (red title)       │
│                                       بكالوريا دورة : YYYY                    │
│                                       شعبة : <branch>                         │
│              بناء على محضر لجنة المداولات ... يشهد أن :                       │
│                                       السيد (ة) : <full name>                 │
│                       المزداد (ة) في : <date> بـ <commune - wilaya>           │
│                       المؤسسة : <institution> / <institution code>           │
│                          قد نجح (ت) بعد حصوله (ها) على النقاط التالية :       │
│  ┌──────────┬──────────┬───────────┬──────────────────────────────┐          │
│  │ المجموع  │ المعامل  │ النقطة/20 │            المادة            │  4 cols  │
│  ├──────────┼──────────┼───────────┼──────────────────────────────┤          │
│  │ 097.50   │    5     │   19.50   │ الرياضيات                    │  12 body │
│  │  --      │    2     │    --     │ اللغة الأمازيغية (not taken) │  rows    │
│  │  -       │          │           │ -   (unused placeholder)     │          │
│  ├──────────┴──────────┼───────────┴──────────────────────────────┤          │
│  │ 407.42   │   30     │                       المجموع العام      │          │
│  └──────────┴──────────┴──────────────────────────────────────────┘          │
│  ┌───────┬────────────┐                                                       │
│  │ 13.58 │ المعدل العام /20                                                    │
│  └───────┴────────────┘                                                       │
│  حرر بالجزائر في : 15 جويلية 2023   (older: 2019/07/16)        [QR code]      │
│  seal + signature (red / blue)                 الرقم السري : ES45aYFu         │
│                    1391500080636083/4FJPrzrXUSEPWD_BC---1374068124            │
└───────────────────────────────────────────────────────────────────────────────┘
```

Facts that shaped the design:

* Text is Arabic with embedded Latin digits; the codes are case-sensitive random strings.
* The table is a **closed system**: `grade × coefficient = total` per row, `Σ totals = overall
  total`, `Σ coefficients = coefficient total`, `overall total / Σ coefficients = average`.
* The body always has 12 uniformly spaced row slots. Unused slots print `-`; an optional
  subject that was not sat (usually Tamazight) prints only its coefficient (`-- | 2 | --`).
* PE (التربية البدنية) is an average of several tests, so its grade is **not** a multiple
  of 0.25 (`17.67`, `19.42`, `19.13`).
* Old transcripts (≤ 2019) use `yyyy/mm/dd` and an alphanumeric serial prefix; newer ones use a
  16-digit prefix. Institution codes have 4-8 digits.
* Real inputs are messy: tilted photos, a sideways page, a phone screenshot with UI chrome,
  faint scans covered by a diagonal watermark ("الديوان الوطني للامتحانات والمسابقات") and
  a red seal / blue signature that overlaps the text.

## 2. Extraction strategy

Plain "OCR the page, then regex" reaches only ~60 % here (Tesseract's Arabic on a whole
watermarked page is poor). The pipeline instead exploits the fixed structure:

1. **Load** – PDF (PyMuPDF): text layer used directly when present, otherwise each page is
   rasterised at 300 dpi (one transcript per page). Images honour EXIF orientation.
2. **Normalise the page** (`ocr/layout.py`)
   * crop to the document via the red frame; deskew from the long rules;
   * find the **table** by grouping horizontal rules that share the same extents (the page frame
     cannot mimic that) - retried at several scales because faded rules sit at the detector's
     threshold;
   * fix rotation: table wider than tall → 0°/180°, otherwise 90°/270°; the boxed average under
     the table's bottom-left corner tells upright from upside-down.
3. **Everything is located relative to the table**, never by absolute page position or by
   reading Arabic labels.
4. **Ink map** (`ocr/preprocessing.py`) – `max(B,G,R)` makes black ink dark while the red seal, blue
   signature and yellow paper turn bright; background normalisation + Otsu suppress the watermark;
   stricter thresholds are tried automatically when a zone cannot be segmented.
5. **Grades table** (`ocr/table_reader.py`) – 12-row grid from autocorrelation of the ink profile,
   each row snapped to its own digits; every numeric cell is cleaned (rules, neighbours, dust
   removed; decimal points and broken glyph fragments kept) and read several ways with a digits-only
   engine; subject cells are read with the Arabic model.
6. **Reconciliation** (`parsers/table_parser.py`) – for each row candidate readings are combined so
   that `grade × coef = total`; a missing value is recomputed; the overall total / coefficient sum
   choose between alternatives; implausible totals (a passing student's total lies between ≈9.5x and
   20x the coefficient sum) are ignored. Rows that still do not reconcile trigger a second, looser
   OCR pass.
7. **Header lines** (`ocr/zone_reader.py`) – text bands above the table are classified by reading the
   printed label at their right end (`السيد`, `المزداد`, `المؤسسة`, `شعبة`, `بكالوريا`) with
   positional fallback; each line is OCR'd with several recipes (Arabic-only first) and digits are
   re-read by the digits engine.
8. **Codes** – secret code and serial are read with 4-16 OCR variants and voted character by
   character; glyph heights settle `x/X`, `s/S`, `o/O`; the serial is rebuilt from its
   `prefix / id + USEPWD_BC--- suffix` structure.
9. **Repair with closed vocabularies** (`data/subjects.json`, `data/lexicon.json`) – subjects are
   fuzzy-matched (also on a dot-insensitive "skeleton"), restricted to the branch's subject set when
   the branch is known; wilaya names and common institution words are snapped only when the OCR
   is a plausible dot-level corruption.
10. **Validate and report** – every field has `ok / derived / low_confidence / missing`, a confidence
    and a note; the arithmetic checks are shown next to the table.

## 3. Assumptions

* One transcript per page; the table is fully visible.
* Text-layer PDFs (digital originals) were **not available**; that path was validated only on a
  synthetic PDF. Real digital originals may order Arabic differently.
* Subject and branch vocabularies come from the samples; add rows to `data/subjects.json` for new ones.
* Names, places and institutions are free text, so their accuracy is bounded by Arabic OCR quality.
