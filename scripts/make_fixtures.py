"""Generate synthetic BAC transcripts (images + a text-layer PDF) with known ground truth.

The layout mirrors the real ONEC transcript (ruled 4-column table, right-aligned Arabic
header lines, boxed average, seal, QR block, secret code, serial code) but every name,
number and code is invented. Output goes to tests/fixtures/ as

    <name>.png | .jpg | .pdf      the document
    <name>.expected.json          ground truth (same schema as TranscriptResult.simple_dict())

Usage:  python scripts/make_fixtures.py
Needs:  pip install -r requirements-dev.txt   (arabic-reshaper, python-bidi)
"""
from __future__ import annotations

import json
import random
import sys
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
OUT = ROOT / "tests" / "fixtures"

try:
    import arabic_reshaper
    from bidi.algorithm import get_display
except ImportError:  # pragma: no cover
    sys.exit("Install dev requirements first: pip install -r requirements-dev.txt")

FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans.ttf",
    "/Library/Fonts/Arial Unicode.ttf",
    "C:/Windows/Fonts/arial.ttf",
    "C:/Windows/Fonts/tahoma.ttf",
]
BOLD_CANDIDATES = [c.replace("DejaVuSans.ttf", "DejaVuSans-Bold.ttf") for c in FONT_CANDIDATES] + FONT_CANDIDATES

W, H = 2480, 3508  # A4 @ 300 dpi


def _font(size: int, bold=False) -> ImageFont.FreeTypeFont:
    for p in (BOLD_CANDIDATES if bold else FONT_CANDIDATES):
        if Path(p).exists():
            return ImageFont.truetype(p, size, layout_engine=ImageFont.Layout.BASIC)
    raise SystemExit("No Arabic-capable TTF font found (install fonts-dejavu-core).")


def ar(text: str) -> str:
    """Shape + reorder Arabic so Pillow (no raqm) draws it correctly."""
    return get_display(arabic_reshaper.reshape(text))


# ---------------------------------------------------------------------------
# data model of a synthetic transcript
# ---------------------------------------------------------------------------
SUBJECTS = {
    "arabic": "اللغة العربية وآدابها", "philosophy": "الفلسفة", "history_geo": "التاريخ والجغرافيا",
    "french": "الفرنسية - لغة أجنبية أولى", "english": "الإنجليزية - لغة أجنبية ثانية",
    "math": "الرياضيات", "amazigh": "اللغة الأمازيغية", "islamic": "العلوم الإسلامية",
    "pe": "التربية البدنية والرياضية", "physics": "العلوم الفيزيائية", "svt": "علوم الطبيعة والحياة",
}
BRANCHES = {
    "experimental_sciences": ("علوم تجريبية", ["svt", "physics", "math", "arabic", "french", "english",
                                               "philosophy", "history_geo", "amazigh", "islamic", "pe"],
                              {"svt": 6, "physics": 5, "math": 5, "arabic": 3, "french": 2, "english": 2,
                               "philosophy": 2, "history_geo": 2, "amazigh": 2, "islamic": 2, "pe": 1}),
    "math": ("رياضيات", ["math", "physics", "arabic", "svt", "history_geo", "french", "english", "philosophy",
                         "amazigh", "islamic", "pe"],
             {"math": 7, "physics": 6, "arabic": 3, "svt": 2, "history_geo": 2, "french": 2, "english": 2,
              "philosophy": 2, "amazigh": 2, "islamic": 2, "pe": 1}),
    "letters_philosophy": ("آداب وفلسفة", ["arabic", "philosophy", "history_geo", "french", "english", "math",
                                          "amazigh", "islamic", "pe"],
                           {"arabic": 6, "philosophy": 6, "history_geo": 4, "french": 3, "english": 3, "math": 2,
                            "amazigh": 2, "islamic": 2, "pe": 1}),
}
MONTHS = {7: "جويلية"}


def make_case(seed: int, branch_id: str, name: str, birth: str, place: str, inst: str, inst_code: str,
              year: int = 2023, skip_amazigh: bool = True) -> dict:
    rng = random.Random(seed)
    branch_ar, order, coefs = BRANCHES[branch_id]
    rows = []
    for sid in order:
        c = coefs[sid]
        if sid == "amazigh" and skip_amazigh:
            rows.append({"subject": SUBJECTS[sid], "grade": None, "coefficient": c, "total": None})
            continue
        g = rng.choice([x / 4 for x in range(24, 81)]) if sid != "pe" else rng.choice(
            [rng.randint(8, 20) * 1.0, 17.67, 19.42])
        rows.append({"subject": SUBJECTS[sid], "grade": g, "coefficient": c, "total": round(g * c, 2)})
    taken = [r for r in rows if r["grade"] is not None]
    total = round(sum(r["total"] for r in taken), 2)
    coef_sum = sum(r["coefficient"] for r in taken)
    avg = round(total / coef_sum + 1e-9, 2)
    rnd = lambda n: "".join(rng.choice("0123456789") for _ in range(n))
    alnum = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKMNPQRSTUVWXYZ23456789"
    return {
        "registration_number": "3" + rnd(7),
        "session_year": year, "branch": branch_ar, "full_name": name,
        "birth_date": birth, "birth_place": place, "institution_name": inst, "institution_code": inst_code,
        "overall_total": f"{total:.2f}", "coefficient_total": coef_sum, "overall_average": f"{avg:.2f}",
        "issue_place": "الجزائر", "issue_date": f"{year}-07-15",
        "secret_code": "".join(rng.choice(alnum) for _ in range(8)),
        "serial_code": f"1{rnd(15)}/{''.join(rng.choice(alnum) for _ in range(8))}USEPWD_BC---1{rnd(9)}",
        "grades": rows,
    }


CASES = {
    "synthetic_experimental": make_case(1, "experimental_sciences", "بن عمر يوسف", "2005-03-14", "وهران - وهران",
                                        "ثانوية الأمير عبد القادر - وهران", "31013001", 2023),
    "synthetic_math": make_case(2, "math", "قاسمي أمينة", "2006-11-02", "قسنطينة - قسنطينة",
                                "ثانوية مالك بن نبي - قسنطينة", "25013007", 2024),
    "synthetic_letters": make_case(3, "letters_philosophy", "حمدي كريم", "2004-08-21", "سطيف - سطيف",
                                   "ثانوية ابن خلدون - سطيف", "19013012", 2023),
}


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------
def render(case: dict) -> Image.Image:
    img = Image.new("RGB", (W, H), (250, 246, 214))
    d = ImageDraw.Draw(img)
    # ornamental frame (red rosettes) + inner rule, like the real document
    d.rectangle([0, 0, W - 1, H - 1], outline=(200, 40, 60), width=70)
    for x in range(40, W - 40, 110):
        for y in (35, H - 35):
            d.ellipse([x - 28, y - 28, x + 28, y + 28], outline=(190, 30, 60), width=8)
    for y in range(40, H - 40, 110):
        for x in (35, W - 35):
            d.ellipse([x - 28, y - 28, x + 28, y + 28], outline=(190, 30, 60), width=8)
    d.rectangle([90, 90, W - 91, H - 91], outline=(70, 130, 70), width=6)
    black = (15, 15, 15)
    f_l, f_m, f_b = _font(50), _font(56), _font(64, True)

    def right(text, y, x=W - 250, font=f_m, fill=black):
        t = ar(text)
        w = d.textlength(t, font=font)
        d.text((x - w, y), t, font=font, fill=fill)

    def left(text, x, y, font=f_m, fill=black):
        d.text((x, y), text, font=font, fill=fill)

    # title (red) + ministry lines
    right("الجمهورية الجزائرية الديمقراطية الشعبية", 200, W - 500, f_b)
    right("وزارة التربية الوطنية", 330, W - 250, f_l)
    title = ar("كشف النقاط")
    d.text(((W - d.textlength(title, font=_font(150, True))) / 2, 560), title, font=_font(150, True), fill=(210, 30, 40))
    # registration number (left)
    left(ar("رقم التسجيل"), 260, 820, f_l)
    left(case["registration_number"], 260, 930, _font(64))
    d.line([260, 1010, 640, 1010], fill=black, width=3)
    # header lines
    right(f"بكالوريا دورة : {case['session_year']}", 1050)
    right(f"شعبة : {case['branch']}", 1140)
    right("بناء على محضر لجنة المداولات فإن مدير الديوان الوطني للامتحانات والمسابقات يشهد أن :", 1290, font=f_l)
    right(f"السيد (ة) : {case['full_name']}", 1400)
    right(f"المزداد (ة) في : {case['birth_date']} بـ {case['birth_place']}", 1490)
    right(f"المؤسسة : {case['institution_name']} / {case['institution_code']}", 1580)
    right("قد نجح (ت) بعد حصوله (ها) على النقاط التالية :", 1690, font=f_l)

    # table
    x0, x1 = 300, 2270
    tw = x1 - x0
    cols = [x0] + [int(x0 + f * tw) for f in (0.173, 0.343, 0.485)] + [x1]
    top, header_bottom = 1780, 1840
    rows_n, pitch = 12, 64
    total_top = header_bottom + rows_n * pitch
    bottom = total_top + 62
    for y in (top, header_bottom, total_top, bottom):
        d.line([x0, y, x1, y], fill=black, width=4)
    for x in cols:
        d.line([x, top, x, total_top if x not in (x0, x1) else bottom], fill=black, width=4)
    heads = ["المجموع", "المعامل", "النقطة /20", "المادة"]
    for i, h in enumerate(heads):
        t = ar(h)
        cx = (cols[i] + cols[i + 1]) / 2
        d.text((cx - d.textlength(t, font=f_l) / 2, top + 4), t, font=f_l, fill=black)

    def cell(text, col, y, font=f_m):
        cx = (cols[col] + cols[col + 1]) / 2
        w = d.textlength(text, font=font)
        d.text((cx - w / 2, y), text, font=font, fill=black)

    for i in range(rows_n):
        y = header_bottom + i * pitch + 6
        if i < len(case["grades"]):
            r = case["grades"][i]
            cell("--" if r["grade"] is None else f"{r['grade']:.2f}", 2, y)
            cell(str(r["coefficient"]), 1, y)
            tot = "--" if r["total"] is None else (f"{r['total']:06.2f}" if r["coefficient"] > 1 else f"{r['total']:.2f}")
            cell(tot, 0, y)
            t = ar(r["subject"])
            d.text((x1 - 20 - d.textlength(t, font=f_m), y), t, font=f_m, fill=black)
        else:
            d.text((x1 - 40, y), "-", font=f_m, fill=black)
    ty = total_top + 4
    cell(case["overall_total"], 0, ty)
    cell(str(case["coefficient_total"]), 1, ty)
    t = ar("المجموع العام")
    d.text(((cols[2] + cols[4]) / 2 - d.textlength(t, font=f_b) / 2, ty), t, font=f_b, fill=black)

    # average box
    by = bottom + 30
    d.rectangle([x0, by, x0 + 820, by + 70], outline=black, width=4)
    d.line([x0 + 340, by, x0 + 340, by + 70], fill=black, width=4)
    d.text((x0 + 90, by + 4), case["overall_average"], font=f_m, fill=black)
    t = ar("المعدل العام /20")
    d.text((x0 + 340 + 240 - d.textlength(t, font=f_m) / 2, by + 4), t, font=f_m, fill=black)
    # issue line
    y_, m_, d_ = case["issue_date"].split("-")
    right(f"حرر بالجزائر في: {int(d_)} {MONTHS[int(m_)]} {y_}", by + 150, x=x0 + 900, font=f_m)
    # seal (red) + signature (blue) on the left, QR block on the right
    d.ellipse([300, 2900, 780, 3380], outline=(200, 30, 40), width=10)
    d.arc([380, 2980, 700, 3300], 20, 300, fill=(200, 30, 40), width=8)
    d.line([250, 3150, 560, 3250], fill=(30, 60, 200), width=8)
    rng = np.random.default_rng(7)
    qr = (rng.random((28, 28)) > 0.5).astype(np.uint8) * 255
    qr = cv2.resize(qr, (240, 240), interpolation=cv2.INTER_NEAREST)
    img.paste(Image.fromarray(qr).convert("RGB"), (1950, 2790))
    # secret + serial codes
    right(f"الرقم السري :", 3090, x=2100, font=f_l)
    left(case["secret_code"], 1420, 3090, _font(52))
    left(case["serial_code"], 1225, 3250, _font(36))
    d.text((1350, 3330), ar("لا يمنح نظير آخر من هذه الشهادة"), font=_font(38), fill=(200, 40, 40))
    return img


def degrade(img: Image.Image, angle=0.0, blur=0.0, noise=0.0, jpeg=None) -> Image.Image:
    arr = cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR)
    if angle:
        m = cv2.getRotationMatrix2D((arr.shape[1] / 2, arr.shape[0] / 2), angle, 1.0)
        arr = cv2.warpAffine(arr, m, (arr.shape[1], arr.shape[0]), borderValue=(235, 235, 235))
    if blur:
        arr = cv2.GaussianBlur(arr, (0, 0), blur)
    if noise:
        arr = np.clip(arr + np.random.default_rng(3).normal(0, noise, arr.shape), 0, 255).astype(np.uint8)
    return Image.fromarray(cv2.cvtColor(arr, cv2.COLOR_BGR2RGB))


def write_text_pdf(case: dict, path: Path) -> None:
    """A PDF with a real text layer (logical-order text, so extraction is exact)."""
    import pymupdf
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    font = None
    for p in FONT_CANDIDATES:
        if Path(p).exists():
            font = p
            break
    page.insert_font(fontname="f0", fontfile=font)

    def put(x, y, txt, size=11):
        page.insert_text((x, y), txt, fontname="f0", fontsize=size)

    put(60, 60, "كشف النقاط", 24)
    put(60, 110, case["registration_number"])
    put(300, 140, f"بكالوريا دورة : {case['session_year']}")
    put(300, 158, f"شعبة : {case['branch']}")
    put(120, 190, f"السيد (ة) : {case['full_name']}")
    put(120, 208, f"المزداد (ة) في : {case['birth_date']} بـ {case['birth_place']}")
    put(120, 226, f"المؤسسة : {case['institution_name']} / {case['institution_code']}")
    y = 280
    for r in case["grades"]:
        g = "--" if r["grade"] is None else f"{r['grade']:.2f}"
        t = "--" if r["total"] is None else f"{r['total']:06.2f}"
        put(80, y, t)
        put(150, y, str(r["coefficient"]))
        put(230, y, g)
        put(320, y, r["subject"])
        y += 20
    put(80, y + 6, case["overall_total"])
    put(150, y + 6, str(case["coefficient_total"]))
    put(320, y + 6, "المجموع العام")
    put(80, y + 36, case["overall_average"])
    put(320, y + 36, "المعدل العام")
    put(120, y + 66, "حرر بالجزائر في: " + f"{int(case['issue_date'][8:])} جويلية {case['issue_date'][:4]}")
    put(300, y + 120, "الرقم السري : " + case["secret_code"])
    put(80, y + 160, case["serial_code"], 8)
    doc.save(path)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    plan = [
        ("synthetic_experimental", "png", {}),
        ("synthetic_math", "jpg", {"angle": 1.4, "blur": 0.8, "noise": 4, "jpeg": 82}),
        ("synthetic_letters", "png", {"angle": -0.9, "blur": 0.6}),
    ]
    for name, ext, deg in plan:
        case = CASES[name]
        img = degrade(render(case), **{k: v for k, v in deg.items() if k != "jpeg"})
        path = OUT / f"{name}.{ext}"
        img.save(path, quality=deg.get("jpeg", 95)) if ext == "jpg" else img.save(path)
        (OUT / f"{name}.expected.json").write_text(json.dumps(case, ensure_ascii=False, indent=2), encoding="utf-8")
        print("wrote", path.name)
    write_text_pdf(CASES["synthetic_experimental"], OUT / "synthetic_textlayer.pdf")
    (OUT / "synthetic_textlayer.expected.json").write_text(
        json.dumps(CASES["synthetic_experimental"], ensure_ascii=False, indent=2), encoding="utf-8")
    print("wrote synthetic_textlayer.pdf")


if __name__ == "__main__":
    main()
