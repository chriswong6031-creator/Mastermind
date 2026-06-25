"""Guards for the desk-lever A/B harness (portfolio.desk_ab) — leakage-free policy proxies.

These tests run with NO vendor/macro and NO network: every policy is a pure function over small
in-memory input fixtures, the stockdata / autonomous-decisions / book dirs are redirected into a
temp sandbox, and the only forward-grading test monkeypatches brain.outcomes.label_thesis so it
never hits a parquet store or a price feed.

Properties pinned down:
  * L1 no_etf drops broad ETFs and keeps single names;
  * L2 concentrated keeps exactly the top-N by Conviction Index, renormalized;
  * L3 timing_gated drops the extended / weak-RS / 'avoid' fixture and FAILS OPEN with no stockdata;
  * L4 autonomous_clone replays the submitted book verbatim and never look-aheads to a future asof;
  * the desk_proxy composes L1∘L3∘L2;
  * grade_forward is leakage-free (delegates to outcomes.label_thesis, which caps req_end at asof);
  * every policy tolerates empty / missing-field inputs and never raises.
"""
from __future__ import annotations

import json
from datetime import date

import pytest

from portfolio import desk_ab
from portfolio import shadow_books as S


# ── fixtures ──────────────────────────────────────────────────────────────────
def _rec(ticker, **kw):
    """A schema-complete decision-input record (GT2), prod-confirmed at 0.10 weight by default."""
    base = {"ticker": ticker, "confluence": 0.4, "is_new": True, "retained": False,
            "forge_confirmed": True, "engine_score": 75, "research_score": 72, "combined": 73,
            "viability": "compelling", "size_mult": 1.0, "base_weight": 0.10, "name_cap": 0.20,
            "weight_forge": 0.10, "weight_prod": 0.10, "committee": None, "sentinel": None,
            "price": 100.0, "raw_prob_correct": 0.66, "horizon_d": 21, "thesis_id": f"d-{ticker}"}
    base.update(kw)
    return base


@pytest.fixture()
def sandbox(tmp_path, monkeypatch):
    """Redirect every desk_ab + shadow_books path into a temp dir (prod state untouched)."""
    monkeypatch.setattr(S, "_INPUTS", tmp_path / "inputs")
    monkeypatch.setattr(S, "_BOOKS", tmp_path / "books")
    monkeypatch.setattr(desk_ab, "_DESK_AB", tmp_path / "desk_ab")
    monkeypatch.setattr(desk_ab, "_STOCKDATA", tmp_path / "stockdata")
    monkeypatch.setattr(desk_ab, "_AUTONOMOUS_DECISIONS", tmp_path / "decisions.jsonl")
    monkeypatch.setattr(desk_ab, "_AUTONOMOUS_LATEST", tmp_path / "latest.json")
    (tmp_path / "stockdata").mkdir(parents=True, exist_ok=True)
    return tmp_path


def _write_stockdata(sandbox, ticker, payload):
    (sandbox / "stockdata" / f"{ticker.upper()}.json").write_text(json.dumps(payload))


# ── L1 — no broad ETFs ────────────────────────────────────────────────────────
def test_no_etf_drops_etfs_keeps_single_names():
    # SMH / XLK / IWM / MTUM are in the etf_universe allowlist; AME / ANET are single names.
    inputs = [_rec("SMH"), _rec("XLK"), _rec("IWM"), _rec("MTUM"), _rec("AME"), _rec("ANET")]
    w = desk_ab.apply_no_etf(inputs)
    assert set(w) == {"AME", "ANET"}                      # every ETF dropped
    assert all(v > 0 for v in w.values())


def test_is_etf_membership_matches_universe():
    assert desk_ab._is_etf("SMH") and desk_ab._is_etf("xlk")
    assert not desk_ab._is_etf("AME") and not desk_ab._is_etf("")


# ── L2 — concentration (top-N by conviction) ──────────────────────────────────
def test_concentrated_keeps_top_n_by_conviction():
    inputs = [_rec(f"T{i}", combined=50 + i) for i in range(6)]  # T5 highest, T0 lowest
    w = desk_ab.apply_concentrated_topN(inputs, n=3)
    assert set(w) == {"T5", "T4", "T3"}                  # exactly the 3 highest-conviction names
    assert abs(sum(w.values()) - 1.0) < 1e-6 or sum(w.values()) <= 1.0


def test_concentrated_renormalizes_to_gross_one():
    inputs = [_rec(f"T{i}", combined=80 - i, weight_forge=0.40, weight_prod=0.40) for i in range(5)]
    w = desk_ab.apply_concentrated_topN(inputs, n=4)     # 4 × 0.40 = 1.6 gross → scaled to 1.0
    assert len(w) == 4 and abs(sum(w.values()) - 1.0) < 1e-6


def test_concentrated_ties_break_deterministically_by_ticker():
    inputs = [_rec("BBB", combined=70), _rec("AAA", combined=70), _rec("CCC", combined=70)]
    w = desk_ab.apply_concentrated_topN(inputs, n=2)
    assert set(w) == {"AAA", "BBB"}                      # equal conviction → ticker asc wins


# ── L3 — entry-timing gate ────────────────────────────────────────────────────
def test_timing_gate_drops_extended_and_weak_rs(sandbox):
    _write_stockdata(sandbox, "GOOD", {"conviction": {"ext": {"grade": "ok"}},
                                       "tech": {"pct_vs_200dma": 8.0}, "alpha": {"rs": 80}})
    _write_stockdata(sandbox, "EXT", {"conviction": {"ext": {"grade": "parabolic"}}})
    _write_stockdata(sandbox, "FAR", {"tech": {"pct_vs_200dma": 45.0}})           # >30% extended
    _write_stockdata(sandbox, "WEAK", {"alpha": {"rs": 20}})                      # RS below median
    _write_stockdata(sandbox, "AVOID", {"ladder": {"entry": {"urgency": "avoid"}}})
    inputs = [_rec("GOOD"), _rec("EXT"), _rec("FAR"), _rec("WEAK"), _rec("AVOID")]
    w = desk_ab.apply_timing_gated(inputs)
    assert set(w) == {"GOOD"}                            # only the clean entry survives


def test_timing_gate_fails_open_when_no_stockdata(sandbox):
    # no stockdata files written → a missing signal must NEVER silently drop a name
    inputs = [_rec("AME"), _rec("ANET")]
    w = desk_ab.apply_timing_gated(inputs)
    assert set(w) == {"AME", "ANET"}


def test_timing_ok_is_defensive_on_garbage(sandbox):
    _write_stockdata(sandbox, "JUNK", {"conviction": "not-a-dict", "tech": None})
    assert desk_ab._timing_ok("JUNK") is True            # malformed → fail open, no raise


# ── L4 — autonomous-clone ─────────────────────────────────────────────────────
def test_autonomous_clone_replays_submitted_book(sandbox):
    row = {"asof": "2026-06-21", "holdings": [{"ticker": "EME", "weight": 0.09},
                                              {"ticker": "SMH", "weight": 0.08},
                                              {"ticker": "ANET", "weight": 0.05}]}
    (sandbox / "decisions.jsonl").write_text(json.dumps(row) + "\n")
    w = desk_ab.apply_autonomous_clone([], asof="2026-06-21")
    assert w == {"EME": 0.09, "SMH": 0.08, "ANET": 0.05}   # verbatim, gross < 1 → unchanged


def test_autonomous_clone_renormalizes_when_over_gross_one(sandbox):
    row = {"asof": "2026-06-21", "holdings": [{"ticker": "A", "weight": 0.6},
                                              {"ticker": "B", "weight": 0.6}]}  # gross 1.2
    (sandbox / "decisions.jsonl").write_text(json.dumps(row) + "\n")
    w = desk_ab.apply_autonomous_clone([], asof="2026-06-21")
    assert abs(sum(w.values()) - 1.0) < 1e-6 and abs(w["A"] - 0.5) < 1e-3


def test_autonomous_clone_no_lookahead_to_future_asof(sandbox):
    # a book submitted on 06-21 must NOT be replayed onto an earlier 06-18 decision date
    row = {"asof": "2026-06-21", "holdings": [{"ticker": "EME", "weight": 0.09}]}
    (sandbox / "decisions.jsonl").write_text(json.dumps(row) + "\n")
    assert desk_ab.apply_autonomous_clone([], asof="2026-06-18") == {}
    assert desk_ab.apply_autonomous_clone([], asof="2026-06-21") == {"EME": 0.09}


def test_autonomous_clone_latest_fallback_only_on_exact_asof(sandbox):
    (sandbox / "latest.json").write_text(json.dumps(
        {"as_of": "2026-06-21", "positions": [{"ticker": "MCY", "weight": 0.05}]}))
    assert desk_ab.apply_autonomous_clone([], asof="2026-06-21") == {"MCY": 0.05}  # exact match
    assert desk_ab.apply_autonomous_clone([], asof="2026-06-22") == {}             # no future leak


# ── combo — desk proxy (L1 ∘ L3 ∘ L2) ─────────────────────────────────────────
def test_desk_proxy_composes_all_three_levers(sandbox):
    # ETF (dropped by L1) + extended name (dropped by L3) + 4 clean names, cap top-2 by conviction
    _write_stockdata(sandbox, "EXT", {"conviction": {"ext": {"grade": "parabolic"}}})
    inputs = [
        _rec("SMH", combined=99),                        # ETF → L1 drops
        _rec("EXT", combined=98),                        # extended → L3 drops
        _rec("AAA", combined=90),
        _rec("BBB", combined=85),
        _rec("CCC", combined=60),                        # survives filters but below top-2
    ]
    w = desk_ab.apply_desk_proxy(inputs, n=2)
    assert set(w) == {"AAA", "BBB"}                      # ETF + extended gone, then top-2 of the rest


def test_dispatch_routes_every_policy(sandbox):
    inputs = [_rec("AME"), _rec("SMH")]
    for p in desk_ab.POLICIES:
        out = desk_ab.apply_desk_policy(p, inputs, asof="2026-06-21")
        assert isinstance(out, dict)                     # every policy returns a weight dict


# ── forward grading — leakage-free, delegates to outcomes.label_thesis ─────────
def test_grade_forward_delegates_and_is_leakage_free(monkeypatch):
    captured = {}

    def fake_label(thesis, asof=None):
        # capture exactly what desk_ab asked outcomes to grade — assert the contract it must honor
        captured["thesis"] = thesis
        captured["asof"] = asof
        return {"resolved": True, "rel_return": 0.07, "barrier": "time", "status": "resolved"}

    monkeypatch.setattr("brain.outcomes.label_thesis", fake_label)
    lab = desk_ab.grade_forward("AME", "2026-06-01", 100.0, asof="2026-06-30", horizon_d=21)
    assert lab["resolved"] and lab["rel_return"] == 0.07
    th = captured["thesis"]
    # the falsifier must be a rel_return-vs-SPY check anchored at the ENTRY date (so label_thesis's
    # own req_end=min(final_end,asof) guard applies — no price past asof is ever read)
    chk = th["falsifier"]["check"]
    assert chk["kind"] == "rel_return" and chk["vs"] == "SPY"
    assert th["state_asof"] == "2026-06-01" and chk["horizon_d"] == 21
    assert captured["asof"] == date(2026, 6, 30)         # asof passed through as a date → the cap


def test_grade_forward_returns_none_on_bad_input():
    assert desk_ab.grade_forward("", "2026-06-01", 100.0) is None
    assert desk_ab.grade_forward("AME", "", 100.0) is None


# ── defensive: empty / missing-field / garbage inputs never raise ─────────────
def test_policies_tolerate_empty_and_missing_fields(sandbox):
    for inp in (None, [], [{}], [{"ticker": "X"}], [{"ticker": "Y", "forge_confirmed": True}]):
        assert isinstance(desk_ab.apply_no_etf(inp), dict)
        assert isinstance(desk_ab.apply_concentrated_topN(inp, 3), dict)
        assert isinstance(desk_ab.apply_timing_gated(inp), dict)
        assert isinstance(desk_ab.apply_desk_proxy(inp, 3), dict)
        assert isinstance(desk_ab.apply_autonomous_clone(inp, asof="2026-06-21"), dict)


def test_run_isolates_to_sandbox_and_builds_all_books(sandbox, monkeypatch):
    monkeypatch.setattr("portfolio.paper_account._current_price",
                        lambda t: {"AME": 100.0, "ANET": 50.0, "SMH": 660.0, "SPY": 400.0}.get(t))
    monkeypatch.setattr("brain.outcomes.label_thesis",
                        lambda t, asof=None: {"resolved": False, "rel_return": None})
    row = {"asof": "2026-06-21", "holdings": [{"ticker": "AME", "weight": 0.5}]}
    (sandbox / "decisions.jsonl").write_text(json.dumps(row) + "\n")
    inputs = [_rec("AME", price=100.0), _rec("ANET", price=50.0), _rec("SMH", price=660.0)]
    res = desk_ab.run("2026-06-21", inputs=inputs)
    ids = {b["id"] for b in res["leaderboard"]}
    assert ids == {p["id"] for p in desk_ab.POLICIES}
    # no_etf must have dropped SMH; prod keeps it
    assert "SMH" not in res["books"]["no_etf"]["holdings"]
    assert "SMH" in res["books"]["prod"]["holdings"]
    # the clone book replays exactly AME
    assert res["books"]["autonomous_clone"]["holdings"] == ["AME"]
    # everything landed under the sandbox; nothing under prod's data/shadow
    assert (sandbox / "desk_ab" / "books" / "prod" / "account.json").exists()


def test_run_never_raises_on_garbage(sandbox, monkeypatch):
    monkeypatch.setattr("portfolio.paper_account._current_price", lambda t: None)
    monkeypatch.setattr("brain.outcomes.label_thesis", lambda t, asof=None: None)
    for bad in (None, [], [{}], [{"ticker": "X"}], [{"ticker": "Y", "forge_confirmed": True}]):
        desk_ab.run("2026-06-21", prices={}, inputs=bad)   # must not raise


def test_run_accrues_a_daily_nav_row_so_the_forward_clock_ticks(sandbox, monkeypatch):
    """Wiring guard: each build day desk_ab.run must mark every book → one NAV row per book per day,
    so the ~168-day forward clock (8 × 21-bday clusters) actually advances. Mirrors how the daily
    flow invokes it right after shadow_books.run (same prices, same stored inputs)."""
    monkeypatch.setattr("portfolio.paper_account._current_price",
                        lambda t: {"AME": 100.0, "ANET": 50.0, "SPY": 400.0}.get(t))
    monkeypatch.setattr("brain.outcomes.label_thesis",
                        lambda t, asof=None: {"resolved": False, "rel_return": None})
    inputs = [_rec("AME", price=100.0), _rec("ANET", price=50.0)]
    desk_ab.run("2026-06-22", prices={"SPY": 400.0}, inputs=inputs)
    desk_ab.run("2026-06-23", prices={"SPY": 400.0}, inputs=inputs)   # next build day
    # prod book has a NAV row for each of the two days (the clock ticked)
    nav = [json.loads(l) for l in
           (sandbox / "desk_ab" / "books" / "prod" / "nav_history.jsonl").read_text().splitlines()
           if l.strip()]
    assert sorted(r["date"] for r in nav) == ["2026-06-22", "2026-06-23"]
    # the daily leaderboard artifact (AB_EXPERIMENT.md §6.2) is persisted under the sandbox
    lb = json.loads((sandbox / "desk_ab" / "leaderboard.json").read_text())
    assert lb["as_of"] == "2026-06-23" and {b["id"] for b in lb["leaderboard"]} == \
        {p["id"] for p in desk_ab.POLICIES}


def test_run_is_idempotent_per_date(sandbox, monkeypatch):
    """Re-running the same build day must not duplicate that day's NAV row (idempotent per date)."""
    monkeypatch.setattr("portfolio.paper_account._current_price",
                        lambda t: {"AME": 100.0, "SPY": 400.0}.get(t))
    monkeypatch.setattr("brain.outcomes.label_thesis",
                        lambda t, asof=None: {"resolved": False, "rel_return": None})
    inputs = [_rec("AME", price=100.0)]
    desk_ab.run("2026-06-22", prices={"SPY": 400.0}, inputs=inputs)
    desk_ab.run("2026-06-22", prices={"SPY": 400.0}, inputs=inputs)   # same day twice
    nav = [json.loads(l) for l in
           (sandbox / "desk_ab" / "books" / "prod" / "nav_history.jsonl").read_text().splitlines()
           if l.strip()]
    assert len([r for r in nav if r["date"] == "2026-06-22"]) == 1


def test_carried_day_empty_inputs_holds_not_liquidates(sandbox, monkeypatch):
    """Carried day (inputs==[]): the input-derived desk books must HOLD, not liquidate. The daily
    loop_maintenance job calls desk_ab.run on quiet days too, so an unguarded empty-inputs run would
    wipe the A/B books. (desk_ab.run swaps S._BOOKS internally, so read account.json off disk.)"""
    monkeypatch.setattr("portfolio.paper_account._current_price",
                        lambda t: {"AME": 100.0, "SPY": 400.0}.get(t))
    monkeypatch.setattr("brain.outcomes.label_thesis",
                        lambda t, asof=None: {"resolved": False, "rel_return": None})

    def _prod_pos():
        p = sandbox / "desk_ab" / "books" / "prod" / "account.json"
        return json.loads(p.read_text())["positions"] if p.exists() else {}

    desk_ab.run("2026-06-22", prices={"SPY": 400.0}, inputs=[_rec("AME", price=100.0)])
    assert (_prod_pos().get("AME") or {}).get("shares", 0) > 0
    # CARRIED day — no inputs → HOLD AME (not liquidate), still mark forward
    res = desk_ab.run("2026-06-23", prices={"SPY": 400.0}, inputs=[])
    assert (_prod_pos().get("AME") or {}).get("shares", 0) > 0, \
        "carried day liquidated the desk book (the empty-inputs trap)"
    assert res["books"]["prod"]["holdings"] == ["AME"]
    nav = [json.loads(l) for l in
           (sandbox / "desk_ab" / "books" / "prod" / "nav_history.jsonl").read_text().splitlines()
           if l.strip()]
    assert "2026-06-23" in [r["date"] for r in nav]   # forward clock advanced on the held day
