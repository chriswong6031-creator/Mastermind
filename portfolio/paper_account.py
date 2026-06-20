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

    # ---- determine target shares for each ticker ----
    target_shares: dict[str, float] = {}
    for ticker, weight in target_weights.items():
        px = prices.get(ticker)
        if px is None or px <= 0:
            continue
        target_dollar = weight * current_nav
        target_shares[ticker] = target_dollar / px

    # ---- process sells first (free up cash before buys) ----
    for ticker, pos in list(state["positions"].items()):
        tgt = target_shares.get(ticker, 0.0)
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

    # also close out tickers no longer in target
    for ticker in list(state["positions"].keys()):
        if ticker not in target_shares:
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
# backfill (hypothetical equity curve over the prior ~90 sessions)
# ---------------------------------------------------------------------------

def build_backfill(target_weights: dict[str, float]) -> list[dict]:
    """Build a hypothetical curve repricing today's target weights over the
    prior 90 trading sessions.  Returns a list of series items (oldest-first)
    with kind="hypothetical".  Returns [] if price data is unavailable.

    The curve is HONEST: it reflects the current allocation repriced over
    prior sessions.  It does NOT represent realized fills.
    """
    try:
        import pandas as pd
        import numpy as np
    except ImportError:
        return []

    tickers = list(target_weights.keys())
    all_tickers = tickers + ["SPY"]

    # build aligned price DataFrame
    frames: dict[str, "pd.Series"] = {}
    for t in all_tickers:
        s = _fetch_price_series(t)
        if s is not None and len(s) > 0:
            frames[t] = s

    if "SPY" not in frames or not any(t in frames for t in tickers):
        return []

    try:
        df = pd.DataFrame(frames).sort_index().dropna(how="all")
        # last 91 rows (90 backtested sessions + the current day we'll drop in realized)
        df = df.tail(91)
        # forward-fill then back-fill to handle staggered series
        df = df.ffill().bfill()
        if len(df) < 2:
            return []
    except Exception:
        return []

    # normalise weights across available tickers
    available_w = {t: w for t, w in target_weights.items() if t in df.columns}
    total_w = sum(available_w.values())
    if total_w <= 0:
        return []
    norm_w = {t: w / total_w for t, w in available_w.items()}

    # SPY benchmark: $1M in SPY at the series start
    spy0 = df["SPY"].iloc[0]
    spy_shares_hyp = _STARTING_NAV / spy0 if spy0 > 0 else 0.0

    # portfolio: weight * NAV at the series start, then rebalanced (held-weight approach)
    # We use a simple held-weight return — same as a daily-rebalanced index
    port_return_series = pd.Series(0.0, index=df.index)
    for t, w in norm_w.items():
        s = df[t]
        pct = s.pct_change().fillna(0.0)
        port_return_series += w * pct

    nav_series = _STARTING_NAV * (1 + port_return_series).cumprod()
    spy_nav_series = spy_shares_hyp * df["SPY"]

    rows: list[dict] = []
    for idx in df.index:
        rows.append({
            "date": idx.date().isoformat(),
            "nav": round(float(nav_series.loc[idx]), 2),
            "spy_nav": round(float(spy_nav_series.loc[idx]), 2),
            "kind": "hypothetical",
        })
    return rows


# ---------------------------------------------------------------------------
# /api/performance payload
# ---------------------------------------------------------------------------

def performance() -> dict:
    """Assemble the /api/performance contract.

    Prepends the hypothetical backfill (kind='hypothetical') to the realized
    forward track (kind='realized').  Returns a safe minimal payload on error.
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
        if not realized_rows:
            return _base

        inception_date = state.get("inception_date", _INCEPTION_DATE)

        # current values from the latest realized row
        latest = realized_rows[-1]
        current_nav = float(latest["nav"])
        cash = float(latest["cash"])
        invested = float(latest.get("invested", 0.0))
        spy_nav_latest = latest.get("spy_nav")

        total_return_pct = (current_nav - _STARTING_NAV) / _STARTING_NAV * 100
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

        # max drawdown over realized track
        import numpy as np
        nav_arr = [float(r["nav"]) for r in realized_rows]
        max_drawdown_pct = 0.0
        if len(nav_arr) > 1:
            running_max = np.maximum.accumulate(nav_arr)
            drawdowns = (np.array(nav_arr) - running_max) / running_max * 100
            max_drawdown_pct = round(float(drawdowns.min()), 4)

        # ---- build series ----
        # 1) load target weights from latest portfolio
        target_weights: dict[str, float] = {}
        try:
            port_path = _DATA / "latest.json"
            if port_path.exists():
                p = json.loads(port_path.read_text())
                for pos in p.get("positions", []):
                    t = pos.get("ticker")
                    w = pos.get("weight")
                    if t and w:
                        target_weights[t] = float(w)
        except Exception:
            pass

        # 2) hypothetical prefix
        hypo_rows: list[dict] = []
        if target_weights:
            hypo_rows = build_backfill(target_weights)
            # trim so the backfill ends BEFORE the first realized row
            if hypo_rows and realized_rows:
                first_realized_date = realized_rows[0]["date"]
                hypo_rows = [r for r in hypo_rows if r["date"] < first_realized_date]

        # 3) realized rows: ensure they have kind tag + spy_nav
        real_series = [
            {
                "date": r["date"],
                "nav": float(r["nav"]),
                "spy_nav": float(r["spy_nav"]) if r.get("spy_nav") is not None else None,
                "kind": "realized",
            }
            for r in realized_rows
        ]

        series = hypo_rows + real_series

        note = (
            "Hypothetical = current allocation repriced over prior sessions (not realized); "
            f"live paper track begins {inception_date}."
        )
        if not hypo_rows:
            note = f"Live paper track begins {inception_date}. Equity curve accrues forward."

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
