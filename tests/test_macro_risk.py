"""Guards for the MACRO RISK OFFICER seat (brain/macro_risk).

The deterministic ``risk_state`` is the spine — we patch ``_collect`` with synthetic fused signal
payloads (so no vendor data is touched) and prove it scores risk_on / caution / risk_off correctly,
names the cracking fragility chain, and emits the teeth. ``apply_risk_state`` is exercised offline as a
pure subtract-only reshaper. The LLM narrative pass is proven flag-OFF (deterministic state stands,
``ran`` False) without ever calling a model."""
from __future__ import annotations

import bot  # noqa: F401  -> vendor/macro onto sys.path
from brain import macro_risk as MR


# ── synthetic fused-signal payloads (the shape MR._collect returns) ──────────────────────────────
def _riskoff_signals():
    return {
        "vol_regime": {"vix": 28.0, "vix_pctile": 0.92, "term_state": "backwardation",
                       "term_ratio": 1.05, "realized_vol": 22.0},
        "put_call": {"equity_pc": 0.7, "sentiment_en": "complacent"},
        "etf_risk": {"tilt": -0.6, "label_en": "RISK-OFF",
                     "legs": {"HYG/TLT": {"chg_5d": -2.0, "chg_1d": -1.2},
                              "DX-Y.NYB": {"chg_5d": 1.5}}},
        "regime": {"quad": "Q1", "quad_name": "Goldilocks", "liquidity_overlay": "contracting",
                   "cycle_tag": "late", "growth_score": -0.3, "inflation_score": -0.4},
        "transition_flags": {"flag_credit_equity": True, "flag_breadth_price": True, "flag_gex": True},
        "conditions": {"complacency": {"warning": True}, "capitulation": {}},
        "sector_rs": [{"ticker": "SMH", "pctile_252d": 100.0, "mom_60d_pct": 48.0},
                      {"ticker": "SOXX", "pctile_252d": 98.0, "mom_60d_pct": 40.0},
                      {"ticker": "XLK", "pctile_252d": 97.0, "mom_60d_pct": 24.0}],
        "crowded_baskets": [{"name": "AI semis", "stage": "distribution", "perf_20d_rel": 6.0}],
        "gex_spy": {"spot": 740.0, "regime": "short", "net_gex_bn": -2.0, "gamma_flip": 748.0,
                    "dist_to_flip_pct": -1.0, "vol_hole_state": "EXPANSION"},
    }


def _benign_signals():
    return {
        "vol_regime": {"vix": 14.0, "vix_pctile": 0.3, "term_state": "contango",
                       "term_ratio": 0.9, "realized_vol": 12.0},
        "put_call": {"equity_pc": 1.0, "sentiment_en": "neutral"},
        "etf_risk": {"tilt": 0.3, "label_en": "RISK-ON", "legs": {}},
        "regime": {"quad": "Q1", "quad_name": "Goldilocks", "liquidity_overlay": "expanding",
                   "cycle_tag": "mid", "growth_score": 0.4, "inflation_score": -0.2},
        "transition_flags": {},
        "conditions": {"complacency": {}, "capitulation": {}},
        "sector_rs": [{"ticker": "XLV", "pctile_252d": 55.0, "mom_60d_pct": 4.0}],
        "crowded_baskets": [],
        "gex_spy": {"spot": 750.0, "regime": "long", "net_gex_bn": 3.0, "gamma_flip": 745.0,
                    "dist_to_flip_pct": 0.7, "vol_hole_state": "NONE"},
    }


def test_risk_off_state_and_teeth(monkeypatch):
    monkeypatch.setattr(MR, "_collect", lambda regime: _riskoff_signals())
    st = MR.risk_state("2026-06-23", {})
    assert st["state"] == "risk_off"
    assert st["fragility"] >= 0.60
    # the cracking driver is the AI build-out chain (SMH/SOXX hot)
    assert any(d["id"] == "ai_buildout" for d in st["drivers"])
    assert st["hot_tickers"] and "SMH" in st["hot_tickers"]
    # teeth: a hard gross cap (≤ risk-off cap) + adds blocked
    assert st["gross_cap"] <= 0.55 + 1e-9
    assert st["allow_adds"] is False
    # driver-aware tilt + a falsifiable, time-bounded exit condition
    assert st["defensive_tilt"]["archetype"] in ("ai_capex_unwind", "liquidity_tightening")
    assert st["falsifier"] and st["check_by"]


def test_risk_on_state(monkeypatch):
    monkeypatch.setattr(MR, "_collect", lambda regime: _benign_signals())
    st = MR.risk_state("2026-06-23", {})
    assert st["state"] == "risk_on"
    assert st["gross_cap"] == 1.0 and st["allow_adds"] is True
    assert st["supportive"] is True


def test_caution_between_thresholds(monkeypatch):
    # a partial-stress tape: liquidity contracting + a soft credit risk-off + dealers short gamma, but
    # vol/crowding still benign → fragility lands between the caution and risk-off thresholds.
    sig = _benign_signals()
    sig["regime"]["liquidity_overlay"] = "contracting"
    sig["regime"]["cycle_tag"] = "late"
    sig["etf_risk"] = {"tilt": -0.3, "label_en": "RISK-OFF", "legs": {}}
    sig["gex_spy"] = {**sig["gex_spy"], "regime": "short", "dist_to_flip_pct": 0.1,
                      "vol_hole_state": "NONE"}
    monkeypatch.setattr(MR, "_collect", lambda regime: sig)
    st = MR.risk_state("2026-06-23", {})
    assert st["state"] == "caution"
    assert 0.55 < st["gross_cap"] <= 0.85                # softer cap than risk-off
    assert st["allow_adds"] is True


def test_gross_cap_and_allow_adds_pure():
    assert MR.gross_cap("risk_on") == 1.0
    assert MR.gross_cap("risk_off") <= 0.55 + 1e-9
    assert 0.55 < MR.gross_cap("caution") <= 0.85
    assert MR.allow_adds("risk_off") is False
    assert MR.allow_adds("caution") is True


def test_apply_risk_state_riskon_noop():
    book = [{"ticker": "NVDA", "weight": 0.3}]
    out = MR.apply_risk_state(book, {"state": "risk_on"})
    assert out is book                                   # byte-identical no-op when calm


def test_apply_risk_state_caps_gross():
    book = [{"ticker": "XLP", "weight": 0.5}, {"ticker": "XLV", "weight": 0.5}]  # gross 1.0, no chains
    out = MR.apply_risk_state(book, {"state": "risk_off", "gross_cap": 0.55, "drivers": []})
    gross = round(sum(r["weight"] for r in out), 4)
    assert abs(gross - 0.55) < 1e-3                       # scaled down to the cash floor
    assert all(r["weight"] > 0 for r in out)             # subtract-only, never negative


def test_apply_risk_state_trims_cracking_chain():
    book = [{"ticker": "NVDA", "weight": 0.3}, {"ticker": "AVGO", "weight": 0.2},
            {"ticker": "XLP", "weight": 0.1}]
    rs = {"state": "risk_off", "gross_cap": 0.55, "drivers": [{"id": "ai_buildout"}]}
    out = MR.apply_risk_state(book, rs)
    by = {r["ticker"]: r["weight"] for r in out}
    assert by["NVDA"] < 0.3 and by["AVGO"] < 0.2         # the cracking chain was trimmed
    assert by["XLP"] == 0.1                              # an unrelated name untouched (gross under cap)


def test_apply_risk_state_blocks_net_new_adds():
    # held=set() → every name is a NET-NEW add; risk-off blocks adds into the cracking chain.
    book = [{"ticker": "NVDA", "weight": 0.2}, {"ticker": "XLP", "weight": 0.1}]
    rs = {"state": "risk_off", "gross_cap": 0.55, "drivers": [{"id": "ai_buildout"}],
          "allow_adds": False}
    out = MR.apply_risk_state(book, rs, held=set())
    tickers = {r["ticker"] for r in out}
    assert "NVDA" not in tickers                          # add into the crack hard-stopped
    assert "XLP" in tickers


def test_apply_risk_state_never_raises():
    assert MR.apply_risk_state(None, None) is None or isinstance(MR.apply_risk_state([], {}), list)
    assert MR.apply_risk_state([], {"state": "risk_off"}) == []


def test_assess_flag_off_returns_deterministic_state(monkeypatch):
    monkeypatch.delenv("MASTERMIND_MACRO_RISK", raising=False)
    monkeypatch.setattr(MR, "_collect", lambda regime: _riskoff_signals())
    st = MR.macro_risk_assess("2026-06-23", {})
    assert st["state"] == "risk_off"                      # the deterministic teeth stand with no LLM
    assert st["ran"] is False                             # the narrative pass did not run
    assert MR.enabled() is False
