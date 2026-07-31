"""Yahoo (yfinance) live price feed — fresh quotes for US, China, and Hong-Kong books.

Serves any yfinance symbol in ONE batched ``yf.download`` call, token-free:
  * US names (bare tickers + ETFs, e.g. ``SMH``, ``NVDA``) — the US books had NO live leg and
    marked off the CI/EOD-lagging vendored snapshot, so on a fast day (SMH -7%) the NAV was wrong.
    paper_account now routes US marks here too.
  * Hong-Kong names (``0700.HK``) — Tushare's ``hk_daily`` is throttled to ~1 req/hr; Yahoo isn't.
  * Mainland A-shares (``600000.SS`` / ``000001.SZ``) — the dashboard live-preview path now warms
    these too instead of freezing the China book on Terminal's published snapshot.
The quote is in the symbol's native currency (USD / HKD / CNY); ``paper_account`` converts it to
the book's base currency via ``portfolio.fx``.

Liveness: the cache carries an INTRADAY TTL (``YAHOO_FEED_TTL_SEC``, default 120s) so a long-lived
server re-fetches a moving symbol rather than freezing it at the day's first print — a book viewed
mid-selloff shows the live drawdown. ``warm(all_tickers)`` once per mark fetches the whole book in a
single request; per-name ``price_local`` lookups reuse the cache until the TTL lapses.

Pure-ish + degrade-never-raise: any network/parse miss leaves the cache untouched and returns None,
so ``paper_account`` falls back to the vendored snapshot and pricing never breaks.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from datetime import date, datetime, timezone

log = logging.getLogger(__name__)

_CACHE: dict[str, float] = {}      # SYMBOL -> latest Close quote in its local currency (per process)
_OPEN_CACHE: dict[str, float] = {} # SYMBOL -> today's session Open price (cleared daily, same TTL)
_TS: dict[str, float] = {}         # SYMBOL -> monotonic ts of its last fetch (drives the intraday TTL)
_ASOF: dict[str, str] = {}         # SYMBOL -> wall-clock UTC fetch time (display/provenance)
_FETCHED_DAY: str | None = None    # the calendar day the cache was populated for (cleared when it rolls)

# Serialise blocking fetches so concurrent callers dedup onto ONE batched download (kills the
# book-switch stampede), and track which symbols a background thread is already fetching so the hot
# request path can refresh the cache off-thread without ever blocking on yfinance.
_LOCK = threading.Lock()
_INFLIGHT: set[str] = set()


def _ttl() -> float:
    """Seconds before a cached quote is re-fetched (intraday liveness). 0 = per-day cache only.
    Tunable via env ``YAHOO_FEED_TTL_SEC`` (default 120)."""
    try:
        return max(0.0, float(os.environ.get("YAHOO_FEED_TTL_SEC", "120")))
    except (TypeError, ValueError):
        return 120.0


def _today() -> str:
    try:
        return date.today().isoformat()
    except Exception:
        return ""


def _reset_if_stale() -> None:
    """Drop the cache when the calendar day rolls so a long-lived server re-fetches each day."""
    global _FETCHED_DAY, _CACHE, _OPEN_CACHE, _TS, _ASOF
    d = _today()
    if _FETCHED_DAY != d:
        _CACHE, _OPEN_CACHE, _TS, _ASOF = {}, {}, {}, {}
        _FETCHED_DAY = d


def _fresh(sym: str, now: float, ttl: float) -> bool:
    """A cached symbol is fresh iff present AND (TTL disabled OR fetched within the TTL window)."""
    if sym not in _CACHE:
        return False
    if ttl <= 0:
        return True
    return (now - _TS.get(sym, 0.0)) <= ttl


def warm(tickers, background: bool = False) -> None:
    """Fetch quotes for `tickers` in ONE batched yfinance call and cache the latest per symbol.
    Re-fetches only symbols that are missing or past the intraday TTL — fresh symbols cost nothing.
    Best-effort: a missing yfinance / network / parse failure leaves the cache as-is, and callers
    degrade to the vendored snapshot.

    ``background=True`` (the hot request path — the dashboard) runs the fetch in a daemon thread and
    returns immediately, so a book SWITCH never blocks on a network download: it serves whatever is
    cached now (see ``price_cached``) or the caller's own instant Terminal-snapshot fallback, and the
    fresh quote lands on a subsequent lookup. Concurrent background callers dedup via ``_INFLIGHT`` —
    the three parallel switch endpoints spawn at most ONE fetch, not three (the old stampede).

    ``background=False`` (scheduler pre-warm / NAV marks) blocks under ``_LOCK`` so two concurrent
    callers serialise onto a single batched download instead of both hitting the network."""
    _reset_if_stale()
    now, ttl = time.monotonic(), _ttl()
    want = sorted({(t or "").upper().strip() for t in (tickers or [])} - {""})
    want = [s for s in want if not _fresh(s, now, ttl)]
    if not want:
        return
    if background:
        # NON-BLOCKING: if a fetch already holds the lock (a blocking pre-warm / NAV mark is mid
        # yf.download), do NOT wait — the request serves the cache / Terminal fallback and the fresh
        # quote lands on a later lookup. Only _INFLIGHT is touched under the (try-acquired) lock.
        if not _LOCK.acquire(blocking=False):
            return
        try:
            todo = [s for s in want if s not in _INFLIGHT]
            if not todo:
                return
            _INFLIGHT.update(todo)
        finally:
            _LOCK.release()
        threading.Thread(target=_bg_fetch, args=(todo,), daemon=True).start()
        return
    with _LOCK:
        # re-check freshness under the lock — a caller we queued behind may have just populated these
        todo = [s for s in want if not _fresh(s, time.monotonic(), ttl)]
        if todo:
            _fetch_and_cache(todo)


def _bg_fetch(want) -> None:
    """Daemon-thread wrapper: fetch off the request thread, then clear the in-flight guard."""
    # Hold the same lock as blocking warm() for the duration of yf.download. A live-marks request
    # arriving just after the dashboard's background warm then waits for that one batch and reuses
    # it instead of launching a duplicate download.
    with _LOCK:
        try:
            todo = [s for s in want if not _fresh(s, time.monotonic(), _ttl())]
            if todo:
                _fetch_and_cache(todo)
        finally:
            _INFLIGHT.difference_update(want)


def _fetch_and_cache(want) -> None:
    """The actual batched yfinance download + cache populate (Close for marks, Open for settle)."""
    now = time.monotonic()
    fetched_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        import yfinance as yf
        df = yf.download(want, period="7d", progress=False, auto_adjust=False, threads=False)
    except Exception as e:  # noqa: BLE001
        log.debug("yahoo_feed download failed for %s (%s)", want, e)
        return
    try:
        close = df["Close"]                       # DataFrame (multi-symbol) or Series (single)
    except Exception as e:  # noqa: BLE001
        log.debug("yahoo_feed: no Close column (%s)", e)
        return
    # Also grab the Open column for settle._open_price_usd — today's session Open is the
    # correct fill price for orders that queued overnight (avoids the ~11am partial-day mark).
    try:
        open_col = df["Open"]
    except Exception:
        open_col = None
    try:
        if hasattr(close, "columns"):             # multi-symbol → column per ticker
            for sym in close.columns:
                s = close[sym].dropna()
                if len(s):
                    k = str(sym).upper().strip()
                    _CACHE[k], _TS[k], _ASOF[k] = float(s.iloc[-1]), now, fetched_at
            # Populate _OPEN_CACHE from the last row's Open value (the current/today's open).
            if open_col is not None and hasattr(open_col, "columns"):
                for sym in open_col.columns:
                    s = open_col[sym].dropna()
                    if len(s):
                        k = str(sym).upper().strip()
                        v = float(s.iloc[-1])
                        if v > 0:
                            _OPEN_CACHE[k] = v
        else:                                     # single ticker → a bare Series
            s = close.dropna()
            if len(s) and len(want) == 1:
                _CACHE[want[0]], _TS[want[0]], _ASOF[want[0]] = (
                    float(s.iloc[-1]), now, fetched_at)
            if open_col is not None and not hasattr(open_col, "columns"):
                so = open_col.dropna()
                if len(so) and len(want) == 1:
                    v = float(so.iloc[-1])
                    if v > 0:
                        _OPEN_CACHE[want[0]] = v
    except Exception as e:  # noqa: BLE001
        log.debug("yahoo_feed parse failed (%s)", e)


def price_local(ticker: str, asof: str | None = None) -> float | None:
    """The latest Yahoo quote for `ticker` in its LOCAL currency (USD for US, HKD for ``*.HK``), or
    None when unavailable. Refreshes the symbol if it is missing or past the intraday TTL; a single
    up-front ``warm(all_tickers)`` keeps a whole book to one request."""
    t = (ticker or "").upper().strip()
    if not t:
        return None
    warm([t])                                     # no-op when the symbol is still fresh
    return _CACHE.get(t)


def price_cached(ticker: str) -> float | None:
    """Cache-ONLY lookup: the last cached quote in its LOCAL currency if still fresh, else None.
    NEVER fetches — for the hot request path (the dashboard). Pair with a prior
    ``warm(..., background=True)`` and a Terminal-snapshot fallback so a book switch never blocks."""
    _reset_if_stale()
    t = (ticker or "").upper().strip()
    if not t:
        return None
    if _fresh(t, time.monotonic(), _ttl()):
        return _CACHE.get(t)
    return None


def quote_cached(ticker: str) -> dict | None:
    """Cache-only quote plus freshness/provenance metadata; never touches the network."""
    _reset_if_stale()
    t = (ticker or "").upper().strip()
    now = time.monotonic()
    if not t or not _fresh(t, now, _ttl()):
        return None
    fetched_mono = _TS.get(t, now)
    return {
        "ticker": t,
        "price_local": _CACHE.get(t),
        "source": "yahoo_intraday",
        "as_of": _ASOF.get(t),
        "time_kind": "feed_retrieval",
        "age_seconds": max(0, round(now - fetched_mono, 1)),
        "fresh": True,
    }


def open_price_local(ticker: str) -> float | None:
    """Today's session OPEN price for `ticker` in its LOCAL currency, or None.

    Populated as a side-effect of ``warm()`` from the ``Open`` column of the same
    yfinance 7d daily download — so a prior ``warm([ticker])`` call makes this free.
    Used by ``bot.settle._open_price_usd`` to fill overnight-queued Brain orders at the
    true session open rather than the mid-session last print.  Returns None pre-open,
    when the feed is unavailable, or when yfinance is not installed.
    """
    t = (ticker or "").upper().strip()
    if not t:
        return None
    warm([t])                                     # ensure the daily bar has been fetched
    return _OPEN_CACHE.get(t) or None            # None → caller falls back to Close / snapshot


_HEALTH_PROBE = "0700.HK"          # Tencent — about as liquid as Hong Kong gets; if Yahoo can't
                                   # price IT, the live feed is genuinely down, not a per-name gap.


def feed_healthy(probe: str | None = None) -> bool | None:
    """Is the live Yahoo feed actually serving quotes? Tri-state, mirroring
    ``tushare_feed.feed_healthy`` so the HK book gates identically to the China book:
      * ``True``  — a canonical liquid name prices through the live path.
      * ``False`` — yfinance is importable but the probe won't price: a live OUTAGE.
      * ``None``  — yfinance isn't importable; the live feed isn't deployed (tests stub it off).
    Yahoo needs no token, so importability stands in for "the live feed is deployed"."""
    try:
        import yfinance  # noqa: F401
    except Exception:
        return None
    return price_local(probe or _HEALTH_PROBE) is not None


def clear_cache() -> None:
    """Drop the per-process price memo (tests / a forced refresh)."""
    global _CACHE, _OPEN_CACHE, _TS, _FETCHED_DAY
    _CACHE, _OPEN_CACHE, _TS = {}, {}, {}
    _FETCHED_DAY = None
