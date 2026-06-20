"""Persistent positions ledger — tracks open/close timestamps across book builds.

Keyed by "<sleeve>:<ticker>" in data/portfolio/positions_ledger.json.
Crash-safe: a corrupt or missing file starts fresh.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent.parent
_LEDGER_PATH = _ROOT / "data" / "portfolio" / "positions_ledger.json"
_WEIGHT_CHANGE_THRESHOLD = 0.01   # 1 percentage point = material weight change


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _held_days(opened_at: str, closed_at: str | None = None) -> int | None:
    """Days between open and close (or now). Returns None if opened_at is missing."""
    if not opened_at:
        return None
    try:
        t0 = datetime.fromisoformat(opened_at)
        t1 = datetime.fromisoformat(closed_at) if closed_at else datetime.now(timezone.utc)
        return max(0, (t1 - t0).days)
    except Exception:
        return None


def _load() -> dict[str, Any]:
    """Load the ledger; return {} on corrupt/missing file."""
    try:
        if _LEDGER_PATH.exists():
            return json.loads(_LEDGER_PATH.read_text())
    except Exception:
        pass
    return {}


def _save(ledger: dict[str, Any]) -> None:
    _LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
    _LEDGER_PATH.write_text(json.dumps(ledger, indent=2, default=str, ensure_ascii=False))


def update(positions: list[dict], asof_iso: str) -> None:
    """Call once per book build to reconcile the ledger with the current positions.

    positions: the assembled book list (each dict must have 'ticker' and 'sleeve' at minimum,
               plus optional 'weight', 'entry_price').
    asof_iso:  the date string from the regime (used as the 'as_of' watermark).
    """
    now = _now_iso()
    ledger = _load()

    # build a set of keys currently in the book
    current_keys: dict[str, dict] = {}
    for p in positions:
        key = f"{p.get('sleeve', 'unknown')}:{p['ticker']}"
        current_keys[key] = p

    # --- handle currently-in-book positions ---
    for key, p in current_keys.items():
        weight = p.get("weight")
        price = (p.get("entry_levels") or {}).get("price") or p.get("entry_price")
        ticker = p["ticker"]
        sleeve = p.get("sleeve", "unknown")

        if key not in ledger:
            # new entry
            ledger[key] = {
                "ticker": ticker,
                "sleeve": sleeve,
                "opened_at": now,
                "open_as_of": asof_iso,
                "closed_at": None,
                "close_as_of": None,
                "still_open": True,
                "last_seen": now,
                "entry_weight": weight,
                "entry_price": price,
                "current_weight": weight,
                "thesis_id": p.get("thesis_id"),
                "history": [{"event": "open", "ts": now, "weight": weight, "price": price}],
            }
        else:
            entry = ledger[key]
            if not entry.get("still_open"):
                # re-opened position — restart
                entry["opened_at"] = now
                entry["open_as_of"] = asof_iso
                entry["closed_at"] = None
                entry["close_as_of"] = None
                entry["still_open"] = True
                entry["entry_weight"] = weight
                entry["entry_price"] = price
                entry["history"].append({"event": "open", "ts": now, "weight": weight, "price": price})

            # track material weight changes
            prior_weight = entry.get("current_weight") or 0.0
            if weight is not None and prior_weight is not None:
                delta = abs((weight or 0.0) - (prior_weight or 0.0))
                if delta > _WEIGHT_CHANGE_THRESHOLD:
                    event = "add" if (weight or 0) > (prior_weight or 0) else "trim"
                    entry["history"].append({"event": event, "ts": now,
                                             "weight": weight, "price": price})

            entry["last_seen"] = now
            entry["current_weight"] = weight

    # --- handle positions that have left the book ---
    for key, entry in ledger.items():
        if entry.get("still_open") and key not in current_keys:
            entry["still_open"] = False
            entry["closed_at"] = now
            entry["close_as_of"] = asof_iso
            entry["history"].append({"event": "close", "ts": now,
                                     "weight": 0, "price": None})

    _save(ledger)


def open_positions() -> list[dict]:
    """Return open positions shaped for /api/trades 'open' array."""
    ledger = _load()
    out = []
    for key, e in ledger.items():
        if not e.get("still_open"):
            continue
        out.append({
            "ticker": e["ticker"],
            "sleeve": e["sleeve"],
            "opened_at": e.get("opened_at"),
            "held_days": _held_days(e.get("opened_at")),
            "entry_weight": e.get("entry_weight"),
            "current_weight": e.get("current_weight"),
            "entry_price": e.get("entry_price"),
        })
    # newest first (most recently opened)
    out.sort(key=lambda x: x.get("opened_at") or "", reverse=True)
    return out


def closed_positions() -> list[dict]:
    """Return closed positions shaped for /api/trades 'closed' array."""
    ledger = _load()
    out = []
    for key, e in ledger.items():
        if e.get("still_open"):
            continue
        out.append({
            "ticker": e["ticker"],
            "sleeve": e["sleeve"],
            "opened_at": e.get("opened_at"),
            "closed_at": e.get("closed_at"),
            "held_days": _held_days(e.get("opened_at"), e.get("closed_at")),
            "exit_reason": "removed from book",
        })
    out.sort(key=lambda x: x.get("closed_at") or "", reverse=True)
    return out


def get_entry_info(sleeve: str, ticker: str) -> dict:
    """Return {opened_at, held_days, entry_price} for a given position key, or {} if not found."""
    ledger = _load()
    key = f"{sleeve}:{ticker}"
    e = ledger.get(key)
    if not e:
        return {}
    return {
        "opened_at": e.get("opened_at"),
        "held_days": _held_days(e.get("opened_at")),
        "entry_price": e.get("entry_price"),
    }
