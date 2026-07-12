"""Tushare price feed — fresh same-day A-share closes so the China book's P&L is marked live.

The vendored macro snapshot can lag several days (it refreshes on the macro build cadence). Tushare's
``daily`` endpoint returns the current trade date's close for EVERY Shanghai/Shenzhen A-share in ONE
bulk call, which we cache by trade date — so marking the whole book costs ~1 API call/day.

Scope is **A-shares only** (``*.SS`` / ``*.SZ``) — the clear freshness win. Tushare's HK ``hk_daily``
feed is throttled to ~1 call/hour on the standard token tier (unusable for an 8-name book), so Hong
Kong marks against Yahoo instead (see ``data_layer.yahoo_feed``) and US ADRs keep the vendored US
snapshot in ``paper_account._live_price``.

Token: ``TUSHARE_TOKEN`` from the environment, falling back to the gitignored ``.env`` (never
committed). Pure-ish + degrade-never-raise: any miss returns None and paper_account falls back to
the snapshot, so a token/network/rate-limit problem never breaks pricing.
"""
from __future__ import annotations

import json
import logging
import os
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path

log = logging.getLogger(__name__)

_ROOT = Path(__file__).resolve().parent.parent
_API = "http://api.tushare.pro"
_WALK_BACK_DAYS = 5                       # skip weekends/holidays / a not-yet-published today

_CACHE: dict[str, dict[str, float]] = {}  # trade_date(YYYYMMDD) -> {TS_CODE: close in CNY}
_TOKEN: str | None = None
_TOKEN_LOADED = False


def _token() -> str | None:
    global _TOKEN, _TOKEN_LOADED
    if _TOKEN_LOADED:
        return _TOKEN
    _TOKEN_LOADED = True
    _TOKEN = os.environ.get("TUSHARE_TOKEN") or None
    if not _TOKEN:
        try:
            for line in (_ROOT / ".env").read_text(encoding="utf-8").splitlines():
                if line.startswith("TUSHARE_TOKEN="):
                    _TOKEN = line.split("=", 1)[1].strip() or None
                    break
        except Exception:
            pass
    return _TOKEN


def available() -> bool:
    return bool(_token())


def _to_ts_code(ticker: str) -> str | None:
    """macro-format A-share ticker → Tushare code, or None for non-A-share.
    Shanghai ``*.SS`` → ``*.SH``; Shenzhen ``*.SZ`` stays ``*.SZ``."""
    t = (ticker or "").upper().strip()
    if t.endswith(".SS"):
        return t[:-3] + ".SH"
    if t.endswith(".SZ"):
        return t
    return None


def _call(api: str, params: dict, fields: str) -> dict | None:
    tok = _token()
    if not tok:
        return None
    body = json.dumps({"api_name": api, "token": tok, "params": params, "fields": fields}).encode()
    req = urllib.request.Request(_API, data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            r = json.load(resp)
    except Exception as e:  # noqa: BLE001
        log.debug("tushare %s request failed (%s)", api, e)
        return None
    if r.get("code") != 0:
        log.debug("tushare %s error: %s", api, r.get("msg"))
        return None
    return r.get("data")


def _load_day(td: str, today: str, *, force: bool = False) -> dict[str, float]:
    """All A-share CNY closes for trade_date `td` (one bulk call), cached.

    Past trade dates cache permanently; TODAY caches only once it has data, so an intraday empty
    (today's bar not published until after the close) is re-fetched rather than frozen empty.

    ``force=True`` bypasses the cache READ and hits the live feed — for ``feed_healthy``'s outage
    probe, so a genuine close cached BEFORE an outage cannot mask an ongoing outage in a long-lived
    process. A forced probe never caches an EMPTY result (it must not poison a prior good close with a
    transient miss); a successful forced probe DOES cache, so a later ``price_local`` pays no extra call."""
    if not force and td in _CACHE:
        return _CACHE[td]
    out: dict[str, float] = {}
    data = _call("daily", {"trade_date": td}, "ts_code,close")
    if data and data.get("items"):
        for row in data["items"]:
            try:
                code, close = row[0], row[1]
                if close is not None:
                    out[code] = float(close)
            except (TypeError, ValueError, IndexError):
                pass
    if out:
        _CACHE[td] = out
    elif not force and td != today:
        _CACHE[td] = out          # non-forced: cache a genuinely-empty PAST date (holiday) to avoid re-fetch
    return out


def price_local(ticker: str, asof: str | None = None) -> float | None:
    """The most recent A-share CNY close for `ticker` (macro format), or None if unavailable
    (non-A-share, no token, or no data). Walks back a few days to skip non-trading days / a
    today whose close hasn't published yet."""
    ts = _to_ts_code(ticker)
    if not ts or not _token():
        return None
    try:
        d0 = datetime.fromisoformat((asof or date.today().isoformat())).date()
    except Exception:
        d0 = date.today()
    today = d0.isoformat().replace("-", "")
    for back in range(0, _WALK_BACK_DAYS + 1):
        td = (d0 - timedelta(days=back)).isoformat().replace("-", "")
        day = _load_day(td, today)
        if ts in day:
            return day[ts]
    return None


def feed_healthy(asof: str | None = None) -> bool | None:
    """Is the bulk A-share ``daily`` feed actually serving fresh closes for a recent trade date?

    Tri-state — the distinction the China book gates on:
      * ``True``  — the bulk call returned a non-empty market (the feed is up; safe to trade).
      * ``False`` — a token IS configured but every walked-back trade date came back EMPTY: the
                    feed is down / throttled / the token is rejected. An OUTAGE.
      * ``None``  — no token configured at all; the live feed isn't deployed and the book runs on
                    the vendored snapshot by design (tests, or a token-less deployment). NOT an outage.

    ``daily`` is all-or-nothing — one call returns the WHOLE Shanghai/Shenzhen market — so an empty
    result with a token present means the feed itself is unavailable, never that individual names are
    missing. That is exactly the condition ``bot.china`` must refuse to trade on: a HELD name still
    marks off the stale per-name snapshot while a fresh CANDIDATE returns ``priceable=false``, handing
    the Brain an asymmetric (untrustworthy) priceable map. Walks back the same few days as
    ``price_local`` so a weekend / not-yet-published today is not mistaken for an outage. Reuses the
    per-trade-date cache, so a later ``price_local`` in the same run pays no extra call."""
    if not _token():
        return None
    try:
        d0 = datetime.fromisoformat((asof or date.today().isoformat())).date()
    except Exception:
        d0 = date.today()
    today = d0.isoformat().replace("-", "")
    for back in range(0, _WALK_BACK_DAYS + 1):
        td = (d0 - timedelta(days=back)).isoformat().replace("-", "")
        if _load_day(td, today, force=True):      # LIVE probe (bypass cache) so an ongoing outage,
            return True                            # not a pre-outage cached close, decides feed health
    return False


def clear_cache() -> None:
    """Drop the per-process price + token memo (tests / a forced refresh)."""
    global _TOKEN, _TOKEN_LOADED
    _CACHE.clear()
    _TOKEN, _TOKEN_LOADED = None, False
