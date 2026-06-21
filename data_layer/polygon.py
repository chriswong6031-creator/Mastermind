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


def daily_closes(ticker: str, start: str, end: str) -> dict:
    """Adjusted daily closes for [start, end] (ISO dates) as {YYYY-MM-DD: close}. Best-effort
    (returns {} on any failure); disk-cached under data/cache/aggs since historical closes are
    immutable. Used for deterministic outcome labeling — full coverage for US single names that
    the macro yahoo store doesn't track."""
    import json
    import os
    from datetime import datetime, timezone
    from pathlib import Path

    t = (ticker or "").upper().strip()
    if not t or not start or not end:
        return {}
    cdir = Path(__file__).resolve().parent.parent / "data" / "cache" / "aggs"
    cf = cdir / f"{t}_{start}_{end}.json"
    try:
        if cf.exists():
            return json.loads(cf.read_text())
    except Exception:  # noqa: BLE001
        pass
    out: dict = {}
    try:
        import requests
        key = (os.environ.get("POLYGON_API_KEY") or os.environ.get("MASSIVE_API_KEY") or "").strip()
        if key:
            url = f"https://api.polygon.io/v2/aggs/ticker/{t}/range/1/day/{start}/{end}"
            r = requests.get(url, params={"adjusted": "true", "sort": "asc", "limit": 50000,
                                          "apiKey": key}, timeout=12)
            if r.ok:
                for row in (r.json() or {}).get("results") or []:
                    ts, c = row.get("t"), row.get("c")
                    if ts is None or c is None:
                        continue
                    d = datetime.fromtimestamp(ts / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
                    out[d] = float(c)
    except Exception:  # noqa: BLE001 — labeling degrades to 'unresolved' on failure, never fatal
        out = {}
    if out:
        try:
            cdir.mkdir(parents=True, exist_ok=True)
            cf.write_text(json.dumps(out))
        except Exception:  # noqa: BLE001
            pass
    return out


_details_cache: dict = {}


def ticker_details(ticker: str) -> Optional[dict]:
    """Best-effort company reference (name, description, market_cap, sector) from Polygon's
    /v3/reference/tickers/{ticker}. Returns None on ANY failure (unkeyed, paywalled, network,
    timeout) — callers must degrade gracefully. Cached for the process (incl. None results)."""
    import os
    t = (ticker or "").upper().strip()
    if not t:
        return None
    if t in _details_cache:
        return _details_cache[t]
    out = None
    try:
        import requests
        key = (os.environ.get("POLYGON_API_KEY") or os.environ.get("MASSIVE_API_KEY") or "").strip()
        if key:
            r = requests.get(f"https://api.polygon.io/v3/reference/tickers/{t}",
                             params={"apiKey": key}, timeout=5)
            if r.ok:
                res = (r.json() or {}).get("results") or {}
                sic = (res.get("sic_description") or "").strip()
                out = {
                    "name": res.get("name") or None,
                    "description": (res.get("description") or "").strip() or None,
                    "market_cap": res.get("market_cap"),
                    "sector": sic.title() if sic else None,
                }
    except Exception:  # noqa: BLE001 — reference lookup is optional context, never fatal
        out = None
    _details_cache[t] = out
    return out


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
