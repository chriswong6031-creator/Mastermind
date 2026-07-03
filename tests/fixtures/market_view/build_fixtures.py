"""Frozen incident + calm fixtures for the market_view replay battery (task E0.4).

These are TRIMMED, HAND-BUILT regime/sector-cycle snapshots — NOT copies of the live
production JSON (the hard rule: never pin live market states; intent-only assertions).  They
encode the DOCUMENTED incident SHAPE (label lies risk_on while validated planes dissent
risk_off) at each frozen session, with each plane's own ``asof`` set to the session date so the
per-plane freshness gate passes when the test freezes "today" to the session.

Two families:
  * incident_regime(session)  — the 06-24..07-01 semis-breakdown shape.  From 06-26..07-01 the
    validated consensus (radar + cycles + mtf) reads risk_off against the risk_on Q1 label →
    label_vs_planes.conflict=True.  06-24 is the soft/allowed-either boundary.
  * calm_regime(session)      — a high-confidence agreeing tape → conflict=False, all validated
    planes risk_on.

Plus paired sector_cycles snapshots (the cycles() plane's source).  The builder writes nothing;
the test monkeypatches regime_frame._REGION_PATHS / _CYCLES_PATH at these payloads.
"""
from __future__ import annotations

from typing import Any

# The frozen incident sessions, in order.  06-24 is the soft boundary (conflict allowed either
# way); 06-26..07-01 are the HARD conflict=True sessions.
INCIDENT_SESSIONS: tuple[str, ...] = (
    "2026-06-24", "2026-06-25", "2026-06-26", "2026-06-29", "2026-06-30", "2026-07-01",
)
HARD_CONFLICT_SESSIONS: tuple[str, ...] = (
    "2026-06-26", "2026-06-29", "2026-06-30", "2026-07-01",
)


def _mtf_signals(session: str, *, risk_off: bool) -> dict[str, Any]:
    """A per-ticker MTF block.  risk_off=True → the semis-block bearish tape (more short-bias +
    a high trail-breach share, the SMH tell); risk_off=False → a healthy long-bias tape."""
    if risk_off:
        # 8 short-bias, 3 long-bias, 6 of 12 breaching → adapter reads risk_off.
        states = (["short-bias"] * 8) + (["long-bias"] * 3) + ["mixed"]
        breach = ([True] * 6) + ([False] * 6)
    else:
        states = (["long-bias"] * 9) + (["mixed"] * 2) + ["short-bias"]
        breach = [False] * 12
    tickers = ["SMH", "SOXX", "NVDA", "AMD", "AVGO", "XLK", "QQQ",
               "XLV", "XLU", "XLP", "XLY", "MTUM"]
    signals = []
    for i, tk in enumerate(tickers):
        signals.append({
            "ticker": tk,
            "asof": session,
            "state": states[i % len(states)],
            "above200": True,
            "weekly_bull": not risk_off,
            "trail_breach": breach[i % len(breach)],
            "trail_stop": 100.0,
        })
    return {"asof": session, "tf": "3D", "universe": "us_deep",
            "source": "fixture", "signals": signals}


def incident_regime(session: str) -> dict[str, Any]:
    """The frozen incident regime snapshot for *session* — label lies risk_on, planes dissent.

    Label: Q1 Goldilocks, confidence 0.327, transition_state STABLE (the exact regressed read).
    risk_radar: state='caution', growth scare (VALIDATED risk_off plane).
    mtf_signals: the semis-block bearish tape (VALIDATED risk_off plane) on HARD sessions.
    froth_fragility: narrowing-top / alert (ADVISORY risk_off).
    cross_asset: concentrated top-0% (ADVISORY risk_off).
    """
    hard = session in HARD_CONFLICT_SESSIONS
    return {
        "date": session,
        "quad": "Q1",
        "quad_name": "Goldilocks",
        "confidence": 0.327,
        "liquidity_overlay": "expanding",
        "transition_state": "STABLE",
        "flip_condition": {"margin": 0.05},
        "contradicting": ["breadth", "credit", "ratio"],
        "risk_radar": {
            "schema": "risk_radar.v1", "asof": session, "state": "caution",
            "alert": False, "dominant_scare": "growth", "top_score": 75.4,
            "conjunction": False,
            "headline_en": "CAUTION: Growth scare / defensive rotation (75/100).",
            "drawdown_prob": {"h5": 0.03, "h10": 0.08, "h21": 0.19, "lift_h21": 1.07},
            "cap_leadership": False, "favor_entries": True,
        },
        "mtf_signals": _mtf_signals(session, risk_off=hard),
        "froth_fragility": {
            "asof": session, "headline": 40.6, "band": "watch",
            "quadrant": "narrowing_top",
            "quadrant_en": "Euphoric, leaders distributing under a held index — narrowing-top risk",
            "alert": True, "stealth_fire": False, "unwind_risk": False, "low_naaim_flag": False,
        },
        "risk_state": {
            "schema": "risk_state.v1", "asof": session, "score": 28.1,
            "state": "caution", "label_en": "Caution", "gross_factor": 0.9,
            "cap_leadership": False,
        },
        "turning_point": {
            "asof": session, "present": True, "state": "weakening",
            "raw_fire": True, "active": True, "put_state": "put-present",
            "headline_en": "Regime turn forming — defensive rotation underway",
        },
        "vol_shock": {"asof": session, "score": 53.8, "score_int": 54,
                      "band": "elevated", "gex_gate_scored": False},
        "cross_asset": {
            "asof": session, "verdict": "concentrated",
            "headline": "CONCENTRATED — 3 of 6 markets are one bet. Top 0% of 5y.",
            "absorption_pctile_5y": 1.0,
            "dominant_cluster": ["US", "Commodities", "Dollar"],
        },
        "market_drivers": {
            "asof": session, "verdict": "clear", "primary": "real_rate_shock",
            "primary_label": "Real-rate shock",
            "direction": "real yields falling", "dir_sign": -1, "confidence": "low",
        },
        "dislocation": {
            "asof": session, "verdict": "calm",
            "headline": "No acute dislocation.", "put_state": "put-present",
            "dislocation_active": False,
        },
        "macro_risk": {
            "score": 0.15, "label": "low",
            "components": {"recession": 0.039, "drawdown": 0.151},
        },
    }


def incident_sector_cycles(session: str) -> dict[str, Any]:
    """Frozen sector_cycles for the incident — offense topping, board late-cycle-dominated.

    The offensive leaders (XLK, XLY, XLRE) are Peak/late; the mid-cycle sectors are enough that
    late_cycle count >= entry_favored count → the cycles() plane reads risk_off (defensive
    rotation).  meta.asOf = *session* so the freshness gate passes when today is frozen there.
    """
    def sec(ticker: str, phase: str, pos: float, osc: float) -> dict[str, Any]:
        return {"ticker": ticker, "id": ticker.lower(), "kind": "sector",
                "name": ticker,
                "now": {"phase": phase, "phaseLabel": phase, "pos": pos,
                        "osc_slope": osc, "above200d": True}}
    # The defensive-rotation signature: the offensive/cyclical leaders (XLK, XLY, XLRE, XLC,
    # SMH, XLI) have run to Peak/Downturn and are rolling over (late_cycle), while only the
    # true defensives (XLV, XLU, XLP) are basing (entry-favored).  late(6) > entry(3) → the
    # cycles() plane reads risk_off — the board is late-cycle-dominated, not the entry-favored
    # broad tape a healthy risk-on regime shows.  This is the documented "offense topping while
    # defensives lead" read, not a hand-tuned flip.
    sectors = [
        sec("XLK", "Peak", 82.0, -18.4),      # late (offense topping)
        sec("XLY", "Peak", 78.0, -12.0),      # late
        sec("XLRE", "Peak", 81.7, -5.7),      # late
        sec("SMH", "Peak", 90.0, -20.0),      # late (basket-block, ignored by cycles() keying)
        sec("XLC", "Downturn", 72.0, -9.0),   # late
        sec("XLI", "Peak", 74.0, -8.0),       # late (cyclical rolling over)
        sec("XLV", "Trough", 20.0, 8.0),      # entry (defensive basing)
        sec("XLU", "Trough", 16.0, 11.0),     # entry
        sec("XLP", "Recovery", 30.0, 6.0),    # entry
        sec("XLB", "Downturn", 48.0, -2.4),   # neutral (pos<70)
        sec("XLE", "Downturn", 55.0, -4.0),   # neutral
        sec("XLF", "Downturn", 60.0, -6.0),   # neutral (mid-cycle, not entry)
    ]
    return {"meta": {"asOf": session, "n_sectors": len(sectors), "benchmark": "SPY"},
            "sectors": sectors, "baskets": []}


def calm_regime(session: str) -> dict[str, Any]:
    """A high-confidence agreeing tape — every validated plane risk_on, no conflict.

    Label: Q1 Goldilocks, HIGH confidence 0.82, STABLE.  risk_radar all-clear, mtf healthy
    long-bias, no froth alert, cross-asset diversified.  The calm-tape control (§4.6): conflict
    False, class OFFENSE — byte-identical zero drift.
    """
    return {
        "date": session,
        "quad": "Q1", "quad_name": "Goldilocks", "confidence": 0.82,
        "liquidity_overlay": "expanding", "transition_state": "STABLE",
        "flip_condition": {"margin": 0.30},
        "contradicting": [],
        "risk_radar": {
            "schema": "risk_radar.v1", "asof": session, "state": "calm",
            "alert": False, "dominant_scare": "none", "top_score": 12.0,
            "conjunction": False, "headline_en": "All-clear — no dominant scare.",
            "drawdown_prob": {"h21": 0.06}, "cap_leadership": False, "favor_entries": True,
        },
        "mtf_signals": _mtf_signals(session, risk_off=False),
        "froth_fragility": {
            "asof": session, "headline": 12.0, "band": "calm", "quadrant": "healthy",
            "quadrant_en": "Broad participation, healthy leadership",
            "alert": False, "stealth_fire": False, "unwind_risk": False,
        },
        "risk_state": {
            "schema": "risk_state.v1", "asof": session, "score": 8.0,
            "state": "risk_on", "label_en": "Risk-on", "gross_factor": 1.0,
        },
        "turning_point": {"asof": session, "present": False, "state": "normal",
                          "raw_fire": False, "active": False, "put_state": "put-present"},
        "vol_shock": {"asof": session, "score": 18.0, "band": "low", "gex_gate_scored": False},
        "cross_asset": {"asof": session, "verdict": "diversified",
                        "headline": "Diversified — no crowding.",
                        "absorption_pctile_5y": 0.30, "dominant_cluster": []},
        "market_drivers": {"asof": session, "verdict": "clear", "primary": "earnings",
                           "primary_label": "Earnings", "direction": "up", "dir_sign": 1,
                           "confidence": "high"},
        "dislocation": {"asof": session, "verdict": "calm", "headline": "No dislocation.",
                        "dislocation_active": False, "put_state": "put-present"},
        "macro_risk": {"score": 0.05, "label": "low", "components": {}},
    }


def calm_sector_cycles(session: str) -> dict[str, Any]:
    """Frozen sector_cycles for the calm tape — broad entry-favored board, cycles reads risk_on."""
    def sec(ticker: str, phase: str, pos: float, osc: float) -> dict[str, Any]:
        return {"ticker": ticker, "id": ticker.lower(), "kind": "sector", "name": ticker,
                "now": {"phase": phase, "phaseLabel": phase, "pos": pos,
                        "osc_slope": osc, "above200d": True}}
    sectors = [
        sec("XLK", "Expansion", 55.0, 15.0), sec("XLY", "Recovery", 30.0, 12.0),
        sec("XLV", "Expansion", 50.0, 10.0), sec("XLF", "Expansion", 54.0, 20.0),
        sec("XLI", "Expansion", 53.0, 19.0), sec("XLC", "Trough", 20.0, 8.0),
        sec("XLB", "Recovery", 35.0, 9.0), sec("XLE", "Trough", 25.0, 7.0),
        sec("XLU", "Expansion", 40.0, 6.0), sec("XLP", "Recovery", 30.0, 5.0),
        sec("XLRE", "Recovery", 28.0, 4.0),
    ]
    return {"meta": {"asOf": session, "n_sectors": len(sectors), "benchmark": "SPY"},
            "sectors": sectors, "baskets": []}
