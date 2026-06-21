"""$1,000,000 paper-trading account — persistent NAV, fills, equity curve.

PAPER ONLY — never executes real trades, never touches a broker.

State files (all in data/portfolio/):
  account.json      — inception_date, starting_nav, cash, positions, spy_shares
  fills.jsonl       — one JSON line per simulated fill
  nav_history.jsonl — one JSON line per mark() call (daily NAV snapshot)

Price sources:
  - Leadership sleeve ETFs + SPY: lib.store.read("yahoo", ticker)["close"]
  - Conviction single-name tickers: breadth/_closes_cache.parquet
"""
from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import bot  # noqa: F401  -> vendor/macro onto sys.path

_ROOT = Path(__file__).resolve().parent.parent
_DATA = _ROOT / "data" / "portfolio"
_ACCOUNT_PATH = _DATA / "account.json"
_FILLS_PATH = _DATA / "fills.jsonl"
_NAV_PATH = _DATA / "nav_history.jsonl"

_STARTING_NAV = 1_000_000.0
_INCEPTION_DATE = date.today().isoformat()  # forward-realized track begins today


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _ensure_dir() -> None:
    _DATA.mkdir(parents=True, exist_ok=True)


def _load_account() -> dict[str, Any]:
    """Load account state; return a fresh $1M state on any corruption."""
    try:
        if _ACCOUNT_PATH.exists():
            raw = json.loads(_ACCOUNT_PATH.read_text())
            # basic schema validation
            if (
                isinstance(raw.get("cash"), (int, float))
                and isinstance(raw.get("positions"), dict)
                and raw.get("starting_nav")
            ):
                return raw
    except Exception:
        pass
    return {
        "inception_date": _INCEPTION_DATE,
        "starting_nav": _STARTING_NAV,
        "cash": _STARTING_NAV,
        "positions": {},          # TICKER -> {shares, avg_cost}
        "spy_shares": None,       # set on first mark()
        "spy_inception_price": None,
    }


def _save_account(state: dict[str, Any]) -> None:
    _ensure_dir()
    _ACCOUNT_PATH.write_text(json.dumps(state, indent=2, default=str))


def _append_jsonl(path: Path, record: dict) -> None:
    _ensure_dir()
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, default=str) + "\n")


def _load_jsonl(path: Path) -> list[dict]:
    """Load all lines from a JSONL file; skip corrupt lines."""
    if not path.exists():
        return []
    rows: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except Exception:
            pass
    return rows


# ---------------------------------------------------------------------------
# price loaders (engine price store)
# ---------------------------------------------------------------------------

def _fetch_price_series(ticker: str) -> "pd.Series | None":
    """Return a date-indexed close Series from the macro engine price stores.

    Priority:
      1. lib.store.read("yahoo", ticker) — has sector ETFs + SPY
      2. breadth/_closes_cache.parquet   — has the S&P large-cap single names
    """
    try:
        import pandas as pd
        from lib import store  # vendored macro lib
        df = store.read("yahoo", ticker)
        if df is not None and "close" in df.columns and len(df) > 0:
            s = df["close"].astype(float).dropna()
            s.index = pd.to_datetime(s.index)
            return s
    except Exception:
        pass

    try:
        import pandas as pd
        from lib import config  # vendored macro lib
        closes_path = config.data_dir() / "breadth" / "_closes_cache.parquet"
        if closes_path.exists():
            cache = None
            try:
                import pandas as _pd
                cache = _pd.read_parquet(closes_path)
            except Exception:
                pass
            if cache is not None and ticker in cache.columns:
                s = cache[ticker].astype(float).dropna()
                s.index = _pd.to_datetime(s.index)
                return s
    except Exception:
        pass

    return None


def _live_price(ticker: str) -> float | None:
    """Read the live price from vendor/macro/site/stockdata/<TICKER>.json."""
    try:
        p = _ROOT / "vendor" / "macro" / "site" / "stockdata" / f"{ticker}.json"
        if p.exists():
            raw = json.loads(p.read_text())
            v = (raw.get("tech") or {}).get("price")
            if v is not None:
                return float(v)
    except Exception:
        pass
    return None


def _current_price(ticker: str) -> float | None:
    """Best available current/last-close price for a ticker: the stockdata live mark first,
    else the last point of the engine price series (covers the leadership-sleeve ETFs)."""
    px = _live_price(ticker)
    if px and px > 0:
        return px
    s = _fetch_price_series(ticker)
    try:
        if s is not None and len(s) > 0:
            return float(s.iloc[-1])
    except Exception:
        pass
    return None


def reset_cost_basis_to_market(prices: dict[str, float] | None = None) -> dict[str, float]:
    """Reset every holding's avg_cost to its CURRENT market price → wipes unrealized P&L.

    Used when the book is marked flat with no trading (e.g. the market has been closed all day):
    nothing actually traded, so carrying a stale unrealized gain/loss is wrong. Only the cost
    basis — and therefore unrealized P&L — is reset to zero as of now.

    NAV-safe: a holding is reset ONLY to a real current mark. `prices` (the same marks nav()/
    positions_pnl() use) is preferred; the stockdata live price is the per-name fallback. A name
    with NO available mark is SKIPPED (never reset to a stale series value) so the avg_cost — which
    nav() falls back to when a live quote is missing — can't silently shift the portfolio total.
    Returns {ticker: new_cost_basis}. Paper-only."""
    state = _load_account()
    prices = prices or {}
    updated: dict[str, float] = {}
    for ticker, pos in state.get("positions", {}).items():
        px = prices.get(ticker)
        if px is None:
            px = _live_price(ticker)          # the stockdata mark (consistent with the marks elsewhere)
        if px and px > 0 and pos.get("shares"):
            pos["avg_cost"] = round(float(px), 4)
            updated[ticker] = pos["avg_cost"]
    _save_account(state)
    return updated


# ---------------------------------------------------------------------------
# core account operations
# ---------------------------------------------------------------------------

def nav(prices: dict[str, float]) -> float:
    """Current NAV = cash + market value of all positions."""
    state = _load_account()
    mktval = sum(
        pos["shares"] * prices.get(ticker, pos["avg_cost"])
        for ticker, pos in state["positions"].items()
    )
    return state["cash"] + mktval


def positions_pnl(prices: dict[str, float]) -> dict[str, dict]:
    """Per-ticker live P&L from the account's average-cost lots, marked to `prices`.

    Returns {TICKER: {shares, avg_cost, current_price, market_value,
                      unrealized_pnl, unrealized_pct}}. Values are None when a
    live price is missing (offline) so callers can render an honest dash."""
    state = _load_account()
    out: dict[str, dict] = {}
    for ticker, pos in state.get("positions", {}).items():
        shares = float(pos.get("shares") or 0.0)
        avg = float(pos.get("avg_cost") or 0.0)
        px = prices.get(ticker)
        rec = {
            "shares": shares,
            "avg_cost": round(avg, 4) if avg else None,
            "current_price": round(px, 4) if px else None,
            "market_value": None,
            "unrealized_pnl": None,
            "unrealized_pct": None,
        }
        if px and avg and shares:
            rec["market_value"] = round(shares * px, 2)
            rec["unrealized_pnl"] = round((px - avg) * shares, 2)
            rec["unrealized_pct"] = round((px / avg - 1) * 100, 2)
        out[ticker] = rec
    return out


def rebalance(
    target_weights: dict[str, float],
    prices: dict[str, float],
    asof: str,
) -> None:
    """Simulate fills to reach target_weights * current_nav.

    Rules:
    - No leverage: gross weight is clamped to 1.0 if needed.
    - Cash floored at 0.
    - Fills recorded to fills.jsonl.
    - account.json updated atomically.
    """
    state = _load_account()
    gross = sum(target_weights.values())
    if gross > 1.0:
        # scale down proportionally so we stay cash-positive
        scale = 1.0 / gross
        target_weights = {k: v * scale for k, v in target_weights.items()}

    current_nav = (
        state["cash"]
        + sum(
            pos["shares"] * prices.get(ticker, pos["avg_cost"])
            for ticker, pos in state["positions"].items()
        )
    )

    fills: list[dict] = []

    # ---- determine target shares for each ticker we can PRICE this run ----
    targeted = set(target_weights)                 # everything we INTEND to hold (priced or not)
    target_shares: dict[str, float] = {}
    for ticker, weight in target_weights.items():
        px = prices.get(ticker)
        if px is None or px <= 0:
            continue                               # targeted but unpriceable this run -> carry, don't trade
        target_dollar = weight * current_nav
        target_shares[ticker] = target_dollar / px

    # ---- process sells first (free up cash before buys) ----
    # ONLY adjust a held position we can price AND that is in the target. A held name that is
    # targeted but has no price THIS run is NOT touched (the old code defaulted its target to 0 and
    # liquidated the whole position on a transient missing quote — a spurious exit).
    for ticker, pos in list(state["positions"].items()):
        if ticker not in target_shares:
            continue
        tgt = target_shares[ticker]
        cur = pos["shares"]
        diff = tgt - cur
        if diff < -1e-9:
            sell_shares = -diff
            px = prices.get(ticker, pos["avg_cost"])
            value = sell_shares * px
            state["cash"] += value
            pos["shares"] = tgt
            if pos["shares"] < 1e-9:
                del state["positions"][ticker]
            fills.append({
                "date": asof,
                "ticker": ticker,
                "side": "sell",
                "shares": round(sell_shares, 6),
                "price": round(px, 4),
                "value": round(value, 2),
            })

    # close out only tickers GENUINELY dropped from the target (not merely unpriceable this run)
    for ticker in list(state["positions"].keys()):
        if ticker not in targeted:
            pos = state["positions"][ticker]
            px = prices.get(ticker, pos["avg_cost"])
            sell_shares = pos["shares"]
            value = sell_shares * px
            state["cash"] += value
            del state["positions"][ticker]
            fills.append({
                "date": asof,
                "ticker": ticker,
                "side": "sell",
                "shares": round(sell_shares, 6),
                "price": round(px, 4),
                "value": round(value, 2),
            })

    # ---- process buys ----
    for ticker, tgt in target_shares.items():
        cur = state["positions"].get(ticker, {}).get("shares", 0.0)
        diff = tgt - cur
        if diff > 1e-9:
            px = prices.get(ticker)
            if px is None or px <= 0:
                continue
            # clamp so we don't spend more than available cash
            buy_shares = min(diff, state["cash"] / px)
            if buy_shares < 1e-9:
                continue
            value = buy_shares * px
            state["cash"] = max(0.0, state["cash"] - value)
            if ticker in state["positions"]:
                old = state["positions"][ticker]
                total_shares = old["shares"] + buy_shares
                old["avg_cost"] = (
                    (old["shares"] * old["avg_cost"] + value) / total_shares
                )
                old["shares"] = total_shares
            else:
                state["positions"][ticker] = {
                    "shares": buy_shares,
                    "avg_cost": px,
                }
            fills.append({
                "date": asof,
                "ticker": ticker,
                "side": "buy",
                "shares": round(buy_shares, 6),
                "price": round(px, 4),
                "value": round(value, 2),
            })

    _save_account(state)
    for fill in fills:
        _append_jsonl(_FILLS_PATH, fill)


def execute_fill(ticker: str, side: str, *, weight: float | None = None,
                 shares: float | None = None, price: float | None = None,
                 prices: dict[str, float] | None = None,
                 asof: str | None = None) -> dict:
    """A SINGLE-NAME paper fill, funded from / credited to cash.

    Unlike rebalance() — which takes a FULL target book and SELLS anything not in it —
    this adds, trims, or exits EXACTLY one ticker and never touches any other position.
    It is how the advisor chat conducts an ad-hoc paper trade.

    side  : "buy" | "sell"
    sizing: buy  -> `weight` (fraction of NAV) or explicit `shares`
            sell -> explicit `shares`, or omit both to EXIT the whole position
    Returns {ok, ticker, side, shares, price, value, cash_after}; ok=False on no price /
    insufficient cash / nothing to sell. Paper-only; reversible.
    """
    ticker = ticker.upper()
    side = (side or "").lower()
    asof = asof or date.today().isoformat()
    state = _load_account()
    px = price if (price and price > 0) else _current_price(ticker)
    if not px or px <= 0:
        return {"ok": False, "ticker": ticker, "error": "no price available"}
    pos = state["positions"].get(ticker)

    if side == "buy":
        if shares is None:
            pmap = dict(prices or {})
            pmap.setdefault(ticker, px)
            dollars = max(0.0, float(weight or 0.0)) * nav(pmap)
            shares = dollars / px
        shares = min(float(shares), state["cash"] / px)          # cash-bounded, no leverage
        if shares <= 1e-9:
            return {"ok": False, "ticker": ticker, "error": "insufficient cash / zero size"}
        value = shares * px
        state["cash"] = max(0.0, state["cash"] - value)
        if pos:
            total = pos["shares"] + shares
            pos["avg_cost"] = (pos["shares"] * pos["avg_cost"] + value) / total
            pos["shares"] = total
        else:
            state["positions"][ticker] = {"shares": shares, "avg_cost": px}
        fill = {"date": asof, "ticker": ticker, "side": "buy",
                "shares": round(shares, 6), "price": round(px, 4), "value": round(value, 2)}
    else:                                                        # sell / trim / exit
        if not pos or pos["shares"] <= 1e-9:
            return {"ok": False, "ticker": ticker, "error": "no position to sell"}
        sell = pos["shares"] if shares is None else min(float(shares), pos["shares"])
        if sell <= 1e-9:
            return {"ok": False, "ticker": ticker, "error": "zero size"}
        value = sell * px
        state["cash"] += value
        pos["shares"] -= sell
        if pos["shares"] < 1e-9:
            del state["positions"][ticker]
        fill = {"date": asof, "ticker": ticker, "side": "sell",
                "shares": round(sell, 6), "price": round(px, 4), "value": round(value, 2)}

    _save_account(state)
    _append_jsonl(_FILLS_PATH, fill)
    return {"ok": True, **fill, "cash_after": round(state["cash"], 2)}


def mark(prices: dict[str, float], asof: str) -> None:
    """Snapshot NAV to nav_history.jsonl. Also initialises SPY shares on first call."""
    state = _load_account()

    # initialise SPY benchmark on first mark
    spy_px = prices.get("SPY")
    if state.get("spy_shares") is None and spy_px and spy_px > 0:
        state["spy_shares"] = _STARTING_NAV / spy_px
        state["spy_inception_price"] = spy_px
        _save_account(state)

    current_nav = state["cash"] + sum(
        pos["shares"] * prices.get(ticker, pos["avg_cost"])
        for ticker, pos in state["positions"].items()
    )
    invested = current_nav - state["cash"]

    spy_nav: float | None = None
    if state.get("spy_shares") and spy_px:
        spy_nav = state["spy_shares"] * spy_px

    record = {
        "date": asof,
        "nav": round(current_nav, 2),
        "cash": round(state["cash"], 2),
        "invested": round(invested, 2),
        "spy_nav": round(spy_nav, 2) if spy_nav is not None else None,
    }
    # idempotent per date: keep exactly one NAV row per calendar date (replace, don't
    # append) so repeated book builds on the same day don't pile up duplicate points.
    rows: list[dict] = []
    if _NAV_PATH.exists():
        for line in _NAV_PATH.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            if r.get("date") != asof:
                rows.append(r)
    rows.append(record)
    _NAV_PATH.write_text("\n".join(json.dumps(r, default=str) for r in rows) + "\n")


# ---------------------------------------------------------------------------
# SPY history loader (used by performance() for the comparison line only)
# ---------------------------------------------------------------------------

def _load_spy_history(window: int = 91) -> "list[tuple[str, float]] | list":
    """Return [(date_str, close), ...] for SPY over the last `window` sessions.

    Uses the same store loader as _fetch_price_series so it works offline as
    long as the engine price cache is populated.  Returns [] if unavailable.
    """
    s = _fetch_price_series("SPY")
    if s is None or len(s) == 0:
        return []
    try:
        s = s.sort_index().tail(window)
        return [(idx.date().isoformat(), float(v)) for idx, v in s.items()]
    except Exception:
        return []


# ---------------------------------------------------------------------------
# /api/performance payload
# ---------------------------------------------------------------------------

def performance() -> dict:
    """Assemble the /api/performance contract.

    Series is HONEST:
      - spy_nav = real SPY history normalised to $1,000,000 at the first date
        of the window (S&P actual up/down, scaled to a $1M start).
      - nav (our portfolio) = $1,000,000 FLAT for every date before
        inception_date; from inception onward it uses the real marked NAV from
        nav_history.jsonl.  No hypothetical repricing of our allocation ever.
      - kind = "pre_inception" for the flat prefix, "realized" from inception.

    Returns a safe minimal payload on error.
    """
    _base: dict[str, Any] = {
        "inception_date": _INCEPTION_DATE,
        "starting_nav": _STARTING_NAV,
        "current_nav": _STARTING_NAV,
        "cash": _STARTING_NAV,
        "invested": 0.0,
        "total_return_pct": 0.0,
        "vs_spy_pct": 0.0,
        "day_change_pct": 0.0,
        "max_drawdown_pct": 0.0,
        "realized_since": _INCEPTION_DATE,
        "series": [],
        "note": "No data yet — run the book build to initialise.",
    }

    try:
        state = _load_account()
        realized_rows = _load_jsonl(_NAV_PATH)

        inception_date = state.get("inception_date", _INCEPTION_DATE)

        # current values (fall back to starting NAV if no realized rows yet)
        if realized_rows:
            latest = realized_rows[-1]
            current_nav = float(latest["nav"])
            cash = float(latest["cash"])
            invested = float(latest.get("invested", 0.0))
            spy_nav_latest = latest.get("spy_nav")
        else:
            current_nav = _STARTING_NAV
            cash = state.get("cash", _STARTING_NAV)
            invested = 0.0
            spy_nav_latest = None

        total_return_pct = (current_nav - _STARTING_NAV) / _STARTING_NAV * 100

        # vs_spy_pct: compare our return SINCE INCEPTION vs SPY since inception
        vs_spy_pct: float = 0.0
        if spy_nav_latest:
            spy_return = (float(spy_nav_latest) - _STARTING_NAV) / _STARTING_NAV * 100
            vs_spy_pct = round(total_return_pct - spy_return, 4)

        # day-over-day change
        day_change_pct: float = 0.0
        if len(realized_rows) >= 2:
            prev_nav = float(realized_rows[-2]["nav"])
            if prev_nav > 0:
                day_change_pct = round((current_nav - prev_nav) / prev_nav * 100, 4)

        # max drawdown over realized track only
        import numpy as np
        nav_arr = [float(r["nav"]) for r in realized_rows]
        max_drawdown_pct = 0.0
        if len(nav_arr) > 1:
            running_max = np.maximum.accumulate(nav_arr)
            drawdowns = (np.array(nav_arr) - running_max) / running_max * 100
            max_drawdown_pct = round(float(drawdowns.min()), 4)

        # ---- build series ----
        # Load SPY history for the chart window (the benchmark line).
        spy_history = _load_spy_history(91)  # list of (date_str, close)

        series: list[dict] = []

        if spy_history:
            # Normalise SPY so spy_nav == $1M at the first date of the window
            spy0 = spy_history[0][1]
            spy_scale = _STARTING_NAV / spy0 if spy0 > 0 else 1.0

            # Build a quick lookup from the realized rows for nav by date
            realized_by_date: dict[str, float] = {
                r["date"]: float(r["nav"]) for r in realized_rows
            }

            for date_str, spy_close in spy_history:
                spy_nav_val = round(spy_close * spy_scale, 2)

                if date_str < inception_date:
                    # Pre-inception: our portfolio is flat at $1M — we did not exist yet
                    series.append({
                        "date": date_str,
                        "nav": _STARTING_NAV,
                        "spy_nav": spy_nav_val,
                        "kind": "pre_inception",
                    })
                else:
                    # Realized: use real NAV from nav_history.jsonl if available,
                    # otherwise stay flat (today's book hasn't run yet)
                    nav_val = realized_by_date.get(date_str, _STARTING_NAV)
                    series.append({
                        "date": date_str,
                        "nav": nav_val,
                        "spy_nav": spy_nav_val,
                        "kind": "realized",
                    })
        else:
            # No SPY data (fully offline / price store empty): emit realized rows only
            for r in realized_rows:
                series.append({
                    "date": r["date"],
                    "nav": float(r["nav"]),
                    "spy_nav": float(r["spy_nav"]) if r.get("spy_nav") is not None else None,
                    "kind": "realized",
                })

        note = (
            f"Portfolio starts at ${_STARTING_NAV:,.0f} on {inception_date}; "
            "flat until the live daily track accrues. "
            "S&P 500 shown over the same window for comparison (real history)."
        )

        return {
            "inception_date": inception_date,
            "starting_nav": _STARTING_NAV,
            "current_nav": round(current_nav, 2),
            "cash": round(cash, 2),
            "invested": round(invested, 2),
            "total_return_pct": round(total_return_pct, 4),
            "vs_spy_pct": vs_spy_pct,
            "day_change_pct": day_change_pct,
            "max_drawdown_pct": max_drawdown_pct,
            "realized_since": inception_date,
            "series": series,
            "note": note,
        }
    except Exception as exc:
        _base["note"] = f"Performance unavailable: {exc}"
        return _base
