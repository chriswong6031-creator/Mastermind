"""Short-TTL in-memory response cache for read-only GET /api/* endpoints.

On a 1-core serve-only mirror the dashboard fires ~20 /api/* calls on load, and several recompute
heavy artifacts per request (e.g. /api/outcomes rebuilds labels+track-record+calibration; the
/api/portfolios tab-list re-prices every book) — so repeat/concurrent reads peg the single core and
Cloudflare 524s. This caches each GET /api/* JSON response in memory for MASTERMIND_RESP_CACHE_TTL
seconds (keyed by path+query): the first request computes, the rest are instant.

GATED by ``MASTERMIND_RESP_CACHE_TTL`` (seconds; 0 or unset = DISABLED). The canonical Mac, which
mutates state on every build, leaves it unset and is unaffected; the serve-only box sets it (its
data only changes every ~90s via the state sync, so a few seconds of staleness is harmless).

Correctness:
  * Installed BEFORE the auth gate so auth stays OUTERMOST — an unauthenticated request never reaches
    the cache, so no cached data can leak to an unauthorized caller.
  * Only GET, only ``/api/*`` (minus a live-lookup denylist), only 200 ``application/json`` responses.
  * Keyed by path+query, NOT per user — the dashboard's read data is identical for every authorized
    viewer, and the auth gate already ran upstream.
"""
from __future__ import annotations

import os
import time

_CACHE: dict[str, tuple[float, bytes, str]] = {}   # key -> (expiry_monotonic, body, media_type)
_MAX_ENTRIES = 512

# Never cache: live per-request lookups keyed off user-supplied query text, and the interactive
# Portfolio Desk (/api/pfolio/*) where a just-added position must show immediately (not after the TTL).
_DENY_PREFIXES = ("/api/self_directed/search", "/api/self_directed/quote", "/api/pfolio/",
                  "/api/account",        # per-user Supabase profile — never shared/public
                  "/api/mastermind_ai")  # W-AI admin surface — always fresh, never mirror-cached

# The origin already holds these read-only responses for ``MASTERMIND_RESP_CACHE_TTL`` seconds.
# Let the browser/edge reuse a response for five seconds too, then serve it for at most another
# five seconds while revalidating.
# This removes repeated global edge latency during one dashboard switch without changing the
# origin's freshness budget or caching any interactive/operator endpoint.
_CLIENT_CACHE_HEADERS = {
    "Cache-Control": "public, max-age=5, stale-while-revalidate=5",
}


def _ttl() -> float:
    try:
        return max(0.0, float(os.environ.get("MASTERMIND_RESP_CACHE_TTL", "0")))
    except (TypeError, ValueError):
        return 0.0


def _cacheable(path: str) -> bool:
    return path.startswith("/api/") and not any(path.startswith(p) for p in _DENY_PREFIXES)


def clear() -> None:
    """Drop the cache (tests / a forced refresh)."""
    _CACHE.clear()


def install(app) -> None:
    """Wire the response cache onto a FastAPI app. A no-op at request time when the TTL env is 0/unset,
    so it is always safe to install unconditionally."""
    from starlette.responses import Response

    @app.middleware("http")
    async def _resp_cache(request, call_next):
        ttl = _ttl()
        if ttl <= 0 or request.method != "GET" or not _cacheable(request.url.path):
            return await call_next(request)

        key = request.url.path + "?" + (request.url.query or "")
        now = time.monotonic()
        hit = _CACHE.get(key)
        if hit is not None and hit[0] > now:
            return Response(content=hit[1], status_code=200, media_type=hit[2],
                            headers={"x-mm-cache": "hit", **_CLIENT_CACHE_HEADERS})

        resp = await call_next(request)
        # Read the streamed body ONCE (the original iterator is then consumed), cache it, and hand back
        # a fresh Response carrying the same bytes. Only 200 application/json is cached.
        ctype = resp.headers.get("content-type", "")
        if resp.status_code == 200 and "application/json" in ctype:
            body = b""
            async for chunk in resp.body_iterator:
                body += chunk
            media = ctype.split(";")[0].strip() or "application/json"
            if len(_CACHE) < _MAX_ENTRIES:
                _CACHE[key] = (now + ttl, body, media)
            return Response(content=body, status_code=200, media_type=media,
                            headers={"x-mm-cache": "miss", **_CLIENT_CACHE_HEADERS})
        return resp

    return None
