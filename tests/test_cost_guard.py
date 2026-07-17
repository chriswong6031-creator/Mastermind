"""Guards for the nightly Opus-cost tripwire (brain/cost_guard).

The safety contract under test:
  * ACCUMULATES — record() adds to a (book, asof) running total; multiple records sum;
  * OFF BY DEFAULT — with no cap configured (env unset / <= 0) over_budget is ALWAYS False, so
    the desk is byte-identical to today no matter how much was recorded;
  * GATES PAST THE CAP — with a cap set, over_budget flips True once spend reaches/exceeds it;
  * SUMMARY SHAPE — summary() reports cap/enabled/total/per-book spent+over;
  * NEVER RAISES — missing data, garbage costs, and bad input all degrade to safe defaults;
  * the gating helper returns the engine fallback when over budget (judgment_book.build).
  * ESTIMATE_COST model normalizer — provider prefixes, bracket/colon suffixes, longest-prefix
    match, and unknown/None/empty models all return 0.0.
"""
from __future__ import annotations

import importlib

import pytest


CAP_ENV = "MASTERMIND_NIGHTLY_USD_CAP"


@pytest.fixture()
def guard(tmp_path, monkeypatch):
    """A fresh cost_guard whose ledger dir is redirected into tmp_path, with the cap env cleared
    (OFF) by default. Tests opt INTO a cap via monkeypatch.setenv."""
    monkeypatch.delenv(CAP_ENV, raising=False)
    import brain.cost_guard as cg
    cg = importlib.reload(cg)
    monkeypatch.setattr(cg, "_DIR", tmp_path / "cost")
    return cg


# ── accumulation ─────────────────────────────────────────────────────────────
def test_record_accumulates(guard):
    assert guard.spent("autonomous", "2026-06-23") == 0.0
    guard.record("autonomous", 0.75, "2026-06-23")
    guard.record("autonomous", 0.50, "2026-06-23")
    assert guard.spent("autonomous", "2026-06-23") == pytest.approx(1.25)


def test_record_is_per_book_and_per_date(guard):
    guard.record("autonomous", 1.0, "2026-06-23")
    guard.record("china", 2.0, "2026-06-23")
    guard.record("autonomous", 1.0, "2026-06-24")
    assert guard.spent("autonomous", "2026-06-23") == pytest.approx(1.0)
    assert guard.spent("china", "2026-06-23") == pytest.approx(2.0)
    assert guard.spent("autonomous", "2026-06-24") == pytest.approx(1.0)


def test_record_ignores_none_and_nonpositive_and_garbage(guard):
    guard.record("autonomous", None, "2026-06-23")
    guard.record("autonomous", 0.0, "2026-06-23")
    guard.record("autonomous", -5.0, "2026-06-23")
    guard.record("autonomous", "not-a-number", "2026-06-23")
    assert guard.spent("autonomous", "2026-06-23") == 0.0


# ── OFF by default (the byte-identical invariant) ────────────────────────────
def test_cap_default_off(guard):
    assert guard.cap() == 0.0


def test_over_budget_false_when_off_even_after_huge_spend(guard):
    guard.record("autonomous", 1000.0, "2026-06-23")
    # cap is OFF (unset) → never over budget, no seat ever skipped → today's behaviour
    assert guard.over_budget("autonomous", "2026-06-23") is False


def test_cap_le_zero_treated_as_off(guard, monkeypatch):
    monkeypatch.setenv(CAP_ENV, "0")
    guard.record("autonomous", 50.0, "2026-06-23")
    assert guard.over_budget("autonomous", "2026-06-23") is False
    monkeypatch.setenv(CAP_ENV, "-3")
    assert guard.over_budget("autonomous", "2026-06-23") is False


# ── gates once the cap is armed ──────────────────────────────────────────────
def test_over_budget_true_past_cap(guard, monkeypatch):
    monkeypatch.setenv(CAP_ENV, "2.0")
    guard.record("autonomous", 1.0, "2026-06-23")
    assert guard.over_budget("autonomous", "2026-06-23") is False   # under
    guard.record("autonomous", 1.0, "2026-06-23")
    assert guard.over_budget("autonomous", "2026-06-23") is True    # at the cap (>=)
    # a different book under the same cap is independent
    assert guard.over_budget("china", "2026-06-23") is False


def test_garbage_cap_env_is_off(guard, monkeypatch):
    monkeypatch.setenv(CAP_ENV, "not-a-float")
    guard.record("autonomous", 100.0, "2026-06-23")
    assert guard.cap() == 0.0
    assert guard.over_budget("autonomous", "2026-06-23") is False


# ── summary shape ────────────────────────────────────────────────────────────
def test_summary_shape_off(guard):
    guard.record("autonomous", 1.5, "2026-06-23")
    guard.record("china", 0.5, "2026-06-23")
    s = guard.summary("2026-06-23")
    assert s["asof"] == "2026-06-23"
    assert s["cap"] == 0.0
    assert s["enabled"] is False
    assert s["total"] == pytest.approx(2.0)
    assert s["books"]["autonomous"]["spent"] == pytest.approx(1.5)
    # OFF → nothing is ever "over"
    assert s["books"]["autonomous"]["over"] is False
    assert s["books"]["china"]["over"] is False


def test_summary_shape_armed(guard, monkeypatch):
    monkeypatch.setenv(CAP_ENV, "1.0")
    guard.record("autonomous", 1.5, "2026-06-23")
    guard.record("china", 0.5, "2026-06-23")
    s = guard.summary("2026-06-23")
    assert s["enabled"] is True
    assert s["cap"] == pytest.approx(1.0)
    assert s["books"]["autonomous"]["over"] is True     # 1.5 >= 1.0
    assert s["books"]["china"]["over"] is False         # 0.5 < 1.0


# ── never raises on missing / unknown data ───────────────────────────────────
def test_never_raises_on_missing(guard):
    # no ledger file written yet
    assert guard.spent("nope", "2099-01-01") == 0.0
    assert guard.over_budget("nope", "2099-01-01") is False
    s = guard.summary("2099-01-01")
    assert s["books"] == {}
    assert s["total"] == 0.0


def test_spent_and_summary_default_asof_today(guard):
    # asof=None resolves to today; must not raise and must round-trip
    guard.record("autonomous", 0.9)
    assert guard.spent("autonomous") == pytest.approx(0.9)
    assert guard.summary()["total"] == pytest.approx(0.9)


# ── estimate_cost model normalizer ───────────────────────────────────────────

def test_estimate_cost_empty_string_returns_zero():
    import brain.cost_guard as cg
    assert cg.estimate_cost("", 1_000_000, 1_000_000) == 0.0


def test_estimate_cost_none_returns_zero():
    import brain.cost_guard as cg
    assert cg.estimate_cost(None, 1_000_000, 1_000_000) == 0.0


def test_estimate_cost_exact_key():
    import brain.cost_guard as cg
    # claude-opus-4-8: $5/MTok in, $25/MTok out = $30/MTok combined
    cost = cg.estimate_cost("claude-opus-4-8", 1_000_000, 1_000_000)
    assert cost == pytest.approx(30.0)


def test_estimate_cost_bracket_suffix_stripped():
    """'claude-opus-4-8[1m]' should resolve identically to 'claude-opus-4-8'."""
    import brain.cost_guard as cg
    cost_plain = cg.estimate_cost("claude-opus-4-8", 1_000_000, 0)
    cost_bracket = cg.estimate_cost("claude-opus-4-8[1m]", 1_000_000, 0)
    assert cost_bracket == pytest.approx(cost_plain)
    assert cost_bracket > 0.0


def test_estimate_cost_provider_prefix_stripped():
    """'us.anthropic.claude-opus-4-8' should resolve identically to 'claude-opus-4-8'."""
    import brain.cost_guard as cg
    cost_plain = cg.estimate_cost("claude-opus-4-8", 1_000_000, 0)
    cost_prefix = cg.estimate_cost("us.anthropic.claude-opus-4-8", 1_000_000, 0)
    assert cost_prefix == pytest.approx(cost_plain)
    assert cost_prefix > 0.0


def test_estimate_cost_date_suffix_longest_prefix_match():
    """'claude-sonnet-4-6-20990101' (date-versioned) maps to 'claude-sonnet-4-6' via longest prefix."""
    import brain.cost_guard as cg
    cost_plain = cg.estimate_cost("claude-sonnet-4-6", 1_000_000, 0)
    cost_dated = cg.estimate_cost("claude-sonnet-4-6-20990101", 1_000_000, 0)
    assert cost_dated == pytest.approx(cost_plain)
    assert cost_dated > 0.0


def test_estimate_cost_unknown_model_returns_zero():
    """An unknown model id returns 0.0 (never falls through to a default rate)."""
    import brain.cost_guard as cg
    assert cg.estimate_cost("gpt-x", 1_000_000, 1_000_000) == 0.0


# ── the gating helper falls back to the engine book when over budget ──────────
def test_judgment_build_falls_back_when_over_budget(monkeypatch):
    """brain.judgment_book.build must return the input engine ``sized`` UNCHANGED when the book is
    over its nightly cost cap — without ever invoking the expensive PM/committee seats."""
    monkeypatch.setenv("MASTERMIND_FLAGSHIP_JUDGMENT", "1")     # arm the judgment path
    monkeypatch.setenv(CAP_ENV, "1.0")

    from brain import judgment_book, cost_guard
    # force "flagship" over budget for this asof
    monkeypatch.setattr(cost_guard, "over_budget",
                        lambda book, asof=None: book == "flagship")
    # if the gate failed, build() would import these seats — make that loud
    import brain.pm_conviction as pmc
    monkeypatch.setattr(pmc, "build_book",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("PM seat ran while over budget")))

    sized = [{"ticker": "AAPL", "weight": 0.05}]
    out = judgment_book.build(sized, [], regime={}, asof="2026-06-23",
                              gate_info={}, shadow_inputs=None)
    assert out is sized      # byte-identical engine fallback, PM seat never touched
