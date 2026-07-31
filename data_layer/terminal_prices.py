"""Terminal (Macro Dashboard) per-ticker snapshot prices — instant, network-free local reads.

"Terminal" is the Macro Dashboard front-end (app.mastermind-x.com). Its price surface is one JSON
per ticker under ``vendor/macro/site/{stockdata|hkstockdata|chinastockdata}/<T>.json`` with the last
mark at JSON path ``tech.price`` (in the name's NATIVE currency — USD for US, HKD for ``*.HK``, CNY
for A-shares). The Macro build publishes these via Cloudflare R2 (mirrored by
``data_layer.macro_refresh._sync_r2``).

Why this module exists — the dashboard perf fix. The dashboard read path must never block on a live
Yahoo/Tushare network fetch (the cause of the ~5s book-switch stall). It serves the hot in-process
live cache when warm, and falls back to THIS snapshot (a single local file read, ~instant) when the
cache is cold. It is the "fast dashboard source".

Boundary (charter P7 — the ONE marking layer owns NAV): these EOD-lagging snapshots are for the
DASHBOARD read only. The authoritative NAV is still marked from the LIVE feeds by the scheduler
(``portfolio.marks`` / ``yahoo_feed`` / ``tushare_feed``) — nothing here writes NAV, so a stale
snapshot can never mis-mark the book of record.

Degrade-never-raise: any missing file / parse error returns None and the caller falls through to its
own next source. In a sparse worktree (no ``vendor/macro`` checkout) every lookup returns None, which
is correct — the live feeds still price the book.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent


def _dir_for(ticker: str) -> str:
    """The per-venue stockdata subdir that holds a ticker's snapshot (matches paper_account._live_price)."""
    t = ticker.upper()
    if t.endswith((".SS", ".SZ")):
        return "chinastockdata"
    if t.endswith(".HK"):
        return "hkstockdata"
    return "stockdata"


def price_local(ticker: str) -> float | None:
    """The Terminal snapshot price in the ticker's NATIVE currency, or None.

    USD for US names (``stockdata/``), HKD for ``*.HK`` (``hkstockdata/``), CNY for A-shares
    (``chinastockdata/``). A plain file read — no import of the engine, no network.
    """
    t = (ticker or "").upper().strip()
    if not t:
        return None
    try:
        p = _ROOT / "vendor" / "macro" / "site" / _dir_for(t) / f"{t}.json"
        if p.exists():
            v = (json.loads(p.read_text()).get("tech") or {}).get("price")
            if v is not None and float(v) > 0:
                return float(v)
    except Exception:
        pass
    return None


def quote_local(ticker: str) -> dict | None:
    """Terminal snapshot quote with file-mtime provenance; a local read, never a live fetch."""
    t = (ticker or "").upper().strip()
    if not t:
        return None
    try:
        path = _ROOT / "vendor" / "macro" / "site" / _dir_for(t) / f"{t}.json"
        if not path.exists():
            return None
        value = (json.loads(path.read_text()).get("tech") or {}).get("price")
        if value is None or float(value) <= 0:
            return None
        modified = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        age = max(0.0, (datetime.now(timezone.utc) - modified).total_seconds())
        return {
            "ticker": t,
            "price_local": float(value),
            "source": "terminal_snapshot",
            "as_of": modified.isoformat(timespec="seconds"),
            "time_kind": "snapshot_file_mtime",
            "age_seconds": round(age, 1),
            "fresh": False,
        }
    except Exception:
        return None


def price_usd(ticker: str) -> float | None:
    """The Terminal snapshot price converted to USD (non-US venues via ``portfolio.fx``), or None."""
    local = price_local(ticker)
    if local is None:
        return None
    t = (ticker or "").upper().strip()
    if t.endswith((".HK", ".SS", ".SZ")):
        try:
            from portfolio import fx
            usd = fx.to_usd(local, t)
            return float(usd) if usd and usd > 0 else None
        except Exception:
            return None
    return local
