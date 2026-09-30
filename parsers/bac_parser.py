"""Field parsers for the BAC transcript ("كشف نقاط البكالوريا").

Two entry points:
  * `build_fields_from_zones` – scanned documents (zones already OCR'd by `zone_reader`)
  * `parse_text_layer`        – PDFs that carry a real text layer (no OCR needed)

Both share the same line-level parsers, so label variants / regexes live in one place.
"""
from __future__ import annotations

import re
from collections import Counter
from datetime import date

from rapidfuzz import fuzz

from services.models import DERIVED, LOW, MISSING, OK, Field
from .lexicon import fix_specialty, fix_wilaya, fix_words
from .normalize import (MONTHS_AR, arabic_only, clean_line, normalize_ar, skeleton_ar,
                        strip_marks, to_ascii_digits)
from .subjects import match_branch

# key, Arabic label, English label  (order = display order)
FIELD_DEFS: list[tuple[str, str, str]] = [
    ("registration_number", "رقم التسجيل", "Registration number"),
    ("session_year", "بكالوريا دورة", "Session year"),
    ("branch", "شعبة", "Branch / stream"),
    ("full_name", "السيد (ة)", "Full name"),
    ("birth_date", "المزداد (ة) في", "Birth date"),
    ("birth_place", "بـ", "Birth place"),
    ("institution_name", "المؤسسة", "Institution name"),
    ("institution_code", "رمز المؤسسة", "Institution code"),
    ("overall_total", "المجموع العام", "Overall total"),
    ("coefficient_total", "مجموع المعاملات", "Coefficient total"),
    ("overall_average", "المعدل العام", "Overall average /20"),
    ("issue_place", "حرر بـ", "Issue place"),
    ("issue_date", "حرر بالجزائر في", "Issue date"),
    ("secret_code", "الرقم السري", "Secret code"),
    ("serial_code", "الكود أسفل الصفحة (USEPWD_BC)", "Bottom serial code"),
]

# label words that must be stripped from the front of a line
LABEL_WORDS = {
    "name": ["السيد", "السيدة", "السيد(ة)"],
    "birth": ["المزداد", "المزدادة", "في"],
    "institution": ["المؤسسة", "المؤسسه"],
    "branch": ["شعبة", "شعبه"],
    "year": ["بكالوريا", "دورة", "دوره"],
}
_PUNCT_ONLY = re.compile(r"^[\s()\[\]{}|:;.,\-–—_•·\\/٭*'\"‘’“”!?٠-٩0-9ةأاءـ]*$")


def new_fields() -> dict[str, Field]:
    return {k: Field(k, ar, en) for k, ar, en in FIELD_DEFS}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _is_label(token: str, words: list[str]) -> bool:
    t = normalize_ar(token)
    if not t:
        return bool(_PUNCT_ONLY.match(token))
    if len(t) <= 2 and _PUNCT_ONLY.match(token):
        return True
    return any(fuzz.ratio(t, normalize_ar(w)) >= 62 for w in words)


def strip_label(text: str, kind: str) -> str:
    """Remove the printed label ('السيد (ة) :' ...) from the start of an OCR'd line."""
    text = clean_line(text)
    words = LABEL_WORDS[kind]
    if ":" in text[:40]:
        head, tail = text.split(":", 1)
        head_tokens = head.split()
        if head_tokens and len(head_tokens) <= 4 and any(
                fuzz.ratio(normalize_ar(t), normalize_ar(w)) >= 55 for t in head_tokens for w in words):
            return tail.strip()
    tokens = text.split()
    i = 0
    while i < len(tokens) and i < 4 and _is_label(tokens[i], words):
        i += 1
    return " ".join(tokens[i:]).strip(" :-")


def char_vote(cands: list[str], min_len: int = 1, max_len: int = 99) -> tuple[str | None, float]:
    """Vote character-by-character over candidates of the modal length. Returns the
    consensus string and the fraction of characters that had a strict majority."""
    cands = [c for c in cands if min_len <= len(c) <= max_len]
    if not cands:
        return None, 0.0
    length = Counter(len(c) for c in cands).most_common(1)[0][0]
    same = [c for c in cands if len(c) == length]
    out, agree = [], 0
    for i in range(length):
        cnt = Counter(c[i] for c in same)
        top, n = cnt.most_common(1)[0]
        # tie -> earliest candidate wins (candidate order = best recipe first)
        if len(cnt) > 1 and list(cnt.values()).count(n) > 1:
            top = next(c[i] for c in same if cnt[c[i]] == n)
        out.append(top)
        agree += n / len(same)
    return "".join(out), agree / length


def _tokens_after_number(tokens: list[str]) -> list[str]:
    for i, tok in enumerate(tokens):
        if sum(ch.isdigit() for ch in to_ascii_digits(tok)) >= 4:
            return tokens[i + 1:]
    return []


# ---------------------------------------------------------------------------
# individual parsers (return plain values; None when not found)
# ---------------------------------------------------------------------------
def parse_year(text: str, numbers: list[str], digits_line: str = "") -> int | None:
    """Session year: the 4-digit year most of the reads agree on (ties -> first seen)."""
    pool = " ".join([to_ascii_digits(text), *numbers, digits_line])
    years = [int(y) for y in re.findall(r"(?<!\d)((?:19|20)\d{2})(?!\d)", pool)]
    years = [y for y in years if 1962 <= y <= date.today().year + 1]
    if not years:
        return None
    counts = Counter(years)
    top = max(counts.values())
    return next(y for y in years if counts[y] == top)


def parse_date_digits(s: str) -> tuple[int, int, int] | None:
    """(year, month, day) from '2004-06-25', '25-06-2004', '2019/07/16' ..."""
    s = to_ascii_digits(s)
    m = re.search(r"((?:19|20)\d{2})\D{1,2}(\d{1,2})\D{1,2}(\d{1,2})", s)
    if m:
        y, a, b = int(m.group(1)), int(m.group(2)), int(m.group(3))
    else:
        m = re.search(r"(?<!\d)(\d{1,2})\D{1,2}(\d{1,2})\D{1,2}((?:19|20)\d{2})(?!\d)", s)
        if not m:
            return None
        b, a, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
    # printed as yyyy-mm-dd or dd-mm-yyyy; the middle group is always the month
    month, day = a, b
    if not (1 <= month <= 12 and 1 <= day <= 31):
        if 1 <= b <= 12 and 1 <= a <= 31:
            month, day = b, a
        else:
            return None
    return y, month, day


_STRICT_DATE = re.compile(r"^(?:(?:19|20)\d{2}[-/]\d{2}[-/]\d{2}|\d{2}[-/]\d{2}[-/](?:19|20)\d{2})$")


def is_clean_date(s: str) -> bool:
    """True when the digit string is exactly a date (no junk / stray separators)."""
    return bool(_STRICT_DATE.match(to_ascii_digits(s).strip(" .")))


def iso(y: int, m: int, d: int) -> str | None:
    try:
        return date(y, m, d).isoformat()
    except ValueError:
        return None


def parse_birth(text: str, numbers: list[str], digits_line: str = "") -> tuple[str | None, str | None]:
    """-> (iso birth date, birth place)"""
    dt, _clean = parse_birth_date(text, numbers, digits_line)
    return dt, parse_birth_place(text)


def parse_birth_date(text: str, numbers: list[str], digits_line: str = "") -> tuple[str | None, bool]:
    """-> (iso date, read_cleanly). Clean means some source contained exactly a date."""
    dt = None
    clean = False
    for cand in [*numbers, text, digits_line]:
        got = parse_date_digits(cand)
        if got:
            d = iso(*got)
            if d:
                if dt is None:
                    dt = d
                # any clean candidate that agrees confirms the date
                m = re.search(r"\S*(?:19|20)\d{2}\S*", to_ascii_digits(cand))
                if m and is_clean_date(m.group(0)) and d == dt:
                    clean = True
    return dt, clean


def parse_birth_place(text: str) -> str | None:
    tail = clean_line(text)
    tokens = tail.split()
    after = _tokens_after_number(tokens)
    if not after:
        body = strip_label(tail, "birth")
        after = body.split()
    return _clean_place(after)


def _clean_place(tokens: list[str]) -> str | None:
    toks = re.sub(r"\s*[-–—]\s*", " - ", " ".join(tokens)).split()   # 'وهران-' -> 'وهران -'
    while toks and (normalize_ar(toks[0]) in {"ب", "بـ"} or len(normalize_ar(toks[0])) <= 1
                    or not re.search(r"[ء-ي]", toks[0])):
        toks.pop(0)
    out = []
    for t in toks:
        t = strip_marks(t)
        if t in {"-", "–", "—"}:
            out.append("-")
        elif re.search(r"[ء-ي]", t):
            out.append("".join(ch for ch in t if re.match(r"[ء-ي]", ch)))
    place = " ".join(out).strip(" -")
    place = re.sub(r"(\s-)+\s*$", "", place)
    if not place:
        return None
    pieces = [p.strip() for p in place.split(" - ")]
    # last piece is the wilaya; the first is the commune (often equal to the wilaya)
    pieces[-1] = fix_wilaya(pieces[-1], 82)
    if len(pieces) > 1:
        pieces[0] = fix_wilaya(pieces[0], 90)
    return " - ".join(pieces)


def _institution_code(numbers: list[str], body: str) -> str | None:
    """Institution codes have 4-8 digits. OCR sometimes appends stray digits, so candidates
    longer than 8 are trimmed and the code most reads agree on wins."""
    cands = [re.sub(r"\D", "", n) for n in numbers]
    cands += re.findall(r"(?<!\d)\d{4,10}(?!\d)", to_ascii_digits(body))
    cands = [c[:8] for c in cands if 3 <= len(c) <= 12]
    if not cands:
        return None
    counts = Counter(cands)
    top = max(counts.values())
    for c in cands:                      # first among the most frequent = reading order
        if counts[c] == top:
            return c
    return cands[-1]


def parse_institution(text: str, numbers: list[str]) -> tuple[str | None, str | None]:
    """-> (name, code)"""
    body = strip_label(text, "institution")
    code = _institution_code(numbers, body)
    # the name is everything before the '/' (or code)
    if re.search(r"[/\\]", body):
        before, after = re.split(r"[/\\]", to_ascii_digits(body), maxsplit=1)
        tail = " ".join(t for t in after.split() if re.search(r"[ء-ي]", t))     # text-layer PDFs
        name_part = before.rstrip(" -") + (" - " + tail if tail else "")
    else:
        name_part = body
    name_part = re.sub(r"\d+", " ", name_part)
    toks = []
    for t in name_part.split():
        if t in {"-", "–", "—"}:
            toks.append("-")
        elif re.search(r"[ء-ي]", t):
            word = strip_marks("".join(ch for ch in t if re.match(r"[ء-يً-ٟ]", ch)))
            if len(normalize_ar(word)) >= 2:          # a lone letter is OCR debris
                toks.append(word)
    name = " ".join(toks).strip(" -")
    if name:
        name = fix_words(name)
        toks2 = name.split()
        name = " ".join(t for i, t in enumerate(toks2) if i == 0 or t != toks2[i - 1] or t == "-")
        pieces = [p.strip() for p in name.split(" - ")]
        if len(pieces) > 1:                       # "<institution> - <wilaya>"
            pieces[-1] = fix_wilaya(pieces[-1], 84)
        name = " - ".join(pieces)
    return (name or None), code


def parse_name(text: str) -> str | None:
    body = strip_label(text, "name")
    toks = [t for t in arabic_only(body).split() if len(normalize_ar(t)) >= 2]  # drop stray 1-letter junk
    return " ".join(toks) or None


def parse_branch_text(text: str) -> tuple[str | None, float]:
    body = strip_label(text, "branch")
    cleaned = arabic_only(body)
    if not cleaned:
        return None, 0.0
    branch, score = match_branch(cleaned)
    if branch and score >= 70:
        # keep the specialty for technical branches ("تقني رياضي - هندسة كهربائية")
        if branch.id == "technical_math":
            rest = " ".join(cleaned.split()[2:])
            spec = fix_specialty(rest) if rest else None
            return ("تقني رياضي - " + spec if spec else cleaned), score
        return branch.name_ar, score
    return cleaned, score


def parse_issue(text: str, numbers: list[str], digits_line: str = "") -> tuple[str | None, str | None]:
    """-> (place, iso date)"""
    t = clean_line(text)
    place = None
    flat = normalize_ar(t)
    if "جزائر" in flat or fuzz.partial_ratio(skeleton_ar("الجزائر"), skeleton_ar(t)) >= 80:
        place = "الجزائر"          # the printed label is "حرر بالجزائر في"
    elif "حر" in flat:
        m = re.search(r"ب(ال[ء-ي]{3,})", strip_marks(t))
        place = m.group(1) if m else "الجزائر"
    # numeric date (2019/07/16 style)
    for cand in [t, *numbers, digits_line]:
        got = parse_date_digits(cand)
        if got:
            d = iso(*got)
            if d:
                return place, d
    # "17 جويلية 2025"
    month = None
    for tok in arabic_only(t).split():
        nt = normalize_ar(tok)
        for name, num in MONTHS_AR.items():
            if fuzz.ratio(nt, normalize_ar(name)) >= 72:
                month = num
    pool = " ".join([to_ascii_digits(t), *numbers])
    year = next((int(y) for y in re.findall(r"(?<!\d)((?:19|20)\d{2})(?!\d)", pool)), None)
    day = None
    for n in re.findall(r"(?<!\d)(\d{1,2})(?!\d)", pool):
        if 1 <= int(n) <= 31:
            day = int(n)
            break
    if year and month and day:
        return place, iso(year, month, day)
    return place, None


# ---------------------------------------------------------------------------
# serial + secret code
# ---------------------------------------------------------------------------
_SERIAL_SPLIT = re.compile(r"USEPWD_?B\w?C", re.I)


def parse_serial(cands: list[str]) -> tuple[str | None, float]:
    """The serial is `<16 digits>/<8 alnum>USEPWD_BC---<10 digits>` (older transcripts use an
    alphanumeric prefix). OCR garbles the
    random part, so every component is voted separately across all OCR variants."""
    prefixes, ids, suffixes = [], [], []
    for c in cands:
        c = c.replace(" ", "")
        m = _SERIAL_SPLIT.search(c)
        if not m:
            continue
        left, right = c[:m.start()], c[m.end():]
        if "/" not in left:
            continue
        pre, ident = left.rsplit("/", 1)
        pre = re.sub(r"[^A-Za-z0-9]", "", pre)
        suf = re.match(r"-*(\d+)", right)
        if not (6 <= len(pre) <= 20 and 3 <= len(ident) <= 12 and suf):
            continue
        prefixes.append(pre)
        ids.append(re.sub(r"[^A-Za-z0-9]", "", ident))
        suffixes.append(suf.group(1)[:12])
    if not prefixes:
        return None, 0.0
    pre, a1 = char_vote(prefixes)
    ident, a2 = char_vote(ids)
    suf, a3 = char_vote(suffixes)
    # agreement among only two or three parsable reads proves little: scale it down
    support = min(1.0, 0.5 + len(prefixes) / 8.0)
    return f"{pre}/{ident}USEPWD_BC---{suf}", (a1 + a2 + a3) / 3 * support


_CASE_PAIRS = set("cosvwxz")      # letters whose upper / lower case differ only by size


def fix_case_by_height(code: str, heights: list[float]) -> str:
    """OCR often confuses x/X, s/S, o/O ... Glyph heights settle it: a capital reaches the
    full cap height, a lower-case letter only ~70 % of it. Only applied when the number of
    detected glyphs equals the length of the code (otherwise the alignment is unreliable)."""
    if len(heights) != len(code) or len(code) < 4:
        return code
    out = []
    for ch, h in zip(code, heights):
        if ch.lower() in _CASE_PAIRS:
            out.append(ch.upper() if h >= 0.86 else ch.lower())
        else:
            out.append(ch)
    return "".join(out)


def parse_secret(cands: list[str], heights: list[float] | None = None) -> tuple[str | None, float]:
    cands = [re.sub(r"[^A-Za-z0-9]", "", c) for c in cands]
    code, agree = char_vote(cands, 5, 10)
    if code and heights:
        code = fix_case_by_height(code, heights)
    return code, agree


# ---------------------------------------------------------------------------
# scanned document: zones -> fields
# ---------------------------------------------------------------------------
def _ocr_conf(lr) -> float:
    return max(0.0, min(1.0, lr.conf / 100.0)) if lr and lr.conf >= 0 else 0.4


def build_fields_from_zones(zones, table_result, fields: dict[str, Field] | None = None) -> dict[str, Field]:
    """`table_result` = (rows, (overall, how), (coefsum, how), avg candidates handled here)."""
    fields = fields or new_fields()
    L = zones.lines

    # registration number ------------------------------------------------------
    eight = [c for c in zones.reg_candidates if len(c) == 8]   # registration numbers have 8 digits
    reg, agree = char_vote(eight or [c for c in zones.reg_candidates if 7 <= len(c) <= 9], 7, 9)
    if reg:
        fields["registration_number"].set(reg, OK if agree > 0.6 and len(reg) == 8 else LOW,
                                          0.55 + 0.4 * agree, raw=" | ".join(zones.reg_candidates[:3]))

    # session year -----------------------------------------------------------------
    if "year" in L:
        year = parse_year(L["year"].text, L["year"].numbers, L["year"].digits_line)
        if year:
            fields["session_year"].set(year, OK, min(0.98, 0.6 + 0.4 * _ocr_conf(L["year"])), raw=L["year"].text)

    # branch ----------------------------------------------------------------------
    if "branch" in L:
        branch, score = parse_branch_text(L["branch"].text)
        if branch:
            fields["branch"].set(branch, OK if score >= 70 else LOW,
                                 min(0.98, score / 100.0) if score >= 70 else 0.45, raw=L["branch"].text)

    # name ---------------------------------------------------------------------------
    if "name" in L:
        name = parse_name(L["name"].text)
        if name:
            n_words = len(name.split())
            conf = _ocr_conf(L["name"]) * (0.95 if 2 <= n_words <= 6 else 0.7)
            fields["full_name"].set(name, OK if conf >= 0.6 else LOW, conf, raw=L["name"].text,
                                    note=None if conf >= 0.6 else "Arabic OCR confidence is low - check manually")

    # birth ----------------------------------------------------------------------------
    if "birth" in L:
        b = L["birth"]
        bdate, clean = parse_birth_date(b.text, b.numbers, b.digits_line)
        bplace = parse_birth_place(b.text)
        if bdate:
            y = int(bdate[:4])
            plausible = 1940 <= y <= date.today().year - 14
            good = plausible and clean
            fields["birth_date"].set(bdate, OK if good else LOW, 0.92 if good else 0.4,
                                     raw=b.text + "  |  " + " ".join(b.numbers),
                                     note=None if good else "date digits were noisy - verify")
        if bplace:
            fields["birth_place"].set(bplace, OK if _ocr_conf(b) > 0.6 else LOW,
                                      min(0.9, _ocr_conf(b)), raw=b.text)

    # institution ----------------------------------------------------------------------
    if "institution" in L:
        i = L["institution"]
        iname, icode = parse_institution(i.text, i.numbers)
        if iname:
            fields["institution_name"].set(iname, OK if _ocr_conf(i) > 0.6 else LOW,
                                           min(0.92, _ocr_conf(i)), raw=i.text)
        if icode:
            good = 4 <= len(icode) <= 8
            fields["institution_code"].set(icode, OK if good else LOW, 0.9 if good else 0.4, raw=i.text)

    # totals from the table ----------------------------------------------------------
    rows, (overall, how_o), (coefsum, how_c) = table_result
    conf_map = {"confirmed": 0.98, "ocr_only": 0.55, "computed": 0.75}
    fields["overall_total"].set(f"{overall:.2f}", OK if how_o == "confirmed" else (DERIVED if how_o == "computed" else LOW),
                                conf_map[how_o], note=None if how_o == "confirmed" else f"{how_o}: does not match sum of rows"
                                if how_o == "ocr_only" else "computed from the sum of rows")
    fields["coefficient_total"].set(int(coefsum), OK if how_c == "confirmed" else (DERIVED if how_c == "computed" else LOW),
                                    conf_map[how_c])

    # average -----------------------------------------------------------------------------
    computed = round(overall / coefsum + 1e-9, 2) if coefsum else None
    read = None
    for c in zones.avg_candidates:
        c = c.strip(".")
        m = re.fullmatch(r"(\d{1,2})\.(\d{1,2})", c) or (re.fullmatch(r"(\d{2,4})", c) and None)
        if m:
            v = float(f"{m.group(1)}.{m.group(2).ljust(2, '0')}")
            if 0 <= v <= 20:
                read = v if read is None or (computed is not None and abs(v - computed) < abs(read - computed)) else read
        elif re.fullmatch(r"\d{3,4}", c):        # lost decimal point
            v = float(f"{c[:-2]}.{c[-2:]}")
            if 0 <= v <= 20 and read is None:
                read = v
    if read is not None and computed is not None and abs(read - computed) <= 0.011:
        fields["overall_average"].set(f"{read:.2f}", OK, 0.98, raw=" | ".join(zones.avg_candidates))
    elif computed is not None:
        note = "recomputed as total / coefficients" + (f" (OCR read {read:.2f})" if read is not None else "")
        fields["overall_average"].set(f"{computed:.2f}", DERIVED, 0.8, raw=" | ".join(zones.avg_candidates), note=note)
    elif read is not None:
        fields["overall_average"].set(f"{read:.2f}", LOW, 0.5, raw=" | ".join(zones.avg_candidates))

    # issue place/date ----------------------------------------------------------------
    if "issue" in L:
        iss = L["issue"]
        place, idate = parse_issue(iss.text, iss.numbers, iss.digits_line)
        if place:
            fields["issue_place"].set(place, OK, 0.9, raw=iss.text)
        if idate:
            yr = fields["session_year"].value
            ok = (yr is None) or (int(idate[:4]) in (yr, yr + 1))
            if not ok and yr is not None:
                # session year and issue year disagree: one of the two reads is wrong -> flag both
                sy = fields["session_year"]
                sy.status, sy.confidence = LOW, min(sy.confidence, 0.45)
                sy.note = f"does not match the issue date year ({idate[:4]}) - verify"
            fields["issue_date"].set(idate, OK if ok else LOW, 0.92 if ok else 0.45, raw=iss.text,
                                     note=None if ok else "issue year does not match the session year")

    # codes --------------------------------------------------------------------------------
    secret, agree = parse_secret(zones.secret_candidates, zones.secret_glyph_heights)
    if secret:
        fields["secret_code"].set(secret, OK if agree >= 0.8 else LOW, 0.4 + 0.55 * agree,
                                  raw=" | ".join(zones.secret_candidates[:4]),
                                  note=None if agree >= 0.8 else "case-sensitive code, OCR variants disagreed - verify")
    serial, agree = parse_serial(zones.serial_candidates)
    if serial:
        fields["serial_code"].set(serial, OK if agree >= 0.85 else LOW, 0.4 + 0.55 * agree,
                                  raw=(zones.serial_candidates or [""])[0],
                                  note=None if agree >= 0.85 else "random part is hard to OCR - verify")
    return fields


# ---------------------------------------------------------------------------
# text-layer PDFs
# ---------------------------------------------------------------------------
def looks_like_transcript_text(text: str) -> bool:
    t = normalize_ar(strip_marks(text))
    hits = sum(1 for w in ("كشف", "النقاط", "بكالوريا", "المجموع", "المعدل") if w in t)
    rev = normalize_ar(strip_marks(text[::-1]))
    hits_rev = sum(1 for w in ("كشف", "النقاط", "بكالوريا", "المجموع", "المعدل") if w in rev)
    return max(hits, hits_rev) >= 3 or "USEPWD" in text
