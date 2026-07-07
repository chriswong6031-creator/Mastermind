"""Portfolio CRUD proxy — FastAPI router for /api/pfolio/*.

All mutations target Supabase only (no VPS-local state, no LLM, no scheduler).
PRD-R7: no position values are logged. PRD-R8: serve-only carve-out applies (see app/auth.py).
PRD-R2: no fused numeric score; risk field is a lane-state composite read from the W2 artifact.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import date as _date, datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel

log = logging.getLogger("mastermind.pfolio")

router = APIRouter(prefix="/api/pfolio")

# ---------------------------------------------------------------------------
# Supabase connection (service role key — operator-scoped, not user-auth)
# ---------------------------------------------------------------------------

def _sb_url() -> str | None:
    return os.environ.get("SUPABASE_URL") or None


def _sb_service_key() -> str | None:
    return os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or None


def _sb_ready() -> bool:
    return bool(_sb_url() and _sb_service_key())


def _sb_headers() -> dict:
    k = _sb_service_key()
    return {
        "apikey": k or "",
        "Authorization": f"Bearer {k or ''}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }


# ---------------------------------------------------------------------------
# Operator UUID cache — resolved once from auth admin API
# ---------------------------------------------------------------------------

_operator_uid_cache: dict[str, str | None] = {}  # {"uid": ..., "ts": ...}
_UID_TTL = 3600.0  # 1 hour


def _operator_email() -> str:
    return os.environ.get("MASTERMIND_OPERATOR_EMAIL", "demo@mastermind.test")


def _resolve_operator_uid() -> str | None:
    """Resolve the operator's Supabase auth.users UUID via the admin API.

    Cached in module state for _UID_TTL seconds. On any failure returns None
    (fail-soft — endpoints return {"ok": false, "error": "supabase_unavailable"}).
    PRD-R7: never logs the UID value.
    """
    now = time.monotonic()
    cached_uid = _operator_uid_cache.get("uid")
    cached_ts = _operator_uid_cache.get("ts", 0.0)
    if cached_uid is not None and (now - cached_ts) < _UID_TTL:
        return cached_uid

    if not _sb_ready():
        return None

    url = _sb_url()
    email = _operator_email()
    try:
        import httpx
        resp = httpx.get(
            f"{url}/auth/v1/admin/users",
            params={"email": email},
            headers={
                "apikey": _sb_service_key() or "",
                "Authorization": f"Bearer {_sb_service_key() or ''}",
            },
            timeout=8.0,
        )
        if resp.status_code != 200:
            log.warning("pfolio: auth admin users lookup failed: %s", resp.status_code)
            return None
        data = resp.json()
        users = data if isinstance(data, list) else (data.get("users") or [])
        for u in users:
            if (u.get("email") or "").lower() == email.lower():
                uid = u.get("id")
                _operator_uid_cache["uid"] = uid
                _operator_uid_cache["ts"] = now
                return uid
        log.warning("pfolio: operator email not found in auth.users")
        return None
    except Exception as exc:
        log.warning("pfolio: UID resolution failed: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _unavailable() -> JSONResponse:
    return JSONResponse({"ok": False, "error": "supabase_unavailable"})


def _table_missing_resp() -> JSONResponse:
    return JSONResponse({
        "ok": False,
        "error": "table_missing",
        "setup": "run sql/0002_portfolio_positions.sql",
    })


def _is_table_missing(status: int, body: Any) -> bool:
    """PostgREST 404 with code 42P01 = relation does not exist."""
    if status == 404:
        if isinstance(body, dict) and (body.get("code") == "42P01" or "does not exist" in str(body.get("message", ""))):
            return True
    return False


def _normalize_ticker(raw: str) -> str | None:
    """Upper-case, strip whitespace. Allow bare symbols and venue suffixes (0700.HK, 600519.SS).
    Returns None on empty or obviously invalid input."""
    t = (raw or "").strip().upper()
    if not t:
        return None
    # Allow: letters, digits, dot, hyphen (e.g. BRK.B, BRK-B)
    if not re.match(r'^[A-Z0-9][A-Z0-9.\-]{0,19}$', t):
        return None
    return t


def _risk_for_position(pos_id: str, ticker: str) -> dict | None:
    """Read risk_state.json for this position/ticker. Returns None when absent.

    The file is produced by the W2/W3 lane composer.
    PRD-R2: risk field is raw lane states, never a fused score.
    PRD-R7: entry_price/entry_date stripped from path block before returning.
    """
    try:
        path = Path(__file__).resolve().parent.parent / "data" / "portfolio_watch" / "risk_state.json"
        if not path.exists():
            return None
        data = json.loads(path.read_text())
        positions = data.get("positions") or []
        for p in positions:
            if p.get("position_id") == pos_id or (p.get("ticker") or "").upper() == ticker.upper():
                # PRD-R7: strip entry_price, entry_date, ref_close from path
                raw_path = p.get("path") or {}
                safe_path = {
                    k: v for k, v in raw_path.items()
                    if k not in ("entry_date", "entry_price", "ref_close")
                }
                return {
                    "role": p.get("role"),
                    "role_label": p.get("role_label"),
                    "elevated_lanes": p.get("elevated_lanes"),
                    "lane_total": p.get("lane_total"),
                    "lanes": p.get("lanes"),
                    "personality": p.get("personality"),
                    "events": p.get("events"),
                    "path": safe_path,
                }
        return None
    except Exception:
        return None


def _market_block() -> dict | None:
    """Read the market block from risk_state.json. None when absent."""
    try:
        path = Path(__file__).resolve().parent.parent / "data" / "portfolio_watch" / "risk_state.json"
        if not path.exists():
            return None
        data = json.loads(path.read_text())
        market = data.get("market") or None
        if market:
            asof = data.get("asof") or market.get("asof")
            market = dict(market)
            market["asof"] = asof
            market["state_asof"] = asof
        return market
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Module-level TTL cache for prev_close (avoids per-request yf.download)
# ---------------------------------------------------------------------------

_PREV_CLOSE_CACHE: dict[str, tuple[float, float]] = {}  # ticker -> (prev_close, expiry_ts)
_PREV_CLOSE_TTL = 600  # seconds (~10 min; intraday refresh cadence)


def _get_prev_close_cached(ticker: str) -> float | None:
    """Return cached prev_close for ticker, or None if expired / unavailable."""
    entry = _PREV_CLOSE_CACHE.get(ticker)
    if entry is not None:
        val, expiry = entry
        if time.monotonic() < expiry:
            return val
    return None


def _set_prev_close_cached(ticker: str, val: float) -> None:
    _PREV_CLOSE_CACHE[ticker] = (val, time.monotonic() + _PREV_CLOSE_TTL)


def _enrich_quotes(positions: list[dict]) -> list[dict]:
    """Attach live quote data to positions. Best-effort; leaves fields null on failure.

    Uses yahoo_feed.warm() + price_local() — same pattern as web.py _live_prices().
    Computes: last, day_change_pct, market_value, pnl_usd, pnl_pct, portfolio totals.
    """
    if not positions:
        return positions

    tickers = list({(p.get("ticker") or "").upper() for p in positions if p.get("ticker")})
    if not tickers:
        return positions

    price_map: dict[str, float] = {}
    prev_map: dict[str, float] = {}
    try:
        from data_layer import yahoo_feed
        yahoo_feed.warm(tickers)
        for t in tickers:
            v = yahoo_feed.price_local(t)
            if v and v > 0:
                price_map[t] = float(v)
    except Exception:
        pass

    # prev_close: serve from TTL cache where available; batch-fetch only uncached tickers
    uncached = [t for t in tickers if _get_prev_close_cached(t) is None]
    for t in tickers:
        cached = _get_prev_close_cached(t)
        if cached is not None:
            prev_map[t] = cached

    if uncached:
        try:
            import yfinance as yf
            df = yf.download(uncached, period="5d", progress=False, auto_adjust=False, threads=False)
            close = df.get("Close") if hasattr(df, "get") else df["Close"]
            if hasattr(close, "columns"):
                for sym in close.columns:
                    s = close[sym].dropna()
                    if len(s) >= 2:
                        val = float(s.iloc[-2])
                        prev_map[str(sym).upper()] = val
                        _set_prev_close_cached(str(sym).upper(), val)
            elif len(uncached) == 1:
                s = close.dropna() if close is not None else None
                if s is not None and len(s) >= 2:
                    val = float(s.iloc[-2])
                    prev_map[uncached[0]] = val
                    _set_prev_close_cached(uncached[0], val)
        except Exception:
            pass

    out = []
    for p in positions:
        t = (p.get("ticker") or "").upper()
        last = price_map.get(t)
        prev = prev_map.get(t)
        entry = p.get("entry_price")
        shares = p.get("shares")

        day_change_pct = None
        if last is not None and prev is not None and prev > 0:
            day_change_pct = round((last - prev) / prev * 100, 4)

        market_value = None
        if last is not None and shares is not None:
            try:
                market_value = round(float(shares) * last, 4)
            except Exception:
                pass

        pnl_usd = None
        pnl_pct = None
        if last is not None and entry is not None and shares is not None:
            try:
                pnl_usd = round((last - float(entry)) * float(shares), 4)
                if float(entry) > 0:
                    pnl_pct = round((last - float(entry)) / float(entry) * 100, 4)
            except Exception:
                pass

        pos_id = str(p.get("id") or "")
        risk = _risk_for_position(pos_id, t)

        out.append({
            **p,
            "last": last,
            "prev_close": prev,
            "day_change_pct": day_change_pct,
            "market_value": market_value,
            "pnl_usd": pnl_usd,
            "pnl_pct": pnl_pct,
            "risk": risk,
        })

    return out


def _portfolio_totals(enriched: list[dict]) -> dict:
    """Compute portfolio-level totals over positions that have shares.

    Returns total_value, day_pnl, total_pnl_usd.
    """
    total_value = 0.0
    day_pnl = 0.0
    total_pnl_usd = 0.0
    has_value = False

    for p in enriched:
        if p.get("shares") is None:
            continue
        mv = p.get("market_value")
        if mv is not None:
            total_value += mv
            has_value = True
        day_chg = p.get("day_change_pct")
        last = p.get("last")
        shares = p.get("shares")
        if day_chg is not None and last is not None and shares is not None:
            try:
                prev = last / (1 + day_chg / 100)
                day_pnl += (last - prev) * float(shares)
            except Exception:
                pass
        pnl = p.get("pnl_usd")
        if pnl is not None:
            total_pnl_usd += pnl

    if not has_value:
        return {"total_value": None, "day_pnl": None, "total_pnl_usd": None}
    return {
        "total_value": round(total_value, 4),
        "day_pnl": round(day_pnl, 4),
        "total_pnl_usd": round(total_pnl_usd, 4),
    }


# ---------------------------------------------------------------------------
# Pydantic request models
# ---------------------------------------------------------------------------

class _PositionCreate(BaseModel):
    ticker: str
    shares: float | None = None
    entry_price: float | None = None
    entry_date: str | None = None   # ISO date string
    notes: str | None = None


class _PositionPatch(BaseModel):
    shares: float | None = None
    entry_price: float | None = None
    entry_date: str | None = None
    notes: str | None = None
    status: str | None = None


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.get("/positions")
def get_positions() -> JSONResponse:
    """Fetch operator's open positions, enrich with live quotes and risk state.

    PRD-R2: no fused score; risk is raw lane states.
    PRD-R7: no logging of position values.
    """
    if not _sb_ready():
        return _unavailable()
    uid = _resolve_operator_uid()
    if uid is None:
        return _unavailable()

    try:
        import httpx
        url = _sb_url()
        resp = httpx.get(
            f"{url}/rest/v1/portfolio_positions",
            params={
                "user_id": f"eq.{uid}",
                "status": "eq.open",
                "order": "created_at",
                "select": "*",
            },
            headers=_sb_headers(),
            timeout=10.0,
        )
        if _is_table_missing(resp.status_code, resp.json() if resp.content else {}):
            return _table_missing_resp()
        if resp.status_code != 200:
            log.warning("pfolio: positions fetch failed: %s", resp.status_code)
            return _unavailable()
        positions = resp.json() or []
    except Exception as exc:
        log.warning("pfolio: positions fetch error: %s", exc)
        return _unavailable()

    enriched = _enrich_quotes(positions)
    totals = _portfolio_totals(enriched)
    market = _market_block()

    payload: dict[str, Any] = {
        "ok": True,
        "positions": enriched,
        **totals,
    }
    if market is not None:
        payload["market"] = market

    return JSONResponse(payload)


@router.post("/positions")
def create_position(req: _PositionCreate) -> JSONResponse:
    """Insert a new open position for the operator.

    PRD-R7: no values logged. Validates numerics > 0.
    """
    if not _sb_ready():
        return _unavailable()
    uid = _resolve_operator_uid()
    if uid is None:
        return _unavailable()

    ticker = _normalize_ticker(req.ticker)
    if not ticker:
        return JSONResponse({"ok": False, "error": "invalid_ticker"}, status_code=422)

    if req.shares is not None and req.shares <= 0:
        return JSONResponse({"ok": False, "error": "shares_must_be_positive"}, status_code=422)
    if req.entry_price is not None and req.entry_price <= 0:
        return JSONResponse({"ok": False, "error": "entry_price_must_be_positive"}, status_code=422)

    row: dict[str, Any] = {
        "user_id": uid,
        "ticker": ticker,
        "status": "open",
    }
    if req.shares is not None:
        row["shares"] = req.shares
    if req.entry_price is not None:
        row["entry_price"] = req.entry_price
    if req.entry_date is not None:
        row["entry_date"] = req.entry_date
    if req.notes is not None:
        row["notes"] = req.notes

    try:
        import httpx
        url = _sb_url()
        resp = httpx.post(
            f"{url}/rest/v1/portfolio_positions",
            json=row,
            headers=_sb_headers(),
            timeout=10.0,
        )
        if _is_table_missing(resp.status_code, resp.json() if resp.content else {}):
            return _table_missing_resp()
        if resp.status_code not in (200, 201):
            log.warning("pfolio: position insert failed: %s", resp.status_code)
            return _unavailable()
        created = resp.json()
        if isinstance(created, list):
            created = created[0] if created else {}
        return JSONResponse({"ok": True, "position": created})
    except Exception as exc:
        log.warning("pfolio: position insert error: %s", exc)
        return _unavailable()


@router.patch("/positions/{position_id}")
def update_position(position_id: str, req: _PositionPatch) -> JSONResponse:
    """Update a position. Always scoped to operator's user_id.

    Sets updated_at=now(); sets closed_at when status changes to 'closed'.
    PRD-R7: no values logged.
    """
    if not _sb_ready():
        return _unavailable()
    uid = _resolve_operator_uid()
    if uid is None:
        return _unavailable()

    if req.status is not None and req.status not in ("open", "closed"):
        return JSONResponse({"ok": False, "error": "invalid_status"}, status_code=422)
    if req.shares is not None and req.shares <= 0:
        return JSONResponse({"ok": False, "error": "shares_must_be_positive"}, status_code=422)
    if req.entry_price is not None and req.entry_price <= 0:
        return JSONResponse({"ok": False, "error": "entry_price_must_be_positive"}, status_code=422)

    patch: dict[str, Any] = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    if req.shares is not None:
        patch["shares"] = req.shares
    if req.entry_price is not None:
        patch["entry_price"] = req.entry_price
    if req.entry_date is not None:
        patch["entry_date"] = req.entry_date
    if req.notes is not None:
        patch["notes"] = req.notes
    if req.status is not None:
        patch["status"] = req.status
        if req.status == "closed":
            patch["closed_at"] = datetime.now(timezone.utc).isoformat()

    if len(patch) == 1:  # only updated_at — nothing to change
        return JSONResponse({"ok": True, "note": "no_changes"})

    try:
        import httpx
        url = _sb_url()
        resp = httpx.patch(
            f"{url}/rest/v1/portfolio_positions",
            json=patch,
            params={"id": f"eq.{position_id}", "user_id": f"eq.{uid}"},
            headers=_sb_headers(),
            timeout=10.0,
        )
        if _is_table_missing(resp.status_code, resp.json() if resp.content else {}):
            return _table_missing_resp()
        if resp.status_code not in (200, 204):
            log.warning("pfolio: position patch failed: %s", resp.status_code)
            return _unavailable()
        updated = resp.json()
        if isinstance(updated, list):
            updated = updated[0] if updated else {}
        return JSONResponse({"ok": True, "position": updated})
    except Exception as exc:
        log.warning("pfolio: position patch error: %s", exc)
        return _unavailable()


@router.delete("/positions/{position_id}")
def delete_position(position_id: str) -> JSONResponse:
    """Hard delete a position. Always scoped to operator's user_id.

    PRD-R7: no values logged.
    """
    if not _sb_ready():
        return _unavailable()
    uid = _resolve_operator_uid()
    if uid is None:
        return _unavailable()

    try:
        import httpx
        url = _sb_url()
        resp = httpx.delete(
            f"{url}/rest/v1/portfolio_positions",
            params={"id": f"eq.{position_id}", "user_id": f"eq.{uid}"},
            headers=_sb_headers(),
            timeout=10.0,
        )
        if _is_table_missing(resp.status_code, resp.json() if resp.content else {}):
            return _table_missing_resp()
        if resp.status_code not in (200, 204):
            log.warning("pfolio: position delete failed: %s", resp.status_code)
            return _unavailable()
        return JSONResponse({"ok": True})
    except Exception as exc:
        log.warning("pfolio: position delete error: %s", exc)
        return _unavailable()


@router.get("/alerts")
def get_alerts() -> JSONResponse:
    """Return last 50 alerts from alerts.jsonl (reverse chrono). Fail-soft [].

    PRD-R7: alert records contain no entry_price, shares, or notional values.
    """
    try:
        path = Path(__file__).resolve().parent.parent / "data" / "portfolio_watch" / "alerts.jsonl"
        if not path.exists():
            return JSONResponse({"ok": True, "alerts": []})
        lines = [ln.strip() for ln in path.read_text().splitlines() if ln.strip()]
        records = []
        for line in reversed(lines[-100:]):  # last 100, reversed → newest first
            try:
                records.append(json.loads(line))
            except Exception:
                pass
        return JSONResponse({"ok": True, "alerts": records[:50]})
    except Exception as exc:
        log.warning("pfolio: alerts read failed: %s", exc)
        return JSONResponse({"ok": True, "alerts": []})
