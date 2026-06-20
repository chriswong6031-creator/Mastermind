"""Live (15-min delayed) equity quotes — for the Brain and the dashboard.

Thin wrapper over the vendored macro Polygon client
(`collectors.polygon_options.PolygonOptions`), which hits api.polygon.io (or a
Polygon-compatible clone) for the stock snapshot. The key resolves from
POLYGON_API_KEY, falling back to MASSIVE_API_KEY (the macro repo's existing
Polygon-compatible secret). Degrades to None per-symbol when offline / unkeyed —
never raises.

Quotes are cached in-process for `_TTL` seconds so one page load (Positions +
Trade History both want the held names) doesn't re-hit the API per panel.

Delayed data is fine here: this is a paper book, marks are EOD-grade.
"""
from __future__ import annotations

import time
from typing import Iterable, Optional

import bot  # noqa: F401  -> puts vendor/macro on sys.path

_TTL = 60.0  # seconds; a minute of staleness on a delayed feed is immaterial
_cache: dict[str, tuple[float, Optional[float]]] = {}
_client = None
_client_tried = False


def _get_client():
    """Lazy singleton macro Polygon client, or None if unavailable/unkeyed."""
    global _client, _client_tried
    if _client_tried:
        return _client
    _client_tried = True
    try:
        from collectors.polygon_options import PolygonOptions
        c = PolygonOptions()
        _client = c if c.enabled() else None
    except Exception:
        _client = None
    return _client


def available() -> bool:
    """True if a keyed Polygon client is reachable."""
    return _get_client() is not None


def quote(ticker: str) -> Optional[float]:
    """Live (delayed) last/close for one ticker, or None. Cached `_TTL` seconds."""
    t = (ticker or "").upper().strip()
    if not t:
        return None
    now = time.time()
    hit = _cache.get(t)
    if hit and (now - hit[0]) < _TTL:
        return hit[1]
    c = _get_client()
    px: Optional[float] = None
    if c is not None:
        try:
            px = c.spot(t)
        except Exception:
            px = None
    _cache[t] = (now, px)
    return px


def quotes(tickers: Iterable[str]) -> dict[str, Optional[float]]:
    """Batch quotes -> {TICKER: price|None}. De-dupes; uses the per-symbol cache."""
    out: dict[str, Optional[float]] = {}
    for t in tickers or []:
        tk = (t or "").upper().strip()
        if tk and tk not in out:
            out[tk] = quote(tk)
    return out
