"""One-shot correction: void the market-closed fills and re-express the book as
PENDING orders awaiting the next open.

Context (2026-06-21, Sunday): every fill in the paper book was booked while the
US market was CLOSED — the 06-17/06-18 builds ran off-hours, Friday 06-19 was the
Juneteenth holiday, and 06-20/06-21 are the weekend. Under the new market-hours
doctrine those were never legitimate executions: nothing trades when the desk is
shut. So we:

  • WIPE every fill (all the buys AND the spurious rebalance sells) — fills.jsonl
    holds executed trades only, and none executed.
  • RESET the paper account to a clean $1,000,000 cash, zero positions.
  • RE-QUEUE the intended book (the weights in latest.json) as PENDING buy orders,
    each estimated at the previous close (= the avg_cost on file = the 06-18 close),
    to fill at the next open (Monday 2026-06-22) at the real market price.
  • FLATTEN nav_history to an honest flat $1M (no trade => no NAV move).
  • TAG the book (latest.json / portfolio.json) so the dashboard shows each holding
    as PENDING, and CLEAR the positions ledger (nothing is actually held yet).

Idempotent: re-running reproduces the same clean pending state from latest.json's
target weights. Paper-only; touches nothing but data/portfolio/*.
"""
from __future__ import annotations

import json
from pathlib import Path

import bot  # noqa: F401  -> vendor/macro onto sys.path
from portfolio import market_calendar

_ROOT = Path(__file__).resolve().parent.parent
_PF = _ROOT / "data" / "portfolio"
_STARTING_NAV = 1_000_000.0
_INCEPTION = "2026-06-19"

# Canonical previous-close marks (the Thursday 2026-06-18 closes that were on file as each
# name's avg_cost before the reset). Embedded so this script is self-contained and idempotent
# — it does NOT depend on account.json, which the reset itself empties. Weights come from the
# live book (latest.json); a name absent from this map is skipped (no prev close to estimate).
_PREV_CLOSE: dict[str, float] = {
    "SMH": 659.88, "XLK": 191.44, "MTUM": 338.52, "IWM": 295.59,
    "AME": 237.42, "CHRW": 185.04, "FSS": 118.52, "AVGO": 411.35,
    "ANET": 169.67, "APH": 163.96, "DD": 47.71,
}


def main() -> None:
    latest_path = _PF / "latest.json"
    account_path = _PF / "account.json"
    book = json.loads(latest_path.read_text())
    try:
        account = json.loads(account_path.read_text())
    except Exception:
        account = {}

    inception = account.get("inception_date", _INCEPTION)
    # previous close = the last real session before the next open (Thu 2026-06-18)
    next_open_day = market_calendar.next_open_day().isoformat()
    placed_asof = market_calendar.previous_trading_day(
        market_calendar.next_open().date()).isoformat()

    est_prices = dict(_PREV_CLOSE)

    # ---- build PENDING buy orders from the intended target weights ----
    pending: list[dict] = []
    for p in book.get("positions", []):
        tk = p["ticker"].upper()
        weight = float(p.get("weight") or 0.0)
        px = est_prices.get(tk)
        if not px or px <= 0 or weight <= 0:
            continue
        shares = (weight * _STARTING_NAV) / px
        pending.append({
            "id": f"{placed_asof}-{tk}-buy",
            "ticker": tk,
            "side": "buy",
            "shares": round(shares, 6),
            "est_price": round(float(px), 4),
            "est_value": round(weight * _STARTING_NAV, 2),
            "weight": round(weight, 4),
            "placed_asof": placed_asof,
            "fill_after": next_open_day,
            "status": "pending",
        })

    # ---- 1) fills.jsonl: executed trades only — none executed ----
    (_PF / "fills.jsonl").write_text("")

    # ---- 2) account.json: clean $1M cash, no positions (keep SPY benchmark) ----
    account["cash"] = _STARTING_NAV
    account["positions"] = {}
    account["starting_nav"] = _STARTING_NAV
    account.setdefault("inception_date", inception)
    account_path.write_text(json.dumps(account, indent=2, default=str))

    # ---- 3) pending_orders.json ----
    (_PF / "pending_orders.json").write_text(json.dumps(pending, indent=2, default=str))

    # ---- 4) nav_history.jsonl: honest flat $1M (nothing traded) ----
    nav_rows = [
        {"date": inception, "nav": _STARTING_NAV, "cash": _STARTING_NAV,
         "invested": 0.0, "spy_nav": _STARTING_NAV},
        {"date": "2026-06-21", "nav": _STARTING_NAV, "cash": _STARTING_NAV,
         "invested": 0.0, "spy_nav": _STARTING_NAV},
    ]
    (_PF / "nav_history.jsonl").write_text(
        "\n".join(json.dumps(r) for r in nav_rows) + "\n")

    # ---- 5) latest.json / portfolio.json: tag holdings PENDING ----
    pend_by_tk = {o["ticker"]: o for o in pending}
    for p in book.get("positions", []):
        o = pend_by_tk.get(p["ticker"].upper())
        if o:
            p["pending"] = True
            p["status"] = "pending_open"
            p["est_price"] = o["est_price"]
            p["fill_after"] = next_open_day
            # not yet opened — clear stale open marks so the UI shows no held-days
            p["opened_at"] = None
            p["held_days"] = None
    book["market_status"] = "closed"
    book["pending_orders"] = pending
    for name in ("latest.json", "portfolio.json"):
        (_PF / name).write_text(json.dumps(book, indent=2, default=str, ensure_ascii=False))

    # ---- 6) positions_ledger.json: nothing actually held ----
    (_PF / "positions_ledger.json").write_text("{}")

    print(f"reset complete: {len(pending)} pending buy orders, est. at prev close ({placed_asof}),")
    print(f"  fill at next open {next_open_day}; account = ${_STARTING_NAV:,.0f} cash, 0 positions, 0 fills.")
    total_est = sum(o["est_value"] for o in pending)
    print(f"  total queued ≈ ${total_est:,.0f} ({total_est/_STARTING_NAV:.1%} of NAV); "
          f"cash after fills ≈ ${_STARTING_NAV-total_est:,.0f}.")


if __name__ == "__main__":
    main()
