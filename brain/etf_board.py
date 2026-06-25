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

# Leading-fragility escalation thresholds (tunable engine internals — the per-name overextension /
# factor-concentration limits the Brain reasons with live in config/etf_strategy.yml; these govern
# how many LEADING fragility warns flip the risk gate ahead of the coincident gauges).
_FRAG_ELEVATED_MIN = 1       # ≥1 leading warn → at least elevated (offensive gross capped ~80%)
_FRAG_STRESSED_MIN = 3       # the full fragile cluster (e.g. neg GEX + breadth div + concentration) → stressed
_ABSORPTION_CONCENTRATED_PCTILE = 0.90   # cross-asset absorption ≥90th pctile = one-factor regime


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
# fragility — the LEADING risk signals the coincident gauges miss
# ---------------------------------------------------------------------------

def build_fragility(regime: dict | None = None) -> dict:
    """Distil the dashboard's LEADING fragility signals — the ones the board used to strip and the
    coincident ``risk_state`` gauges (dislocation / drawdown / systemic / VIX-term) don't see until a
    move is already underway: dealer gamma sign (GEX), breadth divergence, cross-asset concentration
    (absorption percentile), per-name take-profit cycle flags, and the dominant market driver vs the
    book's growth/semis leadership.

    These FRONT-RUN the coincident read. The booleans ``{gex_negative, breadth_divergence,
    concentrated}`` are what ``risk_state`` escalates on; ``signals`` + ``note`` are what the Brain
    reads on the board. Pure + degrade-never-raise: a thin block on a cold dashboard, never an
    exception. (Motivated by the 06-22 top-buy: every one of these was lit while risk_state read calm.)
    """
    r = regime if regime is not None else _read_regime()
    cond = r.get("conditions") if isinstance(r.get("conditions"), dict) else {}
    alerts = r.get("alerts") if isinstance(r.get("alerts"), list) else []
    xa = r.get("cross_asset") if isinstance(r.get("cross_asset"), dict) else {}
    comp = cond.get("complacency") if isinstance(cond.get("complacency"), dict) else (
        r.get("complacency") if isinstance(r.get("complacency"), dict) else {})
    md = r.get("market_drivers") if isinstance(r.get("market_drivers"), dict) else {}

    signals: list[dict] = []

    # 1. dealer gamma — a flipped / net-negative GEX means dealers AMPLIFY moves (sell into weakness):
    #    the classic air-pocket precondition. Sourced from the same alert the dashboard surfaces.
    gex_alert = next((a for a in alerts
                      if isinstance(a, dict) and a.get("rule") == "gex_flip_cross"), None)
    gex_negative = gex_alert is not None
    if gex_negative:
        signals.append({"key": "gex_negative", "level": "warn",
                        "detail": gex_alert.get("message") or "net GEX flipped negative (dealers short gamma)"})

    # 2. breadth divergence — the index sits near highs on narrow participation; the advance is thin.
    breadth_div = bool(comp.get("breadth_div"))
    if breadth_div:
        b200, prox = comp.get("breadth_above200_pctile"), comp.get("spy_high_prox")
        bits = ["breadth diverging"]
        if isinstance(b200, (int, float)):
            bits.append(f"only {round(b200 * 100)}th-pctile names >200d")
        if isinstance(prox, (int, float)):
            bits.append(f"SPY {round(prox * 100)}% of its high")
        signals.append({"key": "breadth_divergence", "level": "warn", "detail": ", ".join(bits)})

    # 3. cross-asset concentration — high absorption (a one-factor regime) has historically led drawdowns.
    absb_pctile = xa.get("absorption_pctile_5y")
    concentrated = (isinstance(absb_pctile, (int, float)) and absb_pctile >= _ABSORPTION_CONCENTRATED_PCTILE) \
        or str(xa.get("verdict") or "").lower() == "concentrated"
    if concentrated:
        signals.append({"key": "cross_asset_concentration", "level": "warn",
                        "detail": xa.get("headline") or (
                            f"absorption {round(absb_pctile * 100)}th-pctile (one-factor regime)"
                            if isinstance(absb_pctile, (int, float)) else "cross-asset concentrated")})

    # 4. per-name take-profit cycle flags — the underlyings of the ETFs the book is long flashing
    #    NEARING A HIGH / TOPPING. Informational (does not escalate the gate by itself), but it's the
    #    'your leaders are extended' tell the doctrine's exit stops want to see.
    topping = [str(a.get("message") or "") for a in alerts
               if isinstance(a, dict) and a.get("rule") == "sector_holdings_accumulation"
               and any(k in str(a.get("message") or "").upper()
                       for k in ("TAKE PROFITS", "NEARING A HIGH", "TOPPING"))]
    if topping:
        signals.append({"key": "holdings_topping", "level": "info",
                        "detail": f"{len(topping)} ETF underlying(s) flagged take-profit/topping",
                        "names": topping[:8]})

    # 5. dominant driver — what's ACTUALLY moving the tape, and where AI/semis leadership ranks in it.
    dominant = None
    if md:
        ai = next((s for s in (md.get("scores") or [])
                   if isinstance(s, dict) and s.get("family") == "equity-leadership"), None)
        dominant = {"primary": md.get("primary_label") or md.get("primary"),
                    "direction": md.get("direction"),
                    "ai_semis_strength": (ai or {}).get("strength")}

    warn_count = sum(1 for s in signals if s.get("level") == "warn")
    level = "high" if warn_count >= _FRAG_STRESSED_MIN else ("elevated" if warn_count >= _FRAG_ELEVATED_MIN else "low")
    return {
        "level": level,
        "warn_count": warn_count,
        "gex_negative": gex_negative,
        "breadth_divergence": breadth_div,
        "concentrated": concentrated,
        "absorption_pctile_5y": absb_pctile,
        "dominant_driver": dominant,
        "signals": signals,
        "note": ("LEADING fragility — these front-run the coincident risk gauges. Negative GEX (dealers "
                 "short gamma → moves amplify), breadth divergence (a thin advance), and cross-asset "
                 "concentration (a one-factor regime) together are the setup that precedes air-pockets. "
                 "When ≥1 is lit, de-gross BEFORE risk_state catches up — and do NOT spend cash into "
                 "strength on a quiet coincident read."),
    }


# ---------------------------------------------------------------------------
# risk_state — the distilled crisis read (display == enforcement)
# ---------------------------------------------------------------------------

def risk_state(regime: dict | None = None, spy_trend: dict | None = None,
               fragility: dict | None = None) -> dict:
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

    # LEADING-fragility escalation — front-runs the coincident gauges above. A lit fragility warn
    # (negative GEX / breadth divergence / cross-asset concentration) nudges the gate to elevated even
    # on a quiet coincident tape; the full fragile cluster (≥_FRAG_STRESSED_MIN) goes stressed. This is
    # the fix for risk_state reading 'calm — all clear' the day before a momentum unwind.
    frag = fragility if isinstance(fragility, dict) else build_fragility(r)
    frag_warns: list[str] = []
    if frag.get("gex_negative"):
        frag_warns.append("leading:gex_negative")
    if frag.get("breadth_divergence"):
        frag_warns.append("leading:breadth_divergence")
    if frag.get("concentrated"):
        frag_warns.append("leading:cross_asset_concentrated")

    if stressed or len(frag_warns) >= _FRAG_STRESSED_MIN:
        state = "stressed"
    elif elevated or frag_warns:
        state = "elevated"
    else:
        state = "calm"
    return {
        "state": state,
        "reasons": (stressed + elevated + frag_warns) or ["all clear"],
        "drawdown_band": dd_band or None,
        "vix_term": vix_term or None,
        "dislocation": disloc.get("verdict"),
        "systemic_stress": sys_state or None,
        "spy_above_200d": spy.get("above_200d"),
        "fragility_level": frag.get("level"),
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

    fragility = build_fragility(r)        # LEADING risk — computed once, fed into risk_state below
    return {
        "as_of": r.get("date"),
        "regime": regime or {"note": "regime not built yet"},
        # risk_state reuses the trend already computed AND the fragility just distilled, so the
        # displayed gate and the enforced crisis floor key off the exact same leading + coincident read.
        "risk_state": risk_state(r, spy_trend=trend.get("SPY"), fragility=fragility),
        "fragility": fragility,
        "sector_rs": sector_rs,
        "pair_ratios": r.get("pair_ratios"),
        "cross_asset_absorption": (r.get("cross_asset") or {}).get("absorption_ratio")
        if isinstance(r.get("cross_asset"), dict) else None,
        "duration": duration,
        "trend": trend,
        "universe": etf_universe.GROUPS,
        "note": ("Confirmation over prediction: prefer ETFs ranked high in sector_rs AND above their "
                 "200d trend; size duration/cash by risk_state; in a stressed read the desk caps "
                 "offensive gross. But read `fragility` FIRST: negative GEX, breadth divergence and "
                 "cross-asset concentration LEAD drawdowns and the coincident risk_state lags them — "
                 "de-gross when they light up, and don't buy what's already most extended (per-name "
                 "take-profit flags). RS/factor signals are descriptive (rank-IC≈0) — synthesise, don't chase."),
    }
