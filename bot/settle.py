"""Market-hours discipline for the free-form Brain books — queue on close, settle at the open.

The Brain books (autonomous, etf, china, hk) each submit a COMPLETE target book once per trading
day, AFTER their market's close. Booking those as instantaneous fills at a stale / overnight price
is wrong — and re-running the build churns the book with phantom trims. Instead each book's run
calls ``execute_or_queue``: when its market is OPEN it rebalances at the live mark (real fills); when
CLOSED it QUEUES the decided target (``paper_account.save_pending_target``) and books NOTHING. The
scheduler then calls ``settle_open`` at the next open, which fills the queued target with ONE
rebalance at the live open marks. This mirrors the flagship's queue_orders/fill_pending discipline
(bot/phase2.py), generalized to a full target (sells + trims + buys) via ``paper_account.settle_target``.

**Open-price semantics (A4a fix):** the settle job runs at ~15:00 UTC (10 am ET), which means
``yahoo_feed.warm`` returns the LAST print of the still-open partial-day bar rather than the
true session open (9:30 ET), while the flagship (phase2.py) fills via ``paper_account.fill_pending``
which inherits the live mark at queue time — roughly the open.  To unify semantics, the USD-book
``_price`` helper now tries:
  1. ``polygon.day_open(ticker)`` — the snapshot ``day.o`` field, the flagship's semantic.
  2. ``yahoo_feed.open_price_local(ticker)`` — the latest daily bar's Open column.
  3. ``paper_account._current_price(ticker)`` — today's Close/last (previous behaviour).
Each fill gets a ``fill_price_source`` stamp (``polygon_open`` / ``yahoo_open`` / ``last_price``)
so mixed semantics are at least auditable when the fallback fires.

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


def _diff_trades(before: dict, after: dict, prices: dict,
                 sources: dict | None = None) -> list[dict]:
    """Compute the executed trade list from a before/after positions snapshot.

    ``sources`` is an optional ``{ticker: fill_price_source}`` map.  When provided,
    each trade record gains a ``fill_price_source`` field (``'polygon_open'``,
    ``'yahoo_open'``, or ``'last_price'``) so mixed-semantics fills are auditable."""
    trades = []
    for t in sorted(set(before) | set(after)):
        b = float((before.get(t) or {}).get("shares") or 0.0)
        a = float((after.get(t) or {}).get("shares") or 0.0)
        d = a - b
        if abs(d) < 1e-6:
            continue
        px = prices.get(t)
        row: dict = {
            "ticker": t,
            "side": "buy" if d > 0 else "sell",
            "shares": round(abs(d), 4),
            "price": round(px, 4) if px else None,
            "value": round(abs(d) * px, 2) if px else None,
        }
        if sources is not None:
            row["fill_price_source"] = sources.get(t, "last_price")
        trades.append(row)
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


def _open_price_usd(ticker: str,
                    *,
                    _polygon=None,
                    _yahoo=None,
                    _paper=None) -> tuple[float | None, str]:
    """Best-estimate USD session-OPEN price for a US-listed ticker, with source attribution.

    Priority (A4a): polygon day-open (snapshot ``day.o``) → yahoo daily-bar Open → last price
    (today's Close / vendored snapshot).  Returns ``(price_or_None, source_label)`` where
    ``source_label`` is one of ``'polygon_open'``, ``'yahoo_open'``, or ``'last_price'``.

    The ``_polygon`` / ``_yahoo`` / ``_paper`` kwargs are injection points for tests: pass
    callables with the same signatures as the real functions to avoid network calls.
    """
    # Allow tests to inject stubs; default to the real modules (lazy to avoid import cost
    # on the Asia/non-USD code paths that call _price with ccy!='USD').
    if _polygon is None:
        try:
            from data_layer import polygon as _pg
            _polygon = _pg.day_open
        except Exception:
            _polygon = lambda t: None  # noqa: E731

    if _yahoo is None:
        try:
            from data_layer import yahoo_feed as _yf
            _yahoo = _yf.open_price_local
        except Exception:
            _yahoo = lambda t: None  # noqa: E731

    if _paper is None:
        try:
            from portfolio import paper_account as _pa
            _paper = _pa._current_price
        except Exception:
            _paper = lambda t: None  # noqa: E731

    t = (ticker or "").upper().strip()
    if not t:
        return None, "last_price"

    try:
        px = _polygon(t)
        if px and float(px) > 0:
            return float(px), "polygon_open"
    except Exception:
        pass

    try:
        px = _yahoo(t)
        if px and float(px) > 0:
            return float(px), "yahoo_open"
    except Exception:
        pass

    try:
        px = _paper(t)
        if px and float(px) > 0:
            return float(px), "last_price"
    except Exception:
        pass

    return None, "last_price"


def _price_and_sources(pid: str, syms,
                       *,
                       _open_price_fn=None) -> tuple[dict[str, float], dict[str, str]]:
    """Live marks for `syms` in `pid`'s base currency plus per-ticker source labels.

    For USD books (autonomous): prefer session open price (A4a) via ``_open_price_usd``.
    For the ETF book: the dedicated live-Yahoo Close pricer (its own warm logic).
    For HKD/CNY books: FX-converted last price (unchanged — no open semantics after Asia close).

    Returns ``(prices, sources)`` where ``sources`` maps ticker → source label
    (``'polygon_open'`` | ``'yahoo_open'`` | ``'last_price'`` | ``'etf_universe'``).
    ``_open_price_fn`` is an injection point for tests (same signature as ``_open_price_usd``).
    """
    from portfolio import paper_account, registry
    prices: dict[str, float] = {}
    sources: dict[str, str] = {}

    if pid == "etf":
        # ETF book: the dedicated live-Yahoo pricer (unchanged — has its own warm logic).
        from portfolio import etf_universe
        etf_universe.warm(syms)
        for t in syms:
            px = etf_universe.price(t)
            if px and px > 0:
                prices[t] = px
                sources[t] = "etf_universe"
        return prices, sources

    ccy = registry.currency(pid)
    if ccy == "USD":
        # USD Brain books (autonomous): prefer session open so fill semantics match the flagship.
        # The source label is stamped on each executed trade for auditability.
        price_fn = _open_price_fn or _open_price_usd
        for t in syms:
            px, src = price_fn(t)
            if px and px > 0:
                prices[t] = px
                sources[t] = src
        return prices, sources

    from portfolio import fx
    for t in syms:
        base = fx.usd_to(paper_account._current_price(t), ccy)
        if base and base > 0:
            prices[t] = base
            sources[t] = "last_price"
    return prices, sources


def _price(pid: str, syms, *, _open_price_fn=None) -> dict[str, float]:
    """Prices only — thin wrapper over ``_price_and_sources`` for callers that don't need sources."""
    prices, _ = _price_and_sources(pid, syms, _open_price_fn=_open_price_fn)
    return prices


def settle_open(pid: str, asof: str | None = None,
                *,
                _open_price_fn=None) -> dict:
    """At `pid`'s market OPEN, fill the queued target (decided on a prior closed session) with one
    rebalance at the live open marks, re-mark NAV, and republish the book contract so the dashboard
    shows the filled positions. No-op (safe) if the market is closed or nothing is queued.

    For USD Brain books the fill prices are the session OPEN (polygon day.o → yahoo Open → last
    price fallback); each trade in ``executed`` carries a ``fill_price_source`` stamp so mixed
    semantics are auditable in the blotter.  ``_open_price_fn`` is an injection point for tests.
    """
    from portfolio import paper_account, position_log, registry
    asof = asof or date.today().isoformat()
    if not is_open(pid):
        return {"ok": False, "skipped": "market_closed"}
    if not paper_account.load_pending_target(pid):
        return {"ok": False, "skipped": "nothing_queued"}
    bench = registry.benchmark(pid)
    sym_set = set(_held(pid)) | {bench} | set((paper_account.load_pending_target(pid) or {}).get("target") or {})
    prices, sources = _price_and_sources(pid, sym_set, _open_price_fn=_open_price_fn)
    before = dict((paper_account._load_account(pid).get("positions") or {}))
    target = paper_account.settle_target(prices, asof, portfolio_id=pid) or {}
    after = dict((paper_account._load_account(pid).get("positions") or {}))
    # Pass sources so each trade row is stamped with fill_price_source for auditability.
    executed = _diff_trades(before, after, prices, sources=sources)
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
