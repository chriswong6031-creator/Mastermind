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
import os
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import bot  # noqa: F401  -> vendor/macro onto sys.path

_ROOT = Path(__file__).resolve().parent.parent
_DATA = _ROOT / "data" / "portfolio"
_ACCOUNT_PATH = _DATA / "account.json"
_FILLS_PATH = _DATA / "fills.jsonl"
_NAV_PATH = _DATA / "nav_history.jsonl"


def _pending_path() -> Path:
    """Pending-orders file, derived from `_DATA` at call time so tests that patch
    `_DATA` (without patching every path constant) still redirect it."""
    return _DATA / "pending_orders.json"

_STARTING_NAV = 1_000_000.0
_INCEPTION_DATE = date.today().isoformat()  # forward-realized track begins today


# ---------------------------------------------------------------------------
# No-trade band (rebalancing tolerance)
# ---------------------------------------------------------------------------
# A Brain book restates its FULL target book every run. When it re-states the same weight
# for a name it intends to HOLD, that weight — measured against a NAV/price that has drifted
# since the last mark — almost never equals the position's current value to the dollar, so a
# naive snap-to-target generates a tiny "rebalancing" trim/add the Brain never asked for
# (e.g. a 19.8-share sell out of 1007, ~2% of the line). Those de-minimis fills clutter the
# trade dashboard with confusing noise.
#
# The band fixes that: an incremental adjustment to a CONTINUING position (held before AND
# still in the target) is only executed when its notional clears this fraction of NAV; below
# it, the position is left untouched (drift accumulates against the live target each run, so a
# genuinely meaningful move still trades once it crosses the band — no unbounded drift). A
# brand-new entry and a full exit (name dropped from the target) ALWAYS execute — they are
# deliberate decisions, never de-minimis noise. Override via env for tuning.
def _no_trade_band_frac() -> float:
    try:
        return max(0.0, float(os.environ.get("MASTERMIND_NO_TRADE_BAND_FRAC", "0.01")))
    except (TypeError, ValueError):
        return 0.01


# ---------------------------------------------------------------------------
# multi-portfolio path resolution
# ---------------------------------------------------------------------------
# Mastermind now harnesses several independent books. Every public operation takes an
# optional `portfolio_id`: None or the default ('flagship') resolves to the legacy
# module-global path constants (kept patchable so the existing test fixtures that
# monkeypatch _DATA/_ACCOUNT_PATH/_FILLS_PATH/_NAV_PATH still redirect the store);
# any other id resolves to a per-id subdir under data/portfolios/<id>/ via the registry.

def _paths(portfolio_id: str | None = None) -> dict[str, Path]:
    from portfolio import registry
    if not portfolio_id or portfolio_id == registry.DEFAULT_ID:
        return {"data": _DATA, "account": _ACCOUNT_PATH, "fills": _FILLS_PATH,
                "nav": _NAV_PATH, "pending": _pending_path()}
    base = registry.data_dir(portfolio_id)
    return {"data": base, "account": base / "account.json", "fills": base / "fills.jsonl",
            "nav": base / "nav_history.jsonl", "pending": base / "pending_orders.json"}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _ensure_dir(portfolio_id: str | None = None) -> None:
    _paths(portfolio_id)["data"].mkdir(parents=True, exist_ok=True)


def _load_account(portfolio_id: str | None = None) -> dict[str, Any]:
    """Load account state; return a fresh $1M state on any corruption."""
    _account_path = _paths(portfolio_id)["account"]
    try:
        if _account_path.exists():
            raw = json.loads(_account_path.read_text())
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


def _save_account(state: dict[str, Any], portfolio_id: str | None = None) -> None:
    _ensure_dir(portfolio_id)
    _paths(portfolio_id)["account"].write_text(json.dumps(state, indent=2, default=str))


# --------------------------------------------------------------------------- #
# cash sweep — idle cash earns a money-market yield, so a Brain that holds cash
# for lack of conviction is REWARDED (~4%/yr), not penalized vs a fully-invested
# benchmark. Incentivizes discipline over forced marginal buys.
# --------------------------------------------------------------------------- #
_CASH_YIELD_DEFAULT = 0.04            # 4% annualized money-market sweep
_TRADING_DAYS = 252


def _cash_yield_rate() -> float:
    """Annual cash-sweep rate; tunable via env CASH_YIELD_ANNUAL (default 4%)."""
    try:
        return float(os.environ.get("CASH_YIELD_ANNUAL", _CASH_YIELD_DEFAULT))
    except (TypeError, ValueError):
        return _CASH_YIELD_DEFAULT


def accrue_cash_yield(asof: str, portfolio_id: str | None = None,
                      annual_rate: float | None = None) -> float:
    """Accrue ONE trading-day of money-market yield to the cash balance, IDEMPOTENT per
    (book, calendar date): a re-run on the same ``asof`` is a no-op (no double-accrual). Best-effort;
    never raises. Returns the (possibly grown) cash balance. Call once per trading day, before
    ``mark()`` — the daily mark job (Mon-Fri) supplies the ~252 accruals/yr the rate/252 step
    assumes. Only the cash value changes; ``mark()``'s idempotent nav_history write is untouched."""
    rate = _cash_yield_rate() if annual_rate is None else float(annual_rate)
    try:
        state = _load_account(portfolio_id)
        cash = float(state.get("cash") or 0.0)
        if state.get("cash_yield_through") == asof or cash <= 0 or rate <= 0:
            return round(cash, 2)
        cash = round(cash * (1.0 + rate / _TRADING_DAYS), 2)
        state["cash"] = cash
        state["cash_yield_through"] = asof
        _save_account(state, portfolio_id)
        return cash
    except Exception:  # noqa: BLE001 — the sweep is additive; never break a build/mark
        try:
            return round(float(_load_account(portfolio_id).get("cash") or 0.0), 2)
        except Exception:  # noqa: BLE001
            return 0.0


def _append_jsonl(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
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
    """Best live mark for a ticker, in USD.

    Dispatches by venue suffix:
      * ``*.SS`` / ``*.SZ`` → the LIVE Tushare A-share close (CNY) when available, else the vendored
                              ``chinastockdata/`` snapshot → USD
      * ``*.HK``            → the LIVE Yahoo close (HKD) when available, else the vendored
                              ``hkstockdata/`` snapshot → USD
      * else                → ``stockdata/``     (USD, incl. US-listed China ADRs)
    The China/HK legs convert their LOCAL quote to USD via ``portfolio.fx`` so the paper account's
    single-currency NAV stays honest (it holds all three venues at once). Live marks come from
    Tushare for A-shares (``daily``) and Yahoo for Hong Kong; both degrade to the snapshot on any
    miss (Tushare's ``hk_daily`` is too rate-limited to mark a multi-name HK book)."""
    t = (ticker or "").upper().strip()
    if t.endswith(".SS") or t.endswith(".SZ"):
        sub, convert = "chinastockdata", True
    elif t.endswith(".HK"):
        sub, convert = "hkstockdata", True
    else:
        sub, convert = "stockdata", False

    local: float | None = None
    # A-shares: the fresh Tushare CNY close (``daily``, bulk + cached). Hong Kong: the fresh Yahoo
    # HKD close (Tushare's ``hk_daily`` is throttled to ~1 call/hr — see data_layer.yahoo_feed).
    # Both lag-correct the vendored snapshot; US ADRs have no live leg and use the snapshot below.
    if t.endswith((".SS", ".SZ")):
        try:
            from data_layer import tushare_feed
            local = tushare_feed.price_local(t)
        except Exception:
            local = None
    elif t.endswith(".HK"):
        try:
            from data_layer import yahoo_feed
            local = yahoo_feed.price_local(t)
        except Exception:
            local = None
    # Fallback (and the only path for US ADRs): the vendored per-name snapshot.
    if local is None:
        try:
            p = _ROOT / "vendor" / "macro" / "site" / sub / f"{t}.json"
            if p.exists():
                v = (json.loads(p.read_text()).get("tech") or {}).get("price")
                if v is not None:
                    local = float(v)
        except Exception:
            local = None
    if local is None:
        return None
    if convert:
        from portfolio import fx
        return fx.to_usd(local, t)
    return local


def _current_price(ticker: str) -> float | None:
    """Best available current/last-close price for a ticker: the stockdata live mark first,
    else the last point of the engine price series (covers the leadership-sleeve ETFs)."""
    px = _live_price(ticker)
    if px and px > 0:
        return px
    s = _fetch_price_series(ticker)
    try:
        if s is not None and len(s) > 0:
            v = float(s.iloc[-1])
            # The series stores (yahoo / breadth cache) quote in LOCAL currency; convert a China/HK
            # name to USD so the fallback can't leak a raw CNY/HKD mark into NAV (bare US tickers pass
            # through unchanged). Mirrors the conversion _live_price already does for the live mark.
            from portfolio import fx
            return fx.to_usd(v, ticker)
    except Exception:
        pass
    return None


def _benchmark_for(portfolio_id: str | None) -> str:
    """The equity-curve comparison symbol for a book (registry-resolved; 'SPY' fallback for
    the US books, 'FXI' for the all-China book)."""
    try:
        from portfolio import registry
        return registry.benchmark(portfolio_id)
    except Exception:
        return "SPY"


def reset_cost_basis_to_market(prices: dict[str, float] | None = None,
                               portfolio_id: str | None = None) -> dict[str, float]:
    """Reset every holding's avg_cost to its CURRENT market price → wipes unrealized P&L.

    Used when the book is marked flat with no trading (e.g. the market has been closed all day):
    nothing actually traded, so carrying a stale unrealized gain/loss is wrong. Only the cost
    basis — and therefore unrealized P&L — is reset to zero as of now.

    NAV-safe: a holding is reset ONLY to a real current mark. `prices` (the same marks nav()/
    positions_pnl() use) is preferred; the stockdata live price is the per-name fallback. A name
    with NO available mark is SKIPPED (never reset to a stale series value) so the avg_cost — which
    nav() falls back to when a live quote is missing — can't silently shift the portfolio total.
    Returns {ticker: new_cost_basis}. Paper-only."""
    state = _load_account(portfolio_id)
    prices = prices or {}
    updated: dict[str, float] = {}
    for ticker, pos in state.get("positions", {}).items():
        px = prices.get(ticker)
        if px is None:
            px = _live_price(ticker)          # the stockdata mark (consistent with the marks elsewhere)
        if px and px > 0 and pos.get("shares"):
            pos["avg_cost"] = round(float(px), 4)
            updated[ticker] = pos["avg_cost"]
    _save_account(state, portfolio_id)
    return updated


# ---------------------------------------------------------------------------
# core account operations
# ---------------------------------------------------------------------------

def nav(prices: dict[str, float], portfolio_id: str | None = None) -> float:
    """Current NAV = cash + market value of all positions."""
    state = _load_account(portfolio_id)
    mktval = sum(
        pos["shares"] * prices.get(ticker, pos["avg_cost"])
        for ticker, pos in state["positions"].items()
    )
    return state["cash"] + mktval


def positions_pnl(prices: dict[str, float], portfolio_id: str | None = None) -> dict[str, dict]:
    """Per-ticker live P&L from the account's average-cost lots, marked to `prices`.

    Returns {TICKER: {shares, avg_cost, current_price, market_value,
                      unrealized_pnl, unrealized_pct}}. Values are None when a
    live price is missing (offline) so callers can render an honest dash."""
    state = _load_account(portfolio_id)
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
    portfolio_id: str | None = None,
) -> None:
    """Simulate fills to reach target_weights * current_nav.

    Rules:
    - No leverage: gross weight is clamped to 1.0 if needed.
    - Cash floored at 0.
    - Fills recorded to fills.jsonl.
    - account.json updated atomically.
    """
    state = _load_account(portfolio_id)
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

    # No-trade band, in dollars, for this run's NAV. Incremental adjustments to a continuing
    # position below this notional are suppressed (see _no_trade_band_frac for the rationale).
    band = _no_trade_band_frac() * current_nav

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
            # No-trade band: a sub-band trim of a name we're still holding (tgt > 0, so never a
            # full exit) is left alone — don't manufacture a tiny sell the Brain never intended.
            if value < band:
                continue
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
            # No-trade band: skip a sub-band ADD to a CONTINUING position (held before this run).
            # A brand-new entry (cur ~ 0) is a deliberate open and always executes.
            if cur > 1e-9 and diff * px < band:
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

    _save_account(state, portfolio_id)
    fills_path = _paths(portfolio_id)["fills"]
    for fill in fills:
        _append_jsonl(fills_path, fill)


def execute_fill(ticker: str, side: str, *, weight: float | None = None,
                 shares: float | None = None, price: float | None = None,
                 prices: dict[str, float] | None = None,
                 asof: str | None = None, portfolio_id: str | None = None) -> dict:
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
    state = _load_account(portfolio_id)
    px = price if (price and price > 0) else _current_price(ticker)
    if not px or px <= 0:
        return {"ok": False, "ticker": ticker, "error": "no price available"}
    pos = state["positions"].get(ticker)

    if side == "buy":
        if shares is None:
            pmap = dict(prices or {})
            pmap.setdefault(ticker, px)
            dollars = max(0.0, float(weight or 0.0)) * nav(pmap, portfolio_id)
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

    _save_account(state, portfolio_id)
    _append_jsonl(_paths(portfolio_id)["fills"], fill)
    return {"ok": True, **fill, "cash_after": round(state["cash"], 2)}


# ---------------------------------------------------------------------------
# pending orders — overnight / market-closed buy queue
# ---------------------------------------------------------------------------
# When the desk decides to buy while the market is CLOSED, nothing trades: we
# record a PENDING order with an estimated price (the previous close) and let it
# fill at the NEXT open, at the real market price. Pending orders never touch
# cash or positions until they fill — NAV stays honest while they sit in queue.

def load_pending(portfolio_id: str | None = None) -> list[dict]:
    """All currently-queued (unfilled) orders. [] on missing/corrupt file."""
    try:
        p = _paths(portfolio_id)["pending"]
        if p.exists():
            data = json.loads(p.read_text())
            if isinstance(data, list):
                return data
    except Exception:
        pass
    return []


def _save_pending(orders: list[dict], portfolio_id: str | None = None) -> None:
    _ensure_dir(portfolio_id)
    _paths(portfolio_id)["pending"].write_text(json.dumps(orders, indent=2, default=str))


def queue_orders(
    target_weights: dict[str, float],
    est_prices: dict[str, float],
    asof: str,
    *,
    nav_base: float | None = None,
    fill_after: str | None = None,
    portfolio_id: str | None = None,
) -> list[dict]:
    """Queue PENDING buy orders to move the book toward `target_weights`, sized at
    `est_prices` (the previous close). Used when the market is CLOSED — no fill, no
    cash/position change. Only BUYS are queued (the desk does not sell while shut);
    the whole pending list is REPLACED so the latest decision wins (idempotent per
    build). Returns the pending list. Paper-only.

    nav_base : the NAV the weights are fractions of (defaults to the current marked
               NAV at est_prices). fill_after : the next-open date string for display.
    """
    from datetime import date as _date  # local — avoid shadowing module-level date
    state = _load_account(portfolio_id)
    if nav_base is None:
        nav_base = state["cash"] + sum(
            pos["shares"] * est_prices.get(tk, pos["avg_cost"])
            for tk, pos in state["positions"].items()
        )
    if fill_after is None:
        try:
            from portfolio import market_calendar
            fill_after = market_calendar.next_open_day().isoformat()
        except Exception:
            fill_after = None

    orders: list[dict] = []
    for ticker, weight in target_weights.items():
        ticker = ticker.upper()
        px = est_prices.get(ticker)
        if px is None or px <= 0:
            continue                                   # can't estimate without a prior close
        target_shares = (weight * nav_base) / px
        held = state["positions"].get(ticker, {}).get("shares", 0.0)
        shares = round(target_shares - held, 6)            # match the stored/displayed precision
        value = round(shares * px, 2)
        if shares <= 0.0 or value <= 0.0:
            continue                                   # already at/above target, or a sub-rounding
                                                       # residue that would render as "BUY 0 / $0"
        orders.append({
            "id": f"{asof}-{ticker}-buy",
            "ticker": ticker,
            "side": "buy",
            "shares": shares,
            "est_price": round(px, 4),
            "est_value": value,
            "weight": round(float(weight), 4),
            "placed_asof": asof,
            "fill_after": fill_after,
            "status": "pending",
        })
    _save_pending(orders, portfolio_id)
    return orders


def fill_pending(prices: dict[str, float], asof: str, portfolio_id: str | None = None) -> list[dict]:
    """Fill every queued order at the live `prices` (the market price at the open).

    Each order buys its queued SHARE COUNT at the real fill price (cash-bounded, no
    leverage); a real fill is appended to fills.jsonl and the position updated. An
    order with no available market price stays pending. Returns the executed fills.
    Call this FIRST on any build that runs while the market is open."""
    pending = load_pending(portfolio_id)
    if not pending:
        return []
    state = _load_account(portfolio_id)
    fills: list[dict] = []
    still_pending: list[dict] = []
    for o in pending:
        ticker = (o.get("ticker") or "").upper()
        want = float(o.get("shares") or 0.0)
        px = prices.get(ticker)
        if px is None or px <= 0:
            px = _current_price(ticker)
        if not px or px <= 0 or want <= 1e-9:
            still_pending.append(o)                    # can't fill without a price — keep queued
            continue
        buy = min(want, state["cash"] / px) if px else 0.0
        if buy <= 1e-9:
            still_pending.append(o)                    # out of cash — keep queued
            continue
        value = buy * px
        state["cash"] = max(0.0, state["cash"] - value)
        pos = state["positions"].get(ticker)
        if pos:
            total = pos["shares"] + buy
            pos["avg_cost"] = (pos["shares"] * pos["avg_cost"] + value) / total
            pos["shares"] = total
        else:
            state["positions"][ticker] = {"shares": buy, "avg_cost": px}
        fills.append({
            "date": asof, "ticker": ticker, "side": "buy",
            "shares": round(buy, 6), "price": round(px, 4), "value": round(value, 2),
            "from_pending": True,
        })
    _save_account(state, portfolio_id)
    fills_path = _paths(portfolio_id)["fills"]
    for fill in fills:
        _append_jsonl(fills_path, fill)
    _save_pending(still_pending, portfolio_id)
    return fills


# ---------------------------------------------------------------------------
# pending TARGET — the market-closed branch for the free-form Brain books
# ---------------------------------------------------------------------------
# The flagship queues BUY orders when shut (queue_orders, above). The Brain books submit a COMPLETE
# target book (sells + trims + buys), so a buy-only queue can't represent "rebalance to this at the
# open." Instead we persist the whole decided target and settle it with one rebalance() at the next
# open. Idempotent: re-running a closed session REPLACES the queued target (latest decision wins), so
# repeatedly building after the close can never churn the book — nothing is filled until the open.

def _pending_target_path(portfolio_id: str | None = None) -> Path:
    return _paths(portfolio_id)["data"] / "pending_target.json"


def save_pending_target(target_weights: dict[str, float], asof: str,
                        portfolio_id: str | None = None) -> None:
    """Persist a decided target book to settle at the NEXT market open — no fills now. Idempotent."""
    _ensure_dir(portfolio_id)
    payload = {
        "target": {str(k).upper(): float(v) for k, v in (target_weights or {}).items() if v},
        "asof": asof,
        "queued_at": datetime.now(timezone.utc).isoformat(),
    }
    _pending_target_path(portfolio_id).write_text(json.dumps(payload, indent=2, default=str))


def load_pending_target(portfolio_id: str | None = None) -> dict | None:
    """The queued target ({target, asof, queued_at}) or None. Survives a corrupt file."""
    try:
        p = _pending_target_path(portfolio_id)
        if p.exists():
            d = json.loads(p.read_text())
            if isinstance(d, dict) and isinstance(d.get("target"), dict):
                return d
    except Exception:
        pass
    return None


def clear_pending_target(portfolio_id: str | None = None) -> None:
    try:
        _pending_target_path(portfolio_id).unlink()
    except FileNotFoundError:
        pass
    except Exception:
        pass


def settle_target(prices: dict[str, float], asof: str,
                  portfolio_id: str | None = None) -> dict | None:
    """If a target was queued while the market was closed, rebalance to it now (at the open marks)
    and clear it. Returns the settled target dict, or None if nothing was queued. Paper-only."""
    pt = load_pending_target(portfolio_id)
    if not pt:
        return None
    target = pt.get("target") or {}
    rebalance(target, prices, asof, portfolio_id=portfolio_id)
    clear_pending_target(portfolio_id)
    return target


def mark(prices: dict[str, float], asof: str, portfolio_id: str | None = None,
         benchmark: str | None = None) -> None:
    """Snapshot NAV to nav_history.jsonl. Also initialises the benchmark shares on first call.

    The benchmark symbol is registry-resolved per book ('SPY' for the US books, 'FXI' for the
    all-China book); its inception shares are stored in the back-compat ``spy_shares`` slot, so
    ``spy_nav`` in the history is the comparison line for whichever benchmark the book uses."""
    state = _load_account(portfolio_id)
    bench = benchmark or _benchmark_for(portfolio_id)

    nav_path = _paths(portfolio_id)["nav"]

    # initialise the benchmark on first mark
    spy_px = prices.get(bench)
    if state.get("spy_shares") is None and spy_px and spy_px > 0:
        state["spy_shares"] = _STARTING_NAV / spy_px
        state["spy_inception_price"] = spy_px
        _save_account(state, portfolio_id)

    # ADDITIVE: persist each held position's latest mark onto the account so non-mark readers
    # (and the dashboard) have a stored current_price even between live-quote refreshes. We use the
    # SAME prices dict the NAV computation below uses, falling back to avg_cost when a name has no
    # live quote this run (so a stale/missing quote can't be written as a misleadingly precise mark).
    # Existing readers ignore the field; nav() still recomputes from `prices`, so this never alters NAV.
    _marked_any = False
    for ticker, pos in state["positions"].items():
        px = prices.get(ticker)
        if px is None or px <= 0:
            px = pos.get("avg_cost")
        if px is not None:
            pos["current_price"] = round(float(px), 4)
            _marked_any = True
    if _marked_any:
        _save_account(state, portfolio_id)

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
    if nav_path.exists():
        for line in nav_path.read_text().splitlines():
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
    _ensure_dir(portfolio_id)
    nav_path.write_text("\n".join(json.dumps(r, default=str) for r in rows) + "\n")


# ---------------------------------------------------------------------------
# SPY history loader (used by performance() for the comparison line only)
# ---------------------------------------------------------------------------

def _load_spy_history(window: int = 91, symbol: str = "SPY") -> "list[tuple[str, float]] | list":
    """Return [(date_str, close), ...] for the benchmark `symbol` over the last `window` sessions.

    Uses the same store loader as _fetch_price_series so it works offline as
    long as the engine price cache is populated.  Returns [] if unavailable.
    """
    s = _fetch_price_series(symbol)
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

def performance(portfolio_id: str | None = None,
                prices: dict[str, float] | None = None) -> dict:
    """Assemble the /api/performance contract.

    Series is HONEST:
      - spy_nav = real SPY history normalised to $1,000,000 at the first date
        of the window (S&P actual up/down, scaled to a $1M start).
      - nav (our portfolio) = $1,000,000 FLAT for every date before
        inception_date; from inception onward it uses the real marked NAV from
        nav_history.jsonl.  No hypothetical repricing of our allocation ever.
      - kind = "pre_inception" for the flat prefix, "realized" from inception.

    `prices` (TICKER → price in the book's BASE currency, e.g. live delayed quotes for the US
    books, snapshot/FX marks for HK/China) makes `current_nav` LIVE: the CURRENT account holdings
    are valued now (cash + live market value — realized P&L is already booked into cash) instead of
    freezing on the last once-daily nav_history snapshot. Without it the function degrades to the
    last realized row (back-compat). This is what makes the dashboard NAV move intraday.

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
        state = _load_account(portfolio_id)
        realized_rows = _load_jsonl(_paths(portfolio_id)["nav"])
        bench = _benchmark_for(portfolio_id)

        inception_date = state.get("inception_date", _INCEPTION_DATE)

        try:
            today_iso = date.today().isoformat()
        except Exception:
            today_iso = ""

        # LIVE current values: when the caller passes today's marks (base ccy), value the CURRENT
        # account holdings now (cash + live market value) instead of freezing on the last daily
        # nav_history snapshot — this is what moves the NAV intraday. Fall back to the last realized
        # row, then to starting NAV.
        live_nav: float | None = None
        if prices:
            try:
                live_nav = nav(prices, portfolio_id)
            except Exception:
                live_nav = None

        if live_nav is not None:
            current_nav = live_nav
            cash = float(state.get("cash", _STARTING_NAV))
            invested = current_nav - cash
            spy_nav_latest = realized_rows[-1].get("spy_nav") if realized_rows else None
        elif realized_rows:
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

        # day-over-day change. With a live mark, compare to the last daily close STRICTLY before
        # today (so we don't divide by today's own frozen snapshot); otherwise the prior row.
        day_change_pct: float = 0.0
        if live_nav is not None:
            prior = [r for r in realized_rows if (r.get("date") or "") < today_iso]
            if prior:
                prev_nav = float(prior[-1]["nav"])
                if prev_nav > 0:
                    day_change_pct = round((current_nav - prev_nav) / prev_nav * 100, 4)
        elif len(realized_rows) >= 2:
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
        # Load the benchmark history for the chart window (the comparison line).
        spy_history = _load_spy_history(91, bench)  # list of (date_str, close)

        series: list[dict] = []

        if spy_history:
            # Normalise SPY so spy_nav == $1M at the first date of the window
            spy0 = spy_history[0][1]
            spy_scale = _STARTING_NAV / spy0 if spy0 > 0 else 1.0

            # Build a quick lookup from the realized rows for nav by date
            realized_by_date: dict[str, float] = {
                r["date"]: float(r["nav"]) for r in realized_rows
            }
            # keep the chart endpoint in step with the live header NAV
            if live_nav is not None and today_iso:
                realized_by_date[today_iso] = current_nav

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
                nav_val = float(r["nav"])
                if live_nav is not None and today_iso and r.get("date") == today_iso:
                    nav_val = current_nav      # live-mark today's point
                series.append({
                    "date": r["date"],
                    "nav": nav_val,
                    "spy_nav": float(r["spy_nav"]) if r.get("spy_nav") is not None else None,
                    "kind": "realized",
                })

        note = (
            f"Portfolio starts at ${_STARTING_NAV:,.0f} on {inception_date}; "
            "flat until the live daily track accrues. "
            f"{bench} shown over the same window for comparison (real history)."
        )

        return {
            "inception_date": inception_date,
            "starting_nav": _STARTING_NAV,
            "current_nav": round(current_nav, 2),
            "cash": round(cash, 2),
            "invested": round(invested, 2),
            "total_return_pct": round(total_return_pct, 4),
            "vs_spy_pct": vs_spy_pct,
            "benchmark": bench,
            "day_change_pct": day_change_pct,
            "max_drawdown_pct": max_drawdown_pct,
            "realized_since": inception_date,
            "series": series,
            "note": note,
        }
    except Exception as exc:
        _base["note"] = f"Performance unavailable: {exc}"
        return _base
