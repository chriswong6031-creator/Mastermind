"""Self-Directed book — the THIRD Mastermind portfolio.

DOCTRINE.md frames two consumers of the engine: SELF (the bot's own autonomous paper
book) and OPERATOR (read-only advisories over the user's real holdings). This is the
third and only book the *user* drives directly: you search the name, you size the order,
you write the thesis. The engine here is a dumb, honest executor — no scorer, no gate, no
auto-execution. PAPER ONLY.

Rules (kept consistent with the rest of the system):
  - Long-only, no leverage. Buys are bounded by cash; sells are bounded by shares held.
  - Market OPEN  → the order fills immediately at the live market price.
  - Market CLOSED → the order is queued PENDING and fills at the NEXT session's OPEN price.
  - Pending orders settle lazily: any read (book/history) or new order, while the market is
    open, first sweeps the pending queue and fills it at the day's open (Polygon snapshot
    `day.o`), falling back to the last price if the open isn't published yet.

State (data/portfolio/self_directed/ — fully isolated from the autonomous account.json):
  account.json   — inception_date, starting_nav, cash, positions {TICKER:{shares,avg_cost}}
  fills.jsonl    — one line per executed fill (feeds the Trade History blotter)
  pending.json   — queued orders awaiting the next open
  theses.json    — {TICKER: {note, updated_at}} — your conviction notes

Every external dependency (Polygon, the macro price store, the market clock) is imported
lazily and is optional, so this module imports and unit-tests cleanly with no vendor data —
inject `price`/`prices`/`market_open`/`now` to pin behaviour.
"""
from __future__ import annotations

import json
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

_ROOT = Path(__file__).resolve().parent.parent
_DATA = _ROOT / "data" / "portfolio" / "self_directed"
_ACCOUNT_PATH = _DATA / "account.json"
_FILLS_PATH = _DATA / "fills.jsonl"
_PENDING_PATH = _DATA / "pending.json"
_THESES_PATH = _DATA / "theses.json"

_STARTING_NAV = 1_000_000.0


# ---------------------------------------------------------------------------
# persistence helpers
# ---------------------------------------------------------------------------

def _ensure_dir() -> None:
    _DATA.mkdir(parents=True, exist_ok=True)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _fresh_account() -> dict[str, Any]:
    return {
        "inception_date": datetime.now(timezone.utc).date().isoformat(),
        "starting_nav": _STARTING_NAV,
        "cash": _STARTING_NAV,
        "positions": {},          # TICKER -> {shares, avg_cost}
    }


def _load_account() -> dict[str, Any]:
    """Load account state; a fresh $1M book on any corruption."""
    try:
        if _ACCOUNT_PATH.exists():
            raw = json.loads(_ACCOUNT_PATH.read_text())
            if isinstance(raw.get("cash"), (int, float)) and isinstance(raw.get("positions"), dict):
                raw.setdefault("starting_nav", _STARTING_NAV)
                raw.setdefault("inception_date", _fresh_account()["inception_date"])
                return raw
    except Exception:
        pass
    return _fresh_account()


def _save_account(state: dict[str, Any]) -> None:
    _ensure_dir()
    _ACCOUNT_PATH.write_text(json.dumps(state, indent=2, default=str))


def _append_fill(fill: dict) -> None:
    _ensure_dir()
    with _FILLS_PATH.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(fill, default=str) + "\n")


def _load_fills() -> list[dict]:
    if not _FILLS_PATH.exists():
        return []
    rows: list[dict] = []
    for i, line in enumerate(_FILLS_PATH.read_text(encoding="utf-8").splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
            r["_seq"] = i
            rows.append(r)
        except Exception:
            pass
    rows.sort(key=lambda r: (r.get("date") or "", r.get("_seq", 0)))
    return rows


def _load_pending() -> list[dict]:
    try:
        if _PENDING_PATH.exists():
            data = json.loads(_PENDING_PATH.read_text())
            return data if isinstance(data, list) else []
    except Exception:
        pass
    return []


def _save_pending(orders: list[dict]) -> None:
    _ensure_dir()
    _PENDING_PATH.write_text(json.dumps(orders, indent=2, default=str))


def _load_theses() -> dict[str, dict]:
    try:
        if _THESES_PATH.exists():
            data = json.loads(_THESES_PATH.read_text())
            return data if isinstance(data, dict) else {}
    except Exception:
        pass
    return {}


def _save_theses(theses: dict[str, dict]) -> None:
    _ensure_dir()
    _THESES_PATH.write_text(json.dumps(theses, indent=2, default=str, ensure_ascii=False))


# ---------------------------------------------------------------------------
# price + clock resolution (all lazy / optional)
# ---------------------------------------------------------------------------

def _stockdata_price(ticker: str) -> Optional[float]:
    """Last mark from vendor/macro/site/stockdata/<T>.json — a plain file read, no import."""
    try:
        p = _ROOT / "vendor" / "macro" / "site" / "stockdata" / f"{ticker.upper()}.json"
        if p.exists():
            v = (json.loads(p.read_text()).get("tech") or {}).get("price")
            if v is not None and float(v) > 0:
                return float(v)
    except Exception:
        pass
    return None


def _live_quote(ticker: str) -> Optional[float]:
    try:
        from data_layer import polygon
        px = polygon.quote(ticker)
        if px and float(px) > 0:
            return float(px)
        # delayed/EOD-tier keys: quote() may be empty outside market hours — fall back to the
        # snapshot's best price (down to prior-day close) so any US name is still priceable.
        px = polygon.snapshot_price(ticker)
        if px and float(px) > 0:
            return float(px)
    except Exception:
        return None
    return None


def _current_price(ticker: str) -> Optional[float]:
    """Best available current price: live Polygon quote, then the stockdata snapshot mark."""
    return _live_quote(ticker) or _stockdata_price(ticker)


def _open_price(ticker: str) -> Optional[float]:
    """The session's OPEN (for filling queued orders), falling back to last price."""
    try:
        from data_layer import polygon
        o = polygon.day_open(ticker)
        if o and float(o) > 0:
            return float(o)
    except Exception:
        pass
    return _current_price(ticker)


def _market_open(now: datetime | None = None) -> bool:
    try:
        from portfolio import market_clock
        return market_clock.is_open(now)
    except Exception:
        return False


def _market_status() -> dict:
    try:
        from portfolio import market_clock
        return market_clock.status()
    except Exception:
        return {"is_open": False, "session": "closed", "now_et": None, "next_open": None}


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


# ---------------------------------------------------------------------------
# core fill mechanics (mutates the passed account dict; returns a fill or None)
# ---------------------------------------------------------------------------

def _apply_fill(state: dict, ticker: str, side: str, shares: float, px: float,
                asof: str) -> Optional[dict]:
    """Apply ONE long-only fill to `state`. Buys are clamped to cash, sells to shares held.
    Returns the fill record, or None if nothing could transact (no cash / no shares)."""
    ticker = ticker.upper()
    positions = state.setdefault("positions", {})
    pos = positions.get(ticker)

    if side == "buy":
        shares = min(float(shares), state["cash"] / px) if px > 0 else 0.0
        if shares <= 1e-9:
            return None
        value = shares * px
        state["cash"] = max(0.0, state["cash"] - value)
        if pos:
            total = pos["shares"] + shares
            pos["avg_cost"] = (pos["shares"] * pos["avg_cost"] + value) / total
            pos["shares"] = total
        else:
            positions[ticker] = {"shares": shares, "avg_cost": px}
    else:  # sell — bounded by shares held (long-only: no shorting)
        if not pos or pos["shares"] <= 1e-9:
            return None
        shares = min(float(shares), pos["shares"])
        if shares <= 1e-9:
            return None
        value = shares * px
        state["cash"] += value
        pos["shares"] -= shares
        if pos["shares"] <= 1e-9:
            del positions[ticker]

    return {"date": asof, "ticker": ticker, "side": side,
            "shares": round(shares, 6), "price": round(px, 4), "value": round(value, 2)}


# ---------------------------------------------------------------------------
# pending-order settlement
# ---------------------------------------------------------------------------

def settle_pending(*, now: datetime | None = None, market_open: bool | None = None,
                   prices: dict[str, float] | None = None) -> list[dict]:
    """Fill any queued orders at the session OPEN, IF the market is open. No-op otherwise.

    A queued buy with no cash / a queued sell with no shares at open is dropped as
    `cancelled` (it would be a no-op fill). Returns the settled order records."""
    pending = _load_pending()
    if not pending:
        return []
    if market_open is None:
        market_open = _market_open(now)
    if not market_open:
        return []

    state = _load_account()
    asof = _today()
    settled: list[dict] = []
    still_pending: list[dict] = []
    prices = prices or {}

    for o in pending:
        ticker = (o.get("ticker") or "").upper()
        px = prices.get(ticker) or _open_price(ticker)
        if not px or px <= 0:
            still_pending.append(o)          # unpriceable this sweep — keep queued
            continue
        fill = _apply_fill(state, ticker, o.get("side"), float(o.get("shares") or 0), px, asof)
        if fill:
            fill["order_id"] = o.get("order_id")
            fill["queued"] = True
            _append_fill(fill)
            settled.append({**o, "status": "filled", "filled_price": fill["price"],
                            "filled_at": _now_iso()})
        else:
            settled.append({**o, "status": "cancelled",
                            "reason": "no cash" if o.get("side") == "buy" else "no shares"})

    _save_account(state)
    _save_pending(still_pending)
    return settled


# ---------------------------------------------------------------------------
# the public order API
# ---------------------------------------------------------------------------

def place_order(ticker: str, side: str, shares: float, *, price: float | None = None,
                now: datetime | None = None, market_open: bool | None = None) -> dict:
    """Place a single buy/sell. Returns one of:
      {ok:True, status:'filled', fill:{...}, cash_after}     — executed at market now
      {ok:True, status:'pending', order:{...}}               — queued for the next open
      {ok:False, error:'...'}                                — rejected (bad input / no price / nothing to do)
    """
    ticker = (ticker or "").upper().strip()
    side = (side or "").lower().strip()
    try:
        shares = float(shares)
    except (TypeError, ValueError):
        return {"ok": False, "error": "shares must be a number"}
    if not ticker:
        return {"ok": False, "error": "ticker required"}
    if side not in ("buy", "sell"):
        return {"ok": False, "error": "side must be 'buy' or 'sell'"}
    if shares <= 0:
        return {"ok": False, "error": "shares must be positive"}

    if market_open is None:
        market_open = _market_open(now)

    # market open → settle any stragglers, then fill this one immediately
    if market_open:
        settle_pending(now=now, market_open=True)
        state = _load_account()
        px = float(price) if (price and float(price) > 0) else _current_price(ticker)
        if not px or px <= 0:
            return {"ok": False, "error": f"no live price available for {ticker}"}
        # pre-flight long-only guards (so the user gets a clear message, not a silent no-op)
        if side == "buy" and state["cash"] < px:
            return {"ok": False, "error": "insufficient cash"}
        if side == "sell":
            held = (state.get("positions", {}).get(ticker) or {}).get("shares", 0.0)
            if held <= 1e-9:
                return {"ok": False, "error": f"no {ticker} position to sell"}
        fill = _apply_fill(state, ticker, side, shares, px, _today())
        if not fill:
            return {"ok": False, "error": "order could not be filled"}
        _save_account(state)
        _append_fill(fill)
        return {"ok": True, "status": "filled", "fill": fill, "cash_after": round(state["cash"], 2)}

    # market closed → queue PENDING (fills at the next open). Reject obvious no-ops up front.
    if side == "sell":
        state = _load_account()
        held = (state.get("positions", {}).get(ticker) or {}).get("shares", 0.0)
        if held <= 1e-9:
            return {"ok": False, "error": f"no {ticker} position to sell"}
    order = {
        "order_id": _gen_order_id(ticker),
        "ticker": ticker,
        "side": side,
        "shares": round(shares, 6),
        "placed_at": _now_iso(),
        "status": "pending",
    }
    pending = _load_pending()
    pending.append(order)
    _save_pending(pending)
    return {"ok": True, "status": "pending", "order": order}


def cancel_order(order_id: str) -> bool:
    """Remove a still-pending order. Returns True if one was removed."""
    pending = _load_pending()
    kept = [o for o in pending if o.get("order_id") != order_id]
    if len(kept) == len(pending):
        return False
    _save_pending(kept)
    return True


_order_seq = 0


def _gen_order_id(ticker: str) -> str:
    global _order_seq
    _order_seq += 1
    # monotonic-ish, human-readable; uniqueness within a process is all we need
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    return f"{ticker}-{stamp}-{_order_seq}"


# ---------------------------------------------------------------------------
# conviction theses
# ---------------------------------------------------------------------------

def set_thesis(ticker: str, note: str) -> Optional[dict]:
    """Save (or clear, if note is empty) the user's conviction note for a ticker."""
    ticker = (ticker or "").upper().strip()
    if not ticker:
        return None
    theses = _load_theses()
    note = (note or "").strip()
    if note:
        theses[ticker] = {"note": note, "updated_at": _now_iso()}
    else:
        theses.pop(ticker, None)
    _save_theses(theses)
    return theses.get(ticker)


# ---------------------------------------------------------------------------
# read models — the book + the blotter
# ---------------------------------------------------------------------------

def quote_info(ticker: str) -> dict:
    """Live price + reference name for the order-ticket display. Best-effort."""
    ticker = (ticker or "").upper().strip()
    if not ticker:
        return {"ticker": "", "price": None}
    name = ""
    try:
        from data_layer import polygon
        det = polygon.ticker_details(ticker)
        if det:
            name = det.get("name") or ""
    except Exception:
        pass
    return {"ticker": ticker, "price": _current_price(ticker), "name": name,
            "market": _market_status()}


def book(*, prices: dict[str, float] | None = None, now: datetime | None = None,
         market_open: bool | None = None) -> dict:
    """The Self-Directed book contract for the dashboard.

    Settles pending orders first (if the market is open), then returns positions with live
    marks + weights, the allocation scorecard, the market state, and the pending queue.
    `prices` (TICKER→px) overrides live marks (used by tests / a shared price fetch)."""
    settle_pending(now=now, market_open=market_open, prices=prices)
    state = _load_account()
    theses = _load_theses()
    prices = dict(prices or {})

    positions_raw = state.get("positions", {})
    marks: dict[str, float] = {}
    for ticker, pos in positions_raw.items():
        px = prices.get(ticker) or _current_price(ticker)
        if px and px > 0:
            marks[ticker] = float(px)

    invested = 0.0
    for ticker, pos in positions_raw.items():
        px = marks.get(ticker, pos.get("avg_cost") or 0.0)
        invested += pos.get("shares", 0.0) * px
    cash = state.get("cash", 0.0)
    nav = cash + invested

    positions: list[dict] = []
    for ticker, pos in positions_raw.items():
        shares = float(pos.get("shares") or 0.0)
        avg = float(pos.get("avg_cost") or 0.0)
        px = marks.get(ticker)
        mv = shares * px if px else None
        th = theses.get(ticker) or {}
        positions.append({
            "ticker": ticker,
            "shares": round(shares, 6),
            "avg_cost": round(avg, 4) if avg else None,
            "current_price": round(px, 4) if px else None,
            "market_value": round(mv, 2) if mv is not None else None,
            "unrealized_pnl": round((px - avg) * shares, 2) if (px and avg) else None,
            "unrealized_pct": round((px / avg - 1) * 100, 2) if (px and avg) else None,
            "weight": round(mv / nav, 6) if (mv is not None and nav > 0) else None,
            "thesis": th.get("note"),
            "thesis_updated_at": th.get("updated_at"),
        })
    # heaviest weight first; un-priced names sink to the bottom
    positions.sort(key=lambda p: (p["weight"] if p["weight"] is not None else -1), reverse=True)

    weights = [p["weight"] for p in positions if p["weight"] is not None]
    total_unreal = sum(p["unrealized_pnl"] for p in positions if p["unrealized_pnl"] is not None)

    pending = _mark_pending(_load_pending(), prices)

    return {
        "starting_nav": state.get("starting_nav", _STARTING_NAV),
        "inception_date": state.get("inception_date"),
        "nav": round(nav, 2),
        "cash": round(cash, 2),
        "invested": round(invested, 2),
        "positions": positions,
        "allocation": {
            "cash_pct": round(cash / nav, 6) if nav > 0 else 1.0,
            "invested_pct": round(invested / nav, 6) if nav > 0 else 0.0,
            "gross": round(invested / nav, 6) if nav > 0 else 0.0,
            "n_positions": len(positions),
            "largest_weight": round(max(weights), 6) if weights else 0.0,
            "total_unrealized_pnl": round(total_unreal, 2),
            "total_return_pct": round((nav - state.get("starting_nav", _STARTING_NAV))
                                      / state.get("starting_nav", _STARTING_NAV) * 100, 4),
        },
        "pending": pending,
        "market": _market_status(),
    }


def _mark_pending(pending: list[dict], prices: dict[str, float]) -> list[dict]:
    """Attach an indicative current price + est. value to each queued order for display."""
    out = []
    for o in pending:
        ticker = (o.get("ticker") or "").upper()
        px = prices.get(ticker) or _current_price(ticker)
        shares = float(o.get("shares") or 0.0)
        out.append({
            "order_id": o.get("order_id"),
            "ticker": ticker,
            "side": o.get("side"),
            "shares": round(shares, 6),
            "placed_at": o.get("placed_at"),
            "est_price": round(px, 4) if px else None,
            "est_value": round(shares * px, 2) if px else None,
        })
    # newest first
    out.sort(key=lambda o: o.get("placed_at") or "", reverse=True)
    return out


def history(*, prices: dict[str, float] | None = None, now: datetime | None = None,
            market_open: bool | None = None) -> dict:
    """Trade-history blotter: every executed fill (FIFO realized P&L on sells, live
    unrealized on open buy remainders) + the pending queue + a realized summary."""
    settle_pending(now=now, market_open=market_open, prices=prices)
    fills = _load_fills()
    live = {(k or "").upper(): v for k, v in (prices or {}).items()}

    lots: dict[str, deque] = defaultdict(deque)
    rows: list[dict] = []
    for f in fills:
        tk = (f.get("ticker") or "").upper()
        side = f.get("side")
        shares = float(f.get("shares") or 0.0)
        px = float(f.get("price") or 0.0)
        row = {
            "date": f.get("date"), "ticker": tk, "action": side,
            "shares": round(shares, 6), "price": round(px, 4), "value": f.get("value"),
            "exit_price": None, "realized_pnl": None, "realized_pct": None,
            "unrealized_pnl": None, "unrealized_pct": None,
            "queued": bool(f.get("queued")), "still_open": False, "open_shares": None,
        }
        if side == "buy":
            lots[tk].append([shares, px, row])
        elif side == "sell":
            remaining, cost_basis, matched = shares, 0.0, 0.0
            q = lots[tk]
            while remaining > 1e-9 and q:
                lot = q[0]
                take = min(lot[0], remaining)
                cost_basis += take * lot[1]
                matched += take
                lot[0] -= take
                remaining -= take
                if lot[0] <= 1e-9:
                    q.popleft()
            row["exit_price"] = round(px, 4)
            if matched > 1e-9:
                avg = cost_basis / matched
                row["price"] = round(avg, 4)
                row["realized_pnl"] = round((px - avg) * matched, 2)
                row["realized_pct"] = round((px / avg - 1) * 100, 2) if avg else None
        rows.append(row)

    for tk, q in lots.items():
        live_px = live.get(tk) or _current_price(tk)
        for shares_rem, cost, row in q:
            if shares_rem <= 1e-9:
                continue
            row["still_open"] = True
            row["open_shares"] = round(shares_rem, 6)
            if live_px and cost:
                row["unrealized_pnl"] = round((live_px - cost) * shares_rem, 2)
                row["unrealized_pct"] = round((live_px / cost - 1) * 100, 2)

    rows.reverse()  # newest first
    sells = [r for r in rows if r["action"] == "sell" and r["realized_pnl"] is not None]
    realized_total = round(sum(r["realized_pnl"] for r in sells), 2)
    wins = sum(1 for r in sells if r["realized_pnl"] > 0)
    return {
        "history": rows,
        "pending": _mark_pending(_load_pending(), dict(prices or {})),
        "realized_total": realized_total,
        "n_closed": len(sells),
        "n_buys": sum(1 for r in rows if r["action"] == "buy"),
        "win_rate": round(wins / len(sells), 4) if sells else None,
    }
