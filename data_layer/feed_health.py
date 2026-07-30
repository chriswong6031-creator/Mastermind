"""Live-feed health probes — is a venue's FRESH price feed actually serving this turn?

The Greater-China paper books mark a held name off the vendored per-name snapshot whenever the
live feed misses (``paper_account._live_price``). That silently masks a feed OUTAGE: the bulk
A-share feed (Tushare ``daily``) and the HK feed (Yahoo) are effectively all-or-nothing, so when
one is down, a currently-HELD name still prices off its stale snapshot while a brand-new CANDIDATE
— absent from the snapshot universe — returns ``priceable=false``. The Brain is then handed an
ASYMMETRIC, untrustworthy priceable map. On 2026-06-22 that fooled the China Brain into parking
~48% cash citing a (false) "no investable candidates" constraint (see
``data/portfolios/china/decisions.jsonl``).

``bot.china`` / ``bot.hk`` call ``status()`` BEFORE running the Brain and refuse to transact when
the book's venue feed is down — flagging/aborting the turn rather than trading on a partial map.
A US book (no venue restriction) has no fresh-feed gate: its delayed-but-WHOLE snapshot prices
every name uniformly, so there's no asymmetry to guard against.
"""
from __future__ import annotations


def status(venue: str | None, asof: str | None = None) -> dict:
    """Health of the live feed for ``venue`` (one of the book's ``registry.venues``).

    Returns ``{venue, feed, status, asof}`` where ``status`` is:
      * ``"up"``        — the venue's live feed is serving fresh closes (safe to trade).
      * ``"down"``      — the live feed is deployed but unavailable: an OUTAGE the caller must
                          refuse to trade on (the priceable map would be stale and asymmetric).
      * ``"snapshot"``  — no live feed is deployed for this venue; the book runs on the vendored
                          snapshot by design (NOT an outage — e.g. tests, or a token-less deploy),
                          or the venue has no fresh-feed gate at all (the US books).
    """
    v = (venue or "").strip()
    if v == "A-share":
        healthy, feed = _ashare(asof)
    elif v == "HK":
        healthy, feed = _yahoo(), "yahoo"
    else:
        healthy, feed = None, None             # US / unrestricted books: no fresh-feed asymmetry to gate
    # ``None`` means the live adapter is not deployed. Snapshot mode is safe
    # only when a canonical liquid name actually exists in the local snapshot
    # tree. The VPS incident on 2026-07-30 had neither yfinance nor
    # site/hkstockdata; treating that as snapshot mode let the HK Brain see
    # every holding as unpriceable and publish a false 100%-cash target.
    if healthy is True:
        st = "up"
    elif healthy is False:
        st = "down"
    elif v in {"A-share", "HK"}:
        st = "snapshot" if _snapshot_available(v) else "down"
    else:
        st = "snapshot"
    return {"venue": v, "feed": feed, "status": st, "asof": asof}


def is_down(venue: str | None, asof: str | None = None) -> bool:
    """True iff the venue's live feed is deployed but unavailable (an outage to abort on)."""
    return status(venue, asof)["status"] == "down"


def _tushare(asof: str | None):
    try:
        from data_layer import tushare_feed
        return tushare_feed.feed_healthy(asof)
    except Exception:
        return None


def _yahoo(probe: str | None = None):
    try:
        from data_layer import yahoo_feed
        return yahoo_feed.feed_healthy(probe)
    except Exception:
        return None


def _ashare(asof: str | None) -> tuple[bool | None, str]:
    """A-share live-feed waterfall.

    Yahoo carries Shanghai/Shenzhen symbols and is reachable from the VPS,
    while Tushare can be regionally unreachable from non-China hosts. Prefer
    the token-free Yahoo probe; retain Tushare as the second live source.
    """
    yahoo = _yahoo("600519.SS")
    if yahoo is True:
        return True, "yahoo"
    tushare = _tushare(asof)
    if tushare is True:
        return True, "tushare"
    if yahoo is False and tushare is False:
        return False, "yahoo+tushare"
    if tushare is False:
        return False, "tushare"
    if yahoo is False:
        return False, "yahoo"
    return None, "yahoo+tushare"


def _snapshot_available(venue: str) -> bool:
    """Whether the venue has a usable local fallback mark (NEVER raises)."""
    try:
        from data_layer import terminal_prices

        probe = "0700.HK" if venue == "HK" else "600519.SS"
        px = terminal_prices.price_local(probe)
        return bool(px and float(px) > 0)
    except Exception:
        return False
