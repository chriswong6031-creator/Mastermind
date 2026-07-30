"""Account / profile broker for the Mastermind account panel.

The single privileged endpoint set (living on app.mastermind-x.com) that BOTH the
app and the macro dashboard call to read + manage the signed-in user's Supabase
account. It is SELF-AUTHORIZING per-user: it verifies the caller's OWN Supabase
access token — a ``Authorization: Bearer`` header (the macro site, which has the
token via its Supabase JS session) or the shared ``.mastermind-x.com`` SSO cookie
(the app, same origin) — against ``GET /auth/v1/user``, then performs any change
ONLY on that verified user's id. A client can never target another account.

Privileged writes that must skip email confirmation (email change, delete) use the
SERVICE-ROLE key (``SUPABASE_SERVICE_ROLE_KEY``, server-only, never shipped to any
browser). Password + preference writes use the user's OWN token (no service role,
no confirmation email). Everything degrades soft: with no service key, an email
change falls back to Supabase's standard confirmation flow and delete is disabled —
the rest of the panel is unaffected.

Reuses the Supabase plumbing already in ``app/auth.py`` (URL / anon key / the
@supabase/ssr cookie decoder) so there's one source of truth for the config.
"""
from __future__ import annotations

import logging
import os
import re

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app import auth   # reuse _supabase_url / _supabase_anon / _supabase_decode

log = logging.getLogger("mastermind.account")

router = APIRouter(tags=["account"])

_MIN_PW = 8
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_PROVIDER_LABEL = {
    "email": "Email / magic link", "google": "Google", "github": "GitHub",
    "apple": "Apple", "azure": "Microsoft", "facebook": "Facebook",
    "twitter": "X / Twitter", "phone": "Phone",
}


# ------------------------------------------------------------------- config ----

def _service_key() -> str | None:
    """The server-only service-role secret (a JWT ``eyJ…`` or ``sb_secret_…``).

    NOT the ``sbp_…`` Management-API personal token — that can't touch auth users.
    """
    return (os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
            or os.environ.get("SUPABASE_SERVICE_KEY") or None)


def admin_enabled() -> bool:
    return bool(_service_key() and auth._supabase_url())


def _supa_cfg():
    """The PUBLIC client config (url + anon key) so the panel can drive Supabase JS
    (magic-link sign-in / token) without hardcoding it in either site's HTML. The
    anon key is public by design (RLS enforces per-user isolation)."""
    u, a = auth._supabase_url(), auth._supabase_anon()
    return {"url": u, "anon_key": a} if (u and a) else None


# ---------------------------------------------------------- identify caller ----

def _bearer(request) -> str | None:
    h = request.headers.get("authorization", "")
    return h[7:].strip() or None if h.lower().startswith("bearer ") else None


def _caller_token(request) -> str | None:
    """The caller's Supabase ACCESS token: an explicit Bearer header (macro) or the
    shared SSO cookie (app). None if neither is present."""
    tok = _bearer(request)
    if tok:
        return tok
    sess = auth._supabase_decode(request)
    if sess and sess.get("access_token"):
        return sess["access_token"]
    return None


async def _get_user(token: str) -> dict | None:
    """The Supabase user object for ``token`` (id/email/metadata/identities) or None."""
    url = auth._supabase_url()
    if not url:
        return None
    try:
        import httpx
        async with httpx.AsyncClient(timeout=6.0) as c:
            r = await c.get(url + "/auth/v1/user",
                            headers={"Authorization": f"Bearer {token}",
                                     "apikey": auth._supabase_anon() or ""})
        return r.json() if r.status_code == 200 else None
    except Exception:  # noqa: BLE001 — network/import issues => treat as signed-out
        return None


async def _require_user(request):
    """(token, user) for the authenticated caller, or (None, None)."""
    token = _caller_token(request)
    if not token:
        return None, None
    user = await _get_user(token)
    if not user or not user.get("id"):
        return None, None
    return token, user


# ------------------------------------------------------- Supabase requests ----

async def _admin_request(method: str, path: str, json_body=None):
    """Call the Supabase Admin API with the service-role key. Returns the httpx
    Response, or None when no service key is configured (caller falls back)."""
    key = _service_key()
    url = auth._supabase_url()
    if not key or not url:
        return None
    import httpx
    async with httpx.AsyncClient(timeout=8.0) as c:
        return await c.request(method, url + path, json=json_body,
                               headers={"apikey": key, "Authorization": f"Bearer {key}",
                                        "Content-Type": "application/json"})


async def _user_put(token: str, body: dict):
    """Update the user via THEIR OWN token (``PUT /auth/v1/user``) — no service role."""
    import httpx
    async with httpx.AsyncClient(timeout=8.0) as c:
        return await c.put(auth._supabase_url() + "/auth/v1/user", json=body,
                           headers={"Authorization": f"Bearer {token}",
                                    "apikey": auth._supabase_anon() or "",
                                    "Content-Type": "application/json"})


def _sb_err(r) -> str:
    try:
        j = r.json()
        return (j.get("msg") or j.get("error_description") or j.get("message")
                or j.get("error") or f"Request failed ({r.status_code}).")
    except Exception:  # noqa: BLE001
        return f"Request failed ({r.status_code})."


def _login_method(user: dict):
    """(primary_provider, [providers], human_label)."""
    app_md = user.get("app_metadata") or {}
    providers = list(app_md.get("providers") or [])
    primary = app_md.get("provider") or (providers[0] if providers else "")
    for i in (user.get("identities") or []):
        p = i.get("provider")
        if p and p not in providers:
            providers.append(p)
    if primary and primary not in providers:
        providers.insert(0, primary)
    if not providers:
        providers = ["email"]
    primary = primary or providers[0]
    label = " · ".join(_PROVIDER_LABEL.get(p, p.title()) for p in providers)
    return primary, providers, label


# ----------------------------------------------------------------- routes ----

@router.get("/api/account")
async def get_account(request: Request):
    """The signed-in user's profile for the panel. 200 with ``authenticated:false``
    (never 401) when there's no linked Supabase user, so the panel can render a
    signed-out / password-only state."""
    token, user = await _require_user(request)
    if not user:
        return {"authenticated": False, "plan": "free", "plan_label": "Free",
                "admin_ops": admin_enabled(), "supabase": _supa_cfg()}
    provider, providers, label = _login_method(user)
    md = user.get("user_metadata") or {}
    plan = (md.get("plan") or "free")
    return {
        "authenticated": True,
        "id": user.get("id"),
        "email": user.get("email") or None,
        "email_confirmed": bool(user.get("email_confirmed_at") or user.get("confirmed_at")),
        "phone": user.get("phone") or None,
        "name": md.get("name") or md.get("full_name") or None,
        "avatar_url": md.get("avatar_url") or md.get("picture") or None,
        "provider": provider,
        "providers": providers,
        "provider_label": label,
        "created_at": user.get("created_at"),
        "last_sign_in_at": user.get("last_sign_in_at"),
        "plan": plan,
        "plan_label": "Free" if plan == "free" else str(plan).title(),
        "prefs": {"theme": md.get("theme"), "lang": md.get("lang")},
        "admin_ops": admin_enabled(),
        "supabase": _supa_cfg(),
    }


class PwReq(BaseModel):
    password: str


@router.post("/api/account/password")
async def set_password(req: PwReq, request: Request):
    """Set / reset the password directly — NO email round-trip. Uses the admin API
    when available (works even for OAuth/magic-link-only users), else the user's own
    session (also no email)."""
    token, user = await _require_user(request)
    if not user:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    pw = (req.password or "").strip()
    if len(pw) < _MIN_PW:
        return JSONResponse({"error": f"Password must be at least {_MIN_PW} characters."},
                            status_code=400)
    r = await _admin_request("PUT", f"/auth/v1/admin/users/{user['id']}", {"password": pw})
    if r is None:                                   # no service key -> self-serve (no email)
        r = await _user_put(token, {"password": pw})
    if r.status_code in (200, 201):
        return {"ok": True}
    return JSONResponse({"error": _sb_err(r)}, status_code=400)


class EmailReq(BaseModel):
    email: str


@router.post("/api/account/email")
async def set_email(req: EmailReq, request: Request):
    """Add or change the email. With the service key this is instant + confirmed
    (``email_confirm:true``, no email sent). Without it, falls back to the user's own
    session, which triggers Supabase's confirmation link (``confirmation_required``)."""
    token, user = await _require_user(request)
    if not user:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    email = (req.email or "").strip().lower()
    if not _EMAIL_RE.match(email):
        return JSONResponse({"error": "Enter a valid email address."}, status_code=400)
    r = await _admin_request("PUT", f"/auth/v1/admin/users/{user['id']}",
                             {"email": email, "email_confirm": True})
    if r is not None:
        if r.status_code in (200, 201):
            return {"ok": True, "email": email, "confirmation_required": False}
        return JSONResponse({"error": _sb_err(r)}, status_code=400)
    # no service key: self-serve change -> Supabase emails a confirmation link
    r = await _user_put(token, {"email": email})
    if r.status_code in (200, 201):
        return {"ok": True, "email": email, "confirmation_required": True}
    return JSONResponse({"error": _sb_err(r)}, status_code=400)


class PrefsReq(BaseModel):
    theme: str | None = None
    lang: str | None = None


@router.post("/api/account/prefs")
async def set_prefs(req: PrefsReq, request: Request):
    """Remember appearance/language on the ACCOUNT (Supabase user_metadata) so the
    look follows the user to any device/site they sign in on. User-token write —
    no service role, no email."""
    token, user = await _require_user(request)
    if not user:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    md = dict(user.get("user_metadata") or {})
    changed = False
    if req.theme in ("light", "dark"):
        md["theme"] = req.theme; changed = True
    if req.lang in ("en", "zh"):
        md["lang"] = req.lang; changed = True
    if changed:
        r = await _user_put(token, {"data": md})
        if r.status_code not in (200, 201):
            return JSONResponse({"error": _sb_err(r)}, status_code=400)
    return {"ok": True, "prefs": {"theme": md.get("theme"), "lang": md.get("lang")}}


@router.post("/api/account/signout-everywhere")
async def signout_everywhere(request: Request):
    """Revoke every session for this user (global refresh-token revoke)."""
    token, user = await _require_user(request)
    if not user:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        import httpx
        async with httpx.AsyncClient(timeout=6.0) as c:
            r = await c.post(auth._supabase_url() + "/auth/v1/logout",
                             params={"scope": "global"},
                             headers={"Authorization": f"Bearer {token}",
                                      "apikey": auth._supabase_anon() or ""})
        return {"ok": r.status_code in (200, 204)}
    except Exception:  # noqa: BLE001
        return JSONResponse({"error": "Could not reach the auth server."}, status_code=502)


class DeleteReq(BaseModel):
    confirm: str


@router.post("/api/account/delete")
async def delete_account(req: DeleteReq, request: Request):
    """Permanently delete the account. Requires the service key; guarded by a
    type-your-email confirmation."""
    token, user = await _require_user(request)
    if not user:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if not admin_enabled():
        return JSONResponse({"error": "Account deletion isn't available yet."}, status_code=501)
    if (req.confirm or "").strip().lower() != (user.get("email") or "").strip().lower():
        return JSONResponse({"error": "Type your email exactly to confirm."}, status_code=400)
    r = await _admin_request("DELETE", f"/auth/v1/admin/users/{user['id']}")
    if r is not None and r.status_code in (200, 204):
        return {"ok": True}
    return JSONResponse({"error": _sb_err(r) if r is not None else "Deletion failed."},
                        status_code=400)
