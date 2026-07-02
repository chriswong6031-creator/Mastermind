"""Tests for portfolio.risk_sizing — vol-managed sizing overlay (graceful, additive)."""
from __future__ import annotations

from portfolio import risk_sizing as rs


def _seed(mult, gross=1.0, state="neutral"):
    rs.reset_cache()
    rs._CACHE["mult"] = dict(mult)
    rs._CACHE["regime"] = {"gross_mult": gross, "state": state}


def test_apply_reweights_by_inverse_vol_and_preserves_budget():
    _seed({"A": 1.6, "B": 0.5, "C": 1.0})
    pos = [{"ticker": "A", "weight": 0.1}, {"ticker": "B", "weight": 0.1},
           {"ticker": "C", "weight": 0.1}]
    rs.apply(pos, budget=0.30, name_cap=0.20)
    w = {p["ticker"]: p["weight"] for p in pos}
    assert w["A"] > w["C"] > w["B"]                       # calm > neutral > wild
    assert abs(sum(w.values()) - 0.30) < 1e-6            # renormalized to budget


def test_lean_out_regime_holds_cash():
    # NEW-SIZE-1: the incoming book is only 0.2 gross (2 x 0.1). PRE-FIX, renorm scaled the book UP
    # to `budget * gross` = 0.30 * 0.75 = 0.225 — INFLATING a 0.2 book past what arrived (the
    # haircut-erasure bug). POST-FIX the target is capped at incoming gross: min(0.2, 0.30) * 0.75 =
    # 0.15, so the de-gross dial trims the actual 0.2 book, never inflates it.
    _seed({"A": 1.0, "B": 1.0}, gross=0.75, state="lean_out")
    pos = [{"ticker": "A", "weight": 0.1}, {"ticker": "B", "weight": 0.1}]
    rs.apply(pos, budget=0.30, name_cap=0.5)
    assert abs(sum(p["weight"] for p in pos) - 0.15) < 1e-6    # min(incoming=0.2, budget=0.30) * 0.75


def test_lean_in_does_not_lever_above_budget():
    _seed({"A": 1.0}, gross=1.2, state="lean_in")
    pos = [{"ticker": "A", "weight": 0.1}]
    rs.apply(pos, budget=0.10, name_cap=0.5)
    assert pos[0]["weight"] <= 0.10 + 1e-9               # full budget, never levered


def test_name_cap_respected():
    _seed({"A": 1.6, "B": 0.1})
    pos = [{"ticker": "A", "weight": 0.1}, {"ticker": "B", "weight": 0.1}]
    rs.apply(pos, budget=0.50, name_cap=0.08)
    assert all(p["weight"] <= 0.08 + 1e-9 for p in pos)


def test_missing_field_is_neutral():
    _seed({})                                            # no macro field => everything 1.0
    assert rs.vol_mult("ZZZZ") == 1.0
    assert rs.selection_gross() == 1.0
    pos = [{"ticker": "X", "weight": 0.1}, {"ticker": "Y", "weight": 0.1}]
    rs.apply(pos, budget=0.20, name_cap=0.5)
    assert abs(pos[0]["weight"] - pos[1]["weight"]) < 1e-9    # unchanged relative sizing


def test_empty_or_zero_budget_is_safe():
    _seed({"A": 1.5})
    assert rs.apply([], budget=0.3) == []
    pos = [{"ticker": "A", "weight": 0.1}]
    assert rs.apply(pos, budget=0.0) is pos


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# NEW-SIZE-1 — renorm is SCALE-DOWN-ONLY: a post-haircut book must never be re-inflated to budget.
# ─────────────────────────────────────────────────────────────────────────────────────────────────

def test_new_size_1_renorm_preserves_upstream_haircut():
    """A 3-name book that arrived at 0.0315 gross (post-0.7 initial-size haircut) must STAY ~0.0315,
    not be renormalised back up to budget.

    PRE-FIX: target = budget * selection_gross() = 0.30 * 1.0 = 0.30, and w = raw/tot*target
    renormalised the three equal post-haircut weights back up to sum ~0.30 (0.08 name_cap each),
    mathematically ERASING the *0.7 catalyst haircut. POST-FIX: target = min(incoming_gross=0.0315,
    budget=0.30) * 1.0 = 0.0315, so the haircut survives.
    """
    _seed({})                                                # all off-board -> but injected fn below
    pos = [{"ticker": "A", "weight": 0.0105}, {"ticker": "B", "weight": 0.0105},
           {"ticker": "C", "weight": 0.0105}]                # 3 x 0.0105 = 0.0315 incoming gross
    # inject a no-series price fn so off-board names degrade to neutral 1.0 (isolates the renorm math)
    rs.apply(pos, budget=0.30, name_cap=0.08, price_series_fn=lambda t: None)
    total = sum(p["weight"] for p in pos)
    assert abs(total - 0.0315) < 1e-4                         # stayed at incoming gross, NOT 0.24/0.30
    assert all(p["weight"] < 0.08 - 1e-9 for p in pos)        # no name pinned to name_cap
    # and vol_mult neutral (no series) keeps it equal-weight within the preserved gross
    assert max(p["weight"] for p in pos) - min(p["weight"] for p in pos) < 1e-9


def test_new_size_1_over_budget_book_is_scaled_down():
    """The down-scaling path still fits budget: a genuinely over-budget book (sum > budget) IS scaled
    down to budget, so NEW-SIZE-1 only removes the UP-inflation, never the DOWN-fit."""
    _seed({})
    pos = [{"ticker": "A", "weight": 0.30}, {"ticker": "B", "weight": 0.30},
           {"ticker": "C", "weight": 0.30}]                  # 0.90 incoming, well over budget
    rs.apply(pos, budget=0.30, name_cap=0.20, price_series_fn=lambda t: None)
    total = sum(p["weight"] for p in pos)
    assert abs(total - 0.30) < 1e-4                           # scaled DOWN to fit budget


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# NEW-SIZE-2 — off-board names: coverage diagnostic + on-the-fly inverse-vol fallback (not silent 1.0)
# ─────────────────────────────────────────────────────────────────────────────────────────────────

def _series(vol: float, n: int = 120):
    """A synthetic close series with a target per-step std ~= `vol` (deterministic, fixture-injected —
    never touches live data). Built as a cumulative product of alternating +/- vol steps."""
    import pandas as pd
    idx = pd.date_range("2024-01-01", periods=n, freq="D")
    closes = [100.0]
    for i in range(1, n):
        step = vol if (i % 2 == 0) else -vol
        closes.append(closes[-1] * (1.0 + step))
    return pd.Series(closes, index=idx)


class _Book(list):
    """Minimal _SizedBook stand-in that carries data_health so diagnostics have a place to land."""
    data_health: dict | None = None


def test_new_size_2_coverage_diagnostic_fires_on_disjoint_book():
    """When the book is fully off-board (coverage < 0.5), a board_coverage_low diagnostic must be
    emitted onto the _SizedBook.data_health — the inert primary lever is VISIBLE, not laundered."""
    _seed({"AAPL": 1.2, "ATO": 0.9})                         # board carries defensives; book is disjoint
    book = _Book([{"ticker": "NVDA", "weight": 0.05}, {"ticker": "AMD", "weight": 0.05},
                  {"ticker": "SMCI", "weight": 0.05}])
    book.data_health = {}
    rs.apply(book, budget=0.30, name_cap=0.08, price_series_fn=lambda t: None)
    diags = book.data_health.get("risk_sizing_diagnostics", [])
    kinds = {d["kind"] for d in diags}
    assert "board_coverage_low" in kinds
    cov = next(d for d in diags if d["kind"] == "board_coverage_low")
    assert cov["coverage"] == 0.0 and cov["n_total"] == 3


def test_new_size_2_off_board_high_vol_name_shrinks_not_neutral():
    """An off-board name with a supplied HIGH-vol series gets a size_mult < 1.0 (inverse-vol), NOT the
    silent neutral 1.0 (the pre-fix behaviour). A calmer off-board name gets a larger multiplier."""
    _seed({})
    series = {"WILD": _series(0.05), "CALM": _series(0.005)}
    book = [{"ticker": "WILD", "weight": 0.05}, {"ticker": "CALM", "weight": 0.05}]
    rs.apply(book, budget=0.30, name_cap=0.20, price_series_fn=lambda t: series.get(t))
    mults = {p["ticker"]: p["vol_mult"] for p in book}
    assert mults["WILD"] < 1.0                               # high vol -> shrunk below neutral
    assert mults["CALM"] > mults["WILD"]                     # calmer name sized larger (inverse vol)
    # the wilder name ends up with LESS weight than the calm one (risk redistribution within budget)
    w = {p["ticker"]: p["weight"] for p in book}
    assert w["CALM"] > w["WILD"]


def test_new_size_2_off_board_no_series_degrades_to_exactly_one():
    """An off-board name with NO price series degrades to EXACTLY 1.0 — the true fail-closed neutral
    (today's behaviour), matching the invariant (no data -> no lever, never a fabricated one)."""
    _seed({})
    book = [{"ticker": "OPAQUE", "weight": 0.05}, {"ticker": "ALSO", "weight": 0.05}]
    rs.apply(book, budget=0.30, name_cap=0.20, price_series_fn=lambda t: None)
    assert all(p["vol_mult"] == 1.0 for p in book)


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# NEW-SIZE-3 — degenerate equal-weight probe + fallback restores dispersion (no synthetic dispersion)
# ─────────────────────────────────────────────────────────────────────────────────────────────────

def test_new_size_3_degenerate_equal_weight_diagnostic_fires():
    """> 80% identical input weight AND all-off-board (vol_mult dispersion ~0) => a
    'degenerate_equal_weight' diagnostic fires (the book is running equal-weight, surfaced not
    corrected). Reproduces the RC3 uniform-confluence x inert-lever collapse."""
    _seed({})
    book = _Book([{"ticker": f"T{i}", "weight": 0.0153} for i in range(20)])
    book.data_health = {}
    rs.apply(book, budget=0.30, name_cap=0.08, price_series_fn=lambda t: None)
    diags = book.data_health.get("risk_sizing_diagnostics", [])
    assert any(d["kind"] == "degenerate_equal_weight" for d in diags)
    # PRE-FIX pathology confirmation: with no vol lever the output is pure equal weight (CV ~ 0).
    ws = [p["weight"] for p in book]
    mean = sum(ws) / len(ws)
    var = sum((w - mean) ** 2 for w in ws) / len(ws)
    cv = (var ** 0.5) / mean
    assert cv < 1e-6                                          # equal-weight collapse, as documented


def test_new_size_3_fallback_restores_dispersion_cv_over_point_one():
    """After the NEW-SIZE-2 fallback with varied injected vols, the resulting weight vector has real
    dispersion (CV > 0.1) even under uniform confluence — the honest remedy (restore the real vol
    lever) rather than synthesising conviction."""
    _seed({})
    import random
    random.seed(7)
    vols = {f"T{i}": _series(0.004 + 0.03 * random.random()) for i in range(20)}
    book = _Book([{"ticker": f"T{i}", "weight": 0.0153} for i in range(20)])
    book.data_health = {}
    rs.apply(book, budget=0.30, name_cap=0.08, price_series_fn=lambda t: vols.get(t))
    ws = [p["weight"] for p in book]
    mean = sum(ws) / len(ws)
    var = sum((w - mean) ** 2 for w in ws) / len(ws)
    cv = (var ** 0.5) / mean
    assert cv > 0.1                                           # real vol dispersion restored


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# NEW-SIZE-4 — book-cap re-asserted AFTER the research size_mult multiply (phase2 composition)
# ─────────────────────────────────────────────────────────────────────────────────────────────────

def test_new_size_4_sector_cap_reasserted_after_return_multiply(monkeypatch):
    """A single-sector 2-name book sitting AT the sector cap, then multiplied by size_mult=1.3 (the
    research RETURN score), breaches SECTOR_MAX_FRACTION*budget — until the FINAL _apply_sector_cap
    re-cap runs (exactly what phase2 now does after the confirmed_sized loop).

    PRE-FIX: phase2 multiplied conviction.build's already-capped weight by size_mult with no re-cap,
    so the sector landed at 0.16 vs the 0.15 cap (0.075 * 1.3 * 2 = 0.195 pre-name-cap; name_cap 0.08
    each -> 0.16). POST-FIX: the re-cap scales the sector back to <= 0.15.
    """
    from portfolio import conviction
    budget = 0.30
    cap = conviction.SECTOR_MAX_FRACTION * budget            # 0.50 * 0.30 = 0.15
    # force both names into ONE real sector so the cap engages (synthetic tickers would be 'Unknown').
    monkeypatch.setattr(conviction, "_sector_of", lambda t: "XLK")
    # book sitting exactly at the cap (2 names, 0.075 each = 0.15), size_mult 1.3 applied like phase2.
    book = [{"ticker": "AAA", "weight": 0.075}, {"ticker": "BBB", "weight": 0.075}]
    name_cap = 0.08
    for p in book:
        p["weight"] = round(min(name_cap, p["weight"] * 1.3), 4)   # mirror phase2:511 multiply
    sector_pre = sum(p["weight"] for p in book)
    assert sector_pre > cap + 1e-9                            # PRE-FIX: breaches (0.16 > 0.15)
    conviction._apply_sector_cap(book, budget)               # the NEW-SIZE-4 final re-cap
    sector_post = sum(p["weight"] for p in book)
    assert sector_post <= cap + 1e-9                          # POST-FIX: back at/under the sector cap


def test_new_size_4_recap_is_noop_on_common_case(monkeypatch):
    """A book with all size_mult <= 1.0 stays under its sector cap, so the re-cap does NOT churn it
    (no weight change on the common case)."""
    from portfolio import conviction
    budget = 0.30
    monkeypatch.setattr(conviction, "_sector_of", lambda t: "XLK")
    book = [{"ticker": "AAA", "weight": 0.05}, {"ticker": "BBB", "weight": 0.05}]  # 0.10 < 0.15 cap
    for p in book:
        p["weight"] = round(min(0.08, p["weight"] * 0.9), 4)  # size_mult <= 1.0
    before = [p["weight"] for p in book]
    conviction._apply_sector_cap(book, budget)
    after = [p["weight"] for p in book]
    assert before == after                                    # untouched — no churn
