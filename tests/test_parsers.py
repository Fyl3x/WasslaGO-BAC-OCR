"""Pure-logic tests: no OCR engine needed."""
import pytest

from ocr.table_reader import RawCell, RawRow, RawTable
from parsers import bac_parser as bp
from parsers.lexicon import fix_specialty, fix_wilaya, fix_words
from parsers.normalize import (coef_values, grade_values, normalize_ar, overall_total_values, skeleton_ar,
                               to_ascii_digits, total_values)
from parsers.subjects import load_vocabulary, match_branch, match_subject
from parsers.table_parser import parse_table, row_options
from parsers import text_layer


# ---------------------------------------------------------------- numbers
def test_arabic_indic_digits():
    assert to_ascii_digits("٢٠٢٣") == "2023"


@pytest.mark.parametrize("raw,expected", [("19.50", 19.5), ("1950", 19.5), ("17.67", 17.67), ("20.00.", 20.0)])
def test_grade_repairs_lost_decimal_and_keeps_pe_averages(raw, expected):
    assert expected in [v for v, _ in grade_values(raw)]


def test_total_strips_ruled_line_bleed():
    assert 136.5 in [v for v, _ in total_values("1136.50")]


def test_overall_total_may_exceed_200():
    assert 407.42 in [v for v, _ in overall_total_values("407.42")]
    assert 407.42 not in [v for v, _ in total_values("407.42")]


def test_coefficient_single_digit():
    assert coef_values("7") == [(7.0, 1.0)]


# ---------------------------------------------------------------- Arabic text
def test_normalize_and_skeleton():
    assert normalize_ar("الفلسفة") == normalize_ar("الفَلْسَفَة")
    assert skeleton_ar("مؤصصة") == skeleton_ar("مؤسسة")


def test_lexicon_repairs():
    assert fix_wilaya("وهرأن") == "وهران"
    assert fix_words("مؤصصة التربية") == "مؤسسة التربية"
    assert fix_specialty("هندسة كيربالية") == "هندسة كهربائية"


def test_subject_matching_uses_branch_candidates():
    v = load_vocabulary()
    # garbled OCR of "الفلسفة": free matching prefers "law", the branch restricts it
    sub, _ = match_subject("الفا 7 1 1", v, candidates=["arabic", "philosophy", "history_geo"], expected_id="philosophy")
    assert sub.id == "philosophy"
    assert match_subject("الرياضيات", v)[0].id == "math"
    assert match_branch("العلوم التجريبية")[0].id == "experimental_sciences"


# ---------------------------------------------------------------- line parsers
def test_dates():
    assert bp.parse_date_digits("2004-06-25") == (2004, 6, 25)
    assert bp.parse_date_digits("25-06-2004") == (2004, 6, 25)
    assert bp.parse_date_digits("2019/07/16") == (2019, 7, 16)
    assert bp.parse_date_digits("5532006-11-0655") == (2006, 11, 6)      # junk around the date
    assert bp.parse_date_digits("1234") is None


def test_birth_line():
    dt, place = bp.parse_birth("المزداد (ة) في: 2007-02-25 ب غرداية - غرداية", ["2007-02-25"])
    assert dt == "2007-02-25" and place == "غرداية - غرداية"
    _, clean = bp.parse_birth_date("المزداد في", ["5532006-11-0655"])
    assert clean is False


def test_institution_line_and_code_trimming():
    name, code = bp.parse_institution("المؤسسة : ثانوية الأمير عبد القادر - وهران/ 31013001", ["310130015"])
    assert name == "ثانوية الأمير عبد القادر - وهران" and code == "31013001"      # stray 9th digit trimmed
    assert bp.parse_institution("المؤسسة : مديرية التربية ورقلة - ورقلة/2500", ["2500/"])[1] == "2500"


def test_name_and_branch_and_issue():
    assert bp.parse_name("السيد (ة) : بن عمر يوسف") == "بن عمر يوسف"
    assert bp.parse_name("السبدزة) : قاسمي أمينة ا") == "قاسمي أمينة"
    assert bp.parse_branch_text("شعبة : العلوم التجريبية")[0] == "علوم تجريبية"
    assert bp.parse_branch_text("شعبة : تقلي رياضي - هندسة كيربالية")[0] == "تقني رياضي - هندسة كهربائية"
    assert bp.parse_issue("حرر بالجزائر في: 17 جويلية 2025", ["17", "2025"]) == ("الجزائر", "2025-07-17")
    assert bp.parse_issue("حرر بالجزائر في: 2019/07/16", [])[1] == "2019-07-16"


def test_year_and_codes():
    assert bp.parse_year("بكالوريا دورة : 2023", ["2023"]) == 2023
    votes = ["Ab3dE9xQ", "Abadeqxo", "Ab3dE9xQ", "Ab3dE9xQ", "Ab3de9xq"]
    assert bp.parse_secret(votes)[0] == "Ab3dE9xQ"
    reads = ["1391500080636083/4FJPrzrXUSEPWD_BC---1374068124o",
             "13915000806360834NB72rXUSEPWD_BC--1374068124",        # garbled '/' -> ignored
             "1391500080636083/4FJPrzrXUSEPWD_BC---1374068124"]
    serial, few = bp.parse_serial(reads)
    assert serial == "1391500080636083/4FJPrzrXUSEPWD_BC---1374068124"
    assert few < 0.85                                  # only two parsable reads: not trusted yet
    _, many = bp.parse_serial(reads * 4)
    assert many > 0.9                                  # the same agreement over many reads is trusted
    # older transcripts have an alphanumeric prefix
    assert bp.parse_serial(["kader11/1987USEPWD_BC---1563225307"])[0] == "kader11/1987USEPWD_BC---1563225307"


# ---------------------------------------------------------------- table logic
def cell(*cands, dash=False, empty=False):
    return RawCell(candidates=list(cands), dash=dash, empty=empty)


def row(i, subject, g, c, t):
    return RawRow(index=i, y0=0, y1=0, subject_text=subject, grade=g, coef=c, total=t)


def make_table(rows, total, coefsum):
    return RawTable(rows=rows, total_row={"total": cell(*total), "coef": cell(*coefsum)}, pitch=60, n_rows=len(rows))


def test_row_options_prefers_consistent_triple():
    r = row(0, "الرياضيات", cell("19.50", "19.50", "1950"), cell("7", "7"), cell("136.50", "136.50", "1136.50"))
    best = row_options(r)[0]
    assert (best.grade, best.coef, best.total, best.status) == (19.5, 7, 136.5, "ok")


def test_row_options_derives_missing_coefficient():
    r = row(0, "التربية البدنية", cell("18.75", "18.75"), cell(), cell("18.75", "18.75"))
    best = row_options(r)[0]
    assert (best.grade, best.coef, best.total) == (18.75, 1, 18.75) and best.status == "derived"


def test_parse_table_end_to_end_with_amazigh_and_blank_rows():
    rows = [
        row(0, "الرياضيات", cell("19.50"), cell("7"), cell("136.50")),
        row(1, "العلوم الفيزيائية", cell("16.50"), cell("6"), cell("099.00")),
        row(2, "اللغة العربية وآدابها", cell("17.00"), cell("3"), cell("051.00")),
        row(3, "اللغة الأمازيغية", cell(dash=True), cell("2"), cell(dash=True)),
        row(4, "-", cell(empty=True), cell(empty=True), cell(empty=True)),
    ]
    rows[3].grade, rows[3].total = RawCell(dash=True), RawCell(dash=True)
    out_rows, overall, coefsum, checks, _ = parse_table(make_table(rows, ["286.50"], ["16"]), None)
    assert [r.status for r in out_rows] == ["ok", "ok", "ok", "not_taken"]
    assert overall == (286.5, "confirmed") and coefsum == (16.0, "confirmed")
    assert all(c.passed for c in checks)


def test_parse_table_ignores_implausible_total_cell():
    rows = [row(0, "الرياضيات", cell("15.00"), cell("7"), cell("105.00")),
            row(1, "العلوم الفيزيائية", cell("12.00"), cell("6"), cell("072.00")),
            row(2, "اللغة العربية وآدابها", cell("14.00"), cell("3"), cell("042.00"))]
    _, overall, coefsum, checks, _ = parse_table(make_table(rows, ["12.50"], ["2"]), None)
    assert overall == (219.0, "computed") and coefsum == (16, "computed")     # 12.50 / 2 are impossible reads
    assert all(c.passed for c in checks)


def test_reconcile_uses_overall_total_to_pick_between_alternatives():
    # PE read as 0.00 (lost '2') in one candidate set, 20.00 in another: the printed total decides
    rows = [row(0, "الرياضيات", cell("15.00"), cell("7"), cell("105.00")),
            row(1, "العلوم الفيزيائية", cell("12.00"), cell("6"), cell("072.00")),
            row(2, "اللغة العربية وآدابها", cell("14.00"), cell("3"), cell("042.00")),
            row(3, "التربية البدنية والرياضية", cell("0.00", "20.00", "20.00"), cell("1"), cell("0.00", "20.00", "20.00"))]
    out, overall, _, checks, _ = parse_table(make_table(rows, ["239.00"], ["17"]), None)
    assert out[3].grade == 20.0 and checks[1].passed


# ---------------------------------------------------------------- text layer helpers
def test_reverse_arabic_runs_keeps_digits():
    assert text_layer._reverse_arabic_runs("ةرود ايرولاكب 2023") == "بكالوريا دورة 2023"
    assert text_layer.detect_reversed("طاقنلا فشك\n ةرود ايرولاكب2023") is True
    assert text_layer.detect_reversed("كشف النقاط\nبكالوريا دورة : 2023") is False


def test_year_is_a_majority_vote_and_issue_place_survives_ocr_noise():
    assert bp.parse_year("مك نوريا دورة : 2010", ["2019", "2019"]) == 2019
    assert bp.parse_issue("حرر بالخزائر هي 2019/07/16", []) == ("الجزائر", "2019-07-16")


def test_session_year_is_flagged_when_it_contradicts_the_issue_date():
    from types import SimpleNamespace as NS
    lines = {"year": NS(text="بكالوريا دورة : 2010", numbers=["2010"], digits_line="", conf=90.0),
             "issue": NS(text="حرر بالجزائر في: 17 جويلية 2019", numbers=["17", "2019"], digits_line="", conf=90.0)}
    zones = NS(lines=lines, reg_candidates=[], avg_candidates=[], secret_candidates=[], secret_glyph_heights=[],
               serial_candidates=[])
    fields = bp.build_fields_from_zones(zones, ([], (286.5, "computed"), (16, "computed")))
    assert fields["session_year"].status == "low_confidence"
    assert fields["issue_date"].status == "low_confidence"


def test_case_fix_by_glyph_height():
    # 'x' short, 'S' tall, digits tall
    assert bp.fix_case_by_height("xS4x", [0.72, 1.0, 1.0, 0.7]) == "xS4x"
    assert bp.fix_case_by_height("XsZz", [1.0, 0.72, 1.0, 0.72]) == "XsZz"
    assert bp.fix_case_by_height("Xs4x", [0.72, 1.0, 1.0, 0.7]) == "xS4x"       # wrong case corrected
    assert bp.fix_case_by_height("abcd", [1.0]) == "abcd"                        # glyph count mismatch: untouched
