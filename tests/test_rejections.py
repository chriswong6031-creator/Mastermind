"""Guards for the off-policy REJECTION log (portfolio.rejections) — the desk's negative space.

Safety-critical properties:
  * every rejected name (conviction veto / research hold / committee drop / timing withhold) is logged
    once, deduped while open, and isolated to the sandbox (never the prod ledger);
  * the forward grader is leakage-free (delegates to brain.outcomes.label_thesis) and resolves matured
    rows; a carried day (no new items) still grades the open ones;
  * reject stages are classified from the reason; the veto-regret scorecard counts rejects that beat SPY;
  * the selection propensity is 0.0 under the deterministic policy and only positive for BORDERLINE
    soft-rejects when ε-exploration is explicitly armed (hard vetoes are never explored);
  * nothing raises on garbage / empty.
"""
from __future__ import annotations

import pytest

from portfolio import rejections as R


@pytest.fixture()
def sandbox(tmp_path, monkeypatch):
    monkeypatch.setattr(R, "_DIR", tmp_path)
    monkeypatch.setattr(R, "_LEDGER", tmp_path / "ledger.jsonl")
    # default: exploration OFF (clear any ambient env)
    monkeypatch.delenv("MASTERMIND_SELECTION_EXPLORE", raising=False)
    monkeypatch.delenv("MASTERMIND_EXPLORE_EPS", raising=False)
    return tmp_path


def _veto(ticker, **kw):
    base = {"ticker": ticker, "reason": "Vetoed: parabolic", "vetoes": ["parabolic"],
            "bear": [], "confluence": -0.2}
    base.update(kw)
    return base


def _held(ticker, reason, **kw):
    base = {"ticker": ticker, "reason": reason, "combined": 62, "confluence": 0.3, "viability": "marginal"}
    base.update(kw)
    return base


def _no_grade(monkeypatch):
    monkeypatch.setattr("brain.outcomes.label_thesis", lambda t, asof=None: {"resolved": False, "rel_return": None})


# ── stage classification ────────────────────────────────────────────────────────
def test_held_stage_classification():
    assert R._held_stage("timing withhold: extended") == "timing_withhold"
    assert R._held_stage("committee: bear case dominates") == "committee_drop"
    assert R._held_stage("Insufficient research conviction") == "research_hold"


# ── record: open + dedup + isolate ───────────────────────────────────────────────
def test_record_logs_rejected_and_held_deduped(sandbox, monkeypatch):
    _no_grade(monkeypatch)
    rejected = [_veto("PARA")]
    held = [_held("AAA", "Insufficient research conviction"),
            _held("BBB", "committee: bear case dominates"),
            _held("CCC", "timing withhold: extended +30% vs 200dma")]
    R.record("2026-06-01", rejected=rejected, held=held)
    R.record("2026-06-02", rejected=rejected, held=held)   # all still open → no duplicates
    led = R._load_ledger()
    assert len(led) == 4
    stages = {r["ticker"]: r["stage"] for r in led}
    assert stages == {"PARA": "conviction_veto", "AAA": "research_hold",
                      "BBB": "committee_drop", "CCC": "timing_withhold"}
    assert (sandbox / "ledger.jsonl").exists()


def test_record_resolves_matured_forward(sandbox, monkeypatch):
    # AAA rejected; the tape says it BEAT SPY (+8%) → a false negative / veto-regret
    monkeypatch.setattr("brain.outcomes.label_thesis",
                        lambda t, asof=None: {"resolved": True, "rel_return": 0.08})
    R.record("2026-06-01", held=[_held("AAA", "Insufficient research conviction")])
    led = R._load_ledger()
    aaa = [r for r in led if r["ticker"] == "AAA"][0]
    assert aaa["status"] == "resolved" and aaa["realized"] == 0.08


def test_carried_day_grades_open_without_new_items(sandbox, monkeypatch):
    # open a row while unresolved, then a carried day (no items) resolves it forward
    seq = iter([{"resolved": False, "rel_return": None}, {"resolved": True, "rel_return": -0.04}])
    monkeypatch.setattr("brain.outcomes.label_thesis", lambda t, asof=None: next(seq))
    R.record("2026-06-01", held=[_held("AAA", "Insufficient research conviction")])
    assert R._load_ledger()[0]["status"] == "open"
    R.record("2026-06-20")                                  # carried day — no new items, just grade
    assert R._load_ledger()[0]["status"] == "resolved"


def test_record_never_raises_on_garbage(sandbox, monkeypatch):
    _no_grade(monkeypatch)
    for bad in (None, [], [{}], [{"ticker": ""}], "notalist"):
        R.record("2026-06-01", rejected=bad, held=bad)     # must not raise


# ── propensity / exploration flag ─────────────────────────────────────────────────
def test_propensity_zero_when_deterministic(sandbox, monkeypatch):
    _no_grade(monkeypatch)
    R.record("2026-06-01", rejected=[_veto("PARA")],
             held=[_held("AAA", "Insufficient research conviction")])
    led = {r["ticker"]: r for r in R._load_ledger()}
    assert led["PARA"]["propensity"] == 0.0 and led["PARA"]["policy"] == "deterministic"
    assert led["AAA"]["propensity"] == 0.0


def test_propensity_positive_for_borderline_when_armed(sandbox, monkeypatch):
    _no_grade(monkeypatch)
    monkeypatch.setenv("MASTERMIND_SELECTION_EXPLORE", "1")
    monkeypatch.setenv("MASTERMIND_EXPLORE_EPS", "0.05")
    R.record("2026-06-01", rejected=[_veto("PARA")],                       # hard veto → 0
             held=[_held("CMT", "committee: bear case dominates"),         # borderline → eps
                   _held("RH", "Insufficient research conviction")])       # research hold → 0
    led = {r["ticker"]: r for r in R._load_ledger()}
    assert led["CMT"]["propensity"] == 0.05 and led["CMT"]["policy"] == "epsilon_greedy"
    assert led["RH"]["propensity"] == 0.0          # research holds are not borderline-explorable
    assert led["PARA"]["propensity"] == 0.0        # conviction vetoes are never explored


# ── explore_buy (deterministic, flag- + stage-gated) ──────────────────────────────
def test_draw_is_deterministic_and_bounded():
    assert R._draw("AAA", "2026-06-01") == R._draw("AAA", "2026-06-01")
    assert 0.0 <= R._draw("AAA", "2026-06-01") < 1.0
    assert R._draw("AAA", "2026-06-01") != R._draw("BBB", "2026-06-01")   # varies by ticker


def test_explore_buy_off_by_default(sandbox, monkeypatch):
    monkeypatch.delenv("MASTERMIND_SELECTION_EXPLORE", raising=False)
    monkeypatch.setattr(R, "_draw", lambda tk, asof: 0.0)                 # would explore IF armed
    assert R.explore_buy("AAA", "2026-06-01", "committee_drop") is False  # disarmed → never


def test_explore_buy_stage_gated_when_armed(sandbox, monkeypatch):
    monkeypatch.setenv("MASTERMIND_SELECTION_EXPLORE", "1")
    monkeypatch.setenv("MASTERMIND_EXPLORE_EPS", "0.05")
    monkeypatch.setattr(R, "_draw", lambda tk, asof: 0.0)                 # draw < eps → explore
    assert R.explore_buy("AAA", "2026-06-01", "committee_drop") is True
    assert R.explore_buy("AAA", "2026-06-01", "timing_withhold") is True
    assert R.explore_buy("AAA", "2026-06-01", "research_hold") is False   # not borderline
    assert R.explore_buy("AAA", "2026-06-01", "conviction_veto") is False # hard veto never explored
    monkeypatch.setattr(R, "_draw", lambda tk, asof: 0.99)               # draw > eps → no explore
    assert R.explore_buy("AAA", "2026-06-01", "committee_drop") is False


def test_explore_weight_clears_new_position_floor():
    # explored buys must be >= master's MASTERMIND_MIN_POSITION_FRAC (0.5%) or paper_account drops them
    assert R._explore_weight() >= 0.005


def test_explored_buy_logged_and_scored_separately(sandbox, monkeypatch):
    monkeypatch.setenv("MASTERMIND_SELECTION_EXPLORE", "1")
    monkeypatch.setenv("MASTERMIND_EXPLORE_EPS", "0.05")
    rels = {"AAA": 0.06, "BBB": 0.04}   # both beat SPY: AAA was explore-bought, BBB was rejected
    monkeypatch.setattr("brain.outcomes.label_thesis",
                        lambda t, asof=None: {"resolved": True, "rel_return": rels[t["subject"]]})
    R.record("2026-06-01",
             held=[_held("BBB", "committee: bear")],
             explored=[{"ticker": "AAA", "stage": "committee_drop", "reason": "bear", "combined": 70}])
    led = {r["ticker"]: r for r in R._load_ledger()}
    assert led["AAA"]["action"] == "explored_buy" and led["AAA"]["propensity"] == 0.05
    assert led["BBB"]["action"] == "reject"
    sc = R.scorecard("2026-06-21")
    assert sc["explored_cohort"]["n"] == 1 and sc["explored_cohort"]["paid_rate"] == 1.0
    # veto-regret is computed over REJECTS only — the explored buy must not pollute it
    assert sc["veto_regret_rate"] == 1.0
    assert sc["by_stage"].get("committee_drop", {}).get("n") == 1


# ── veto-regret scorecard ─────────────────────────────────────────────────────────
def test_scorecard_veto_regret(sandbox, monkeypatch):
    rels = iter([0.08, -0.03, 0.05])                          # 2 of 3 rejected names beat SPY
    monkeypatch.setattr("brain.outcomes.label_thesis",
                        lambda t, asof=None: {"resolved": True, "rel_return": next(rels)})
    R.record("2026-06-01", held=[_held("AAA", "Insufficient research conviction"),
                                 _held("BBB", "committee: bear"),
                                 _held("CCC", "timing withhold: extended")])
    sc = R.scorecard("2026-06-21")
    assert sc["n_resolved"] == 3
    assert sc["veto_regret_rate"] == round(2 / 3, 3)
    assert sc["by_stage"]["research_hold"]["beat_spy_rate"] == 1.0
    assert sc["status"] == "building"                          # below _MIN_RESOLVED


def test_summary_shape_empty(sandbox):
    s = R.summary("2026-06-21")
    assert "coverage" in s and "scorecard" in s and s["horizon_d"] == R._HORIZON
    assert s["scorecard"]["status"] == "building" and s["scorecard"]["n_resolved"] == 0
