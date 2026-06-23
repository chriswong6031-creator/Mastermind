"""Live overnight 'tape' — the fast cross-asset overlay the Brain books read between close and open.

The macro dashboard (vendor/macro) is an EOD build: it cannot see tonight's tape. A human trader
watches overnight equity-index FUTURES, the Asia→Europe→US handoff, FX/rates/vol while the market is
shut, and arrives at the open already positioned for what moved. This module gives the Brains the
same view — a LIVE read of US index futures, international indices, FX, rates, vol and commodities
via Yahoo (yfinance serves all of these: ES=F, ^N225, ^VIX, …) — plus a distilled overnight risk
read (calm / elevated / stressed) the overnight watch loop and the dashboard both consume.

Pure-ish + degrade-never-raise: a missing yfinance / network / parse leaves an empty tape and a
'calm'/unknown risk read; nothing here raises. Cached ~5 min (the tape moves, but not every second,
and we don't want to hammer Yahoo on every dashboard poll or watch tick)."""
from __future__ import annotations

import time

# The overnight universe, grouped (Yahoo symbols + display names).
GROUPS: dict[str, list[tuple[str, str]]] = {
    "us_futures":   [("ES=F", "S&P 500 fut"), ("NQ=F", "Nasdaq-100 fut"),
                     ("YM=F", "Dow fut"), ("RTY=F", "Russell 2000 fut")],
    "international": [("^N225", "Nikkei 225"), ("^HSI", "Hang Seng"), ("000001.SS", "Shanghai"),
                     ("^KS11", "KOSPI"), ("^STOXX50E", "Euro Stoxx 50"), ("^GDAXI", "DAX"), ("^FTSE", "FTSE 100")],
    "fx_rates":     [("DX-Y.NYB", "US Dollar"), ("JPY=X", "USD/JPY"), ("^TNX", "10Y yield")],
    "vol":          [("^VIX", "VIX")],
    "commodities":  [("CL=F", "WTI crude"), ("GC=F", "Gold")],
    "crypto":       [("BTC-USD", "Bitcoin")],
}
ALL_SYMBOLS = [s for g in GROUPS.values() for s, _ in g]

_TTL = 300.0           # seconds — cache the tape for ~5 min
_cache: dict | None = None
_cache_ts: float = 0.0


def _fetch_changes(symbols: list[str]) -> dict[str, dict]:
    """{symbol: {price, prev, change_pct}} from ONE batched yf.download (last ~5 daily closes →
    last vs prior session). Best-effort; a missing yfinance / network leaves an empty dict."""
    out: dict[str, dict] = {}
    try:
        import yfinance as yf
        df = yf.download(symbols, period="5d", progress=False, auto_adjust=False, threads=False)
        close = df["Close"]
    except Exception:
        return out
    multi = hasattr(close, "columns")
    cols = list(close.columns) if multi else [symbols[0]]
    for sym in cols:
        try:
            s = (close[sym] if multi else close).dropna()
            if len(s) >= 2:
                last, prev = float(s.iloc[-1]), float(s.iloc[-2])
                out[str(sym)] = {"price": round(last, 4), "prev": round(prev, 4),
                                 "change_pct": round((last / prev - 1) * 100, 2) if prev else None}
            elif len(s) == 1:
                out[str(sym)] = {"price": round(float(s.iloc[-1]), 4), "prev": None, "change_pct": None}
        except Exception:
            continue
    return out


def risk_read(groups: dict) -> dict:
    """Distil an overnight risk-on/off read from the tape — the SAME read the watch tripwire and the
    dashboard consume. stressed: US futures down hard, a broad international rout, or a VIX spike;
    elevated: a softer version; else calm."""
    fut = [r["change_pct"] for r in groups.get("us_futures", []) if r.get("change_pct") is not None]
    intl = [r["change_pct"] for r in groups.get("international", []) if r.get("change_pct") is not None]
    vix = next((r for r in groups.get("vol", []) if r.get("ticker") == "^VIX"), {})
    vix_chg = vix.get("change_pct")
    fut_avg = round(sum(fut) / len(fut), 2) if fut else None
    intl_avg = round(sum(intl) / len(intl), 2) if intl else None
    intl_down = sum(1 for x in intl if x < 0)

    state = "calm"
    if ((fut_avg is not None and fut_avg <= -1.5)
            or (intl and intl_avg is not None and intl_avg <= -1.5 and intl_down >= max(1, int(0.6 * len(intl))))
            or (vix_chg is not None and vix_chg >= 12)):
        state = "stressed"
    elif ((fut_avg is not None and fut_avg <= -0.6)
          or (intl and intl_down >= max(1, int(0.6 * len(intl))))
          or (vix_chg is not None and vix_chg >= 6)):
        state = "elevated"

    reasons = []
    if fut_avg is not None:
        reasons.append(f"US futures avg {fut_avg:+.2f}%")
    if intl_avg is not None:
        reasons.append(f"{intl_down}/{len(intl)} intl mkts down (avg {intl_avg:+.2f}%)")
    if vix_chg is not None:
        reasons.append(f"VIX {vix_chg:+.1f}%")
    return {"state": state, "futures_avg_pct": fut_avg, "intl_avg_pct": intl_avg,
            "intl_down": intl_down, "intl_n": len(intl), "vix_change_pct": vix_chg,
            "reasons": reasons or ["no live tape"]}


def tape(force: bool = False) -> dict:
    """The live overnight tape: per-symbol price + overnight % change (grouped) + the distilled risk
    read. Cached ~5 min. Best-effort — empty groups + a calm/'no live tape' read when the feed is down."""
    global _cache, _cache_ts
    now = time.monotonic()
    if not force and _cache is not None and (now - _cache_ts) < _TTL:
        return _cache
    changes = _fetch_changes(ALL_SYMBOLS)
    groups: dict[str, list[dict]] = {}
    for g, syms in GROUPS.items():
        rows = []
        for sym, name in syms:
            c = changes.get(sym)
            if c:
                rows.append({"ticker": sym, "name": name,
                             "price": c.get("price"), "change_pct": c.get("change_pct")})
        groups[g] = rows
    from datetime import datetime, timezone
    out = {"as_of": datetime.now(timezone.utc).isoformat(), "groups": groups,
           "risk": risk_read(groups), "n_symbols": len(changes), "live": bool(changes)}
    _cache, _cache_ts = out, now
    return out


def is_material(t: dict | None = None) -> bool:
    """Deterministic tripwire (free — no LLM): is the overnight tape moving enough to warrant a Brain
    reconsideration? True when the distilled risk read is 'elevated' or 'stressed'."""
    t = t if t is not None else tape()
    return (t.get("risk") or {}).get("state") in ("elevated", "stressed")


def brief(t: dict | None = None) -> str:
    """A one-line human/Brain summary of the overnight tape (for prompts + logs)."""
    t = t if t is not None else tape()
    if not t.get("live"):
        return "Overnight tape unavailable (live feed down)."
    risk = t.get("risk") or {}
    return f"Overnight tape: risk {risk.get('state')} — " + "; ".join(risk.get("reasons") or [])


def clear_cache() -> None:
    """Drop the memo (tests / a forced refresh)."""
    global _cache, _cache_ts
    _cache, _cache_ts = None, 0.0
