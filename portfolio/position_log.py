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

# ── CLOSE-REASON TAXONOMY (observability only — never gates sizing/trading) ──────────────────
# A CLOSED enum of machine-readable close reasons so a nightly rebuild ROTATION is distinguishable
# from a deliberate risk EXIT. The human-readable `reason` string is kept alongside (unchanged);
# `reason_code` is the structured companion. Any close event MAY carry a reason_code; a close event
# written before this field existed (or with no code supplied) has NO reason_code key — legacy reads
# stay byte-valid. Unknown/unsupplied → "unspecified".
REASON_CODES: frozenset[str] = frozenset({
    "rebuild_dropped",    # (1) reconciliation: a held name absent from the freshly-built book
    "hard_veto",          # (2) hard-exit sweep: parabolic / distress / confirmed downtrend
    "time_stop_d5",       # (3) D5 dead-capital time-stop exit
    "cap_trim",           # (4) leadership / name / macro cap trimmed the name to zero
    "risk_officer_exit",  # (5a) Risk Officer thesis-broken exit
    "judgment_exit",      # (5b) Flagship judgment / PM-layer discretionary exit
    "manual",             # (6) operator / advisor-chat ad-hoc close
    "unspecified",        # unknown — the byte-compatible default
})

# Map the legacy free-text `reason` strings that close_position() already receives to a code, so an
# existing caller that passes only the string still lands the RIGHT structured code with no call-site
# change required. Anything unrecognised falls through to "unspecified".
_REASON_STRING_TO_CODE: dict[str, str] = {
    "hard_exit": "hard_veto",
    "hard_exit_sweep": "hard_veto",
    "risk_officer_exit": "risk_officer_exit",
    "macro_risk_cap": "cap_trim",
    "manual": "manual",
}


def _normalize_code(reason_code: str | None, reason: str | None = None) -> str:
    """Resolve a close event's structured reason_code.

    Precedence: an explicit VALID reason_code wins; else infer from the legacy `reason` string via
    the known-string map; else "unspecified". Never raises — an unknown reason_code is coerced to
    "unspecified" rather than admitting an out-of-taxonomy value into the ledger."""
    if reason_code and reason_code in REASON_CODES:
        return reason_code
    if reason and reason in _REASON_STRING_TO_CODE:
        return _REASON_STRING_TO_CODE[reason]
    return "unspecified"


def _ledger_path(portfolio_id: str | None = None) -> Path:
    """Resolve the ledger file. None/default → the legacy module-global (kept patchable
    for the test fixtures); any other id → data/portfolios/<id>/positions_ledger.json."""
    from portfolio import registry
    if not portfolio_id or portfolio_id == registry.DEFAULT_ID:
        return _LEDGER_PATH
    return registry.data_dir(portfolio_id) / "positions_ledger.json"


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


def _load(portfolio_id: str | None = None) -> dict[str, Any]:
    """Load the ledger; return {} on corrupt/missing file."""
    path = _ledger_path(portfolio_id)
    try:
        if path.exists():
            return json.loads(path.read_text())
    except Exception:
        pass
    return {}


def _save(ledger: dict[str, Any], portfolio_id: str | None = None) -> None:
    path = _ledger_path(portfolio_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(ledger, indent=2, default=str, ensure_ascii=False))


def update(positions: list[dict], asof_iso: str, portfolio_id: str | None = None,
           close_reasons: dict[str, str] | None = None,
           reason_codes: dict[str, str] | None = None) -> None:
    """Call once per book build to reconcile the ledger with the current positions.

    positions:     the assembled book list (each dict must have 'ticker' and 'sleeve' at minimum,
                   plus optional 'weight', 'entry_price').
    asof_iso:      the date string from the regime (used as the 'as_of' watermark).
    close_reasons: optional {ticker: reason_string} mapping. When a position leaves the book this
                   build the matching reason (if any) is stamped on the close history event, matching
                   the behaviour of close_position() which always carries a reason. Absent from the
                   mapping or None → reason field omitted (backwards-compatible with legacy reads).
    reason_codes:  optional {ticker: reason_code} mapping — the STRUCTURED companion to close_reasons
                   (see REASON_CODES). Stamped as `reason_code` on the close event so a rebuild
                   rotation is machine-distinguishable from a risk exit. Absent → no reason_code key
                   (byte-compatible legacy read). An out-of-taxonomy value is coerced to "unspecified".
    """
    now = _now_iso()
    ledger = _load(portfolio_id)

    # build a set of keys currently in the book
    current_keys: dict[str, dict] = {}
    for p in positions:
        key = f"{p.get('sleeve', 'unknown')}:{p['ticker']}"
        current_keys[key] = p

    # --- handle currently-in-book positions ---
    for key, p in current_keys.items():
        weight = p.get("weight")
        _levels = p.get("entry_levels") or {}
        price = _levels.get("price") or p.get("entry_price")
        ticker = p["ticker"]
        sleeve = p.get("sleeve", "unknown")
        # PUBLISHED entry-risk levels (P-NEW-3) — the dashboard's per-name stop / buy_zone /
        # entry_grade, read from the book row's entry_levels. Persist-only: NONE when the name
        # carried no published entry_signal (degrades to today's ledger row byte-for-byte).
        published_stop = _levels.get("stop")
        buy_zone = _levels.get("buy_zone")
        entry_grade = _levels.get("entry_grade")

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
                "time_stop_by": p.get("time_stop_by"),
                "published_stop": published_stop,
                "buy_zone": buy_zone,
                "entry_grade": entry_grade,
                "history": [{"event": "open", "ts": now, "as_of": asof_iso,
                             "weight": weight, "price": price}],
            }
        else:
            entry = ledger[key]
            # backfill the published entry-risk levels onto an already-open entry that predates them
            # (add-only; never overwrite a persisted non-null stop with a later miss — invariant:
            # a data gap must never erase the stop we already recorded at entry).
            if published_stop is not None and entry.get("published_stop") is None:
                entry["published_stop"] = published_stop
            if buy_zone and entry.get("buy_zone") is None:
                entry["buy_zone"] = buy_zone
            if entry_grade and entry.get("entry_grade") is None:
                entry["entry_grade"] = entry_grade
            # IDEMPOTENT PER BUILD DATE: a trade event is logged at most once per `as_of`. The book
            # may be rebuilt many times intra-day (dev runs, event interrupts); those rebuilds
            # update the weight silently — they are NOT each a new ADD/TRIM. Without this guard the
            # activity feed shows phantom ADD/TRIM churn (e.g. AVGO ADD 4.8% / TRIM 3.5% / ADD 4.8%
            # within a minute) from successive same-day rebuilds.
            logged_today = (entry.get("history") or [{}])[-1].get("as_of") == asof_iso

            if not entry.get("still_open"):
                # re-opened position — restart
                entry["opened_at"] = now
                entry["open_as_of"] = asof_iso
                entry["closed_at"] = None
                entry["close_as_of"] = None
                entry["still_open"] = True
                entry["entry_weight"] = weight
                entry["entry_price"] = price
                entry["time_stop_by"] = p.get("time_stop_by")
                # a re-open is a fresh entry — adopt the freshly-published stop when present, but a
                # miss must not blank an existing stop (degrade-never-raise: keep the prior level).
                if published_stop is not None:
                    entry["published_stop"] = published_stop
                if buy_zone:
                    entry["buy_zone"] = buy_zone
                if entry_grade:
                    entry["entry_grade"] = entry_grade
                if not logged_today:
                    entry["history"].append({"event": "open", "ts": now, "as_of": asof_iso,
                                             "weight": weight, "price": price})
                    logged_today = True
            # backfill the time-stop clock for an already-open entry that predates this field
            elif entry.get("time_stop_by") is None and p.get("time_stop_by"):
                entry["time_stop_by"] = p.get("time_stop_by")

            # track material weight changes — one rebalance event per build date, not per rebuild
            prior_weight = entry.get("current_weight") or 0.0
            if weight is not None and prior_weight is not None and not logged_today:
                delta = abs((weight or 0.0) - (prior_weight or 0.0))
                if delta > _WEIGHT_CHANGE_THRESHOLD:
                    event = "add" if (weight or 0) > (prior_weight or 0) else "trim"
                    entry["history"].append({"event": event, "ts": now, "as_of": asof_iso,
                                             "weight": weight, "price": price})

            entry["last_seen"] = now
            entry["current_weight"] = weight

    # --- handle positions that have left the book (close once per build date) ---
    for key, entry in ledger.items():
        if entry.get("still_open") and key not in current_keys:
            entry["still_open"] = False
            entry["closed_at"] = now
            entry["close_as_of"] = asof_iso
            if (entry.get("history") or [{}])[-1].get("as_of") != asof_iso:
                ticker = entry.get("ticker", "")
                # Stamp the close reason when the caller supplies one (mirrors close_position()).
                # Absent from the mapping → reason key omitted so legacy reads stay valid.
                _close_event: dict = {"event": "close", "ts": now, "as_of": asof_iso,
                                      "weight": 0, "price": None}
                if close_reasons and ticker and ticker in close_reasons:
                    _close_event["reason"] = close_reasons[ticker]
                # Structured companion: stamp reason_code only when supplied for this ticker, so a
                # build that passes no codes writes a byte-identical (code-free) close event.
                if reason_codes and ticker and ticker in reason_codes:
                    _close_event["reason_code"] = _normalize_code(reason_codes[ticker],
                                                                  close_reasons.get(ticker) if close_reasons else None)
                entry["history"].append(_close_event)

    _save(ledger, portfolio_id)


def record_manual(ticker: str, sleeve: str, *, event: str, weight: float | None = None,
                  price: float | None = None, asof_iso: str | None = None,
                  portfolio_id: str | None = None) -> None:
    """Upsert ONE ledger entry without reconciling the rest of the book.

    update() reconciles against the WHOLE book (anything missing is closed), so it must
    never be called with a partial list. This touches only this one key — used by the
    advisor chat's ad-hoc trades. event: "open"/"add"/"trim"/"close".
    """
    now = _now_iso()
    asof_iso = asof_iso or now[:10]
    ledger = _load(portfolio_id)
    key = f"{sleeve}:{ticker.upper()}"
    entry = ledger.get(key)
    closing = event == "close"
    if entry is None:
        entry = {
            "ticker": ticker.upper(), "sleeve": sleeve,
            "opened_at": now, "open_as_of": asof_iso, "closed_at": None,
            "close_as_of": None, "still_open": not closing, "last_seen": now,
            "entry_weight": weight, "entry_price": price, "current_weight": weight,
            "thesis_id": None, "source": "advisor", "history": [],
        }
        ledger[key] = entry
    if closing:
        entry["still_open"] = False
        entry["closed_at"] = now
        entry["close_as_of"] = asof_iso
        entry["current_weight"] = 0
    else:
        if not entry.get("still_open"):                # re-open
            entry["opened_at"] = now
            entry["open_as_of"] = asof_iso
            entry["closed_at"] = None
            entry["close_as_of"] = None
            entry["entry_weight"] = weight
            entry["entry_price"] = price
        entry["still_open"] = True
        entry["last_seen"] = now
        if weight is not None:
            entry["current_weight"] = weight
    _manual_event: dict = {"event": event, "ts": now, "as_of": asof_iso, "weight": weight, "price": price}
    # An ad-hoc advisor close is a MANUAL exit — tag it structurally (only the close event; adds/trims
    # carry no reason_code, byte-identical to before).
    if closing:
        _manual_event["reason_code"] = "manual"
    entry.setdefault("history", []).append(_manual_event)
    _save(ledger, portfolio_id)


def open_positions(portfolio_id: str | None = None) -> list[dict]:
    """Return open positions shaped for /api/trades 'open' array."""
    ledger = _load(portfolio_id)
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
            "time_stop_by": e.get("time_stop_by"),
            "thesis_id": e.get("thesis_id"),
            # published entry-risk levels (P-NEW-3) — echoed so the audit/API + the stop-breach
            "published_stop": e.get("published_stop"),   # surfacing pass can read them. None on legacy.
            "buy_zone": e.get("buy_zone"),
        })
    # newest first (most recently opened)
    out.sort(key=lambda x: x.get("opened_at") or "", reverse=True)
    return out


def closed_positions(portfolio_id: str | None = None) -> list[dict]:
    """Return closed positions shaped for /api/trades 'closed' array.

    exit_reason is derived from the close history event's 'reason' field when present (stamped
    by either close_position() or update(..., close_reasons=...) — both now write it). Falls back
    to the generic 'removed from book' string so legacy ledger rows (written before this field
    was added) continue to deserialise cleanly.
    """
    ledger = _load(portfolio_id)
    out = []
    for key, e in ledger.items():
        if e.get("still_open"):
            continue
        # Find the most-recent close event to read its reason (if any).
        history = e.get("history") or []
        _last_close = next(
            (ev for ev in reversed(history) if ev.get("event") == "close"),
            None,
        )
        _close_reason = (_last_close or {}).get("reason")
        # Structured code — echoed alongside the human string so the API/audit can machine-filter
        # rotations vs stops. Legacy rows (no reason_code in history) degrade to "unspecified".
        _close_code = (_last_close or {}).get("reason_code") or "unspecified"
        out.append({
            "ticker": e["ticker"],
            "sleeve": e["sleeve"],
            "opened_at": e.get("opened_at"),
            "closed_at": e.get("closed_at"),
            "held_days": _held_days(e.get("opened_at"), e.get("closed_at")),
            "exit_reason": _close_reason or "removed from book",
            "reason_code": _close_code,
        })
    out.sort(key=lambda x: x.get("closed_at") or "", reverse=True)
    return out


def close_position(sleeve: str, ticker: str, asof_iso: str, reason: str = "hard_exit",
                   portfolio_id: str | None = None, reason_code: str | None = None) -> bool:
    """Close a SINGLE open position without touching any other (unlike update(), which closes every
    open key absent from the passed book). Used by the gate-closed hard-exit sweep so a parabolic /
    distress / downtrend name can be exited on a carried-forward day. Returns True if it closed an
    open position.

    reason_code: optional STRUCTURED close code (see REASON_CODES). When omitted it is INFERRED from
    the legacy `reason` string (e.g. 'hard_exit_sweep' → 'hard_veto', 'macro_risk_cap' → 'cap_trim'),
    so existing callers that pass only `reason` still land the correct code without a call-site change.
    An unrecognised string with no explicit code → 'unspecified'. Always additive to the close event."""
    ledger = _load(portfolio_id)
    key = f"{sleeve}:{ticker}"
    entry = ledger.get(key)
    if not entry or not entry.get("still_open"):
        return False
    now = _now_iso()
    entry["still_open"] = False
    entry["closed_at"] = now
    entry["close_as_of"] = asof_iso
    entry["current_weight"] = 0
    entry.setdefault("history", []).append(
        {"event": "close", "ts": now, "as_of": asof_iso, "weight": 0, "price": None,
         "reason": reason, "reason_code": _normalize_code(reason_code, reason)})
    _save(ledger, portfolio_id)
    return True


def get_entry_info(sleeve: str, ticker: str, portfolio_id: str | None = None) -> dict:
    """Return {opened_at, held_days, entry_price} for a given position key, or {} if not found."""
    ledger = _load(portfolio_id)
    key = f"{sleeve}:{ticker}"
    e = ledger.get(key)
    if not e:
        return {}
    return {
        "opened_at": e.get("opened_at"),
        "held_days": _held_days(e.get("opened_at")),
        "entry_price": e.get("entry_price"),
        "time_stop_by": e.get("time_stop_by"),
    }
