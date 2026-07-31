"""The ETF rotation book — universe filter, risk guardrails, signal board, and the build.

Covers the new ETF-only Opus-Brain book: the registry entry, the submit_book allowlist filter
(no single names), the trusted-layer guardrails (single-ETF cap, turnover throttle, crisis floor),
the distilled risk_state, the offline + simulated-submission builds, and the web endpoints.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from portfolio import etf_universe, paper_account, registry


@pytest.fixture
def iso(tmp_path, monkeypatch):
    """Isolate all per-id portfolio state to a tmp root (registry.data_dir derives off _ROOT)."""
    monkeypatch.setattr(registry, "_ROOT", tmp_path, raising=False)
    return tmp_path


# --------------------------------------------------------------------------- universe

def test_registry_has_etf_book(iso):
    assert "etf" in registry.ids()
    meta = registry.get("etf")
    assert meta["kind"] == "etf_brain" and meta["manager"] == "brain"
    assert meta["benchmark"] == "SPY"
    assert registry.currency("etf") == "USD"
    assert registry.data_dir("etf") == iso / "data" / "portfolios" / "etf"


def test_universe_membership_and_buckets():
    assert etf_universe.is_etf("SPY") and etf_universe.is_etf("spy")
    assert etf_universe.is_etf("TLT") and etf_universe.is_etf("SGOV")
    assert not etf_universe.is_etf("AAPL")        # single name — off-list
    assert not etf_universe.is_etf("NVDA")
    assert not etf_universe.is_etf(None)
    # defensive bucket = duration + cash + gold + min-vol/staples/utilities
    for t in ("TLT", "IEF", "SGOV", "BIL", "GLD", "USMV", "XLP", "XLU"):
        assert etf_universe.is_defensive(t), t
    # offensive = growth/cyclical equity (and the rest)
    for t in ("XLK", "SMH", "QQQ", "MTUM"):
        assert t in etf_universe.OFFENSIVE and not etf_universe.is_defensive(t), t
    assert etf_universe.group_of("XLK") == "sectors"
    assert etf_universe.group_of("SGOV") == "cash"


def test_every_etf_universe_member_has_a_canonical_name():
    assert set(etf_universe.ALL) <= set(etf_universe._NAMES)
    assert etf_universe.name_of("sgov") == "iShares 0-3 Month Treasury Bond ETF"
    assert etf_universe.name_of("KRE") == "SPDR S&P Regional Banking ETF"
    assert not etf_universe.is_etf("KRE")  # historical display metadata does not widen the mandate
    assert etf_universe.name_of("AAPL") is None
    assert etf_universe.name_of(None) is None


def test_price_falls_back_to_paper_account(iso, monkeypatch):
    # yfinance is stubbed off in conftest → etf_universe.price degrades to paper_account._current_price
    monkeypatch.setattr(paper_account, "_current_price", lambda t: {"SPY": 740.0}.get(t))
    assert etf_universe.price("SPY") == 740.0
    assert etf_universe.price("ZZZZ") is None


# --------------------------------------------------------------------------- submit filter

def _submit(args):
    from brain import etf_mcp
    return etf_mcp.submit_book.handler(args)


def test_submit_book_rejects_single_names(iso):
    asyncio.run(_submit({
        "holdings": [
            {"ticker": "SPY", "weight": 0.4, "rationale": "broad beta"},
            {"ticker": "XLK", "weight": 0.3, "rationale": "tech leadership"},
            {"ticker": "AAPL", "weight": 0.2, "rationale": "single name — must be rejected"},
            {"ticker": "TQQQ", "weight": 0.1, "rationale": "leveraged — off-list"},
        ],
        "summary": "etf book",
    }))
    from brain import etf_mcp
    sub = etf_mcp.read_submission()
    assert {h["ticker"] for h in sub["holdings"]} == {"SPY", "XLK"}     # only allowlisted ETFs survive
    assert all(etf_universe.is_etf(h["ticker"]) for h in sub["holdings"])
    assert all(h.get("group") for h in sub["holdings"])                # group tagged for display


# --------------------------------------------------------------------------- guardrails

# fixed guardrails so these tests are deterministic regardless of config/etf_strategy.yml
_FIXED_GUARDRAILS = {"max_single_weight": 0.35, "min_trade": 0.015,
                     "offensive_cap": {"stressed": 0.55, "elevated": 0.80}}


def test_guardrail_single_etf_cap(iso, monkeypatch):
    from bot import etf
    monkeypatch.setattr(etf, "_guardrails", lambda: _FIXED_GUARDRAILS)
    adj, notes = etf._apply_guardrails({"SPY": 0.5, "XLK": 0.2}, {"SPY": 740.0, "XLK": 200.0},
                                       {"state": "calm"})
    assert adj["SPY"] == 0.35                       # clamped; excess falls to cash
    assert adj["XLK"] == 0.2
    assert any("SPY" in n for n in notes)


def test_guardrail_turnover_throttle(iso, monkeypatch):
    from bot import etf
    monkeypatch.setattr(etf, "_guardrails", lambda: _FIXED_GUARDRAILS)
    # seed a 2%-of-NAV XLK position so a tiny re-weight is a no-trade
    monkeypatch.setattr(paper_account, "_current_price", lambda t: {"XLK": 200.0}.get(t))
    paper_account.execute_fill("XLK", "buy", shares=100, price=200.0, asof="2026-06-21",
                               portfolio_id="etf")   # 100*200 / 1_000_000 NAV = 2.0%
    prices = {"XLK": 200.0}
    # a 0.5% drift is throttled back to the current 2% weight (no churn)...
    adj, notes = etf._apply_guardrails({"XLK": 0.025}, prices, {"state": "calm"})
    assert adj["XLK"] == pytest.approx(0.02, abs=1e-3)
    assert any("throttle" in n for n in notes)
    # ...but a real 8% move trades through
    adj2, _ = etf._apply_guardrails({"XLK": 0.10}, prices, {"state": "calm"})
    assert adj2["XLK"] == 0.10


def test_guardrail_crisis_floor(iso, monkeypatch):
    from bot import etf
    monkeypatch.setattr(etf, "_guardrails", lambda: _FIXED_GUARDRAILS)
    # stressed read → offensive gross (XLK+SMH = 60%) capped at 55%, defensive TLT untouched. Both
    # offensive names sit UNDER the 35% single-ETF cap, so only the crisis floor bites here (the
    # single-ETF cap is applied first — see test_guardrail_single_etf_cap — so use sub-cap weights).
    prices = {"XLK": 200.0, "SMH": 250.0, "TLT": 90.0}
    adj, notes = etf._apply_guardrails({"XLK": 0.3, "SMH": 0.3, "TLT": 0.2}, prices,
                                       {"state": "stressed"})
    assert adj["XLK"] == pytest.approx(0.275, abs=1e-3)   # 0.3 * 55/60
    assert adj["SMH"] == pytest.approx(0.275, abs=1e-3)
    assert adj["TLT"] == 0.2                              # defensive — exempt from the offensive cap
    assert any("offensive" in n for n in notes)
    # calm read → no crisis cap
    adj2, _ = etf._apply_guardrails({"XLK": 0.3, "SMH": 0.3, "TLT": 0.2}, prices, {"state": "calm"})
    assert adj2["XLK"] == 0.3 and adj2["SMH"] == 0.3


# --------------------------------------------------------------------------- risk_state + board

def test_risk_state_classification(monkeypatch):
    from brain import etf_board
    monkeypatch.setattr(etf_board, "etf_trend", lambda t: {})    # isolate from the price store
    assert etf_board.risk_state({})["state"] == "calm"
    assert etf_board.risk_state(
        {"dislocation": {"verdict": "stressed"}})["state"] == "stressed"
    assert etf_board.risk_state(
        {"drawdown_risk": {"band": "high"}})["state"] == "stressed"
    assert etf_board.risk_state(
        {"risk_appetite": {"vix_term_state": "backwardation"}})["state"] == "stressed"
    assert etf_board.risk_state(
        {"drawdown_risk": {"band": "medium"}})["state"] == "elevated"
    assert etf_board.risk_state(
        {"complacency": {"state": "watch"}})["state"] == "elevated"


def test_risk_state_reads_nested_conditions(monkeypatch):
    # the real regime contract nests these gauges under regime["conditions"]; risk_state must read
    # there (not top-level) or the crisis floor would silently never fire. Top-level still works too.
    from brain import etf_board
    monkeypatch.setattr(etf_board, "etf_trend", lambda t: {})
    assert etf_board.risk_state({"conditions": {"drawdown_risk": {"band": "high"}}})["state"] == "stressed"
    assert etf_board.risk_state({"conditions": {"complacency": {"state": "watch"}}})["state"] == "elevated"
    assert etf_board.risk_state({"conditions": {"systemic_stress": {"state": "elevated"}}})["state"] == "stressed"
    assert etf_board.risk_state({"drawdown_risk": {"band": "high"}})["state"] == "stressed"   # top-level fallback


def test_board_builds(monkeypatch):
    from brain import etf_board
    monkeypatch.setattr(etf_board, "etf_trend", lambda t: {})    # keep it fast + offline
    board = etf_board.build_board()
    assert isinstance(board, dict)
    assert "risk_state" in board and board["risk_state"]["state"] in ("calm", "elevated", "stressed")
    assert board["universe"] == etf_universe.GROUPS


# --------------------------------------------------------------------------- fragility (leading risk)

# a synthetic regime with every leading-fragility signal lit but NO coincident crisis trigger — the
# 06-22 setup: risk gauges read calm while GEX flipped, breadth diverged and the tape was one factor.
_FRAGILE_REGIME = {
    "alerts": [
        {"rule": "gex_flip_cross", "severity": "warn", "message": "GEX: net GEX changed sign (net -57bn)"},
        {"rule": "sector_holdings_accumulation", "severity": "info",
         "message": "XLK: NVDA weight +2.3pp ... cycle NEARING A HIGH·TAKE PROFITS"},
    ],
    "conditions": {
        "complacency": {"state": "watch", "breadth_div": True,
                        "breadth_above200_pctile": 0.38, "spy_high_prox": 0.98},
    },
    "cross_asset": {"verdict": "concentrated", "absorption_pctile_5y": 0.93,
                    "headline": "CONCENTRATED — 3 of 6 markets are one bet"},
    "market_drivers": {"primary_label": "Oil shock", "direction": "oil collapsing",
                       "scores": [{"family": "equity-leadership", "strength": 0.18}]},
}


def test_fragility_surfaces_leading_signals():
    from brain import etf_board
    f = etf_board.build_fragility(_FRAGILE_REGIME)
    assert f["gex_negative"] is True
    assert f["breadth_divergence"] is True
    assert f["concentrated"] is True
    assert f["level"] == "high"                                   # gex + breadth + concentration = 3 warns
    keys = {s["key"] for s in f["signals"]}
    assert {"gex_negative", "breadth_divergence", "cross_asset_concentration", "holdings_topping"} <= keys
    assert f["dominant_driver"]["primary"] == "Oil shock"         # the real driver, not the book's tilt
    # a cold/empty dashboard degrades to all-clear, never raises
    z = etf_board.build_fragility({})
    assert z["level"] == "low" and z["warn_count"] == 0 and z["signals"] == []


def test_risk_state_escalates_on_leading_fragility(monkeypatch):
    from brain import etf_board
    monkeypatch.setattr(etf_board, "etf_trend", lambda t: {})     # isolate the SPY trend read
    # the full fragile cluster, with NO coincident trigger → LEADING-stressed (the fix for 'calm' at the top)
    rs = etf_board.risk_state(_FRAGILE_REGIME)
    assert rs["state"] == "stressed" and rs["fragility_level"] == "high"
    assert any("leading:" in r for r in rs["reasons"])
    # a lone GEX flip is not the whole cluster → elevated, not stressed
    one = etf_board.risk_state({"alerts": [{"rule": "gex_flip_cross", "message": "flip"}]})
    assert one["state"] == "elevated" and one["fragility_level"] == "elevated"
    # a genuinely clean tape still reads calm
    assert etf_board.risk_state({})["state"] == "calm"


# --------------------------------------------------------------------------- overextension + concentration guardrails

# fixed guardrails incl. the new limits so these tests don't depend on config/etf_strategy.yml
_FIXED_GUARDRAILS_V2 = {
    "max_single_weight": 0.35, "min_trade": 0.015,
    "offensive_cap": {"stressed": 0.55, "elevated": 0.80},
    "overextension": {"pct_vs_200d_cap": 40.0, "max_weight": 0.08},
    "factor_clusters": [{"name": "megacap_growth_semis",
                         "members": ["QQQ", "XLK", "SMH", "IGV", "MTUM", "SIZE"], "max_gross": 0.40}],
}


def test_guardrail_overextension_trim(iso, monkeypatch):
    from bot import etf
    from brain import etf_board
    monkeypatch.setattr(etf, "_guardrails", lambda: _FIXED_GUARDRAILS_V2)
    # SMH +60% above its 200d (parabolic) → clamped to 8%; SPY +9% is fine → untouched
    monkeypatch.setattr(etf_board, "etf_trend",
                        lambda t: {"SMH": {"pct_vs_200d": 60.0}, "SPY": {"pct_vs_200d": 9.0}}.get(t, {}))
    adj, notes = etf._apply_guardrails({"SMH": 0.14, "SPY": 0.25}, {"SMH": 668.0, "SPY": 744.0},
                                       {"state": "calm"})
    assert adj["SMH"] == pytest.approx(0.08, abs=1e-6)            # blow-off top not held at size
    assert adj["SPY"] == 0.25
    assert any("overextended SMH" in n for n in notes)


def test_guardrail_factor_cluster_cap(iso, monkeypatch):
    from bot import etf
    from brain import etf_board
    monkeypatch.setattr(etf, "_guardrails", lambda: _FIXED_GUARDRAILS_V2)
    monkeypatch.setattr(etf_board, "etf_trend", lambda t: {})    # no overextension trim — isolate G5
    # QQQ+SMH+MTUM = 50% of one growth/semis factor → scaled to 40% combined; SGOV (defensive) untouched
    target = {"QQQ": 0.20, "SMH": 0.15, "MTUM": 0.15, "SGOV": 0.30}
    prices = {"QQQ": 740.0, "SMH": 668.0, "MTUM": 345.0, "SGOV": 100.0}
    adj, notes = etf._apply_guardrails(target, prices, {"state": "calm"})
    assert adj["QQQ"] + adj["SMH"] + adj["MTUM"] == pytest.approx(0.40, abs=1e-3)   # cluster capped
    assert adj["QQQ"] == pytest.approx(0.16, abs=1e-3)           # each scaled *0.40/0.50
    assert adj["SMH"] == pytest.approx(0.12, abs=1e-3)
    assert adj["SGOV"] == 0.30                                   # defensive, outside the cluster
    assert any("factor cap" in n for n in notes)


def test_persona_advertises_new_guardrails():
    from bot import etf
    p = etf._build_persona()
    assert "blow-off top" in p and "200d" in p                   # overextension clamp advertised
    assert "megacap_growth_semis" in p and "COMBINED gross" in p # factor-cluster cap advertised
    assert "fragility" in p                                      # process points at the leading-risk block


def test_new_guardrails_in_spec_and_fallback(monkeypatch):
    g = etf_universe.guardrails()
    assert g["overextension"]["pct_vs_200d_cap"] == 40 and g["overextension"]["max_weight"] == 0.08
    assert any(c["name"] == "megacap_growth_semis" for c in g["factor_clusters"])
    # a missing/corrupt spec still yields the in-code defaults for the new limits
    monkeypatch.setattr(etf_universe, "load_spec", lambda: {})
    gd = etf_universe.guardrails()
    assert gd["overextension"]["pct_vs_200d_cap"] == 40.0
    assert gd["factor_clusters"] and gd["factor_clusters"][0]["members"]


# --------------------------------------------------------------------------- strategy spec

def test_spec_drives_universe_and_guardrails():
    # the universe + guardrails + horizon come from config/etf_strategy.yml
    assert "SPY" in etf_universe.GROUPS["core_index"]
    assert "SGOV" in etf_universe.GROUPS["cash"] and "VLUE" in etf_universe.GROUPS["factors"]
    g = etf_universe.guardrails()
    assert g["max_single_weight"] == 0.35 and g["min_trade"] == 0.015
    assert g["offensive_cap"]["stressed"] == 0.55 and g["offensive_cap"]["elevated"] == 0.80
    assert etf_universe.horizon_days() == 21


def test_spec_missing_falls_back_to_defaults(monkeypatch):
    # a missing/corrupt spec must never break the book — every getter falls back to in-code defaults
    monkeypatch.setattr(etf_universe, "load_spec", lambda: {})
    g = etf_universe.guardrails()
    assert g["max_single_weight"] == 0.35 and g["offensive_cap"]["stressed"] == 0.55
    assert etf_universe.horizon_days() == 21


def test_persona_built_from_spec():
    from bot import etf
    p = etf._build_persona()
    assert "SPY" in p and "SGOV" in p                  # universe injected from the spec
    assert "35%" in p                                  # live guardrail number injected
    assert "CONFIRMATION OVER PREDICTION" in p         # doctrine injected


# --------------------------------------------------------------------------- accountability

def test_etf_outcomes_grades_and_scores(iso, monkeypatch):
    from portfolio import etf_outcomes, registry
    from brain import outcomes as _oc
    monkeypatch.setattr(etf_universe, "horizon_days", lambda: 3)   # short horizon → resolves fast
    closes = {
        "XLK":  {"2026-01-05": 100, "2026-01-06": 102, "2026-01-07": 104, "2026-01-08": 110, "2026-01-09": 112},
        "SGOV": {"2026-01-05": 100, "2026-01-06": 100, "2026-01-07": 100, "2026-01-08": 100, "2026-01-09": 100},
        "SPY":  {"2026-01-05": 200, "2026-01-06": 200, "2026-01-07": 200, "2026-01-08": 200, "2026-01-09": 200},
    }
    monkeypatch.setattr(_oc, "_closes", lambda t, s, e, cache=True: dict(closes.get((t or "").upper(), {})))
    # seed a decision log: a high-conviction winner (XLK +10% vs SPY) and a low-conviction flat (SGOV)
    p = registry.data_dir("etf") / "decisions.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"asof": "2026-01-05", "summary": "s", "holdings": [
        {"ticker": "XLK", "weight": 0.6, "conviction": "high", "group": "sectors"},
        {"ticker": "SGOV", "weight": 0.4, "conviction": "low", "group": "cash"},
    ]}) + "\n")

    cov = etf_outcomes.grade("2026-02-01")
    assert cov["n_resolved"] == 2
    sc = etf_outcomes.scorecard("2026-02-01")
    assert sc["n_resolved"] == 2
    assert sc["hit_rate"] == 0.5                                    # XLK wins, SGOV flat (rel 0, not >0)
    assert sc["by_conviction"]["high"]["hit_rate"] == 1.0
    assert sc["by_conviction"]["low"]["hit_rate"] == 0.0
    assert sc["book_edge_mean"] == pytest.approx(0.06, abs=1e-3)    # 0.6*0.10 + 0.4*0.0
    # the feedback line the Brain sees mentions the graded count + hit-rate
    line = etf_outcomes.prompt_line("2026-02-01")
    assert "2 picks graded" in line and "hit-rate" in line


def test_grade_is_idempotent(iso, monkeypatch):
    from portfolio import etf_outcomes, registry
    from brain import outcomes as _oc
    monkeypatch.setattr(_oc, "_closes", lambda t, s, e, cache=True: {})   # nothing resolves
    p = registry.data_dir("etf") / "decisions.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"asof": "2026-01-05", "holdings": [
        {"ticker": "XLK", "weight": 0.5, "conviction": "high"}]}) + "\n")
    etf_outcomes.grade("2026-01-06")
    etf_outcomes.grade("2026-01-06")                                # second run must not duplicate
    rows = (registry.data_dir("etf") / "outcomes.jsonl").read_text().strip().splitlines()
    assert len(rows) == 1


def test_grade_refreshes_open_entry_weight(iso, monkeypatch):
    from portfolio import etf_outcomes, registry
    from brain import outcomes as _oc
    monkeypatch.setattr(_oc, "_closes", lambda t, s, e, cache=True: {})   # stays open
    p = registry.data_dir("etf") / "decisions.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"asof": "2026-01-05", "holdings": [
        {"ticker": "XLK", "weight": 0.3, "conviction": "low"}]}) + "\n")
    etf_outcomes.grade("2026-01-06")
    # a same-asof re-run (force=True replaces the decision row) revises the allocation
    p.write_text(json.dumps({"asof": "2026-01-05", "holdings": [
        {"ticker": "XLK", "weight": 0.5, "conviction": "high"}]}) + "\n")
    etf_outcomes.grade("2026-01-06")
    rows = [json.loads(x) for x in (registry.data_dir("etf") / "outcomes.jsonl").read_text().splitlines() if x.strip()]
    assert len(rows) == 1 and rows[0]["weight"] == 0.5 and rows[0]["conviction"] == "high"


# --------------------------------------------------------------------------- the build

def test_run_etf_offline_inaugural(iso, monkeypatch):
    monkeypatch.setattr(paper_account, "_current_price", lambda t: {"SPY": 740.0}.get(t))
    from bot import etf
    out = etf.run_etf(asof="2026-06-21", armed=False)
    assert out["inaugural"] is True
    assert out["decided"] is False
    assert out["nav"] == 1_000_000.0
    latest = json.loads((registry.data_dir("etf") / "latest.json").read_text())
    assert latest["portfolio_id"] == "etf"
    assert latest["schema"] == "portfolio.v1"
    assert latest["kind"] == "etf_brain"


def test_run_etf_executes_and_filters(iso, monkeypatch):
    prices = {"XLK": 200.0, "SGOV": 100.0, "SPY": 740.0}   # VLUE deliberately unpriceable
    monkeypatch.setattr(paper_account, "_current_price", lambda t: prices.get(t))
    from bot import etf, settle
    from brain import etf_board, etf_mcp
    monkeypatch.setattr(etf_board, "risk_state", lambda regime=None: {"state": "calm", "reasons": ["test"]})
    monkeypatch.setattr(settle, "is_open", lambda pid: True)   # force OPEN so it fills (not queues)

    def fake_brain(asof, inaugural):
        etf_mcp.submission_path().parent.mkdir(parents=True, exist_ok=True)
        etf_mcp.submission_path().write_text(json.dumps({
            "holdings": [
                {"ticker": "XLK", "weight": 0.3, "rationale": "tech leadership"},
                {"ticker": "SGOV", "weight": 0.3, "rationale": "T-bill cash yield"},
                {"ticker": "AAPL", "weight": 0.2, "rationale": "single name — trusted layer rejects"},
                {"ticker": "VLUE", "weight": 0.1, "rationale": "value tilt (unpriceable this run)"},
            ],
            "summary": "barbell: tech + bills", "gross": 0.9,
        }))
        return {"ok": True, "text": "x", "cost_usd": 0.0, "model": "claude-opus-4-8"}

    monkeypatch.setattr(etf, "_run_brain", fake_brain)
    out = etf.run_etf(asof="2026-06-21", armed=True)
    assert out["decided"] is True
    assert "AAPL" in out.get("rejected_offlist", [])      # single name rejected in the trusted layer
    assert "VLUE" in out["skipped_unpriceable"]           # in universe but no price → skipped, surfaced
    sides = {(t["ticker"], t["side"]) for t in out["executed"]}
    assert ("XLK", "buy") in sides and ("SGOV", "buy") in sides
    # decision log captured the day's narrative + per-name rationale + risk state
    decs = etf.load_decisions()
    assert decs and decs[0]["summary"] == "barbell: tech + bills"
    assert decs[0]["risk_state"] == "calm"
    assert any(h["ticker"] == "XLK" and h["rationale"] for h in decs[0]["holdings"])
    # positions carry the rationale + group for the dashboard
    latest = json.loads((registry.data_dir("etf") / "latest.json").read_text())
    xlk = next(p for p in latest["positions"] if p["ticker"] == "XLK")
    assert xlk["rationale"] and xlk["sleeve"] == "brain" and xlk["group"] == "sectors"


# --------------------------------------------------------------------------- market-hours gate

def _seed_etf_submission(holdings):
    from brain import etf_mcp
    etf_mcp.submission_path().parent.mkdir(parents=True, exist_ok=True)
    etf_mcp.submission_path().write_text(json.dumps({"holdings": holdings, "summary": "x"}))


def test_run_etf_queues_when_closed_no_fills(iso, monkeypatch):
    prices = {"XLK": 200.0, "SGOV": 100.0, "SPY": 740.0}
    monkeypatch.setattr(paper_account, "_current_price", lambda t: prices.get(t))
    from bot import etf, settle
    from brain import etf_board
    monkeypatch.setattr(etf_board, "risk_state", lambda regime=None: {"state": "calm", "reasons": ["t"]})
    monkeypatch.setattr(settle, "is_open", lambda pid: False)        # market CLOSED
    monkeypatch.setattr(etf, "_run_brain", lambda asof, inaug: (
        _seed_etf_submission([{"ticker": "XLK", "weight": 0.3, "rationale": "a"},
                              {"ticker": "SGOV", "weight": 0.3, "rationale": "b"}]),
        {"ok": True, "model": "m"})[1])

    out = etf.run_etf(asof="2026-06-22", armed=True)
    assert out["decided"] is True and out["market_open"] is False
    assert out["queued_for_open"] is True
    assert out["executed"] == []                                    # NO off-hours fills
    assert not (paper_account._load_account("etf").get("positions") or {})   # book unchanged (all cash)
    pt = paper_account.load_pending_target("etf")
    assert pt and set(pt["target"]) == {"XLK", "SGOV"}             # target queued for the open

    # re-run while STILL closed → still no fills, just re-queues (idempotent, no churn)
    out2 = etf.run_etf(asof="2026-06-22", armed=True)
    assert out2["executed"] == []
    assert not (paper_account._load_account("etf").get("positions") or {})


def test_settle_open_fills_queued_target(iso, monkeypatch):
    prices = {"XLK": 200.0, "SGOV": 100.0, "SPY": 740.0}
    monkeypatch.setattr(paper_account, "_current_price", lambda t: prices.get(t))
    from bot import settle
    from brain import etf_board
    monkeypatch.setattr(etf_board, "risk_state", lambda regime=None: {"state": "calm", "reasons": ["t"]})
    paper_account.save_pending_target({"XLK": 0.3, "SGOV": 0.3}, "2026-06-22", portfolio_id="etf")

    # closed → no-op, target stays queued
    monkeypatch.setattr(settle, "is_open", lambda pid: False)
    assert settle.settle_open("etf", asof="2026-06-22").get("skipped") == "market_closed"
    assert paper_account.load_pending_target("etf") is not None

    # open → fills the queued target and clears it
    monkeypatch.setattr(settle, "is_open", lambda pid: True)
    res = settle.settle_open("etf", asof="2026-06-22")
    assert res["ok"] is True
    sides = {(t["ticker"], t["side"]) for t in res["executed"]}
    assert ("XLK", "buy") in sides and ("SGOV", "buy") in sides
    assert set(paper_account._load_account("etf").get("positions") or {}) == {"XLK", "SGOV"}
    assert paper_account.load_pending_target("etf") is None        # cleared after settle


def test_settle_target_round_trip(iso, monkeypatch):
    # paper_account-level: a queued target settles to a full rebalance at the given marks
    monkeypatch.setattr(paper_account, "_current_price", lambda t: {"SPY": 740.0}.get(t))
    paper_account.save_pending_target({"XLK": 0.5}, "2026-06-22", portfolio_id="etf")
    assert paper_account.load_pending_target("etf")["target"] == {"XLK": 0.5}
    settled = paper_account.settle_target({"XLK": 200.0}, "2026-06-22", portfolio_id="etf")
    assert settled == {"XLK": 0.5}
    assert "XLK" in (paper_account._load_account("etf").get("positions") or {})
    assert paper_account.load_pending_target("etf") is None        # cleared


def test_web_endpoints_etf_aware(iso, monkeypatch):
    monkeypatch.setattr(paper_account, "_current_price", lambda t: {"SPY": 740.0}.get(t))
    from bot import etf
    etf.run_etf(asof="2026-06-21", armed=False)            # seed the etf book

    from fastapi.testclient import TestClient
    from app.main import app
    c = TestClient(app, raise_server_exceptions=True)

    ids = {p["id"] for p in c.get("/api/portfolios").json()["portfolios"]}
    assert "etf" in ids

    dec = c.get("/api/decisions?portfolio=etf").json()
    assert "decisions" in dec

    book = c.get("/api/portfolio?portfolio=etf").json()
    assert book["portfolio_id"] == "etf"
