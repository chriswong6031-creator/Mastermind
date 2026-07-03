"""THE benchmark ledger (charter P7 — one benchmark ledger; P6 — the book that beats us is a named
input) — W-L / L1.

The program started from one fact the system could not compute: *the user's simple defensive basket
beat the Brain during the semis unwind.* This module makes that fact a first-class, daily artifact.

Four bogeys, every one renormalized to **$1.00 at a common inception** on ONE price source (the
marking layer, `portfolio.marks`), so they are directly comparable to each book's normalized NAV:

  1. ``spy``        — SPY buy-and-hold. The market.
  2. ``defensive``  — the user's defensive basket: XLU / XLV / XLF / XLP, equal-weight, rebalanced
                      to equal-weight each mark (a static defensive sleeve — the thing that beat us).
  3. ``do_nothing`` — the carry shadow: whatever the book held at inception, marked forward, never
                      traded. "What if we'd frozen?" — the cost of churn.
  4. ``regime_max`` — the regime-conditional bogey: ``max(spy, defensive)`` applied ONLY when the
                      regime is NOT risk_on (or the transition is WEAKENING); plain SPY in risk_on.
                      So a cash-hoarding book in a calm up-tape still *loses* to the market (it can't
                      hide behind "but defense would have won" when defense was the wrong call).

Renorm math (all bogeys share it): pick the first date on which EVERY constituent has a mark
(``common_inception``); each constituent starts at its inception price; the basket value on date d
is ``mean_i( price_i(d) / price_i(inception) )`` for an equal-weight basket, ``price(d)/price(0)``
for a single name. Every series is a growth-of-$1 curve.

Output: ``data/benchmark/<asof>.json`` (the full curves + a leaderboard) and ``leaderboard()`` —
consumed by cio and the improvement agenda. Best-effort; missing marks degrade a bogey to no-op
(P2), they never fabricate a curve. Nothing here pins a live market state — the regime read is an
injected input (fixture-driven in tests, the daily regime frame in prod)."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

_ROOT = Path(__file__).resolve().parent.parent
_BENCH_DIR = _ROOT / "data" / "benchmark"

# The user's defensive basket — equal-weight, the four sleeves the audit named (utilities /
# healthcare / financials / staples). (These are named constituents, not a tuned threshold, so they
# live in code with the ledger, not in doctrine.yml.)
DEFENSIVE_BASKET = ["XLU", "XLV", "XLF", "XLP"]
SPY = "SPY"


# ─────────────────────────────────────────────────────────────────────────────
# growth-of-$1 renorm (pure)
# ─────────────────────────────────────────────────────────────────────────────
def _common_inception(series: dict, tickers: list[str]) -> Optional[str]:
    """First date on which EVERY ticker in `tickers` has a positive mark. None if never aligned."""
    dated: list[set] = []
    for t in tickers:
        s = series.get(t.upper()) or {}
        dated.append({d for d, px in s.items() if px and px > 0})
    if not dated:
        return None
    common = set.intersection(*dated) if len(dated) > 1 else dated[0]
    return min(common) if common else None


def renorm_basket(series: dict, tickers: list[str], *, inception: str | None = None) -> dict:
    """Growth-of-$1 curve for an EQUAL-WEIGHT basket (a single-name basket is just its own curve).

    ``series`` is {TICKER: {date: price}}. Returns {date: value} where value at inception is 1.0 and
    value(d) = mean over constituents of price(d)/price(inception). A date on which any constituent
    is missing a mark is SKIPPED (the basket is only valued when fully priced) — P2-safe."""
    tickers = [t.upper() for t in tickers]
    inc = inception or _common_inception(series, tickers)
    if inc is None:
        return {}
    base = {}
    for t in tickers:
        p0 = (series.get(t) or {}).get(inc)
        if not p0 or p0 <= 0:
            return {}
        base[t] = float(p0)
    # every date any constituent is priced; value only the fully-priced ones
    all_dates = sorted({d for t in tickers for d in (series.get(t) or {})})
    out: dict = {}
    for d in all_dates:
        if d < inc:
            continue
        ratios = []
        ok = True
        for t in tickers:
            px = (series.get(t) or {}).get(d)
            if not px or px <= 0:
                ok = False
                break
            ratios.append(float(px) / base[t])
        if ok and ratios:
            out[d] = round(sum(ratios) / len(ratios), 6)
    return out


# ─────────────────────────────────────────────────────────────────────────────
# regime-conditional bogey (pure)
# ─────────────────────────────────────────────────────────────────────────────
def _use_defensive(regime: dict | None) -> bool:
    """The regime-conditional switch: True → max(spy, defensive); False → plain SPY.

    True when the regime is NOT risk_on, OR the transition is WEAKENING. Missing regime → False
    (degrade to plain SPY: we do NOT grant the book the defensive alibi without evidence). P2."""
    r = regime or {}
    state = str(r.get("state") or r.get("risk_state") or "risk_on").lower()
    transition = str(r.get("transition_state") or r.get("transition") or "").upper()
    return state != "risk_on" or transition == "WEAKENING"


def regime_max_curve(spy_curve: dict, defensive_curve: dict, regime: dict | None) -> dict:
    """Per-date bogey: max(spy, defensive) on 'defensive-allowed' dates, plain SPY otherwise.

    The regime read is applied uniformly across the window (the current posture governs how we
    grade the whole curve — a single regime dict, injected). On a date the defensive basket has no
    value, the bogey falls back to SPY (P2)."""
    use_def = _use_defensive(regime)
    out: dict = {}
    for d, sv in spy_curve.items():
        if use_def:
            dv = defensive_curve.get(d)
            out[d] = round(max(sv, dv), 6) if dv is not None else round(sv, 6)
        else:
            out[d] = round(sv, 6)
    return out


# ─────────────────────────────────────────────────────────────────────────────
# the ledger builder
# ─────────────────────────────────────────────────────────────────────────────
def _ret_pct(curve: dict) -> Optional[float]:
    if not curve:
        return None
    ks = sorted(curve)
    return round((curve[ks[-1]] / curve[ks[0]] - 1.0) * 100, 4)


def build(price_series: dict, *, asof: str, regime: dict | None = None,
          book_curves: dict | None = None, persist: bool = True) -> dict:
    """Assemble the four-bogey ledger for `asof`.

    ``price_series`` = {TICKER: {date: usd_price}} — the mark history for SPY + the defensive basket
    (produced by portfolio.marks over the window; in tests, the incident fixture). ``book_curves``
    (optional) = {book_id: {date: growth_of_$1}} for the do-nothing/carry comparison + the leaderboard
    (the Brain books' own normalized NAV curves). ``regime`` drives the regime_max bogey.

    Returns {as_of, bogeys:{id: {curve, return_pct, inception, ...}}, leaderboard:[...]}. Every
    bogey degrades to an empty curve (skipped in the leaderboard) if its inputs are missing (P2)."""
    asof = str(asof)[:10]
    # normalize {TICKER: {date: px}}; skip non-dict values (a fixture "_note", any stray metadata)
    series = {}
    for k, sv in (price_series or {}).items():
        if not isinstance(sv, dict):
            continue
        series[(k or "").upper()] = {str(d)[:10]: float(v)
                                     for d, v in sv.items() if isinstance(v, (int, float)) and v > 0}

    spy_curve = renorm_basket(series, [SPY])
    def_curve = renorm_basket(series, DEFENSIVE_BASKET)
    regime_curve = regime_max_curve(spy_curve, def_curve, regime) if spy_curve else {}

    # do-nothing / carry bogey: if a book's inception holdings curve is supplied use it; otherwise
    # the do-nothing counterfactual over the tracked universe is SPY-equivalent (a fully-invested
    # broad hold), so we surface it only when a caller passes an explicit carry curve.
    do_nothing_curve = (book_curves or {}).get("do_nothing") or {}

    bogeys = {
        "spy": {"label": "SPY (market)", "constituents": [SPY], "curve": spy_curve},
        "defensive": {"label": "Defensive basket (XLU/XLV/XLF/XLP EW)",
                      "constituents": DEFENSIVE_BASKET, "curve": def_curve},
        "regime_max": {"label": "Regime-conditional max(SPY, defensive)",
                       "constituents": [SPY, *DEFENSIVE_BASKET], "curve": regime_curve,
                       "defensive_active": _use_defensive(regime)},
        "do_nothing": {"label": "Do-nothing (carry from inception)",
                       "constituents": [], "curve": do_nothing_curve},
    }
    for b in bogeys.values():
        c = b["curve"]
        b["return_pct"] = _ret_pct(c)
        b["inception"] = (sorted(c)[0] if c else None)
        b["n_points"] = len(c)

    result = {"as_of": asof, "bogeys": bogeys,
              "leaderboard": leaderboard(bogeys, book_curves or {})}
    if persist:
        try:
            _BENCH_DIR.mkdir(parents=True, exist_ok=True)
            (_BENCH_DIR / f"{asof}.json").write_text(json.dumps(result, indent=2, default=str))
        except Exception:  # noqa: BLE001
            pass
    return result


def leaderboard(bogeys: dict, book_curves: dict) -> list:
    """Every bogey AND every book curve, ranked by window return (growth-of-$1). The row the whole
    program is about — the defensive basket sitting above the Brain books during a defensive tape —
    is now a computed, ordered fact, consumable by cio + the agenda."""
    rows = []
    for bid, b in (bogeys or {}).items():
        if not b.get("curve"):
            continue
        rows.append({"id": bid, "kind": "bogey", "label": b.get("label"),
                     "return_pct": b.get("return_pct"), "n_points": b.get("n_points"),
                     "inception": b.get("inception")})
    for book_id, curve in (book_curves or {}).items():
        if book_id == "do_nothing" or not curve:            # do_nothing is surfaced as a bogey
            continue
        rows.append({"id": book_id, "kind": "book", "label": book_id,
                     "return_pct": _ret_pct(curve), "n_points": len(curve),
                     "inception": (sorted(curve)[0] if curve else None)})
    return sorted(rows, key=lambda r: (r["return_pct"] is None, -(r["return_pct"] or 0)))


def load(asof: str) -> dict:
    try:
        return json.loads((_BENCH_DIR / f"{str(asof)[:10]}.json").read_text())
    except Exception:  # noqa: BLE001
        return {}


def latest() -> dict:
    """The most recent benchmark ledger on disk (for cio / the agenda)."""
    try:
        files = sorted(_BENCH_DIR.glob("*.json"))
        return json.loads(files[-1].read_text()) if files else {}
    except Exception:  # noqa: BLE001
        return {}
