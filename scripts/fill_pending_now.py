"""Settle a paper book's queued PENDING orders at TODAY's market OPEN — the manual rescue for a
day the scheduled open-market build didn't run, so overnight orders sat unfilled.

For each queued order it fills the queued share count at the session OPEN (Polygon ``day.o``),
falling back to the live quote → snapshot → the order's own estimate. It then re-marks NAV at the
current quote and clears the PENDING tags on the book's ``latest.json`` so the dashboard renders
the names as HOLDING with live unrealized P&L. PAPER ONLY — reversible, mutates only the book's
own state files.

Usage:  python3 scripts/fill_pending_now.py [portfolio_id]   (default: flagship)

USD books (flagship / autonomous / heavyweight) fill at the real Polygon open. Non-USD books
(hk / china) have no Polygon open, so they settle at the current base-currency snapshot mark.
"""
from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import bot  # noqa: F401,E402 — bootstraps vendor/macro onto sys.path

from portfolio import paper_account, registry  # noqa: E402


def _fill_price(ticker: str, ccy: str) -> tuple[float | None, str]:
    """Today's OPEN for `ticker` in the book's base currency, with a graceful fallback chain."""
    if ccy == "USD":
        from data_layer import polygon
        for name, fn in (("day_open", polygon.day_open), ("quote", polygon.quote),
                         ("snapshot", polygon.snapshot_price)):
            try:
                v = fn(ticker)
            except Exception:
                v = None
            if v and float(v) > 0:
                return float(v), name
        return None, "none"
    # non-US single-currency book: settle at the current base-ccy snapshot mark
    from portfolio import fx
    base = fx.usd_to(paper_account._current_price(ticker), ccy)
    return (float(base), "snapshot_fx") if base and base > 0 else (None, "none")


def _current_marks(tickers: list[str], ccy: str) -> dict[str, float]:
    """Current marks (for the post-fill NAV mark), base ccy. Includes the SPY benchmark for USD."""
    out: dict[str, float] = {}
    if ccy == "USD":
        from data_layer import polygon
        syms = list(dict.fromkeys([*tickers, "SPY"]))
        for t in syms:
            try:
                v = polygon.quote(t) or polygon.snapshot_price(t)
            except Exception:
                v = None
            if v and float(v) > 0:
                out[t] = float(v)
    else:
        from portfolio import fx
        for t in tickers:
            base = fx.usd_to(paper_account._current_price(t), ccy)
            if base and base > 0:
                out[t] = float(base)
    return out


def settle(pid: str | None = None, *, require_open: bool = False, log=print) -> dict:
    """Settle `pid`'s queued PENDING orders at today's open. Returns a summary dict
    {filled, marked_nav, remaining, skipped?}. ``require_open`` guards on the book's market being
    open (so an automated caller never books a fill on a closed session). Safe to call when there
    is nothing to do — returns {filled:0}. Paper-only."""
    pid = pid or registry.DEFAULT_ID
    ccy = registry.currency(pid)
    pending = paper_account.load_pending(pid)
    if not pending:
        return {"filled": 0, "remaining": 0, "note": "no pending"}
    if require_open:
        try:
            from portfolio import market_calendar
            if not market_calendar.is_open():
                log(f"[{pid}] market closed — leaving {len(pending)} order(s) queued.")
                return {"filled": 0, "remaining": len(pending), "skipped": "market_closed"}
        except Exception:
            pass

    asof = date.today().isoformat()
    log(f"[{pid}] settling {len(pending)} pending order(s) at the {asof} open ({ccy})…")

    fill_prices: dict[str, float] = {}
    for o in pending:
        t = (o.get("ticker") or "").upper()
        px, src = _fill_price(t, ccy)
        if px is None:
            px, src = float(o.get("est_price") or 0.0), "est_price"
        fill_prices[t] = px
        est = o.get("est_price")
        drift = f"{(px/est - 1)*100:+.2f}%" if est else "—"
        log(f"   {t:8} open={px:>10.4f}  (est {est}  Δ{drift}  via {src})")

    filled = paper_account.fill_pending(fill_prices, asof, portfolio_id=pid)
    log(f"[{pid}] filled {len(filled)} order(s).")

    # re-mark NAV at the current quote so nav_history carries an honest today point
    acct = json.loads((registry.data_dir(pid) / "account.json").read_text())
    held = list((acct.get("positions") or {}).keys())
    marks = _current_marks(held, ccy)
    marked_nav = None
    try:
        paper_account.mark(marks, asof, portfolio_id=pid)
        marked_nav = round(paper_account.nav(marks, pid), 2)
        log(f"[{pid}] marked NAV = {marked_nav:,.2f} {ccy} (cash {acct.get('cash'):,.2f})")
    except Exception as e:  # noqa: BLE001
        log(f"[{pid}] mark warning: {e!r}")

    # clear the PENDING tags on latest.json so the book renders as HOLDING (live marks attach on read)
    latest_path = registry.data_dir(pid) / "latest.json"
    try:
        latest = json.loads(latest_path.read_text())
        for p in latest.get("positions", []):
            if p.get("pending") or p.get("status") == "pending_open":
                fp = fill_prices.get((p.get("ticker") or "").upper())
                if fp:
                    p["entry_price"] = round(fp, 4)
                p.pop("pending", None)
                p.pop("status", None)
                p.pop("est_price", None)
                p.pop("fill_after", None)
        latest["pending_orders"] = []
        latest["market_status"] = "open"
        latest_path.write_text(json.dumps(latest, indent=2, default=str, ensure_ascii=False))
        log(f"[{pid}] cleared PENDING tags on {len(latest.get('positions', []))} position(s).")
    except Exception as e:  # noqa: BLE001
        log(f"[{pid}] latest.json patch warning: {e!r}")

    remaining = len(paper_account.load_pending(pid))
    log(f"[{pid}] done. pending remaining = {remaining}.")
    return {"filled": len(filled), "marked_nav": marked_nav, "remaining": remaining}


def main(pid: str) -> int:
    res = settle(pid)
    if res.get("note") == "no pending":
        print(f"[{pid or registry.DEFAULT_ID}] no pending orders — nothing to settle.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else registry.DEFAULT_ID))
