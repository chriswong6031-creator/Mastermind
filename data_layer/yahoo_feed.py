"""Yahoo (yfinance) live price feed — fresh closes for the Hong-Kong book.

Tushare's A-share ``daily`` endpoint is bulk + high-limit (see ``data_layer.tushare_feed``), but its
``hk_daily`` endpoint is throttled to ~1 request/hour on the standard token tier — far too tight to
mark a multi-name HK book. Yahoo Finance covers Hong Kong (``*.HK``), needs no token, and serves a
whole basket in ONE ``yf.download`` call, so the HK book marks against it instead.

Yahoo's symbol for a Hong-Kong name is identical to the book's (``0700.HK``) — no code mapping — and
the close is in the native quote currency (HKD); ``paper_account`` converts to the book's base
currency via ``portfolio.fx``. We cache the latest close per symbol for the current calendar day, so
``warm()`` once per mark fetches the whole book in a single request and per-name lookups are free.

Pure-ish + degrade-never-raise: any network/parse miss leaves the cache untouched and returns None,
so ``paper_account`` falls back to the vendored snapshot and pricing never breaks.
"""
from __future__ import annotations

import logging
from datetime import date

log = logging.getLogger(__name__)

_CACHE: dict[str, float] = {}      # SYMBOL -> latest close in its local quote currency (per process)
_FETCHED_DAY: str | None = None    # the calendar day _CACHE was populated for (cleared when it rolls)


def _today() -> str:
    try:
        return date.today().isoformat()
    except Exception:
        return ""


def _reset_if_stale() -> None:
    """Drop the cache when the calendar day rolls so a long-lived server re-fetches each day."""
    global _FETCHED_DAY, _CACHE
    d = _today()
    if _FETCHED_DAY != d:
        _CACHE = {}
        _FETCHED_DAY = d


def warm(tickers) -> None:
    """Fetch recent closes for `tickers` in ONE batched yfinance call and cache the latest per
    symbol. Best-effort: a missing yfinance / network / parse failure leaves the cache as-is, and
    callers degrade to the vendored snapshot. Symbols already cached today are skipped."""
    _reset_if_stale()
    want = sorted({(t or "").upper().strip() for t in (tickers or [])} - {""} - set(_CACHE))
    if not want:
        return
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
    try:
        if hasattr(close, "columns"):             # multi-symbol → column per ticker
            for sym in close.columns:
                s = close[sym].dropna()
                if len(s):
                    _CACHE[str(sym).upper().strip()] = float(s.iloc[-1])
        else:                                     # single ticker → a bare Series
            s = close.dropna()
            if len(s) and len(want) == 1:
                _CACHE[want[0]] = float(s.iloc[-1])
    except Exception as e:  # noqa: BLE001
        log.debug("yahoo_feed parse failed (%s)", e)


def price_local(ticker: str, asof: str | None = None) -> float | None:
    """The latest Yahoo close for `ticker` in its LOCAL quote currency (HKD for ``*.HK``), or None
    when unavailable. Lazily warms the cache for this one symbol if the book hasn't been primed via
    ``warm()`` yet this day — but a single up-front ``warm(all_tickers)`` keeps it to one request."""
    t = (ticker or "").upper().strip()
    if not t:
        return None
    _reset_if_stale()
    if t not in _CACHE:
        warm([t])
    return _CACHE.get(t)


_HEALTH_PROBE = "0700.HK"          # Tencent — about as liquid as Hong Kong gets; if Yahoo can't
                                   # price IT, the live feed is genuinely down, not a per-name gap.


def feed_healthy(probe: str | None = None) -> bool | None:
    """Is the live Yahoo HK feed actually serving closes? Tri-state, mirroring
    ``tushare_feed.feed_healthy`` so the HK book gates identically to the China book:
      * ``True``  — a canonical liquid HK name (Tencent ``0700.HK``) prices through the live path.
      * ``False`` — yfinance is importable but the probe won't price: a live OUTAGE. A held name
                    would still mark off the stale snapshot while a fresh candidate returns
                    ``priceable=false`` — the same asymmetric map the China leg guards against.
      * ``None``  — yfinance isn't importable; the live feed isn't deployed (tests stub it off) and
                    the book runs on the vendored snapshot by design. NOT an outage.

    Yahoo needs no token (unlike Tushare), so importability stands in for "the live feed is
    deployed". Reuses ``warm()``'s per-day cache — the probe symbol is fetched at most once."""
    try:
        import yfinance  # noqa: F401
    except Exception:
        return None
    return price_local(probe or _HEALTH_PROBE) is not None


def clear_cache() -> None:
    """Drop the per-process price memo (tests / a forced refresh)."""
    global _CACHE, _FETCHED_DAY
    _CACHE = {}
    _FETCHED_DAY = None
