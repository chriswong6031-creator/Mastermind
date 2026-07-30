"""Complete trade blotter + per-fill P&L, derived from the paper account's fills.

Every simulated fill (one buy or one sell, written by `paper_account.rebalance`)
becomes ONE blotter row, in chronological order. FIFO lot accounting yields, per row:

  - SELL: realized P&L = Σ over the matched open lots of (sell_px − lot_cost) · shares.
          The row's `price` is shown as the matched cost basis; `exit_price` is the sale.
  - BUY : unrealized P&L on the still-open remainder, marked to a live quote.

FIFO is the standard blotter convention and is exact; for single-buy positions it
coincides with the account's average-cost basis. Paper-only — reads
data/portfolio/fills.jsonl, writes nothing.
"""
from __future__ import annotations

import json
from collections import defaultdict, deque
from pathlib import Path
from typing import Optional

_ROOT = Path(__file__).resolve().parent.parent
_FILLS_PATH = _ROOT / "data" / "portfolio" / "fills.jsonl"


def _fills_path(portfolio_id: str | None = None) -> Path:
    """Resolve the fills file. None/default → the legacy module-global (kept patchable
    for the test fixtures); any other id → data/portfolios/<id>/fills.jsonl."""
    from portfolio import registry
    if not portfolio_id or portfolio_id == registry.DEFAULT_ID:
        return _FILLS_PATH
    return registry.data_dir(portfolio_id) / "fills.jsonl"


def _load_fills(portfolio_id: str | None = None) -> list[dict]:
    """All fills, oldest first (stable within a date). Skips corrupt lines."""
    path = _fills_path(portfolio_id)
    if not path.exists():
        return []
    rows: list[dict] = []
    for i, line in enumerate(path.read_text(encoding="utf-8").splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
            r["_seq"] = i  # preserve file order as a stable tiebreak
            rows.append(r)
        except Exception:
            pass
    rows.sort(key=lambda r: (r.get("date") or "", r.get("_seq", 0)))
    return rows


def history(live_prices: Optional[dict[str, float]] = None,
            portfolio_id: str | None = None) -> list[dict]:
    """Return the complete blotter, NEWEST first. Each row:

        {date, ticker, action: 'buy'|'sell', shares, price, value, exit_price,
         realized_pnl, realized_pct, unrealized_pnl, unrealized_pct,
         open_shares, still_open}

    `live_prices` ({TICKER: px}) marks open BUY remainders to market for unrealized.
    """
    fills = _load_fills(portfolio_id)
    live = {(k or "").upper(): v for k, v in (live_prices or {}).items()}
    # Fills are the source of P&L, but some older books contain fractional FIFO
    # residue from historical sizing/rounding migrations.  When an authoritative
    # account exists, use its current share counts to decide which BUY remainders
    # are genuinely open instead of displaying those stale residues as positions.
    authoritative_shares: dict[str, float] | None = None
    try:
        from portfolio import paper_account
        account_path = paper_account._paths(portfolio_id)["account"]
        if account_path.exists():
            account = paper_account._load_account(portfolio_id)
            authoritative_shares = {
                (ticker or "").upper(): max(0.0, float(pos.get("shares") or 0.0))
                for ticker, pos in account.get("positions", {}).items()
                if isinstance(pos, dict)
            }
    except Exception:
        # A malformed/unavailable account must not hide otherwise valid fill history.
        authoritative_shares = None
    # ticker -> FIFO queue of [shares_remaining, cost, row_ref]
    lots: dict[str, deque] = defaultdict(deque)
    rows: list[dict] = []

    for f in fills:
        tk = (f.get("ticker") or "").upper()
        side = f.get("side")
        shares = float(f.get("shares") or 0.0)
        price = float(f.get("price") or 0.0)
        row = {
            "date": f.get("date"),
            "ticker": tk,
            "action": side,
            "shares": round(shares, 6),
            "price": round(price, 4),
            "value": f.get("value"),
            "exit_price": None,
            "realized_pnl": None,
            "realized_pct": None,
            "unrealized_pnl": None,
            "unrealized_pct": None,
            "open_shares": None,
            "still_open": False,
        }

        if side == "buy":
            lots[tk].append([shares, price, row])
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
            row["exit_price"] = round(price, 4)
            if matched > 1e-9:
                avg_cost = cost_basis / matched
                row["price"] = round(avg_cost, 4)  # 'Price' = matched cost basis
                row["realized_pnl"] = round((price - avg_cost) * matched, 2)
                row["realized_pct"] = round((price / avg_cost - 1) * 100, 2) if avg_cost else None
        rows.append(row)

    # mark still-open BUY remainders to the live quote
    for tk, q in lots.items():
        px = live.get(tk)
        account_remaining = (
            authoritative_shares.get(tk, 0.0)
            if authoritative_shares is not None
            else None
        )
        for shares_rem, cost, row in q:
            if shares_rem <= 1e-9:
                continue
            open_shares = (
                min(shares_rem, account_remaining)
                if account_remaining is not None
                else shares_rem
            )
            if account_remaining is not None:
                account_remaining = max(0.0, account_remaining - open_shares)
            if open_shares <= 1e-9:
                continue
            row["still_open"] = True
            row["open_shares"] = round(open_shares, 6)
            if px and cost:
                row["unrealized_pnl"] = round((px - cost) * open_shares, 2)
                row["unrealized_pct"] = round((px / cost - 1) * 100, 2)

    rows.reverse()  # newest first for display
    return rows


def realized_total(portfolio_id: str | None = None) -> float:
    """Sum of realized P&L across all closed (matched) shares — paper lifetime."""
    return round(sum((r.get("realized_pnl") or 0.0) for r in history(portfolio_id=portfolio_id)), 2)


def sell_realized(portfolio_id: str | None = None) -> dict[tuple[str, str], dict]:
    """Per-SELL realized P&L + the fraction of the position trimmed, keyed by ``(date, TICKER)``.

    Uses the SAME FIFO lot accounting as ``history()`` so the Daily Decision Log's sell chips
    agree to the cent with the Trade History blotter, and adds ``pct_of_position`` = shares sold /
    shares held immediately before the sell (1.0 = full exit). A rebalance writes at most one
    net buy or sell per name per day, so ``(date, ticker)`` is a stable key. Each value:

        {shares_sold, pct_of_position, realized_pnl, realized_pct}  # P&L/pct omitted if unmatched
    """
    fills = _load_fills(portfolio_id)
    lots: dict[str, deque] = defaultdict(deque)   # FIFO open lots, mirrors history()
    net: dict[str, float] = defaultdict(float)    # running net shares (for the trim fraction)
    out: dict[tuple[str, str], dict] = {}
    for f in fills:
        tk = (f.get("ticker") or "").upper()
        side = f.get("side")
        shares = float(f.get("shares") or 0.0)
        price = float(f.get("price") or 0.0)
        if side == "buy":
            lots[tk].append([shares, price])
            net[tk] += shares
            continue
        if side != "sell":
            continue
        held_before = net[tk]
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
        net[tk] -= shares
        rec: dict = {"shares_sold": round(shares, 6)}
        if held_before > 1e-9:
            rec["pct_of_position"] = round(min(shares / held_before, 1.0), 4)
        if matched > 1e-9:
            avg_cost = cost_basis / matched
            rec["realized_pnl"] = round((price - avg_cost) * matched, 2) + 0.0   # +0.0 → no -0.0
            rec["realized_pct"] = (round((price / avg_cost - 1) * 100, 2) + 0.0) if avg_cost else None
        out[(f.get("date"), tk)] = rec
    return out
