"""Guards for parallel forward shadow books (portfolio.shadow_books) — leakage-free policy A/B.

The safety-critical properties:
  * policy weighting is PURE + deterministic and attributes one layer each (committee / calibration /
    sizing) — verified exhaustively;
  * the no-calibration policy genuinely RE-DERIVES NEXUS from SENTINEL's raw confidence (so it can
    diverge from prod once calibration bites);
  * the isolated NAV tracker conserves cash on a flat day and tracks price moves correctly;
  * labeling is FORWARD-only (no look-ahead) and reuses the portfolio-agnostic outcomes labeler;
  * a book NEVER touches prod's singleton state (everything lands under the sandbox dir);
  * one open thesis per held name (dedup), resolved theses feed hit-rate + Brier;
  * the driver never raises on garbage and is idempotent per date.
"""
from __future__ import annotations

import json

import pytest

import bot  # noqa: F401 — bootstraps vendor/macro
from portfolio import shadow_books as S


@pytest.fixture()
def sandbox(tmp_path, monkeypatch):
    """Redirect every shadow path into a temp dir so prod state is never touched."""
    monkeypatch.setattr(S, "_SHADOW", tmp_path)
    monkeypatch.setattr(S, "_INPUTS", tmp_path / "inputs")
    monkeypatch.setattr(S, "_BOOKS", tmp_path / "books")
    monkeypatch.setattr(S, "_LEADERBOARD", tmp_path / "leaderboard.json")
    return tmp_path


def _rec(ticker, **kw):
    base = {"ticker": ticker, "confluence": 0.4, "is_new": True, "retained": False,
            "forge_confirmed": True, "engine_score": 75, "research_score": 72, "combined": 73,
            "viability": "compelling", "size_mult": 1.0, "base_weight": 0.10, "name_cap": 0.20,
            "weight_forge": 0.10, "weight_prod": 0.10, "committee": None, "sentinel": None,
            "price": 100.0, "raw_prob_correct": 0.66, "horizon_d": 21, "thesis_id": f"d-{ticker}"}
    base.update(kw)
    return base


def _pol(pid):
    return S._POLICY_BY_ID[pid]


# ── policy weighting (pure) ───────────────────────────────────────────────────
def test_unconfirmed_name_is_zero_for_every_policy():
    r = _rec("X", forge_confirmed=False, weight_forge=0.0, weight_prod=0.0)
    for p in S.POLICIES:
        assert S._policy_weight(p, r) == 0.0


def test_prod_uses_weight_prod():
    r = _rec("AAA", weight_forge=0.12, weight_prod=0.06,  # committee trimmed prod to half
             committee={"action": "trim", "scale": 0.5})
    assert S._policy_weight(_pol("prod"), r) == 0.06


def test_no_committee_ignores_the_committee_drop():
    # prod DROPPED this name (weight_prod 0); no_committee must still buy it at the forge weight
    r = _rec("BBB", weight_forge=0.10, weight_prod=0.0,
             committee={"action": "drop", "scale": 0.0},
             sentinel={"stance": "OPPOSE", "raw_confidence": 0.8, "confidence": 0.8})
    assert S._policy_weight(_pol("prod"), r) == 0.0
    assert S._policy_weight(_pol("no_committee"), r) == 0.10


def test_engine_only_uses_base_weight_capped():
    r = _rec("CCC", base_weight=0.30, name_cap=0.20, size_mult=2.0, weight_forge=0.20)
    assert S._policy_weight(_pol("engine_only"), r) == 0.20      # capped at name_cap
    r2 = _rec("DDD", base_weight=0.08, name_cap=0.20)
    assert S._policy_weight(_pol("engine_only"), r2) == 0.08


def test_no_calibration_rederives_nexus_from_raw_confidence():
    # de-confidenced confidence (0.5) → prod TRIMMED (weight_prod>0); raw confidence (0.8) → a fresh
    # NEXUS pass OPPOSES hard → DROP. So no_calibration must zero a name prod kept.
    r = _rec("EEE", weight_forge=0.10, weight_prod=0.05, combined=73,
             committee={"action": "trim", "scale": 0.5},
             sentinel={"stance": "OPPOSE", "raw_confidence": 0.8, "confidence": 0.5})
    assert S._policy_weight(_pol("prod"), r) == 0.05            # what prod did (calib on)
    assert S._policy_weight(_pol("no_calibration"), r) == 0.0   # raw adversary kills it


def test_no_calibration_matches_prod_when_committee_absent():
    r = _rec("FFF", weight_forge=0.10, weight_prod=0.10, committee=None, sentinel=None)
    assert S._policy_weight(_pol("no_calibration"), r) == 0.10


def test_apply_policy_caps_gross_at_one():
    inputs = [_rec(f"T{i}", weight_forge=0.40, weight_prod=0.40) for i in range(4)]  # gross 1.6
    w = S.apply_policy(_pol("no_committee"), inputs)
    assert abs(sum(w.values()) - 1.0) < 1e-6 and all(v <= 1.0 for v in w.values())


def test_apply_policy_is_pure_does_not_mutate_inputs():
    inputs = [_rec("AAA", weight_prod=0.1)]
    snap = json.dumps(inputs, sort_keys=True)
    S.apply_policy(_pol("prod"), inputs)
    assert json.dumps(inputs, sort_keys=True) == snap


# ── isolated NAV tracker ──────────────────────────────────────────────────────
def test_nav_conserved_on_flat_day_and_tracks_price(sandbox):
    S._rebalance("b", {"AAA": 0.5}, {"AAA": 100.0, "SPY": 400.0}, "2026-06-01")
    flat = S._mark("b", {"AAA": 100.0, "SPY": 400.0}, "2026-06-01")
    assert abs(flat["nav"] - 1_000_000.0) < 1.0                 # no trade cost, flat price → flat NAV
    up = S._mark("b", {"AAA": 110.0, "SPY": 400.0}, "2026-06-02")
    assert abs(up["nav"] - 1_050_000.0) < 50.0                  # 50% in a name +10% → +5% NAV


def test_mark_is_idempotent_per_date(sandbox):
    S._rebalance("b", {"AAA": 0.5}, {"AAA": 100.0}, "2026-06-01")
    S._mark("b", {"AAA": 100.0}, "2026-06-01")
    S._mark("b", {"AAA": 100.0}, "2026-06-01")                  # same date twice
    rows = S._nav_rows("b")
    assert len([r for r in rows if r["date"] == "2026-06-01"]) == 1


def test_missing_price_does_not_swing_nav(sandbox):
    S._rebalance("b", {"AAA": 0.5}, {"AAA": 100.0}, "2026-06-01")
    # next mark has NO price for AAA → falls back to avg_cost, NAV unchanged (no fake swing)
    row = S._mark("b", {}, "2026-06-02")
    assert abs(row["nav"] - 1_000_000.0) < 1.0


# ── end-to-end run ────────────────────────────────────────────────────────────
def test_run_isolates_to_sandbox_and_builds_all_books(sandbox, monkeypatch):
    monkeypatch.setattr("portfolio.paper_account._current_price", lambda t: {"AAA": 100.0, "BBB": 50.0, "SPY": 400.0}.get(t))
    inputs = [
        _rec("AAA", weight_forge=0.12, weight_prod=0.12, price=100.0),
        _rec("BBB", weight_forge=0.10, weight_prod=0.0, price=50.0,   # prod-dropped by committee
             committee={"action": "drop", "scale": 0.0},
             sentinel={"stance": "OPPOSE", "raw_confidence": 0.8, "confidence": 0.8}),
    ]
    res = S.run("2026-06-01", prices={"AAA": 100.0, "BBB": 50.0, "SPY": 400.0}, inputs=inputs)
    ids = {b["id"] for b in res["leaderboard"]}
    assert ids == {"prod", "no_committee", "no_calibration", "engine_only", "risk_tilt"}
    books = res["books"]
    assert books["prod"]["holdings"] == ["AAA"]                 # committee dropped BBB
    assert books["no_committee"]["holdings"] == ["AAA", "BBB"]  # committee off → keeps BBB
    # divergence surfaces in the leaderboard
    nc = next(b for b in res["leaderboard"] if b["id"] == "no_committee")
    assert nc["extra_vs_prod"] == ["BBB"]
    # everything landed under the sandbox; prod dirs untouched
    assert (sandbox / "books" / "prod" / "account.json").exists()
    assert (sandbox / "leaderboard.json").exists()


def test_run_never_raises_on_garbage(sandbox, monkeypatch):
    monkeypatch.setattr("portfolio.paper_account._current_price", lambda t: None)
    for bad in (None, [], [{}], [{"ticker": "X"}], [{"ticker": "Y", "forge_confirmed": True}]):
        S.run("2026-06-01", prices={}, inputs=bad)              # must not raise


def test_carried_day_empty_inputs_holds_not_liquidates(sandbox, monkeypatch):
    """A day that carried NO decision input (inputs==[]) must HOLD the book — an unguarded run would
    target {} for every policy and liquidate held positions, wiping the forward A/B. A GENUINE all-cash
    signal (inputs present but nothing forge-confirmed) must still liquidate."""
    monkeypatch.setattr("portfolio.paper_account._current_price",
                        lambda t: {"AAA": 100.0, "SPY": 400.0}.get(t))
    monkeypatch.setattr("brain.outcomes.label_thesis",
                        lambda t, asof: {"resolved": False, "rel_return": None})
    px = {"AAA": 100.0, "SPY": 400.0}
    # day 1 — build a real position
    S.run("2026-06-01", prices=px, inputs=[_rec("AAA", weight_forge=0.5, weight_prod=0.5, price=100.0)])
    assert (S._load_account("prod")["positions"].get("AAA") or {}).get("shares", 0) > 0
    # day 2 — CARRIED day (no inputs at all): must HOLD AAA, NOT liquidate, and still mark forward
    res = S.run("2026-06-02", prices=px, inputs=[])
    assert (S._load_account("prod")["positions"].get("AAA") or {}).get("shares", 0) > 0, \
        "carried day liquidated the book (the empty-inputs trap)"
    assert res["books"]["prod"]["holdings"] == ["AAA"]
    assert "2026-06-02" in [r["date"] for r in S._nav_rows("prod")]   # forward clock advanced
    # day 3 — GENUINE all-cash signal: inputs PRESENT but nothing confirmed → DO liquidate
    res = S.run("2026-06-03", prices=px,
                inputs=[_rec("AAA", forge_confirmed=False, weight_forge=0.0, weight_prod=0.0)])
    assert "AAA" not in S._load_account("prod")["positions"]
    assert res["books"]["prod"]["holdings"] == []


def test_thesis_deduped_while_open_and_resolves_forward(sandbox, monkeypatch):
    monkeypatch.setattr("portfolio.paper_account._current_price", lambda t: 100.0)
    inputs = [_rec("AAA", weight_forge=0.5, weight_prod=0.5, price=100.0)]
    # day 1 + day 2: same name held → exactly one OPEN thesis (no duplicate per day)
    monkeypatch.setattr("brain.outcomes.label_thesis", lambda t, asof: {"resolved": False, "rel_return": None})
    S.run("2026-06-01", prices={"AAA": 100.0, "SPY": 400.0}, inputs=inputs)
    S.run("2026-06-02", prices={"AAA": 100.0, "SPY": 400.0}, inputs=inputs)
    theses = S._load_theses("prod")
    assert len([t for t in theses if t["subject"] == "AAA"]) == 1
    # now the thesis resolves (won vs SPY) → it feeds hit_rate + Brier
    monkeypatch.setattr("brain.outcomes.label_thesis",
                        lambda t, asof: {"resolved": True, "rel_return": 0.08, "barrier": "time"})
    res = S.run("2026-06-30", prices={"AAA": 100.0, "SPY": 400.0}, inputs=inputs)
    prod = res["books"]["prod"]
    assert prod["n_resolved"] == 1 and prod["hit_rate"] == 1.0 and prod["brier"] is not None


def test_hit_rate_uses_falsifier_predicate_not_positive_return():
    # a bullish thesis with rel_return -2% is WITHIN the -5% falsifier tolerance → a HIT (matches
    # scorer.track_record + the book's own Brier rows), even though the absolute return is negative.
    # The old code counted it a miss (realized>0); hit_rate must now agree with the falsifier.
    def th(realized):
        return {"status": "resolved", "realized": realized, "prob_correct": 0.6,
                "falsifier": {"check": {"op": "<", "threshold": -0.05}}}
    nav = [{"date": "2026-06-01", "nav": 1_000_000.0, "spy_px": 400.0}]
    s = S._book_summary(_pol("prod"), [th(-0.02), th(-0.08), th(0.10)], {"positions": {}}, nav)
    assert s["n_resolved"] == 3
    assert s["hit_rate"] == round(2 / 3, 3)         # -0.02 (in-tolerance) + +0.10 are hits; -0.08 miss
    assert s["hit_rate"] != round(1 / 3, 3)         # guard against regressing to positive-return logic


def test_risk_mult_haircuts_high_vol_and_is_leakage_free():
    # the risk-tilt overlay: subtract-only, haircuts high-vol/lottery names, leakage-free (≤ asof).
    import numpy as np
    import pandas as pd
    idx = pd.bdate_range("2026-01-02", periods=120)
    calm = pd.Series(np.linspace(100, 110, 120), index=idx)          # smooth → low vol
    wr = np.zeros(120); wr[1::2] = 0.05; wr[2::2] = -0.045
    wild = pd.Series(100 * np.cumprod(1 + wr), index=idx)            # ±5%/day → high vol
    panel = {"CALM": calm, "WILD": wild}
    asof = idx[-1].strftime("%Y-%m-%d")
    assert S._risk_mult(panel, "CALM", asof) == 1.0                  # low-risk → full size
    assert S._risk_mult(panel, "WILD", asof) < 1.0                   # high-risk → haircut
    assert S._risk_mult(panel, "WILD", asof) >= S._RT_FLOOR          # never below the floor
    assert S._risk_mult(panel, "ZZZ", asof) == 1.0                   # absent from panel → full size
    early = idx[10].strftime("%Y-%m-%d")                             # only ~10 returns ≤ asof
    assert S._risk_mult(panel, "WILD", early) == 1.0                 # <20 obs → no haircut, no peek


def test_leaderboard_sorted_by_return_with_baseline_flag(sandbox):
    books = {
        "prod": {"id": "prod", "label": "Prod", "is_baseline": True, "return_pct": 1.0,
                 "holdings": ["A"], "n_resolved": 0},
        "no_committee": {"id": "no_committee", "label": "NC", "is_baseline": False, "return_pct": 3.0,
                         "holdings": ["A", "B"], "n_resolved": 0},
    }
    lb = S.leaderboard(books)
    assert lb[0]["id"] == "no_committee"          # higher return first
    assert lb[0]["extra_vs_prod"] == ["B"] and lb[1]["is_baseline"] is True


def test_write_read_inputs_roundtrip(sandbox):
    S.write_inputs("2026-06-01", [_rec("AAA")])
    got = S.read_inputs("2026-06-01")
    assert len(got) == 1 and got[0]["ticker"] == "AAA"
    assert S.read_inputs("2099-01-01") == []      # missing → empty, no raise
