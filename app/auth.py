"""App-level authorization for the Mastermind dashboard.

Mastermind manages paper portfolios and an LLM brain that spends tokens, so the
mutating/LLM-triggering routes must NOT be exposed unprotected. This installs a
middleware that enforces THREE things — and nothing more:

  1. the serve-only POST guard (blocks operator mutations on the read mirror),
  2. the bearer-token OPERATOR tier (mutating/LLM POSTs require a bearer token),
  3. rate limiting on operator paths.

The browser PASSWORD-COOKIE LOGIN FLOW has been REMOVED (page-only scope):
there is no ``/login`` page, no ``/logout`` route, and no session cookie. Browsing
the dashboard (all GETs + read APIs + the SSE stream) requires NO login anywhere,
on both localhost and the internet-facing VPS mirror. ``MASTERMIND_PASSWORD``,
``MASTERMIND_SESSION_DAYS``, and ``MASTERMIND_COOKIE_SECURE`` are now NO-OPS
(kept in the flag registry for backwards compatibility; the middleware ignores
them). ``MASTERMIND_REQUIRE_AUTH`` is likewise a no-op — with no password gate
there is nothing to refuse-to-start over.

OPT-IN by environment, so local dev / the existing workflow is unchanged:

  - ``MASTERMIND_AUTH_TOKEN``   bearer token for the OPERATOR tier + programmatic
        clients (``Authorization: Bearer <token>``). Unset -> operator paths pass
        (dev ergonomics). This is the ONLY credential the app checks.
  - ``MASTERMIND_SERVE_ONLY``   =1 -> serve-only mirror mode (see below).

These read from ``os.environ`` at request time; ``import bot`` (via app.deps) loads
``.env`` first, so a value in ``.env`` is picked up automatically.

OPERATOR ROUTE TIER (MW6 docket #10 / ruling R4 second half)
-------------------------------------------------------------
``_OPERATOR_PATHS`` is the set of mutating/LLM-triggering routes that require the
BEARER token (MASTERMIND_AUTH_TOKEN). This prevents an anonymous client on the
open dashboard from spending LLM tokens or triggering book runs. Source:
data/census/CENSUS.md LLM-triggering tags.

Routes included:
  POST /daily              — gated flagship book (LLM)
  POST /reason             — reasoning pass (LLM)
  POST /research           — research session (LLM)
  POST /chat               — live advisor chat (LLM)
  POST /api/autonomous/run — autonomous Brain book (LLM)
  POST /api/heavyweight/run— heavyweight Brain book (LLM)
  POST /api/china/run      — China Brain book (LLM)
  POST /api/hk/run         — HK Brain book (LLM)
  POST /api/etf/run        — ETF Brain book (LLM)
  POST /api/self_directed/order   — order placement (non-LLM mutating POST)
  POST /api/self_directed/thesis  — thesis write (non-LLM mutating POST)
  POST /api/self_directed/cancel  — order cancel (non-LLM mutating POST)

Read-only dashboard GETs are open (no login).
When no bearer token is configured (dev), operator paths still pass (dev ergonomics).

RATE LIMITS (MW6)
-----------------
In-memory token-bucket per route group, keyed per group:
  - llm     : LLM-triggering operator paths — 8 fires/hour shared + 2/min burst
  - operator: non-LLM mutating operator POSTs — 30/hour
Stdlib only (no slowapi).  429 with Retry-After.
Advisory run-event emitted on each 429.

SERVE-ONLY MODE (MW6)
---------------------
``MASTERMIND_SERVE_ONLY=1`` converts the app to a read-only mirror:
  (a) scheduler is NEVER started (guarded in app.main startup)
  (b) first-run daemon threads are skipped
  (c) ALL operator paths return 403 JSON naming the mirror
  (d) /api/pfolio/* (personal Supabase portfolio CRUD) is BLOCKED entirely —
      ALL methods (GET/POST/PATCH/PUT/DELETE) return 403. See PFOLIO below.
  (e) /health gains "serve_only": true

PERSONAL PFOLIO PANEL (PRD-R8, revised after the browser-login removal)
-----------------------------------------------------------------------
``/api/pfolio/*`` is the personal Supabase portfolio panel (holdings CRUD via a
service-role JWT). It used to be gated solely by the browser PASSWORD COOKIE.
That login flow is now GONE, so the cookie that used to protect it no longer
exists. The new model:
  - CANONICAL localhost instance  -> OPEN. The browser panel has no cookie/bearer
    to send, and the box is localhost-only, so pfolio passes the auth gate
    unauthenticated (it is NOT in _OPERATOR_PATHS; no bearer is required).
  - SERVE-ONLY mirror (public VPS) -> BLOCKED (all methods, 403). The public box
    is a read mirror; the personal panel must not be reachable there now that
    the cookie that used to gate it is gone (it would otherwise expose the
    user's holdings and Supabase writes unauthenticated).

NOTE: two stale worktrees from a prior session contain another agent's
MASTERMIND_SERVE_ONLY WIP — do NOT merge or read those branches. This
implementation is fresh with the same flag name so a later reconcile is trivial.
"""
from __future__ import annotations

import hmac
import logging
import os
import time

from fastapi import Request   # module-level so FastAPI resolves the `request: Request`
                             # annotation under `from __future__ import annotations`

log = logging.getLogger("mastermind.auth")

#: Paths never gated by any check. The browser login flow is gone, so the only
#: always-open route left is the uptime/health probe. (Kept as a named set so
#: scripts/system_census.py can introspect it via getattr.)
_OPEN_PATHS = {"/health"}

# ---------------------------------------------------------------------------
# operator route tier — mutating/LLM-triggering POST paths that require the
# BEARER token (cookie is NOT sufficient).  Source: data/census/CENSUS.md.
# ---------------------------------------------------------------------------

#: LLM-triggering operator POSTs — token bucket: 8/hour shared, 2/min burst.
_LLM_OPERATOR_PATHS: frozenset[str] = frozenset({
    "/daily",
    "/reason",
    "/research",
    "/chat",
    "/api/autonomous/run",
    "/api/heavyweight/run",
    "/api/china/run",
    "/api/hk/run",
    "/api/etf/run",
})

#: Non-LLM mutating operator POSTs — token bucket: 30/hour.
_NON_LLM_OPERATOR_PATHS: frozenset[str] = frozenset({
    "/api/self_directed/order",
    "/api/self_directed/thesis",
    "/api/self_directed/cancel",
    # Mastermind AI admin section (W-AI) — settings/directives/manual cycle
    "/api/mastermind_ai/settings",
    "/api/mastermind_ai/directive",
    "/api/mastermind_ai/act_on_nudges",
    "/api/mastermind_ai/run",
})

#: Union — all operator-tier paths.
_OPERATOR_PATHS: frozenset[str] = _LLM_OPERATOR_PATHS | _NON_LLM_OPERATOR_PATHS

# ---------------------------------------------------------------------------
# PRD-R8 (revised): personal Supabase portfolio CRUD.
# The browser PASSWORD-COOKIE login that used to be the SOLE gate on these
# endpoints has been REMOVED, so pfolio is no longer cookie-protected. The new
# model splits by instance:
#   - CANONICAL localhost instance  -> OPEN (the browser panel has no cookie or
#     bearer to send; these paths are NOT in _OPERATOR_PATHS, so no bearer is
#     required and they pass the auth gate).
#   - SERVE-ONLY mirror (public VPS) -> BLOCKED entirely (all methods, 403);
#     the personal panel must not be reachable on the public read mirror now
#     that the cookie that used to gate it is gone. Enforced in `_gate` below.
# ---------------------------------------------------------------------------
_PFOLIO_PATH_PREFIX = "/api/pfolio/"

# ---------------------------------------------------------------------------
# serve-only mode
# ---------------------------------------------------------------------------

def serve_only() -> bool:
    """True when MASTERMIND_SERVE_ONLY=1 — read-only mirror mode."""
    return os.environ.get("MASTERMIND_SERVE_ONLY", "").strip() in {"1", "true", "yes"}


# ---------------------------------------------------------------------------
# in-memory token buckets for operator-path rate limiting (stdlib only)
# ---------------------------------------------------------------------------

class _TokenBucket:
    """Simple token-bucket rate limiter (thread-safe via the GIL for CPython).

    capacity   : max tokens (burst ceiling)
    rate       : tokens refilled per second
    """
    __slots__ = ("capacity", "rate", "tokens", "last_refill")

    def __init__(self, capacity: float, rate: float) -> None:
        self.capacity = capacity
        self.rate = rate
        self.tokens = float(capacity)
        self.last_refill = time.monotonic()

    def consume(self) -> float | None:
        """Consume one token.  Returns None on success, or seconds-to-wait on rejection."""
        now = time.monotonic()
        elapsed = now - self.last_refill
        self.last_refill = now
        self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)
        if self.tokens >= 1.0:
            self.tokens -= 1.0
            return None
        # fractional tokens remaining — wait time until refilled to 1.0
        return (1.0 - self.tokens) / self.rate


# LLM bucket: burst capacity 2, sustained 8/hour (refill 1 token per 450s —
# a single token bucket cannot independently express both 8/hr and 2/min;
# this is the stricter composition). Module-global state: tests MUST reset
# via reset_rate_buckets() (autouse fixture) or ordering becomes load-bearing.
_llm_bucket = _TokenBucket(capacity=2.0, rate=8.0 / 3600.0)
# Non-LLM operator bucket: 30/hour
_operator_bucket = _TokenBucket(capacity=30.0, rate=30.0 / 3600.0)


def reset_rate_buckets() -> None:
    """Restore both operator rate buckets to full capacity (TEST hook —
    module-global bucket state otherwise leaks across tests/orderings)."""
    _llm_bucket.tokens = _llm_bucket.capacity
    _operator_bucket.tokens = _operator_bucket.capacity


# ---------------------------------------------------------------- config ----

def _bearer_token() -> str | None:
    return os.environ.get("MASTERMIND_AUTH_TOKEN") or None


# ------------------------------------------------------------- authorize ----

def is_operator_authorized(request) -> bool:
    """True iff the request carries a valid BEARER token.

    Operator-tier paths (mutating / LLM-triggering POSTs) require the bearer
    token. An anonymous client on the open dashboard must NOT be able to fire
    LLM-triggering or mutating routes. When no token is configured (dev), this
    returns True so the operator paths remain accessible in development.
    """
    tok = _bearer_token()
    if not tok:
        # No token configured — dev ergonomics: operator paths pass, same as before.
        return True
    auth = request.headers.get("authorization", "")
    return auth.lower().startswith("bearer ") and hmac.compare_digest(auth[7:].strip(), tok)


# --------------------------------------------------------------- install ----

def install(app) -> None:
    """Wire the operator-tier / serve-only / rate-limit middleware onto a FastAPI app.

    Safe to call unconditionally. There is NO browser login: read-only browsing
    (every GET + the SSE stream) is always open. The middleware enforces only:
      1. the serve-only POST guard (MASTERMIND_SERVE_ONLY),
      2. the bearer-token OPERATOR tier (MASTERMIND_AUTH_TOKEN),
      3. rate limiting on operator paths.
    """
    from fastapi.responses import JSONResponse

    def _emit_rate_limit_event(path: str) -> None:
        """Emit an ADVISORY run-event on each 429. Never raises."""
        try:
            from control_plane import run_events
            run_events.append({
                "kind": "guardrail",
                "job": "rate_limit",
                "book": "",
                "step": "operator_rate_limit",
                "status": "warn",
                "severity": "ADVISORY_ONLY",
                "actor": "system",
                "extra": {"path": path},
            })
        except Exception:  # noqa: BLE001
            pass

    @app.middleware("http")
    async def _gate(request: Request, call_next):
        path = request.url.path
        method = request.method

        if method == "OPTIONS" or path in _OPEN_PATHS:
            return await call_next(request)

        # --- serve-only mode: block the personal pfolio panel ENTIRELY ---
        # PRD-R8 (revised): the browser password-cookie login that used to gate
        # /api/pfolio/* is gone, so these endpoints are no longer cookie-protected.
        # On the public read mirror they must NOT be reachable at all (they expose
        # the user's holdings on GET and Supabase writes on POST/PATCH/PUT/DELETE),
        # so block every method here. On the canonical localhost instance this
        # branch is inert and pfolio stays open for the browser panel.
        if serve_only() and path.startswith(_PFOLIO_PATH_PREFIX):
            return JSONResponse(
                {"error": "serve_only",
                 "detail": "portfolio panel disabled on the read mirror"},
                status_code=403,
            )

        # --- serve-only mode: block all operator mutations ---
        if serve_only() and method in {"POST", "PATCH", "PUT", "DELETE"} and path in _OPERATOR_PATHS:
            return JSONResponse(
                {"error": "serve_only", "detail": (
                    "This instance is running in serve-only (read-only mirror) mode. "
                    "Operator mutations are disabled. Use the primary instance."
                )},
                status_code=403,
            )

        # --- read access is OPEN: no login. Every GET, the SSE stream and the
        #     read APIs fall straight through. Only the operator tier below gates. ---

        # --- operator-tier gate: bearer token required for mutating/LLM POSTs ---
        if method == "POST" and path in _OPERATOR_PATHS:
            if not is_operator_authorized(request):
                return JSONResponse(
                    {"error": "operator_bearer_required",
                     "detail": "Operator paths require a bearer token."},
                    status_code=401,
                )

            # --- rate limiting on operator paths ---
            if path in _LLM_OPERATOR_PATHS:
                wait = _llm_bucket.consume()
            else:
                wait = _operator_bucket.consume()

            if wait is not None:
                retry_after = max(1, int(wait) + 1)
                _emit_rate_limit_event(path)
                return JSONResponse(
                    {"error": "rate_limited",
                     "detail": f"Rate limit exceeded. Retry after {retry_after}s.",
                     "retry_after": retry_after},
                    status_code=429,
                    headers={"Retry-After": str(retry_after)},
                )

        return await call_next(request)
