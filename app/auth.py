"""App-level authentication for the Mastermind dashboard.

Mastermind manages paper portfolios and an LLM brain that spends tokens, so it
must NOT be exposed unprotected. This installs a single-password gate over the
WHOLE app (the static UI + every ``/api`` route + the SSE chat stream) backed by
a signed session cookie.

OPT-IN by environment, so local dev / the existing workflow is unchanged:

  - ``MASTERMIND_PASSWORD``     set  -> auth ON.  unset -> auth OFF (logs a warning).
  - ``MASTERMIND_AUTH_TOKEN``   optional bearer token for programmatic clients
        (``Authorization: Bearer <token>``) — e.g. the snapshot push / uptime ping
        that isn't a browser. Independent of the password.
  - ``MASTERMIND_SESSION_DAYS`` cookie lifetime in days (default 30).
  - ``MASTERMIND_COOKIE_SECURE````=1`` -> always mark the cookie ``Secure`` (default:
        Secure only when the request arrived over https — so http://localhost works).
  - ``MASTERMIND_SERVE_ONLY``   =1 -> serve-only mirror mode (see below).

These read from ``os.environ`` at request time; ``import bot`` (via app.deps) loads
``.env`` first, so a value in ``.env`` is picked up automatically.

Cookie = ``b64url(payload).hmac_sha256(payload, key)`` where the key is DERIVED from
the password, so rotating the password invalidates every outstanding session.

OPERATOR ROUTE TIER (MW6 docket #10 / ruling R4 second half)
-------------------------------------------------------------
``_OPERATOR_PATHS`` is the set of mutating/LLM-triggering routes that require the
BEARER token (MASTERMIND_AUTH_TOKEN) — a browser session cookie is NOT sufficient.
This prevents a compromised read session from spending LLM tokens or triggering
book runs.  Source: data/census/CENSUS.md LLM-triggering tags.

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

Read-only dashboard GETs keep the existing cookie-or-token gate.
When auth is disabled entirely (dev), operator paths still pass (dev ergonomics).

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
  (d) /health gains "serve_only": true

NOTE: two stale worktrees from a prior session contain another agent's
MASTERMIND_SERVE_ONLY WIP — do NOT merge or read those branches. This
implementation is fresh with the same flag name so a later reconcile is trivial.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import logging
import os
import time
from urllib.parse import parse_qs

from fastapi import Request   # module-level so FastAPI resolves the `request: Request`
                             # annotation under `from __future__ import annotations`

log = logging.getLogger("mastermind.auth")

_COOKIE = "mm_session"
_OPEN_PATHS = {"/login", "/logout", "/health"}

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
})

#: Union — all operator-tier paths.
_OPERATOR_PATHS: frozenset[str] = _LLM_OPERATOR_PATHS | _NON_LLM_OPERATOR_PATHS

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

def _password() -> str | None:
    return os.environ.get("MASTERMIND_PASSWORD") or None


def _bearer_token() -> str | None:
    return os.environ.get("MASTERMIND_AUTH_TOKEN") or None


def enabled() -> bool:
    return _password() is not None


def _session_days() -> int:
    try:
        return max(1, int(os.environ.get("MASTERMIND_SESSION_DAYS", "30")))
    except ValueError:
        return 30


# --------------------------------------------------------- signed cookie ----

def _key(password: str) -> bytes:
    return hashlib.sha256(("mastermind-session-v1:" + password).encode()).digest()


def _b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _b64u_dec(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def make_cookie(password: str, *, ttl_days: int | None = None, now: float | None = None) -> str:
    now = time.time() if now is None else now
    ttl = (ttl_days if ttl_days is not None else _session_days()) * 86400
    payload = _b64u(json.dumps({"exp": int(now + ttl)}).encode())
    sig = hmac.new(_key(password), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"


def verify_cookie(token: str | None, password: str, *, now: float | None = None) -> bool:
    if not token or "." not in token:
        return False
    payload, _, sig = token.partition(".")
    expected = hmac.new(_key(password), payload.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return False
    try:
        data = json.loads(_b64u_dec(payload))
    except Exception:  # noqa: BLE001
        return False
    now = time.time() if now is None else now
    return float(data.get("exp", 0)) > now


# ------------------------------------------------------------- authorize ----

def check_password(supplied: str) -> bool:
    pw = _password()
    return bool(pw) and hmac.compare_digest(supplied or "", pw)


def is_authorized(request) -> bool:
    """True iff the request carries a valid bearer token OR a valid session cookie."""
    tok = _bearer_token()
    if tok:
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("bearer ") and hmac.compare_digest(auth[7:].strip(), tok):
            return True
    pw = _password()
    return bool(pw) and verify_cookie(request.cookies.get(_COOKIE), pw)


def is_operator_authorized(request) -> bool:
    """True iff the request carries a valid BEARER token.

    Operator-tier paths require the bearer token regardless of session cookie.
    A compromised read session (cookie) must NOT be able to fire LLM-triggering
    or mutating routes.  When auth is disabled (dev), this returns True so the
    operator paths remain accessible in development.
    """
    if not enabled():
        return True
    tok = _bearer_token()
    if not tok:
        # No token configured — treat as disabled for the operator check (same as
        # auth disabled: dev ergonomics unchanged when MASTERMIND_AUTH_TOKEN is absent).
        return True
    auth = request.headers.get("authorization", "")
    return auth.lower().startswith("bearer ") and hmac.compare_digest(auth[7:].strip(), tok)


def safe_next(raw: str | None) -> str:
    """Only allow a same-site relative path as the post-login redirect (no open redirect)."""
    if raw and raw.startswith("/") and not raw.startswith("//"):
        return raw
    return "/"


# --------------------------------------------------------------- install ----

def _login_html(nxt: str, error: bool = False) -> str:
    err = '<p class="err">Incorrect password.</p>' if error else ""
    nxt = html.escape(nxt, quote=True)
    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Mastermind — sign in</title><style>
body{{margin:0;height:100vh;display:flex;align-items:center;justify-content:center;
background:#0b0e14;color:#e6e6e6;font:15px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}}
form{{background:#141925;padding:28px 30px;border:1px solid #232a39;border-radius:12px;width:300px;
box-shadow:0 10px 40px rgba(0,0,0,.4)}}
h1{{font-size:17px;margin:0 0 4px}} .sub{{color:#8b97ad;font-size:12px;margin:0 0 18px}}
input{{width:100%;box-sizing:border-box;padding:10px 12px;margin:0 0 12px;border-radius:8px;
border:1px solid #2c3446;background:#0b0e14;color:#e6e6e6;font-size:14px}}
button{{width:100%;padding:10px;border:0;border-radius:8px;background:#3b82f6;color:#fff;
font-weight:600;font-size:14px;cursor:pointer}} button:hover{{background:#2f6fe0}}
.err{{color:#fca5a5;font-size:12px;margin:0 0 12px}}
</style></head><body>
<form method="post" action="/login">
<h1>Mastermind</h1><p class="sub">Private — sign in to continue.</p>
{err}
<input type="password" name="password" placeholder="Password" autofocus autocomplete="current-password">
<input type="hidden" name="next" value="{nxt}">
<button type="submit">Sign in</button>
</form></body></html>"""


def _require_auth() -> bool:
    """True when the operator has demanded auth-or-refuse at startup."""
    v = os.environ.get("MASTERMIND_REQUIRE_AUTH", "").strip()
    return v.lower() in {"1", "true", "yes"}


def install(app) -> None:
    """Wire the auth gate + the /login, /logout routes onto a FastAPI app.

    Safe to call unconditionally: when no password is configured the middleware
    short-circuits to a pass-through (and logs a one-time warning).

    Raises RuntimeError at startup when MASTERMIND_REQUIRE_AUTH=1 but no
    password is set — production must not boot unauthenticated."""
    from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

    if not enabled():
        if _require_auth():
            raise RuntimeError(
                "MASTERMIND_REQUIRE_AUTH is set but MASTERMIND_PASSWORD is not — "
                "refusing to start unauthenticated. Set MASTERMIND_PASSWORD in .env."
            )
        log.warning(
            "MASTERMIND_PASSWORD not set — auth DISABLED. "
            "Set MASTERMIND_REQUIRE_AUTH=1 in production to refuse startup without a password."
        )

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

        # --- serve-only mode: block all operator mutations ---
        if serve_only() and method == "POST" and path in _OPERATOR_PATHS:
            return JSONResponse(
                {"error": "serve_only", "detail": (
                    "This instance is running in serve-only (read-only mirror) mode. "
                    "Operator mutations are disabled. Use the primary instance."
                )},
                status_code=403,
            )

        # --- standard auth gate ---
        if not enabled() or is_authorized(request):
            pass  # falls through to operator-tier check below
        else:
            # Browser navigation -> bounce to the login page; API/XHR -> 401 JSON.
            if method == "GET" and "text/html" in request.headers.get("accept", ""):
                return RedirectResponse(url=f"/login?next={safe_next(path)}", status_code=303)
            return JSONResponse({"error": "unauthorized"}, status_code=401)

        # --- operator-tier gate: bearer required, cookie not sufficient ---
        if method == "POST" and path in _OPERATOR_PATHS and enabled():
            if not is_operator_authorized(request):
                return JSONResponse(
                    {"error": "operator_bearer_required",
                     "detail": "Operator paths require a bearer token — "
                               "a session cookie is not sufficient."},
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

    @app.get("/login", include_in_schema=False)
    def login_page(next: str = "/"):  # noqa: A002 — query param name
        return HTMLResponse(_login_html(safe_next(next)))

    @app.post("/login", include_in_schema=False)
    async def login_submit(request: Request):
        # Parse urlencoded body with stdlib (no python-multipart dependency).
        form = parse_qs((await request.body()).decode("utf-8", "replace"))
        password = (form.get("password") or [""])[0]
        nxt = safe_next((form.get("next") or ["/"])[0])
        if not check_password(password):
            return HTMLResponse(_login_html(nxt, error=True), status_code=401)
        resp = RedirectResponse(url=nxt, status_code=303)
        secure = os.environ.get("MASTERMIND_COOKIE_SECURE") == "1" or request.url.scheme == "https"
        resp.set_cookie(_COOKIE, make_cookie(_password()), max_age=_session_days() * 86400,
                        httponly=True, samesite="lax", secure=secure, path="/")
        return resp

    @app.get("/logout", include_in_schema=False)
    def logout():
        resp = RedirectResponse(url="/login", status_code=303)
        resp.delete_cookie(_COOKIE, path="/")
        return resp
