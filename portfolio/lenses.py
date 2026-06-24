"""The multi-sided decision matrix — approach every decision from all sides.

NOT a weighted blend (the repo proved blended thematic momentum is rank-IC ~ 0 — averaging
24 lenses would be the worst overfit in the building). Instead three rules:
  (a) DECISION MATRIX — every lens shown as a row with its value + honest status tag.
  (b) CONFLUENCE gates SIZE — how many honest lenses agree, subject to hard vetoes.
  (c) DIVERGENCE is the edge or the trap — where lenses disagree, name which side leads.

Validated lenses (drawdown cone, extension veto) hold decision authority; context lenses
inform conviction but can't drive size alone; a hard veto (parabolic / Altman distress /
cycle-blocked) caps size at 0 no matter how many lenses are bullish. Reads the real engine
outputs under vendor/macro (graceful: a missing field is shown as absent, never imputed).
"""
from __future__ import annotations

import json
from pathlib import Path

import bot  # noqa: F401

_V = Path(__file__).resolve().parent.parent / "vendor" / "macro"


def _load(rel: str):
    p = _V / rel
    return json.loads(p.read_text()) if p.exists() else None


def _g(d, path, default=None):
    cur = d
    for k in path.split("."):
        if isinstance(cur, dict) and k in cur:
            cur = cur[k]
        else:
            return default
    return cur


def _row(lens, value, status, direction, note=""):
    return {"lens": lens, "value": value, "status": status, "direction": direction, "note": note}


# ───────────────────────── price-series helpers (engine store) ─────────────────────────
# A couple of vetoes need real price HISTORY the published per-name JSON does not carry:
#   • the FALLING-KNIFE check needs a recent multi-day return (a 4-day -10% freefall is invisible
#     to the slow 50/200dma + 52w-high fields — they only see it days later), and
#   • the COMMODITY-REGIME gate needs the DRIVING COMMODITY's trend (a gold miner trades on gold,
#     and GC=F is NOT a row in the regime's sector-ETF RS table — so the proxy lookup silently
#     missed and the miner got a free pass).
# Both are computed from the same engine price store paper_account uses (the yahoo store + the
# breadth closes parquet), loaded once and cached. Everything is guarded: if the store is
# unavailable (offline / CI / an un-checked-out submodule) the helpers return None and the
# dependent veto simply does not fire — no false vetoes, behaviour unchanged from before.
_SERIES_CACHE: dict[str, object] = {}
_BREADTH_FRAME = None
_BREADTH_LOADED = False


def _breadth_frame():
    """The macro engine's wide closes cache (S&P single names as columns), loaded once."""
    global _BREADTH_FRAME, _BREADTH_LOADED
    if _BREADTH_LOADED:
        return _BREADTH_FRAME
    _BREADTH_LOADED = True
    try:
        import pandas as pd  # noqa: F401
        from lib import config
        p = config.data_dir() / "breadth" / "_closes_cache.parquet"
        if p.exists():
            import pandas as _pd
            _BREADTH_FRAME = _pd.read_parquet(p)
    except Exception:
        _BREADTH_FRAME = None
    return _BREADTH_FRAME


# the engine's price universe (S&P large-caps, sector ETFs, commodity futures) trades FAR above $1, so
# a sub-$1 (or non-positive) close is a CONTAMINATED tick — the breadth caches carry zero / sub-cent /
# unadjusted ticks (a single $0.005 print next to a $170 one manufactures a +33,999% one-day "return").
# Such a bad `base` price silently poisons the falling-knife velocity (_recent_return) and commodity
# regime reads. Drop them so a data error can't fire (or suppress) a veto. Conservative: it only ever
# removes implausible prices, never a real one for this universe.
_PRICE_FLOOR = 1.0


def _clean_closes(s):
    """Drop non-positive / sub-$1 contaminated ticks from a close-price Series (returns it unchanged
    on any error). Idempotent and safe for the engine's liquid universe."""
    try:
        cleaned = s[s >= _PRICE_FLOOR]
        return cleaned if len(cleaned) else s
    except Exception:
        return s


def _closes(ticker: str):
    """Cached ascending, CLEANED close-price Series for a ticker (single name OR ETF/commodity), or
    None. Mirrors paper_account: the breadth parquet first (S&P single names), then the yahoo store
    (sector ETFs, SPY, commodity futures like GC=F). Contaminated sub-$1 ticks are dropped."""
    if ticker in _SERIES_CACHE:
        return _SERIES_CACHE[ticker]
    s = None
    try:
        import pandas as pd
        fr = _breadth_frame()
        if fr is not None and ticker in getattr(fr, "columns", []):
            s = fr[ticker].astype(float).dropna()
            s.index = pd.to_datetime(s.index)
    except Exception:
        s = None
    if s is None or len(s) == 0:
        try:
            import pandas as pd
            from lib import store
            df = store.read("yahoo", ticker)
            if df is not None and "close" in df.columns and len(df) > 0:
                s = df["close"].astype(float).dropna()
                s.index = pd.to_datetime(s.index)
        except Exception:
            s = None
    try:
        s = _clean_closes(s.sort_index()) if (s is not None and len(s) > 0) else None
    except Exception:
        s = None
    _SERIES_CACHE[ticker] = s
    return s


def _recent_return(ticker: str | None, sessions: int) -> float | None:
    """Trailing total return over the last `sessions` trading days, as a fraction (-0.10 = -10%).
    None when no price history is available (so the dependent veto simply doesn't fire)."""
    if not ticker:
        return None
    s = _closes(ticker)
    try:
        if s is None or len(s) <= sessions:
            return None
        last = float(s.iloc[-1])
        base = float(s.iloc[-1 - sessions])
        return (last / base - 1.0) if base > 0 else None
    except Exception:
        return None


def _commodity_regime(symbol: str) -> dict | None:
    """Trend read on a driving COMMODITY (e.g. GC=F for gold miners) from its own price series:
    {above_200d_trend, mom_60d_pct, downtrend}. None when no history is available. A commodity
    below its 200d trend with non-trivial 60d weakness is a BEAR regime — the gold-miner trap."""
    s = _closes(symbol)
    try:
        if s is None or len(s) < 60:
            return None
        last = float(s.iloc[-1])
        ref = s.tail(200) if len(s) >= 200 else s
        sma200 = float(ref.mean())
        above200 = last > sma200
        base60 = float(s.iloc[-60])
        mom60 = (last / base60 - 1.0) * 100.0 if base60 > 0 else None
        return {"above_200d_trend": above200, "mom_60d_pct": mom60,
                "downtrend": (not above200) and ((mom60 or 0.0) <= -2.0)}
    except Exception:
        return None


# falling-knife thresholds: a sharp, recent multi-day decline we must NOT buy INTO (confirmation
# over prediction — wait for it to stop falling). Tuned to catch a ~4-day -10% freefall (the LPG
# case) that the slow MA / 52w-high fields are still blind to.
_FALL_5D = -0.09          # <= -9% over the last 5 sessions
_FALL_10D = -0.14         # <= -14% over the last 10 sessions
# minimum upside/downside cone for a NEW full-size buy — a symmetric (or inverted) cone is "not
# actually asymmetric", so it does not earn a discretionary conviction slot.
_ASYM_GATE_MIN = 1.1


def _valuation_dir(value_z, cheap, fwd, rev_cagr, eps_cagr):
    """GROWTH-ADJUSTED valuation direction + the PEG used. The raw value factor (P/B, P/S, EY)
    structurally flags every hyper-growth leader 'expensive'; we consult forward-P/E-vs-growth so
    a cheap-for-growth name (low PEG) is not mislabelled. Growth = revenue CAGR first (trailing EPS
    CAGR is noisy/negative on a window), matching the growth lens. Returns (direction, peg)."""
    gr = rev_cagr if (rev_cagr and rev_cagr > 0) else (eps_cagr if (eps_cagr and eps_cagr > 0) else None)
    # PEG only when forward P/E is POSITIVE. A negative forward P/E means the Street models a LOSS —
    # that is the opposite of "cheap for growth", yet fwd<0 / gr>0 yields a negative PEG that would
    # trip the `peg < 0.8` bull path. Guard fwd>0 so a loss-maker never reads valuation-bull.
    peg = round(fwd / gr, 2) if (fwd and fwd > 0 and gr) else None
    # `cheap` is a percentile (high == cheap). 0 is a REAL value (expensive), not missing — the old
    # `(cheap or 50)` idiom silently masked a 0th-pctile (very expensive) name as mid-range neutral.
    cheap_pct = cheap if cheap is not None else 50
    cheap_factor = (value_z or 0) > 0.3 or cheap_pct > 65
    expensive_factor = (value_z or 0) < -0.3 or cheap_pct < 35
    if cheap_factor or (peg is not None and 0 < peg < 0.8):
        return "bull", peg                        # cheap on factors, or cheap-for-growth (low POSITIVE PEG)
    if expensive_factor and (peg is None or peg > 2.0):
        return "bear", peg                        # expensive AND not justified by growth
    return "neutral", peg                          # expensive on factors but growth-justified


def _flows_13f_dir(nb, ns):
    """13F direction with a MIN-SAMPLE + MARGIN gate: a thin read across a handful of curated VIP
    funds is noise for a mega-cap with thousands of holders. Require BOTH (a) >=3 total tracked
    decisions AND (b) a >=2 net margin to fire a direction (else neutral). None when no 13F
    coverage exists. (The old code enforced only the margin — a 2-0 or 0-2 split fired a direction
    despite the docstring promising the >=3 floor.)"""
    if nb is None:
        return None
    _nb, _ns = nb or 0, ns or 0
    if (_nb + _ns) < 3:
        return "neutral"                          # too few tracked decisions to be a signal
    return "bull" if (_nb - _ns) >= 2 else "bear" if (_ns - _nb) >= 2 else "neutral"


# ---------------- alt-data flow lens (Signal Intelligence Desk / Quiver / TrumpFlow) ----------------
def _intelligence() -> dict:
    """The unified per-ticker News+Intelligence bundle (macro engine.intelligence) — one
    file, two facts per name ({news, alt}). Falls back to the standalone feeds when absent."""
    return ((_load("site/intelligence/by_ticker.json") or {}).get("tickers")
            or (_load("data/intelligence/by_ticker.json") or {}).get("tickers") or {})


def _altdata_by_ticker() -> dict:
    """Per-ticker alt-data substrate (engine/altdata_signals, by_ticker.v2)."""
    return _load("site/altdata/by_ticker.json") or _load("data/altdata/by_ticker.json") or {}


def _altdata_mastermind() -> list:
    """The Signal Intelligence Desk's scored emit (engine/altdata_emit, mastermind.v1)."""
    return (_load("site/altdata/mastermind.json") or _load("data/altdata/mastermind.json") or {}).get("signals") or []


def _alt_record(t: str):
    """Best alt-data read for a name: the unified bundle's alt sub-object → the scored
    mastermind signal → the unscored by_ticker.v2 record (only if it has a real channel)."""
    t = t.upper()
    uni = _intelligence().get(t)
    if uni and uni.get("alt"):
        return uni["alt"]
    for s in _altdata_mastermind():
        if (s.get("ticker") or "").upper() == t:
            return {**s, "scored": True}
    rec = (_altdata_by_ticker().get("tickers") or {}).get(t)
    if rec and (rec.get("channels") or rec.get("convergence_score")):
        return {**rec, "scored": False}
    return None


def _altdata_row(t: str):
    """Political/insider/government-contract/affiliation SIGNAL on a name — the supply-side
    'what smart money is DOING' read (vs news_flow's demand-side 'what the tape is SAYING').
    CONTEXT-ONLY — informs conviction + the early-edge/crowded-late divergence, never sizes
    alone. Consumes the scored Signal Intelligence Desk emit when present; legacy convergence
    counts otherwise. None when the name isn't flagged."""
    rec = _alt_record(t)
    if not rec:
        return None
    # ---- scored Signal Intelligence Desk read (signal_score 0-100 + action) ----
    if rec.get("signal_score") is not None:
        score = int(rec.get("signal_score") or 0)
        action = (rec.get("action") or "WATCH").upper()
        conviction = rec.get("conviction") or "low"
        extended = bool(rec.get("extended"))
        affs = rec.get("affiliations") or []
        # bull on a strong buy-side signal; bear on an AVOID/weak read. Extension is NOT
        # gated here — the matrix's extension lens + the political_crowd_trap divergence
        # cross-check it, so a strong-flow-into-extended name stays 'bull' for the trap to catch.
        direction = "bull" if (score >= 65 and action != "AVOID") else \
                    "bear" if (action == "AVOID" or score < 35) else "neutral"
        note = f"signal {score}/100 · {conviction} · {action}"
        if affs:
            note += f" · {len(affs)} affiliated"
        if extended:
            note += " · extended"
        return _row("altdata_flow", {
            "signal_score": score, "conviction": conviction, "action": action,
            "scored_direction": rec.get("direction"), "channels": rec.get("channels") or [],
            "weighted_score": rec.get("weighted_score"), "convergence_score": rec.get("convergence_score"),
            "trump_linked": bool(rec.get("trump_linked")), "rs_vs_spy_60d": rec.get("rs_vs_spy_60d"),
            "extended": extended, "affiliations": affs, "thesis": rec.get("thesis"),
            "falsifier": rec.get("falsifier"), "scored": True}, "context", direction, note)
    # ---- legacy unscored convergence-count read (by_ticker v1/v2) ----
    score = int(rec.get("convergence_score") or 0)
    chans = rec.get("channels") or []
    trump = bool(rec.get("trump_linked"))
    insider = rec.get("insider_net_usd") or 0
    congress = rec.get("congress_net") or 0
    bull = score >= 2 or insider > 0 or congress >= 2 or (trump and rec.get("trump_side") == "buy")
    bear = score == 0 and (rec.get("trump_side") == "sell" or insider < 0)
    direction = "bull" if bull else "bear" if bear else "neutral"
    note = (f"{score}-channel convergence: " + ", ".join(chans)) if score >= 2 else (", ".join(chans) or "alt-data flow")
    if trump:
        note += " · Trump-linked"
    return _row("altdata_flow", {
        "convergence_score": score, "channels": chans, "trump_linked": trump,
        "weighted_score": rec.get("weighted_score"),
        "congress_net": rec.get("congress_net"), "gov_contract_usd_30d": rec.get("gov_contract_usd_30d"),
        "insider_net_usd": rec.get("insider_net_usd"), "dpi_lean": rec.get("dpi_lean"),
        "trump_side": rec.get("trump_side"), "scored": False}, "context", direction, note)


# ---------------- news-flow lens (public-record financial news) ----------------
def _news_by_ticker() -> dict:
    """Per-ticker news-flow substrate published by the macro engine
    (engine/news_signals). Tries the site contract, then the data store."""
    return ((_load("site/news/by_ticker.json") or {}).get("tickers")
            or (_load("data/news/by_ticker.json") or {}).get("tickers")
            or {})


def _news_row(t: str):
    """Public-record financial-news flow for a name — recent headline count, aggregated
    sentiment lean, basket/sector membership from the news surface.
    CONTEXT-ONLY (display-only in the macro engine) — informs narrative/conviction,
    never drives size alone. None when the name has no news record."""
    # unified bundle's news sub-object first, else the standalone news feed
    rec = (_intelligence().get(t.upper(), {}) or {}).get("news") or _news_by_ticker().get(t.upper())
    if not rec:
        return None
    n_recent = rec.get("n_recent") or 0
    lean = rec.get("sentiment_lean") or "neutral"
    n_pos = rec.get("n_pos") or 0
    n_neg = rec.get("n_neg") or 0
    baskets = rec.get("baskets") or []
    sectors = rec.get("sectors") or []
    is_mag7 = bool(rec.get("is_mag7"))
    direction = "bull" if lean == "pos" else "bear" if lean == "neg" else "neutral"
    note = f"{n_recent} recent headlines · lean {lean}"
    if baskets:
        note += f" · {baskets[0]}"
    return _row("news_flow", {
        "n_recent": n_recent, "sentiment_lean": lean, "n_pos": n_pos, "n_neg": n_neg,
        "baskets": baskets, "sectors": sectors, "is_mag7": is_mag7},
        "context", direction, note)


# ---------------- price / trend-confirmation lens (VALIDATED) ----------------
def _trend_row(d) -> dict:
    """Doctrine A1 enforcement: detect whether PRICE has CONFIRMED, not just whether the
    fundamentals/macro look good. A cheap macro/value story with no price confirmation — or one
    that is rolling over — is a value trap, not a buy. Uses the engine's own daily tech block
    (above 50/200dma, %vs 50dma, MACD, RSI, distance off the 52w high). Direction:
      bull  = confirmed uptrend (above both MAs, MACD+, RSI healthy, near highs)
      bear  = rolling over / unconfirmed (below the 50dma, MACD-, RSI weak/over, well off highs)
    'bear' is the no-chase / value-trap guard; the gate refuses to BUY into it."""
    t = d.get("tech") or {}
    a50, a200 = t.get("above50"), t.get("above200")
    p50, p200 = t.get("pct_vs_50dma"), t.get("pct_vs_200dma")
    macd, rsi, offhi = t.get("macd_pos"), t.get("rsi14"), t.get("off_52w_high_pct")
    if a50 is None and p50 is None:
        return _row("trend", None, "missing", None, "no price/tech data")

    # DOWNTREND (bear). Several ways to be in one; the thresholds are tuned so a healthy pullback
    # in a leader (e.g. AVGO: below 50dma, ~ -14% off high, above a rising 200dma) stays NEUTRAL —
    # the "just in time to buy" case — while a genuine decline is caught:
    #   • below the 200dma (long-term trend broken), OR
    #   • below 50dma AND DEEPLY off the high (<= -18%) — a real slide even if MACD still lags
    #     (the NEM gold-miner case: -21% off high, below 50dma, but MACD hadn't crossed yet), OR
    #   • below 50dma AND MACD- AND well off the high (<= -16%) — a confirmed breakdown (LPG), OR
    #   • below 50dma AND MACD- AND RSI < 40 — momentum breaking down.
    off = offhi if offhi is not None else 0
    below200_broken = a200 is False and (p200 is None or p200 <= -2)   # a MEANINGFUL break, not a shallow cross
    # STRUCTURAL downtrend (slow signals) — this is the "name is broken" read and is a HARD exit.
    downtrend = below200_broken \
        or (a50 is False and off <= -18) \
        or (a50 is False and macd is False and off <= -16) \
        or (a50 is False and macd is False and (rsi is not None and rsi < 40))
    # FALLING KNIFE (acute velocity) — a sharp, RECENT multi-day collapse the slow fields miss: a
    # name fresh off its highs that just dropped ~10% in four sessions is still above its 200dma,
    # only mildly off its 52w high, and MACD hasn't crossed — so every structural test above is
    # silent (the LPG freefall the gate bought into). Computed from the real price series. This is
    # an ENTRY veto (don't catch the knife), NOT a structural-downtrend hard exit — so a name we
    # already hold isn't churned out on a single rough week (the synthesis gate splits them).
    tkr = d.get("ticker")
    r5 = _recent_return(tkr, 5)
    r10 = _recent_return(tkr, 10)
    falling_fast = (r5 is not None and r5 <= _FALL_5D) or (r10 is not None and r10 <= _FALL_10D)
    # CONFIRMED UPTREND (bull): above both MAs, MACD+, healthy (not overbought) RSI, near the high
    # AND not in a fresh freefall.
    uptrend = (a50 is True and a200 is True and macd is True
               and (rsi is None or 45 <= rsi <= 75) and (offhi is None or offhi > -12)
               and not falling_fast)
    notes = []
    if a200 is False:
        notes.append("below 200dma (trend broken)")
    if a50 is False:
        notes.append("below 50dma")
    if macd is False:
        notes.append("MACD negative")
    if offhi is not None and offhi <= -16:
        notes.append(f"{offhi:.0f}% off 52w high")
    if rsi is not None and rsi < 40:
        notes.append(f"RSI {rsi:.0f}")
    if falling_fast:
        _drop = min([x for x in (r5, r10) if x is not None], default=None)
        if _drop is not None:
            notes.append(f"down {_drop * 100:.0f}% in recent sessions (falling knife)")
    direction = "bear" if downtrend else "bull" if uptrend else "neutral"
    if direction == "bear":
        note = "downtrend — " + ", ".join(notes)
    elif falling_fast:
        note = "falling knife — " + ", ".join(notes)
    elif direction == "bull":
        note = "price confirmed (uptrend intact)"
    else:
        note = "in a pullback / unconfirmed — not a downtrend"
    return _row("trend", {"above50": a50, "above200": a200, "pct_vs_50dma": p50,
                          "pct_vs_200dma": p200, "macd_pos": macd, "rsi14": rsi,
                          "off_52w_high_pct": offhi, "downtrend": downtrend,
                          "falling_fast": falling_fast, "ret_5d": r5, "ret_10d": r10,
                          "confirmed_uptrend": uptrend}, "validated", direction, note)


# sector -> the sector-ETF the regime's RS table ranks
_SECTOR_ETF = {
    "Financials": "XLF", "Financial Services": "XLF", "Technology": "XLK",
    "Information Technology": "XLK", "Industrials": "XLI", "Health Care": "XLV",
    "Healthcare": "XLV", "Consumer Discretionary": "XLY", "Consumer Cyclical": "XLY",
    "Consumer Staples": "XLP", "Consumer Defensive": "XLP", "Energy": "XLE",
    "Materials": "XLB", "Basic Materials": "XLB", "Real Estate": "XLRE",
    "Utilities": "XLU", "Communication Services": "XLC", "Communications": "XLC",
}

# commodity-driven names whose real benchmark is the COMMODITY, not the broad sector ETF. A gold
# miner in a gold bear market is the textbook trap the sector ETF (XLB) hides — Newmont trades on
# gold, not on chemicals. (The engine carries no industry field, so this is a curated set of the
# major precious-metal miners; extend as needed.)
_COMMODITY_PROXY = {
    "NEM": "GC=F", "GOLD": "GC=F", "AEM": "GC=F", "KGC": "GC=F", "AU": "GC=F", "GFI": "GC=F",
    "HMY": "GC=F", "FNV": "GC=F", "WPM": "GC=F", "RGLD": "GC=F", "PAAS": "GC=F", "AGI": "GC=F",
    "BTG": "GC=F", "EGO": "GC=F", "OR": "GC=F", "SSRM": "GC=F", "CDE": "GC=F", "HL": "GC=F",
}


def _sector_rs_row(d) -> dict:
    """Leadership lens (VALIDATED) — is the name's SECTOR (or driving COMMODITY) actually leading,
    or a laggard? Doctrine: 'be present in the leader' + 'correlation-structure breaking is the
    earliest honest signal'. Catches a whole crowded-but-lagging cohort (cheap regional banks: XLF
    bottom-decile + below 200d; a gold miner while GOLD is in a bear market) WITHOUT touching
    leaders in a top sector. bear = lagging benchmark below its 200d trend AND (bottom-third RS OR
    a steep recent 60-day decline); bull = top-RS benchmark above its trend."""
    sec = (d or {}).get("sector")
    ticker = (d or {}).get("ticker")
    proxy = _COMMODITY_PROXY.get((ticker or "").upper())
    etf = proxy or _SECTOR_ETF.get(sec)
    r = _load("data/regime/latest.json") or {}
    rec = next((x for x in (r.get("sector_rs") or []) if x.get("ticker") == etf), None) if etf else None
    # COMMODITY-DRIVEN name (a miner): judge it by the DRIVING COMMODITY (gold = GC=F) with
    # COMMODITY-appropriate thresholds — a miner that trades on gold must not get the lenient
    # broad-sector RS bar (mom60 <= -12), which would let a mild gold bear pass (the NEM trap). Use
    # the regime table's GC=F row when present, else derive the commodity's trend from its own price
    # series; either way a below-200d + softening commodity votes bear.
    if proxy:
        _a200 = _mom60 = None
        _src = None
        if rec:
            _a200, _mom60, _src = rec.get("above_200d_trend"), rec.get("mom_60d_pct"), "regime_table"
        else:
            cr = _commodity_regime(proxy)
            if cr:
                _a200, _mom60, _src = cr["above_200d_trend"], cr["mom_60d_pct"], "price_series"
        if _a200 is not None:
            lagging = (_a200 is False) and ((_mom60 or 0) <= -2)
            direction = "bull" if (_a200 and (_mom60 or 0) >= 8) else "bear" if lagging else "neutral"
            note = (f"{sec or 'miner'} driven by {proxy} ({_src}): "
                    f"{'above' if _a200 else 'below'} 200d trend"
                    + (f", 60d {_mom60:+.0f}%" if _mom60 is not None else ""))
            return _row("sector_rs", {"sector": sec, "etf": proxy, "commodity": proxy,
                                      "above_200d_trend": _a200, "mom_60d_pct": _mom60,
                                      "commodity_driven": True, "source": _src},
                        "validated", direction, note)
        # commodity regime unavailable (cold store / offline): fail toward CAUTION using the miner's
        # OWN long-term trend (a miner tracks its commodity) rather than silently passing the gate.
        own = (d or {}).get("tech") or {}
        if own.get("above200") is False:
            return _row("sector_rs", {"sector": sec, "etf": proxy, "commodity_driven": True,
                                      "fallback": "own_200dma"}, "validated", "bear",
                        f"{proxy} regime unavailable; {ticker} below its own 200dma — treat driver as bear")
        return _row("sector_rs", {"sector": sec, "etf": proxy, "commodity_driven": True},
                    "missing", None, f"{proxy} regime unavailable")
    if not rec:
        return _row("sector_rs", {"sector": sec, "etf": etf}, "missing", None, "no sector RS map")
    pct = rec.get("pctile_252d")
    a200 = rec.get("above_200d_trend")
    rank = rec.get("rank")
    mom60 = rec.get("mom_60d_pct")
    # a benchmark below its 200d trend that is EITHER bottom-third RS OR in a steep 60-day decline
    lagging = a200 is False and ((pct if pct is not None else 100) < 35 or (mom60 or 0) <= -12)
    direction = "bull" if (a200 and (pct or 0) >= 70) else "bear" if lagging else "neutral"
    # pct can be None (an ETF in the table without a 252d window yet) — guard the format so the lens
    # never raises a TypeError that conviction.build would swallow as a silent name-drop.
    _pct_str = f"{pct:.0f}th pctile" if pct is not None else "pctile N/A"
    note = (f"{sec} ({etf}) RS rank {rank}, {_pct_str}, "
            f"{'above' if a200 else 'below'} 200d trend")
    return _row("sector_rs", {"sector": sec, "etf": etf, "rank": rank, "pctile": pct,
                              "above_200d_trend": a200}, "validated", direction, note)


# ---------------- per-NAME lenses ----------------
def _name_rows(t: str) -> list[dict]:
    d = _load(f"site/stockdata/{t}.json")
    flows = (_load("site/stockdata/fund_flows.json") or {}).get(t)
    gx = _load(f"site/gex/{t}.json")
    ad = _altdata_row(t)                 # political/insider/contract flow — independent of stockdata
    nw = _news_row(t)                    # public-record financial news flow — context only
    rows = []
    if not d:
        # a Trump-linked entity (ABTC/DJT/...) may carry alt-data flow but no S&P stockdata
        base = ([ad] if ad else []) + ([nw] if nw else [])
        return base + [_row("conviction", None, "missing", None, "no stockdata")]

    # valuation — GROWTH-ADJUSTED (the NVDA false-reject fix). The raw value-factor z (built from
    # P/B, P/S, earnings yield) structurally flags EVERY hyper-growth leader as 'expensive': NVDA's
    # value_z=-1.05 comes from a ~32x P/B and ~24x P/S, not from being dear vs its growth. The old
    # formula read only value_z + trailing_pe, so it (a) subtracted a bull AND (b) armed the
    # 'distribution' divergence (lead+valuation-bear+13F-bear) — wrongly rejecting a 16.6x-forward /
    # 67% rev-growth (PEG~0.25) leader. We now consult forward P/E vs growth (PEG, using revenue
    # CAGR like the growth lens — trailing EPS CAGR is noisy/negative on a window): a cheap-for-
    # growth name is NOT 'expensive'.
    vz = _g(d, "valuation.value_z")
    cheap = _g(d, "valuation.trailing_pe.cheap")
    fwd = _g(d, "valuation.forward_pe")
    dirv, peg = _valuation_dir(vz, cheap, fwd, _g(d, "financials.multiyear.rev_cagr"),
                               _g(d, "financials.multiyear.eps_cagr"))
    rows.append(_row("valuation", {"value_z": vz, "cheap_pctile": cheap, "forward_pe": fwd, "peg": peg,
                                   "basis": "forward" if fwd else "trailing-only"}, "context", dirv))

    # quality / accounting
    qz = _g(d, "conviction.axes.quality.z")
    acct = _g(d, "conviction.axes.quality.flags.accounting") or _g(d, "accounting_quality.verdict")
    dirq = "bear" if acct == "warn" else "bull" if (qz or 0) > 0.3 else "neutral"
    rows.append(_row("quality", {"quality_z": qz, "accounting": acct}, "context", dirq))

    # growth
    rc, ec = _g(d, "financials.multiyear.rev_cagr"), _g(d, "financials.multiyear.eps_cagr")
    g = rc if rc is not None else ec
    dirg = "bull" if (g or 0) > 10 else "bear" if (g is not None and g < 0) else "neutral"
    rows.append(_row("growth", {"rev_cagr": rc, "eps_cagr": ec}, "partial" if g is not None else "missing", dirg))

    # solvency (Altman / Piotroski) — feeds the distress veto, but only when the Altman read is
    # ACTUALLY a valid, complete distress signal. The classic Altman Z is structurally invalid for
    # high-leverage non-manufacturers (utilities, financials, REITs — levered by design), so for
    # those its "distress" is CONTEXT, never a hard veto (mirrors the China engine, which already
    # demotes Altman to context). And a score baked with approx=True was computed WITHOUT the X4
    # leverage leg (missing/un-reconstructable liabilities) — too incomplete to size a name to 0 on.
    # Only a complete distress score in a sector where Z is valid escalates to the unoverridable
    # veto; otherwise the distress still shows (and votes bear) but does not hard-block.
    az = _g(d, "financials.multiyear.altman.zone")
    az_approx = bool(_g(d, "financials.multiyear.altman.approx"))
    az_exempt = _altman_sector_exempt(d.get("sector"))
    az_distress = (az == "distress")
    az_veto = az_distress and not az_exempt and not az_approx
    az_context = ("sector-invalid" if (az_distress and az_exempt)
                  else "approx-data" if (az_distress and az_approx) else None)
    rows.append(_row("solvency",
                     {"altman_zone": az, "piotroski": _g(d, "financials.multiyear.piotroski.score"),
                      "altman_veto": az_veto, "altman_context": az_context},
                     "partial" if az else "missing",
                     "bear" if (az_distress and not az_approx) else "neutral"))

    # potential / asymmetry (upside vs downside cone) — derived
    mfe = _g(d, "anticipation.horizons.medium.mfe_med")
    dda = _g(d, "anticipation.horizons.medium.dd_avg")
    asym = (mfe / abs(dda)) if (mfe is not None and dda) else None
    dira = "bull" if (asym or 0) > 1.5 else "bear" if (asym is not None and asym < 0.8) else "neutral"
    rows.append(_row("asymmetry", {"upside_downside": round(asym, 2) if asym else None, "mfe_med": mfe, "dd_avg": dda},
                     "partial" if asym else "missing", dira))

    # risk — drawdown cone (VALIDATED)
    ddt = _g(d, "anticipation.horizons.medium.dd_tail")
    rows.append(_row("risk_drawdown", {"dd_tail": ddt, "p_up": _g(d, "anticipation.horizons.medium.p_up"),
                                       "thin": _g(d, "anticipation.horizons.medium.thin")},
                     "validated", "bear" if (ddt or 0) < -25 else "neutral"))

    # risk — extension veto (VALIDATED)
    grade = _g(d, "conviction.ext.grade") or _g(d, "conviction.extension.grade")
    para = bool(_g(d, "conviction.ext.parabolic") or _g(d, "conviction.extension.parabolic"))
    pv2 = _g(d, "tech.pct_vs_200dma")
    rows.append(_row("extension", {"grade": grade, "parabolic": para, "pct_vs_200dma": pv2}, "validated",
                     "bear" if (para or grade in ("stretched", "parabolic") or (pv2 or 0) >= 30) else "neutral"))

    # price/trend CONFIRMATION (VALIDATED) — extension catches a name that's run too far;
    # this catches the opposite failure the gate was blind to: a name TOPPING / rolling over.
    rows.append(_trend_row(d))

    # sector leadership (VALIDATED) — is the name's sector actually leading, or a laggard cohort?
    rows.append(_sector_rs_row(d))

    # flows — 13F smart money. MIN-SAMPLE + MARGIN gate (the NVDA false-reject fix): a 1-name
    # margin across a handful of curated VIP funds is noise for a mega-cap with thousands of
    # institutional holders — NVDA fired 'bear' (distribution) on 1 buying vs 2 selling while
    # TigerGlobal's +9.1% NVDA add was tagged 'hold' (uncounted). Require >=3 tracked decisions
    # AND a >=2 net margin before firing a direction; otherwise the signal is too thin → neutral.
    nb, ns = _g(d, "smart_money.n_buying"), _g(d, "smart_money.n_selling")
    sm_asof = _g(d, "smart_money.as_of")               # 13F snapshot quarter-end (filed ~45d+ later)
    dirf = _flows_13f_dir(nb, ns)
    note13f = ""
    if nb is not None:
        note13f = (f"LAGGED 13F snapshot{f' as of {sm_asof}' if sm_asof else ''} (filed up to 45d after "
                   f"quarter-end): {nb} added vs {ns} trimmed LAST QUARTER. Positioning context only — "
                   f"NOT real-time flow; says nothing about buying any recent dip/move.")
    rows.append(_row("flows_13f", {"n_buying": nb, "n_selling": ns, "vip": _g(d, "smart_money.vip"),
                                   "as_of": sm_asof},
                     "context" if (nb is not None) else "missing", dirf, note13f))

    # flows — active ETF accumulation
    if flows:
        sig = flows[0] if isinstance(flows, list) else flows
        fd = _g(sig, "direction")
        dire = "bull" if fd in ("add", "accumulate", "accumulating") else "bear" if fd in ("trim", "exit", "distributing") else "neutral"
        rows.append(_row("flows_etf", {"direction": fd, "conviction_pp": _g(sig, "conviction_pp")}, "context", dire))
    else:
        rows.append(_row("flows_etf", None, "missing", None))

    # options positioning
    if gx:
        reg = _g(gx, "summary.gamma_regime") or _g(gx, "summary.regime")
        rows.append(_row("options", {"gamma_regime": reg, "call_wall": _g(gx, "summary.call_wall"),
                                     "put_wall": _g(gx, "summary.put_wall"), "exp_move_d": _g(gx, "expected_move.daily_pct")},
                         "context", "bull" if reg in ("positive", "long") else "bear" if reg in ("negative", "short") else "neutral"))
    else:
        rows.append(_row("options", None, "missing", None))

    # rate/inflation sensitivity
    ms = _g(d, "macro_sensitivity")
    if ms:
        rg = _g(ms, "regime")
        rows.append(_row("rate_inflation", {"tier": _g(ms, "tier"), "duration": _g(ms, "duration"), "regime": rg},
                        "context", "bull" if rg == "tailwind" else "bear" if rg == "headwind" else "neutral"))
    else:
        rows.append(_row("rate_inflation", None, "missing", None))

    # conviction composite (the net per-name read). VERDICT-GATED (2026-06-22): the analyzer's
    # `band`/`score` are a within-board PERCENTILE RANK, so a top-ranked mega-cap reads band='high'
    # even when the analyzer's OWN decision is "wait/avoid" — cycle/extension blocked, size bucket
    # 'avoid', 0% allocation (e.g. NVDA "Strong name · wrong tape — wait for a base"). Casting that
    # RANK as a flat BULL vote let a name the analyzer itself refuses leak a bull vote into the
    # confluence gate. So a high/strong band only votes BULL when the analyzer is NOT blocking the
    # buy; if it blocks (avoid bucket / cycle-blocked / entry-blocked / 0% size), the rank is
    # downgraded to NEUTRAL — the name ranks well but the tape doesn't confirm, so it casts no vote.
    band = _g(d, "conviction.band")
    sz = _g(d, "conviction.size.pct")
    cv_bucket = _g(d, "conviction.size.bucket")
    cv_blocked = (bool(_g(d, "conviction.cycle_blocked"))
                  or cv_bucket == "avoid"
                  or bool(_g(d, "conviction.axes.entry.blocked"))
                  or (sz is not None and sz <= 0))
    if band in ("strong", "high"):
        dirc = "neutral" if cv_blocked else "bull"
    elif band == "avoid":
        dirc = "bear"
    else:
        dirc = "neutral"
    rows.append(_row("conviction", {"band": band, "score": _g(d, "conviction.score"), "size_pct": sz,
                                    "verdict": _g(d, "conviction.verdict"), "blocked": cv_blocked,
                                    "cycle_blocked": bool(_g(d, "conviction.cycle_blocked"))},
                     "partial", dirc))

    # alt-data political/insider/contract flow (context)
    if ad:
        rows.append(ad)
    # news-flow: public-record financial news (context)
    if nw:
        rows.append(nw)
    # name -> theme via basket membership: narrative + policy
    rows += _theme_context_for_name(d)
    rows += _macro_rows()
    return rows


def _theme_signal(theme_id: str | None) -> dict | None:
    """The basket engine's read on a theme: its allocation RANK/score/eligibility (the
    conservative, validated ranking — NOT the momentum-only 'dominant' theme_intel label) plus
    its recent 5d/20d RELATIVE performance. This is how the bot finally LISTENS to what the
    Narrative-Basket engine already knows (e.g. regional_banks ranks #17, 5d rel negative)."""
    if not theme_id:
        return None
    alloc = _load("site/allocationdata/allocation.json") or {}
    ranks = alloc.get("ranks") or []
    rec = next((r for r in ranks if r.get("id") == theme_id or r.get("theme") == theme_id), None)
    n = len(ranks) or 1
    perf = None
    for b in ((_load("site/basketdata/baskets.json") or {}).get("baskets") or []):
        if b.get("id") == theme_id:
            perf = b.get("perf") or {}
            break
    rel5 = ((perf or {}).get("5d") or {}).get("rel")
    rel20 = ((perf or {}).get("20d") or {}).get("rel")
    return {"id": theme_id, "rank": (rec or {}).get("rank"), "n_themes": n,
            "score": (rec or {}).get("score"), "eligible": (rec or {}).get("eligible"),
            "rel_5d": rel5, "rel_20d": rel20}


def _theme_context_for_name(d) -> list[dict]:
    """Per-name narrative/theme lens — now a REAL vote (was direction=None = silent). A name
    whose theme is a low-ranked laggard or is rolling over (5d rel negative) votes BEAR; a name
    in a genuine top-third leadership theme that is NOT rolling over votes BULL."""
    mem = _g(d, "baskets_membership") or _g(d, "baskets_membership.themes")
    theme_id = None
    if isinstance(mem, list) and mem:
        # membership entries key the theme on 'slug' (real data); 'id' is a legacy fallback. The
        # slug matches allocation.json ranks[].id, so _theme_signal resolves. (Without this the
        # whole theme lens was silently dead — caught by the adversarial review.)
        theme_id = mem[0] if isinstance(mem[0], str) else (_g(mem[0], "slug") or _g(mem[0], "id"))
    sig = _theme_signal(theme_id)
    if not sig:
        return [_row("narrative", {"basket": theme_id}, "missing", None,
                     "no mapped theme — theme confirmation unavailable")]
    rank, n, score, rel5 = sig["rank"], sig["n_themes"], sig["score"], sig["rel_5d"]
    # RANK-percentile bands (not the absolute score<0 crossing, which flipped ~2/3 of themes bear).
    # leader = top third; laggard = bottom third or ineligible; a rolling-over theme (5d rel < -1%)
    # is bearish wherever it ranks.
    rolling = rel5 is not None and rel5 < -0.01
    leader = (rank is not None and n and rank <= max(1, n / 3.0)) and not rolling
    laggard = (rank is not None and n and rank > 2.0 * n / 3.0) or sig.get("eligible") is False
    direction = "bear" if (laggard or rolling) else "bull" if leader else "neutral"
    note = f"theme '{theme_id}' rank {rank}/{n}" + (f", score {score:+.2f}" if score is not None else "") \
        + (f", 5d rel {rel5 * 100:+.1f}%" if rel5 is not None else "")
    return [_row("narrative", {"basket": theme_id, "rank": rank, "n_themes": n, "score": score,
                               "rel_5d": rel5, "rel_20d": sig["rel_20d"], "laggard": laggard,
                               "rolling_over": rolling, "leader": leader},
                 "validated", direction, note)]


# ---------------- per-THEME lenses ----------------
def _theme_rows(theme_id: str) -> list[dict]:
    alloc = _load("site/allocationdata/allocation.json") or {}
    rank = next((r for r in (alloc.get("ranks") or []) if r.get("id") == theme_id or r.get("theme") == theme_id), None)
    rows = []
    if rank:
        elig = rank.get("eligible")
        rows.append(_row("narrative", {"rank": rank.get("rank"), "score": rank.get("score"),
                                       "eligible": elig, "durability": rank.get("durability")}, "partial",
                         "bull" if elig else "bear" if elig is False else "neutral"))
    else:
        rows.append(_row("narrative", None, "missing", None))
    rows.append(_policy_row(theme_id))
    rows += _macro_rows()
    return rows


def _policy_row(theme_id: str) -> dict:
    intel = _load("data/policy/intel.json") or {}
    rot = intel.get("rotation") or {}
    targ = {(_g(x, "theme") or _g(x, "sector") or str(x)).lower() for x in (rot.get("targeted") or [])}
    starv = {(_g(x, "theme") or _g(x, "sector") or str(x)).lower() for x in (rot.get("starved") or [])}
    tid = (theme_id or "").lower()
    d = "bull" if tid in targ else "bear" if tid in starv else "neutral"
    return _row("policy_tilt", {"targeted": tid in targ, "starved": tid in starv,
                                "grand_strategy": _g(intel, "administration.grand_strategy")},
                "context", d if (targ or starv) else None)


# ---------------- macro lenses (shared) ----------------
def _vol_regime_row() -> dict:
    """The validated INDEX vol-regime (engine/vol_regime -> site/vol/mastermind.json, schema
    vol_regime.context.v1). CONTEXT-only and SUBTRACT-ONLY: it votes BEAR (nudges gross DOWN)
    in a risk-off kill-switch state (warning / backwardation-stress) and NEUTRAL otherwise —
    it can never manufacture a bullish vote to size UP. It sits in the de-correlated _MACRO_BLOC
    (it reads the same risk-on/off surface as macro_risk / cross_asset), so it never sizes alone;
    it only sharpens the one net macro vote toward caution when market vol stress is rising.
    Drawdown / capital-efficiency, not alpha. Absent file -> 'missing' (shown, never imputed)."""
    v = _load("site/vol/mastermind.json") or _load("data/vol/mastermind.json")
    if not v:
        return _row("vol_regime", None, "missing", None)
    regime = v.get("regime")
    risk_off = regime in ("warning", "backwardation-stress")
    return _row("vol_regime", {
        "regime": regime,
        "kill_switch": bool(v.get("kill_switch")),
        "vol_target_scalar": v.get("vol_target_scalar"),
        "scored_active": bool(v.get("scored_active")),     # gate state — display vs validated
        "scored_score": v.get("scored_score"),
        "ts_slope_state": v.get("ts_slope_state"),
        "fragility": v.get("fragility_confluence"),
    }, "context", "bear" if risk_off else "neutral",
        note=("index vol-regime risk-off — trim gross (context caution, not a size driver)"
              if risk_off else ""))


def _macro_rows() -> list[dict]:
    r = _load("data/regime/latest.json") or {}
    mr = _g(r, "macro_risk.score")
    cuts = _g(r, "fed_path.implied_cuts_12m")
    ca = _g(r, "cross_asset.verdict")
    return [
        _row("macro_risk", {"score": mr, "quad": r.get("quad"), "liquidity": r.get("liquidity_overlay")},
             "context", "bear" if (mr or 0) > 0.66 else "bull" if (mr or 0) < 0.34 else "neutral"),
        _row("fed_path", {"implied_cuts_12m": cuts, "lean": _g(r, "fed_path.headline")}, "context",
             "bull" if (cuts or 0) > 0 else "bear" if (cuts or 0) < 0 else "neutral"),
        _row("cross_asset", {"verdict": ca, "absorption": _g(r, "cross_asset.absorption_ratio")}, "context",
             "bear" if ca in ("concentrated", "one-trade", "fragile") else "neutral"),
        _vol_regime_row(),
    ]


# ---------------- the public API ----------------
def decision_matrix(subject: str, kind: str = "name") -> dict:
    rows = _name_rows(subject.upper()) if kind == "name" else _theme_rows(subject)
    return {"subject": subject, "kind": kind, "rows": rows}


# Sectors where the classic Altman Z-score is structurally invalid: high leverage by design /
# a fundamentally different capital structure (Altman himself excluded financials). For these the
# Altman "distress" zone is CONTEXT only — it never escalates to the unoverridable hard veto.
# (Telecom is genuinely levered too but lives under the mixed GICS "Communication Services" sector
# alongside net-cash names like GOOGL/META, so it is NOT blanket-exempted here.)
_ALTMAN_EXEMPT_SECTORS = ("utilit", "financ", "real estate", "bank", "insur")


def _altman_sector_exempt(sector: str | None) -> bool:
    s = (sector or "").lower()
    return any(tok in s for tok in _ALTMAN_EXEMPT_SECTORS)


def _hard_vetoes(rows: list[dict]) -> list[str]:
    v = []
    for r in rows:
        val = r.get("value") or {}
        if r["lens"] == "extension" and val.get("parabolic"):
            v.append("parabolic")
        if r["lens"] == "solvency" and val.get("altman_veto"):
            v.append("altman_distress")
        if r["lens"] == "conviction" and (val.get("cycle_blocked") or val.get("band") == "avoid" or val.get("size_pct") == 0):
            v.append("cycle_blocked")
    return v


def _divergences(rows: list[dict]) -> list[dict]:
    g = {r["lens"]: r for r in rows}

    def d(lens):
        return (g.get(lens) or {}).get("direction")
    # name-level: conviction band proxies "is this a hot leader/story"; theme-level uses narrative
    lead = d("narrative") or d("conviction")
    # a hard-vetoed name is size-0 no matter what — suppress the BULLISH "buy / edge" divergence
    # labels for it (they otherwise tag a parabolic/Altman/cycle-blocked name as a "full-size
    # candidate", contradicting the gate). The TRAP labels (distribution/crowded_top/crowd_trap)
    # stay — they are warnings, correct to show on any name.
    blocked = bool(_hard_vetoes(rows))
    out = []
    if lead == "bull" and d("valuation") == "bear" and d("flows_13f") == "bear":
        out.append({"pattern": "distribution",
                    "read": "story loudest as smart money leaves — hot/high-conviction but expensive + 13F-selling → late-stage, trim/avoid"})
    if not blocked and d("flows_13f") == "bull" and d("valuation") == "bull" and lead != "bull":
        out.append({"pattern": "early_edge",
                    "read": "price hasn't moved but smart money + value are in — highest-asymmetry early-follow"})
    if not blocked and lead == "bull" and d("valuation") == "bull" and d("flows_13f") == "bull":
        out.append({"pattern": "high_confluence_buy",
                    "read": "all sides align and risk vetoes pass → full-size candidate"})
    if d("extension") == "bear" and d("flows_etf") == "bear":
        out.append({"pattern": "crowded_top",
                    "read": "extended + flows rolling over → avoid regardless of narrative heat"})
    if not blocked and d("policy_tilt") == "bull" and d("valuation") == "bull" and lead != "bull":
        out.append({"pattern": "policy_early",
                    "read": "policy tailwind + cheap before the crowd notices — early thematic edge"})
    # alt-data flow (political/insider/contract convergence) — the edge or the trap
    ad = d("altdata_flow")
    if ad == "bull" and d("extension") == "bear":
        out.append({"pattern": "political_crowd_trap",
                    "read": "political/insider/contract money piling into an already-EXTENDED name — late; the political crowd may be the exit liquidity, not the edge"})
    elif not blocked and ad == "bull" and lead != "bull" and d("valuation") != "bear":
        out.append({"pattern": "political_flow_early",
                    "read": "political/insider/contract flow converging on a name the tape hasn't recognized yet — early alt-data edge (context, not a size driver)"})
    return out


# correlated lens blocs — each collapses to ONE net vote so a cheap-value-in-a-benign-macro
# cohort can't manufacture confluence by tripping five correlated lenses at once. The leadership,
# price, flow and positioning lenses stay INDEPENDENT (they are genuinely different evidence).
_FUND_BLOC = {"valuation", "quality", "growth", "solvency", "asymmetry"}   # the value/quality story
_MACRO_BLOC = {"macro_risk", "fed_path", "cross_asset", "rate_inflation", "vol_regime"}  # the shared regime story

# ── self-calibrating gate: weight each lens vote by its EMPIRICAL reliability ──────────────────────
# The confluence below is no longer a flat vote COUNT — each effective vote is scaled by how often
# that lens actually PREDICTED on resolved theses (brain.outcome_ledger.lens_weights, fed by the
# realized rel-return of matured cohorts). Until enough cohorts resolve every weight is 1.0, so
# confluence is IDENTICAL to the old equal-vote count; as the engine earns a track record, the lenses
# that proved reliable count more and the coin-flip lenses count less — the gate teaches itself.
_LENS_WEIGHTS_CACHE: dict | None = None


def _lens_reliability() -> dict:
    """{lens: weight} from brain.outcome_ledger.lens_weights — {} (=> equal votes, today's behaviour)
    until resolved cohorts accrue. Cached per process; guarded so a missing/empty ledger never breaks
    the gate (returns {} -> all weights default to 1.0)."""
    global _LENS_WEIGHTS_CACHE
    if _LENS_WEIGHTS_CACHE is not None:
        return _LENS_WEIGHTS_CACHE
    w: dict = {}
    try:
        from brain import outcome_ledger
        w = outcome_ledger.lens_weights() or {}
    except Exception:
        w = {}
    _LENS_WEIGHTS_CACHE = w
    return w


def synthesize(matrix: dict) -> dict:
    """Confluence + the size-authority gate.

    DE-CORRELATION: the fundamental/value bloc and the shared macro bloc each collapse to one NET
    vote (was: ~9 correlated lenses, which let a homogeneous cheap cohort clear together). The
    leadership (sector_rs), price (trend), theme (narrative), flow and options lenses vote
    independently — that is genuinely diverse evidence. This makes a cheap laggard-sector name fail
    the gate (its one fundamental bull is outweighed by sector_rs/trend/theme bears) while a leader
    in a top sector with independent flow/price confirmation still passes. Hard vetoes
    (parabolic / Altman / cycle-blocked) still cap size at 0."""
    rows = matrix["rows"]
    by_lens = {r["lens"]: r for r in rows}

    def _bloc_net(names: set) -> str | None:
        votes = [r["direction"] for r in rows if r["lens"] in names and r["direction"] in ("bull", "bear")]
        if not votes:
            return None
        net = sum(1 if v == "bull" else -1 for v in votes)
        return "bull" if net > 0 else "bear" if net < 0 else "neutral"

    fund, macro = _bloc_net(_FUND_BLOC), _bloc_net(_MACRO_BLOC)
    # build the effective votes as (sign, lens_key) so each can be reliability-weighted; the two
    # de-correlated blocs vote as one each, the independent lenses vote individually (as before).
    _rel = _lens_reliability()
    _votes: list[tuple[int, str]] = []
    if fund in ("bull", "bear"):
        _votes.append((1 if fund == "bull" else -1, "_fund_bloc"))
    if macro in ("bull", "bear"):
        _votes.append((1 if macro == "bull" else -1, "_macro_bloc"))
    for r in rows:
        if r["lens"] in _FUND_BLOC or r["lens"] in _MACRO_BLOC:
            continue
        if r["direction"] in ("bull", "bear"):
            _votes.append((1 if r["direction"] == "bull" else -1, r["lens"]))

    bull = sum(1 for s, _ in _votes if s > 0)
    bear = sum(1 for s, _ in _votes if s < 0)
    n = len(_votes)
    vetoes = _hard_vetoes(rows)
    # reliability-weighted confluence: sum(sign * lens_weight) / sum(lens_weight). With every weight
    # at its 1.0 default this is EXACTLY (bull - bear) / n — the prior behaviour — until lens_edge accrues.
    _num = sum(s * _rel.get(lk, 1.0) for s, lk in _votes)
    _den = sum(_rel.get(lk, 1.0) for s, lk in _votes) or 1.0
    confluence = round(_num / _den, 3)
    trend_dir = (by_lens.get("trend") or {}).get("direction")
    theme_dir = (by_lens.get("narrative") or {}).get("direction")
    sector_dir = (by_lens.get("sector_rs") or {}).get("direction")
    sector_lagging = sector_dir == "bear"            # name's sector is below its 200d trend + bottom-third RS
    # ASYMMETRY GATE — a discretionary conviction buy must actually BE asymmetric (upside cone
    # meaningfully larger than the downside cone). A symmetric (~1.0) or inverted cone is "not
    # asymmetric" and does not earn a slot, no matter how the rest reads. None (no cone data) does
    # NOT block — degrade gracefully, same as before.
    asym_ratio = ((by_lens.get("asymmetry") or {}).get("value") or {}).get("upside_downside")
    weak_asymmetry = asym_ratio is not None and asym_ratio < _ASYM_GATE_MIN
    # FALLING KNIFE — an acute recent multi-day collapse blocks a NEW buy (don't catch the knife)
    # but is kept SEPARATE from the structural downtrend so a held name isn't force-exited on it.
    price_falling_fast = bool(((by_lens.get("trend") or {}).get("value") or {}).get("falling_fast"))
    # LEADERSHIP GATE (doctrine: be present in the leader; correlation-structure breaking is the
    # earliest signal). A cheap name in a hard-lagging sector is fighting the tape — it does not
    # get a BUY no matter how cheap, UNLESS it sits in a genuine LEADING theme (a cross-sectional
    # signal that, unlike stale per-name price, a laggard cohort can't fake). This drops the crowded
    # regional-bank cohort while leaving real leaders (Tech/AI) and a hot-theme name in a soft
    # sector untouched.
    leadership_ok = (not sector_lagging) or (theme_dir == "bull")
    # FALLING-KNIFE GATE (doctrine A1: confirmation over prediction). A name in a confirmed
    # DOWNTREND (trend lens = bear) does not get a BUY no matter how cheap or how good the story —
    # you do not catch a falling knife. This is what blocks LPG (4-day -10% freefall, below 50dma)
    # and NEM (gold miner -21% off its high while gold is in a bear market). A healthy pullback in
    # an uptrend reads trend=neutral, not bear, so genuine dip-buys (AVGO) are NOT blocked.
    price_downtrend = trend_dir == "bear"
    if vetoes:
        size_authority = "blocked"
    elif (confluence > 0.3 and leadership_ok and not price_downtrend
          and not price_falling_fast and not weak_asymmetry):
        size_authority = "up"
    elif confluence < -0.3:
        size_authority = "down"
    else:
        size_authority = "hold"
    return {"bull": bull, "bear": bear, "n_scored": n, "confluence": confluence,
            "vetoes": vetoes, "divergences": _divergences(rows),
            "bloc_fund": fund, "bloc_macro": macro,
            "price_unconfirmed": trend_dir == "bear", "theme_unconfirmed": theme_dir == "bear",
            "sector_lagging": sector_lagging, "leadership_ok": leadership_ok,
            "price_downtrend": price_downtrend, "price_falling_fast": price_falling_fast,
            "weak_asymmetry": weak_asymmetry, "asym_ratio": asym_ratio,
            "size_authority": size_authority}


def full(subject: str, kind: str = "name") -> dict:
    m = decision_matrix(subject, kind)
    return {**m, "synthesis": synthesize(m)}
