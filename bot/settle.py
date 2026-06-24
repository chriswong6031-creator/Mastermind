"""Market-hours discipline for the free-form Brain books — queue on close, settle at the open.

The Brain books (autonomous, etf, china, hk) each submit a COMPLETE target book once per trading
day, AFTER their market's close. Booking those as instantaneous fills at a stale / overnight price
is wrong — and re-running the build churns the book with phantom trims. Instead each book's run
calls ``execute_or_queue``: when its market is OPEN it rebalances at the live mark (real fills); when
CLOSED it QUEUES the decided target (``paper_account.save_pending_target``) and books NOTHING. The
scheduler then calls ``settle_open`` at the next open, which fills the queued target with ONE
rebalance at the live open marks. This mirrors the flagship's queue_orders/fill_pending discipline
(bot/phase2.py), generalized to a full target (sells + trims + buys) via ``paper_account.settle_target``.

Calendars: the US books (autonomous, etf) use ``portfolio.market_calendar`` (NYSE); the Greater-China
books (china, hk) use ``portfolio.china_calendar`` (A-share session). Both expose ``is_open()``.
"""
from __future__ import annotations

from datetime import date

import bot  # noqa: F401  -> vendor/macro onto sys.path

_ASIA = {"china", "hk"}


def _calendar(pid: str):
    if pid in _ASIA:
        from portfolio import china_calendar
        return china_calendar
    from portfolio import market_calendar
    return market_calendar


def is_open(pid: str) -> bool:
    """True iff `pid`'s market is open right now. Defaults to False (queue) on any calendar error —
    fail SAFE: never book an off-hours fill because the calendar hiccuped."""
    try:
        return bool(_calendar(pid).is_open())
    except Exception:
        return False


def _diff_trades(before: dict, after: dict, prices: dict) -> list[dict]:
    trades = []
    for t in sorted(set(before) | set(after)):
        b = float((before.get(t) or {}).get("shares") or 0.0)
        a = float((after.get(t) or {}).get("shares") or 0.0)
        d = a - b
        if abs(d) < 1e-6:
            continue
        px = prices.get(t)
        trades.append({"ticker": t, "side": "buy" if d > 0 else "sell",
                       "shares": round(abs(d), 4), "price": round(px, 4) if px else None,
                       "value": round(abs(d) * px, 2) if px else None})
    return trades


def execute_or_queue(pid: str, target: dict[str, float], prices: dict[str, float], asof: str,
                     *, market_open: bool | None = None) -> dict:
    """Market-hours-aware execution for a Brain book. OPEN → settle any target queued on a prior
    closed session, then rebalance to `target` at the live marks (real fills). CLOSED → persist
    `target` to settle at the next open; book NO fills (no off-hours trading, no churn on re-run).
    Returns {executed: [...], queued: bool, market_open: bool, error?}. `prices` is the same marks
    the book uses for NAV, so fills and NAV stay consistent."""
    from portfolio import paper_account
    if market_open is None:
        market_open = is_open(pid)
    out: dict = {"executed": [], "queued": False, "market_open": bool(market_open)}
    before = dict((paper_account._load_account(pid).get("positions") or {}))
    if market_open:
        try:
            paper_account.settle_target(prices, asof, portfolio_id=pid)   # settle any prior queue first
            paper_account.rebalance(target, prices, asof, portfolio_id=pid)
        except Exception as e:                                            # noqa: BLE001
            out["error"] = repr(e)[:200]
        after = dict((paper_account._load_account(pid).get("positions") or {}))
        out["executed"] = _diff_trades(before, after, prices)
    else:
        try:
            paper_account.save_pending_target(target, asof, portfolio_id=pid)
            out["queued"] = True
        except Exception as e:                                            # noqa: BLE001
            out["error"] = repr(e)[:200]
    return out


# ---------------------------------------------------------------------------
# the open-session settle (scheduler-driven)
# ---------------------------------------------------------------------------

def _held(pid: str) -> set[str]:
    from portfolio import paper_account
    return set((paper_account._load_account(pid).get("positions") or {}).keys())


def _price(pid: str, syms) -> dict[str, float]:
    """Live marks for `syms` in `pid`'s base currency, by book: the ETF book uses the live-Yahoo ETF
    pricer; other USD books the shared store; HKD/CNY books convert the store mark via FX."""
    from portfolio import paper_account, registry
    prices: dict[str, float] = {}
    if pid == "etf":
        from portfolio import etf_universe
        etf_universe.warm(syms)
        for t in syms:
            px = etf_universe.price(t)
            if px and px > 0:
                prices[t] = px
        return prices
    ccy = registry.currency(pid)
    if ccy == "USD":
        for t in syms:
            px = paper_account._current_price(t)
            if px and px > 0:
                prices[t] = px
        return prices
    from portfolio import fx
    for t in syms:
        base = fx.usd_to(paper_account._current_price(t), ccy)
        if base and base > 0:
            prices[t] = base
    return prices


def settle_open(pid: str, asof: str | None = None) -> dict:
    """At `pid`'s market OPEN, fill the queued target (decided on a prior closed session) with one
    rebalance at the live open marks, re-mark NAV, and republish the book contract so the dashboard
    shows the filled positions. No-op (safe) if the market is closed or nothing is queued."""
    from portfolio import paper_account, position_log, registry
    asof = asof or date.today().isoformat()
    if not is_open(pid):
        return {"ok": False, "skipped": "market_closed"}
    if not paper_account.load_pending_target(pid):
        return {"ok": False, "skipped": "nothing_queued"}
    bench = registry.benchmark(pid)
    prices = _price(pid, set(_held(pid)) | {bench} | set((paper_account.load_pending_target(pid) or {}).get("target") or {}))
    before = dict((paper_account._load_account(pid).get("positions") or {}))
    target = paper_account.settle_target(prices, asof, portfolio_id=pid) or {}
    after = dict((paper_account._load_account(pid).get("positions") or {}))
    executed = _diff_trades(before, after, prices)
    try:
        position_log.update([{"ticker": t, "sleeve": "brain", "weight": w, "entry_price": prices.get(t)}
                             for t, w in target.items() if t in prices], asof, portfolio_id=pid)
    except Exception:
        pass
    try:
        paper_account.mark(prices, asof, portfolio_id=pid)
    except Exception:
        pass
    _republish(pid, asof)
    return {"ok": True, "executed": executed, "settled_to": sorted(target)}


def _republish(pid: str, asof: str) -> None:
    """Re-emit the book's published contract after a settle (so the dashboard reflects the fills),
    via the book module's republish(). Best-effort; never raises."""
    try:
        import importlib
        mod = importlib.import_module(f"bot.{pid}")
        if hasattr(mod, "republish"):
            mod.republish(asof)
    except Exception:
        pass


def settle_us(asof: str | None = None) -> dict:
    """Settle the queued targets for the US Brain books at the US open (scheduler job)."""
    return {pid: settle_open(pid, asof) for pid in ("autonomous", "etf")}


def settle_asia(asof: str | None = None) -> dict:
    """Settle the queued targets for the Greater-China Brain books at the A-share open (scheduler job)."""
    return {pid: settle_open(pid, asof) for pid in ("china", "hk")}
