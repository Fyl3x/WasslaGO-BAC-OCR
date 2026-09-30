"""Turn raw per-cell OCR candidates into validated grade rows.

Key idea: every row obeys `grade x coefficient = total`, the rows sum to the overall
total, and the coefficients sum to the printed coefficient total. That is enough
redundancy to repair most OCR mistakes and to know when we could not.
"""
from __future__ import annotations

import itertools
from collections import defaultdict
from dataclasses import dataclass

from config import Config
from ocr.table_reader import RawCell, RawRow, RawTable
from services.models import DERIVED, OK, Check, GradeRow
from .normalize import (coef_values, coefsum_values, grade_values, overall_total_values,
                        total_values)
from .subjects import Branch, Vocabulary, load_vocabulary, match_subject

EPS = 0.011


def _aggregate(cell: RawCell, parse) -> dict[float, float]:
    scores: dict[float, float] = defaultdict(float)
    n = max(1, len(cell.candidates))
    for txt in cell.candidates:
        seen = {}
        for v, w in parse(txt):
            seen[v] = max(seen.get(v, 0), w)
        for v, w in seen.items():
            scores[v] += w / n
    return dict(scores)


@dataclass
class RowOption:
    grade: float | None
    coef: int | None
    total: float | None
    score: float
    status: str   # ok | derived | conflict
    note: str = ""


def row_options(row: RawRow) -> list[RowOption]:
    """All consistent readings of a row, best first. Empty list = blank/unreadable row."""
    G = _aggregate(row.grade, grade_values)
    C = _aggregate(row.coef, coef_values)
    T = _aggregate(row.total, total_values)
    opts: dict[tuple, RowOption] = {}

    def add(g, c, t, score, status, note=""):
        key = (g, c, round(t, 2) if t is not None else None)
        if key not in opts or opts[key].score < score:
            opts[key] = RowOption(g, c, key[2], score, status, note)

    for (g, sg), (c, sc) in itertools.product(G.items(), C.items()):
        t_calc = round(g * c, 2)
        hit = next((t for t in T if abs(t - t_calc) < EPS), None)
        if hit is not None:
            add(g, int(c), t_calc, sg + sc + T[hit] + 3.0, OK)
        elif 0 <= t_calc <= 200:
            add(g, int(c), t_calc, sg + sc + 1.0, DERIVED, "total recomputed from grade x coefficient")
    for (g, sg), (t, st) in itertools.product(G.items(), T.items()):
        if g > 0 and abs(t / g - round(t / g)) < 1e-6 and 1 <= round(t / g) <= 9:
            c = int(round(t / g))
            add(g, c, t, sg + st + C.get(float(c), 0.0) + 2.0, OK if C.get(float(c)) else DERIVED,
                "" if C.get(float(c)) else "coefficient recomputed from total / grade")
    for (c, sc), (t, st) in itertools.product(C.items(), T.items()):
        g = round(t / c, 2)
        if 0 <= g <= 20 and abs(g * c - t) < EPS:
            add(round(g, 2), int(c), t, sc + st + G.get(round(g, 2), 0.0) + 2.0,
                OK if G.get(round(g, 2)) else DERIVED,
                "" if G.get(round(g, 2)) else "grade recomputed from total / coefficient")
    # last resort: a single readable cell
    if not opts:
        for c, sc in C.items():
            add(None, int(c), None, sc * 0.3, "conflict", "only the coefficient could be read")
        for g, sg in G.items():
            add(g, None, None, sg * 0.3, "conflict", "only the grade could be read")
    return sorted(opts.values(), key=lambda o: -o.score)


def is_blank(row: RawRow) -> bool:
    cells = (row.grade, row.coef, row.total)
    return all(c.empty or c.dash for c in cells) and not any(c.candidates for c in cells) \
        and not (row.coef.candidates)


def is_not_taken(row: RawRow) -> bool:
    """Optional subject that was not sat: coefficient printed, grade and total are '--'."""
    g_none = row.grade.dash or row.grade.empty
    t_none = row.total.dash or row.total.empty
    return g_none and t_none and bool(row.coef.candidates)


# ---------------------------------------------------------------------------
def parse_table(raw: RawTable, branch: Branch | None, vocab: Vocabulary | None = None):
    """-> (rows, overall_total, coef_sum, checks, notes)"""
    vocab = vocab or load_vocabulary()
    notes: list[str] = []
    order = branch.order if branch else []

    # 1) numeric solve per row -------------------------------------------------
    solved: list[tuple[RawRow, list[RowOption] | None, bool]] = []
    for r in raw.rows:
        if is_not_taken(r):
            solved.append((r, None, True))
            continue
        opts = row_options(r)
        if not opts and is_blank(r):
            continue
        solved.append((r, opts, False))

    # 2) reconcile with the printed totals --------------------------------------
    T_all = _aggregate(raw.total_row["total"], overall_total_values)
    S_all = _aggregate(raw.total_row["coef"], coefsum_values)
    T_all, S_all = _plausible_totals(solved, T_all, S_all)
    chosen: list[int] = [0] * len(solved)
    _reconcile(solved, chosen, T_all, S_all, notes)

    # 3) subject names ------------------------------------------------------------
    rows: list[GradeRow] = []
    used: set[str] = set()
    provisional = []
    for k, (r, opts, not_taken) in enumerate(solved):
        exp = order[r.index] if r.index < len(order) else None
        sub, score = match_subject(r.subject_text, vocab, expected_id=exp)
        provisional.append([r, sub, score, exp])
    # resolve duplicates: best score keeps the subject, others are re-matched without it
    for _ in range(3):
        by_id: dict[str, list[int]] = defaultdict(list)
        for i, (_, sub, score, _) in enumerate(provisional):
            if sub and score >= Config.SUBJECT_MATCH_THRESHOLD:
                by_id[sub.id].append(i)
        dup = False
        for sid, idxs in by_id.items():
            if len(idxs) > 1:
                dup = True
                idxs.sort(key=lambda i: -provisional[i][2])
                for i in idxs[1:]:
                    r, _, _, exp = provisional[i]
                    others = [s for s in vocab.subjects if s != sid]
                    provisional[i][1], provisional[i][2] = match_subject(r.subject_text, vocab, others, exp)
        if not dup:
            break
    matched_ids = {p[1].id for p in provisional if p[1] and p[2] >= Config.SUBJECT_MATCH_THRESHOLD}

    for k, ((r, opts, not_taken), (_, sub, score, exp)) in enumerate(zip(solved, provisional)):
        row_notes: list[str] = []
        name = sub.name_ar if sub and score >= Config.SUBJECT_MATCH_THRESHOLD else None
        if name is None and exp and exp not in matched_ids:
            # position + elimination: the only expected subject nobody claimed
            name = vocab.subjects[exp].name_ar
            matched_ids.add(exp)
            score = 50.0
            row_notes.append("subject inferred from branch row order")
        if name is None:
            row_notes.append("subject name not recognised")
        if not_taken:
            coef_c = _aggregate(r.coef, coef_values)
            coef = int(max(coef_c, key=coef_c.get)) if coef_c else None
            if coef is not None and coef_c[float(coef)] < 0.99:   # the reads disagreed
                row_notes.append("coefficient read unreliably")
                coef = None
            rows.append(GradeRow(k, name, r.subject_text, round(score, 1), None, coef, None,
                                 "not_taken", row_notes + ["optional subject not taken"]))
            continue
        opt = opts[chosen[k]] if opts else None
        if name is None and (opt is None or opt.status == "conflict"):
            continue  # ruled placeholder row ('-'): nothing to report
        if opt is None:
            rows.append(GradeRow(k, name, r.subject_text, round(score, 1), None, None, None,
                                 "unreadable", row_notes + ["numbers unreadable"]))
            continue
        if opt.note:
            row_notes.append(opt.note)
        rows.append(GradeRow(k, name, r.subject_text, round(score, 1), opt.grade, opt.coef, opt.total,
                             opt.status, row_notes))

    # 4) final totals --------------------------------------------------------------
    sum_total = round(sum(r.total or 0 for r in rows), 2)
    sum_coef = sum(r.coefficient or 0 for r in rows if r.status != "not_taken")
    overall = _pick(T_all, sum_total)
    coefsum = _pick(S_all, sum_coef)
    checks = _checks(rows, sum_total, sum_coef, overall, coefsum)
    return rows, overall, coefsum, checks, notes


COEF_SUM_RANGE = (15, 60)     # every BAC branch sums to roughly 25-35
PASS_RATIO = (9.5, 20.0)      # transcripts are issued to graduates: average >= ~10, <= 20


def _plausible_totals(solved, T_all, S_all):
    """Drop OCR reads of the totals row that cannot be right (a passing student's total is
    between ~9.5x and 20x the coefficient sum). Rows are read many times; a single totals
    cell is not, so an implausible cell must not override them."""
    S_ok = {v: w for v, w in S_all.items() if COEF_SUM_RANGE[0] <= v <= COEF_SUM_RANGE[1]}
    row_coefs = sum((opts[0].coef or 0) for r, opts, nt in solved if opts)
    s_ref = max(S_ok, key=S_ok.get) if S_ok else row_coefs
    T_ok = T_all
    if s_ref:
        T_ok = {v: w for v, w in T_all.items() if PASS_RATIO[0] * s_ref <= v <= PASS_RATIO[1] * s_ref + EPS}
    return T_ok, S_ok


def _pick(cands: dict[float, float], computed: float):
    """Prefer an OCR candidate that equals the computed value (=confirmed)."""
    for v in cands:
        if abs(v - computed) < EPS:
            return v, "confirmed"
    if cands:
        return max(cands, key=cands.get), "ocr_only"
    return computed, "computed"


def _reconcile(solved, chosen, T_all, S_all, notes):
    """If the row totals do not add up to the printed overall total / coefficient sum,
    try changing a single row to its next-best option (then a pair) to make them agree."""
    if not T_all:
        return

    def sums():
        st = 0.0
        sc = 0
        for (r, opts, nt), ci in zip(solved, chosen):
            if opts:
                o = opts[ci]
                st += o.total or 0
                sc += o.coef or 0
            elif nt:
                pass
        return round(st, 2), sc

    def ok(st, sc):
        t_ok = any(abs(t - st) < EPS for t in T_all)
        s_ok = (not S_all) or any(abs(v - sc) < EPS for v in S_all)
        return t_ok and s_ok

    st, sc = sums()
    if ok(st, sc):
        return
    best = None
    idx = [i for i, (r, opts, nt) in enumerate(solved) if opts and len(opts) > 1]
    for combo_size in (1, 2):
        for combo in itertools.combinations(idx, combo_size):
            ranges = [range(1, min(4, len(solved[i][1]))) for i in combo]
            for picks in itertools.product(*ranges):
                trial = list(chosen)
                for i, p in zip(combo, picks):
                    trial[i] = p
                old = chosen[:]
                chosen[:] = trial
                st2, sc2 = sums()
                chosen[:] = old
                if ok(st2, sc2):
                    loss = sum(solved[i][1][0].score - solved[i][1][p].score for i, p in zip(combo, picks))
                    if best is None or loss < best[0]:
                        best = (loss, trial)
        if best:
            break
    if best:
        chosen[:] = best[1]
        notes.append("row values adjusted so that the rows add up to the printed overall total")


def _checks(rows, sum_total, sum_coef, overall, coefsum) -> list[Check]:
    checks: list[Check] = []
    bad = [r.index for r in rows if r.grade is not None and r.coefficient and r.total is not None
           and abs(r.grade * r.coefficient - r.total) > EPS]
    checks.append(Check("row_total = grade x coefficient", not bad,
                        "all rows consistent" if not bad else f"inconsistent rows: {bad}"))
    o_val, o_how = overall
    checks.append(Check("sum(row totals) = overall total", abs(sum_total - o_val) < EPS,
                        f"rows sum to {sum_total:.2f}, overall total {o_val:.2f} ({o_how})"))
    c_val, c_how = coefsum
    checks.append(Check("sum(coefficients) = coefficient total", abs(sum_coef - c_val) < EPS,
                        f"coefficients sum to {sum_coef}, printed {c_val:g} ({c_how})"))
    return checks
