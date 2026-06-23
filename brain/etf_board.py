"""The ETF rotation board — the macro dashboard's signals, pre-digested for the ETF Brain.

The dashboard already computes ~everything an ETF-rotation book needs (``vendor/macro/data/regime/
latest.json``): the quad regime, a ranked sector/factor RELATIVE-STRENGTH table (``sector_rs``),
VIX term structure + dislocation + drawdown gauges, rate→equity transmission, and cross-asset
fragility. Rather than make the Brain dig through ~199 engine modules, this module assembles ONE
clean board: the regime frame, the ranked rotation table, a distilled ``risk_state`` (calm /
elevated / stressed — the SAME read the crisis guardrail in ``bot/etf.py`` keys off, so the
displayed board and the enforced floor never disagree), the duration read, and a per-ETF trend
state (above/below 50d & 200d — the doctrine's validated "trend gate", which the regime file does
NOT pre-compute per name).

Pure + degrade-never-raise: every read falls back to ``{}`` / ``None`` so a missing or half-built
dashboard yields a thin-but-valid board, never an exception.
"""
from __future__ import annotations

import json
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_V = _ROOT / "vendor" / "macro"


def _read(rel: str) -> dict:
    """Read a vendored dashboard JSON (relative to vendor/macro). {} on any miss."""
    try:
        p = _V / rel
        if p.exists():
            d = json.loads(p.read_text())
            return d if isinstance(d, dict) else {}
    except Exception:
        pass
    return {}


def _read_regime() -> dict:
    return _read("data/regime/latest.json")


# ---------------------------------------------------------------------------
# per-ETF trend (the doctrine's validated "trend gate")
# ---------------------------------------------------------------------------

def etf_trend(ticker: str) -> dict:
    """{above_50d, above_200d, pct_vs_200d} for a ticker from the engine price store, or {} when no
    series is available. Cheap SMA over the cached daily closes (the regime file ranks RS but does
    not publish a per-name trend state)."""
    try:
        from portfolio import paper_account
        s = paper_account._fetch_price_series(ticker)
        if s is None or len(s) < 50:
            return {}
        s = s.sort_index()
        last = float(s.iloc[-1])
        sma50 = float(s.tail(50).mean())
        sma200 = float(s.tail(200).mean()) if len(s) >= 200 else None
        out = {"close": round(last, 4), "above_50d": last >= sma50}
        if sma200:
            out["above_200d"] = last >= sma200
            out["pct_vs_200d"] = round((last / sma200 - 1) * 100, 2)
        return out
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# risk_state — the distilled crisis read (display == enforcement)
# ---------------------------------------------------------------------------

def risk_state(regime: dict | None = None, spy_trend: dict | None = None) -> dict:
    """Distil the dashboard's risk gauges into one state ∈ {calm, elevated, stressed} with the
    reasons that set it. This is the SINGLE read both the board (display) and ``bot/etf.py``'s
    crisis floor (enforcement) consume, so they can never disagree.

      stressed — acute risk-off: dislocation 'stressed', high 10% drawdown probability, elevated
                 systemic stress, or a backwardated VIX term structure (panic bid for front vol).
      elevated — fragility building: medium drawdown band, a complacency/breadth-divergence watch,
                 tightening financial conditions, or the broad market (SPY) below its 200d trend.
      calm     — none of the above.

    ``spy_trend`` lets the caller pass an already-computed SPY trend to avoid a second price read.
    """
    r = regime if regime is not None else _read_regime()
    # In the published contract these five gauges live UNDER regime["conditions"] (only dislocation
    # + fed_stance are top-level). Read there first, falling back to top-level for robustness so a
    # flattened/partial contract still works.
    cond = r.get("conditions") if isinstance(r.get("conditions"), dict) else {}

    def _g(key: str) -> dict:
        v = cond.get(key)
        if not isinstance(v, dict):
            v = r.get(key)
        return v if isinstance(v, dict) else {}

    disloc = _g("dislocation")          # top-level → _g falls back to r.get("dislocation")
    ddr = _g("drawdown_risk")
    sysr = _g("systemic_stress")
    risk_app = _g("risk_appetite")
    comp = _g("complacency")
    fcond = _g("financial_conditions")

    dd_band = str(ddr.get("band") or "").lower()
    vix_term = str(risk_app.get("vix_term_state") or risk_app.get("vix_term") or "").lower()
    sys_state = str(sysr.get("state") or "").lower()

    stressed: list[str] = []
    if str(disloc.get("verdict") or "").lower() == "stressed":
        stressed.append("dislocation=stressed")
    if dd_band == "high":
        stressed.append("drawdown_risk=high")
    if sys_state and sys_state not in ("calm", "normal", "loose"):
        stressed.append(f"systemic_stress={sys_state}")
    if "backward" in vix_term:
        stressed.append("vix_term=backwardation")

    elevated: list[str] = []
    if dd_band == "medium":
        elevated.append("drawdown_risk=medium")
    if str(comp.get("state") or "").lower() == "watch":
        elevated.append("complacency=watch")
    if str(fcond.get("trend") or "").lower() == "tightening":
        elevated.append("financial_conditions=tightening")
    spy = spy_trend if isinstance(spy_trend, dict) else etf_trend("SPY")
    if spy.get("above_200d") is False:
        elevated.append("SPY<200d")

    if stressed:
        state = "stressed"
    elif elevated:
        state = "elevated"
    else:
        state = "calm"
    return {
        "state": state,
        "reasons": (stressed + elevated) or ["all clear"],
        "drawdown_band": dd_band or None,
        "vix_term": vix_term or None,
        "dislocation": disloc.get("verdict"),
        "systemic_stress": sys_state or None,
        "spy_above_200d": spy.get("above_200d"),
    }


# ---------------------------------------------------------------------------
# the assembled board
# ---------------------------------------------------------------------------

def build_board(universe=None) -> dict:
    """Assemble the compact ETF rotation board the Brain reads in one call: the regime frame, the
    ranked sector/factor RS table, the distilled risk_state, the duration/rate read, and a per-ETF
    trend state across the universe. Best-effort — a thin board on a cold dashboard, never raises."""
    from portfolio import etf_universe
    universe = sorted(universe if universe is not None else etf_universe.ALL)
    r = _read_regime()

    regime = {k: r.get(k) for k in (
        "date", "quad", "quad_name", "growth_score", "inflation_score",
        "cycle_tag", "liquidity_overlay", "confidence") if k in r}

    # the ranked rotation board — the dashboard's sector/factor RS table (top of book), slimmed.
    sector_rs = []
    for row in (r.get("sector_rs") or [])[:20]:
        if not isinstance(row, dict):
            continue
        sector_rs.append({k: row.get(k) for k in (
            "ticker", "rank", "rs", "mom_20d_pct", "mom_60d_pct",
            "above_200d_trend", "pctile_252d") if k in row})

    # duration / rate read — the dedicated transmission contract carries the real-10y level +
    # direction + regime (the bond headwind/tailwind that sizes TLT/IEF) and the rate→equity net
    # effect per name; pair it with the Fed stance.
    tx = _read("data/transmission/latest.json")
    rates = (tx.get("state") or {}).get("rates") or {}
    tnet = tx.get("transmission") or {}
    duration = {
        "fed_stance": (r.get("fed_stance") or {}).get("stance") if isinstance(r.get("fed_stance"), dict) else r.get("fed_stance"),
        "real_10y": rates.get("real_10y"),
        "real_10y_direction": rates.get("direction"),       # 'rising' real rates = bond headwind
        "rates_regime": rates.get("regime"),                # 'restrictive' / 'neutral' / 'accommodative'
        "rate_to_equity_net": {k: (tnet.get(k) or {}).get("net")
                               for k in ("SPY", "QQQ", "IWM", "XLU", "XLRE")
                               if isinstance(tnet.get(k), dict)},
    }

    trend = {}
    for t in universe:
        tv = etf_trend(t)
        if tv:
            trend[t] = tv

    return {
        "as_of": r.get("date"),
        "regime": regime or {"note": "regime not built yet"},
        "risk_state": risk_state(r, spy_trend=trend.get("SPY")),   # reuse the trend already computed
        "sector_rs": sector_rs,
        "pair_ratios": r.get("pair_ratios"),
        "cross_asset_absorption": (r.get("cross_asset") or {}).get("absorption_ratio")
        if isinstance(r.get("cross_asset"), dict) else None,
        "duration": duration,
        "trend": trend,
        "universe": etf_universe.GROUPS,
        "note": ("Confirmation over prediction: prefer ETFs ranked high in sector_rs AND above their "
                 "200d trend; size duration/cash by risk_state; in a stressed read the desk caps "
                 "offensive gross. RS/factor signals are descriptive (rank-IC≈0) — synthesise, don't "
                 "chase."),
    }
