"""Flagship WATCHLIST — the parked-name backstop for the subtract-only entry-timing gate.

A prototype of the first-class WATCH state from the buy-pipeline design
(``docs/design/desk/03-buy-pipeline-and-watchlist.md`` §3.6): a name the engine would otherwise
BUY but whose entry technicals are poor is not bought — it is PARKED here for daily re-review
instead of being force-bought at a bad entry. This module is deliberately minimal: an append-only,
idempotent-per-(ticker, date) JSONL log plus a tiny read API. It owns NO sizing and NEVER touches
prod trading state; the gate in ``bot.phase2`` only ever WITHHOLDS (subtract-only) and records here.

The timing predicate (``timing_withhold``) is the EXACT mirror of the shadow lever
``portfolio.desk_ab.apply_timing_gated`` / ``_timing_ok`` (AB_EXPERIMENT.md §2.4) so the live gate
and the forward A/B arm gate identically: withhold iff
  * extension grade ∈ {stretched, parabolic, extended}  OR the parabolic flag is set, OR
  * pct_vs_200dma >= 30, OR
  * rs < 50, OR
  * urgency == 'avoid', OR
  * eq_grade == 'weak'.
Every field is nullable; a missing field never fires (fail-open) so an absent snapshot can never
silently withhold a name — matching ``_timing_ok``'s fail-open contract.
"""
from __future__ import annotations

import json
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_WATCHLIST = _ROOT / "data" / "portfolios" / "flagship" / "watchlist.jsonl"
# The state snapshot — one row per ACTIVE parked ticker (the re-review state machine, §3.6 of
# docs/design/desk/03-buy-pipeline-and-watchlist.md). Kept SEPARATE from the append-only log above
# so append/latest/for_date stay byte-compatible for the modules that already call them.
_STATE = _ROOT / "data" / "portfolios" / "flagship" / "watchlist_state.jsonl"

# ── re-review state machine constants (the doctrine's TTL / cap rules, §3.6.2) ──
_WATCH = "watch"
_ARMED = "armed"
_EXPIRED = "expired"
_TTL_WATCH = 20          # trading days in WATCH without promotion → EXPIRED (decayed)
_TTL_ARMED = 10          # trading days in ARMED without firing → EXPIRED (decayed)
MAX_WATCH = 40           # cap on the active parked book; lowest-`combined` evicted at the cap

# ── ORIGIN namespaces (the rotation-in park lane, additive) ─────────────────────────────────────
# Every state/log row carries an `origin` tag. LEGACY rows written before this field existed have
# NO `origin` key; they are read back as ORIGIN_TIMING (see `_origin`), so a row persisted by the
# pre-rotation code round-trips byte-identically — the field is inferred on read, never rewritten
# onto a legacy row until that row is next mutated. The two origins occupy SEPARATE caps/TTLs so a
# rotation-in park can never evict a timing park (and vice versa).
ORIGIN_TIMING = "timing"          # the entry-timing withhold lane (the pre-existing behaviour)
ORIGIN_ROTATION = "rotation_in"   # a rotation-in call parked for pre-ignition follow (§ rotation seam)

# Rotation lane caps/TTLs — deliberately LONGER than the timing lane: a sector/name bottoming and
# turning takes longer to confirm than a stretched-entry reset, so a rotation park gets more runway
# before it decays (the operator's "buy on rotation-ins, don't only act on confirmed trades" goal).
MAX_ROTATION_WATCH = 20   # cap on the active rotation-parked book; lowest-`confidence` evicted at cap
_TTL_ROTATION_WATCH = 30  # trading days in WATCH without advancing → EXPIRED (bottoming takes longer)
_TTL_ROTATION_ARMED = 15  # trading days in ARMED without confirming → EXPIRED

# Thresholds — identical to portfolio.desk_ab (the shadow lever) so the two gates never diverge.
_EXT_PCT_VS_200DMA = 30.0           # >= 30% above the 200dma → extended
_WEAK_RS_PCTILE = 50.0              # RS vs SPY below the median → weak relative strength
_BAD_EXT_GRADES = {"stretched", "parabolic", "extended"}
_BAD_URGENCY = {"avoid"}            # entry urgency that says "do not chase"
_BAD_EQ_GRADES = {"weak"}          # entry-quality grade


def timing_withhold(tech: dict | None) -> str | None:
    """Return a human-readable reason iff the name's entry technicals are poor enough to WITHHOLD it,
    else None (keep / buy). `tech` is an ``_entry_tech_fields(ticker)`` dict — every field nullable.
    FAILS OPEN: a None field never fires, so a missing snapshot withholds nothing (mirrors
    ``desk_ab._timing_ok``). Pure; never raises."""
    if not tech:
        return None
    try:
        grade = str(tech.get("eq_grade") or "").lower()
        if grade in _BAD_EXT_GRADES or bool(tech.get("parabolic")):
            return f"extended (grade={grade or 'parabolic'})"
        pv200 = tech.get("pct_vs_200dma")
        if isinstance(pv200, (int, float)) and pv200 >= _EXT_PCT_VS_200DMA:
            return f"extended (pct_vs_200dma={pv200:.0f}>={_EXT_PCT_VS_200DMA:.0f})"
        if str(tech.get("urgency") or "").lower() in _BAD_URGENCY:
            return "entry urgency=avoid"
        rs = tech.get("rs")
        if isinstance(rs, (int, float)) and rs < _WEAK_RS_PCTILE:
            return f"weak RS ({rs:.0f}<{_WEAK_RS_PCTILE:.0f})"
        if grade in _BAD_EQ_GRADES:
            return f"weak entry quality (eq_grade={grade})"
    except Exception:  # noqa: BLE001 — a malformed snapshot must never block the gate
        return None
    return None


def _path() -> Path:
    return _WATCHLIST


def _read_rows() -> list[dict]:
    try:
        return [json.loads(l) for l in _path().read_text().splitlines() if l.strip()]
    except Exception:  # noqa: BLE001
        return []


def _origin(row: dict | None) -> str:
    """The origin lane of a row, defaulting to ORIGIN_TIMING for LEGACY rows with no `origin` key.

    This is the byte-compat read guarantee: a row written before the `origin` field existed has no
    such key, so it is read back as a timing-origin row exactly as before. Only ORIGIN_ROTATION is
    ever persisted explicitly (by `append_rotation`); a stored timing row need not carry the field.
    Pure; never raises — a non-dict / missing key both collapse to ORIGIN_TIMING (fail to the
    pre-existing lane, which is the safe/inert default since the rotation lane is caps-separate)."""
    try:
        o = (row or {}).get("origin")
    except Exception:  # noqa: BLE001
        return ORIGIN_TIMING
    return ORIGIN_ROTATION if o == ORIGIN_ROTATION else ORIGIN_TIMING


def append(ticker: str, asof: str, reason: str, tech: dict | None = None,
           combined: float | None = None) -> bool:
    """Append a withheld-name record, IDEMPOTENT per (ticker, asof): a re-run on the same day for the
    same name replaces (does not duplicate) the row. Returns True if a row was written. Best-effort;
    never raises (the gate must never break the build on a logging failure)."""
    t = (ticker or "").upper().strip()
    if not t or not asof:
        return False
    asof = str(asof)[:10]
    rec = {"ticker": t, "asof": asof, "reason": reason,
           "tech": dict(tech) if isinstance(tech, dict) else None, "combined": combined}
    try:
        rows = [r for r in _read_rows()
                if not ((r.get("ticker") or "").upper() == t and str(r.get("asof"))[:10] == asof)]
        rows.append(rec)
        _path().parent.mkdir(parents=True, exist_ok=True)
        _path().write_text("".join(json.dumps(r, default=str) + "\n" for r in rows))
        return True
    except Exception:  # noqa: BLE001
        return False


def for_date(asof: str) -> list[dict]:
    """Every name parked on `asof` — the daily re-review queue."""
    asof = str(asof)[:10]
    return [r for r in _read_rows() if str(r.get("asof"))[:10] == asof]


def latest() -> list[dict]:
    """The most recent parked record per ticker (the current watchlist for re-review)."""
    by_ticker: dict[str, dict] = {}
    for r in sorted(_read_rows(), key=lambda x: str(x.get("asof"))):
        t = (r.get("ticker") or "").upper()
        if t:
            by_ticker[t] = r
    return sorted(by_ticker.values(), key=lambda x: (x.get("ticker") or ""))


def all_rows() -> list[dict]:
    """The full append-only log (audit / grading source)."""
    return _read_rows()


# ─────────────────────────────────────────────────────────────────────────────
# RE-REVIEW STATE MACHINE — promote / age / expire so parked names don't rot.
#
# A separate snapshot (``_STATE``) holds ONE row per active parked ticker. Each row carries:
#   ticker, asof (first parked), reason, combined, tech, thesis,
#   state ∈ {watch, armed, expired}, last_review (the asof of the last review),
#   days_in_state (trading-day age in the current state).
# The daily build calls ``review(asof, still_withheld=…)`` once. For each active name it asks the
# caller-supplied predicate whether the withhold reason still holds; if it CLEARED the name is
# marked for PROMOTION (re-entry into the desk funnel), else it ages by one trading day and EXPIRES
# past its TTL. ``MAX_WATCH`` is enforced by evicting the lowest-``combined`` active name.
# Everything here is best-effort and NEVER raises — a logging/IO failure must never break the build.
# ─────────────────────────────────────────────────────────────────────────────
def _read_state() -> list[dict]:
    try:
        return [json.loads(l) for l in _STATE.read_text().splitlines() if l.strip()]
    except Exception:  # noqa: BLE001
        return []


def _write_state(rows: list[dict]) -> bool:
    try:
        _STATE.parent.mkdir(parents=True, exist_ok=True)
        _STATE.write_text("".join(json.dumps(r, default=str) + "\n" for r in rows))
        return True
    except Exception:  # noqa: BLE001
        return False


def _seed_from_log(state_by_ticker: dict[str, dict]) -> dict[str, dict]:
    """Back-fill the state snapshot from the append-only log for any parked ticker not yet tracked
    (so names parked by the Gate Officer / L3 timing gate BEFORE this loop existed are picked up).
    A name already EXPIRED in the snapshot is NOT resurrected. Pure; never raises."""
    try:
        for r in latest():
            t = (r.get("ticker") or "").upper().strip()
            if not t or t in state_by_ticker:
                continue
            state_by_ticker[t] = {
                "ticker": t,
                "asof": str(r.get("asof") or "")[:10],
                "reason": r.get("reason"),
                "combined": r.get("combined"),
                "tech": r.get("tech"),
                "thesis": r.get("thesis") or r.get("reason"),
                "state": _WATCH,
                "last_review": None,
                "days_in_state": 0,
            }
    except Exception:  # noqa: BLE001
        pass
    return state_by_ticker


# ─────────────────────────────────────────────────────────────────────────────
# ROTATION-IN park lane (additive; DORMANT — no live call site enrolls yet).
#
# A rotation-in call (from the rotation-calls seam, brain/rotation_intake) is parked here as a
# FIRST-CLASS WATCH/ARMED state row so the desk can hold it through UNCONFIRMED turns — "buy on
# rotation-ins, don't only act on confirmed trades". These rows live in the SAME ``_STATE`` snapshot
# as the timing-origin rows but in a SEPARATE cap/TTL namespace (``origin == ORIGIN_ROTATION``):
#   * they are NEVER aged/expired by the timing ``still_withheld`` predicate,
#   * they are NEVER counted in (or evicted by) the timing ``MAX_WATCH`` eviction, and
#   * a rotation park can never evict a timing park (and vice versa).
# Each rotation row additionally carries ``call_id`` (the immutable join key back to the
# identification call) and ``review_trigger`` (the condition that advances/kills the park — the
# call's falsifier / advancement rule, logged verbatim, never recomputed here).
# ─────────────────────────────────────────────────────────────────────────────
def append_rotation(ticker: str, asof: str, call_id: str, *, target: str | None = None,
                    state: str = _WATCH, confidence: float | None = None,
                    thesis: str | None = None, trigger=None) -> bool:
    """Enroll a ROTATION-origin name as a first-class WATCH/ARMED state row (idempotent per call_id).

    Writes directly into the ``_STATE`` snapshot (a rotation park IS a state row — there is no
    withhold log to seed it from). IDEMPOTENT per ``call_id``: a re-enroll of the same call updates
    the existing row's fields (state/confidence/thesis/trigger/target) WITHOUT resetting its age or
    duplicating it — the ``call_id`` is the immutable join key, so the same underlying turn keeps one
    park row across re-enrolls. A NEW call_id (or a first enroll) creates a fresh WATCH row at
    days_in_state=0. Enforces the rotation cap (``MAX_ROTATION_WATCH``) over rotation rows ONLY,
    evicting the lowest-``confidence`` rotation name — timing rows are never touched.

    Best-effort; NEVER raises (mirrors ``append`` — an enrollment failure must not break a build).
    Returns True iff a row was written. DORMANT today: no live call site invokes this yet.
    """
    t = (ticker or "").upper().strip()
    cid = str(call_id or "").strip()
    if not t or not asof or not cid:
        return False
    asof10 = str(asof)[:10]
    st = state if state in (_WATCH, _ARMED, _EXPIRED) else _WATCH
    try:
        rows = _read_state()
        existing = None
        for r in rows:
            if _origin(r) == ORIGIN_ROTATION and str(r.get("call_id") or "").strip() == cid:
                existing = r
                break
        if existing is not None:
            # re-enroll of the SAME call — refresh fields, keep the row's age/first-seen intact.
            existing["ticker"] = t
            existing["state"] = st
            if confidence is not None:
                existing["confidence"] = confidence
            if thesis is not None:
                existing["thesis"] = thesis
            if trigger is not None:
                existing["review_trigger"] = trigger
            if target is not None:
                existing["target"] = target
        else:
            rows.append({
                "ticker": t,
                "asof": asof10,
                "origin": ORIGIN_ROTATION,
                "call_id": cid,
                "target": target,
                "state": st,
                "confidence": confidence,
                "thesis": thesis or cid,
                "review_trigger": trigger,
                "reason": thesis or f"rotation_in {cid}",
                "combined": None,
                "tech": None,
                "last_review": None,
                "days_in_state": 0,
            })
        # rotation-lane cap — evict lowest-`confidence` ACTIVE rotation rows ONLY (timing untouched).
        rot_active = [r for r in rows
                      if _origin(r) == ORIGIN_ROTATION and r.get("state") != _EXPIRED]
        if len(rot_active) > MAX_ROTATION_WATCH:
            rot_active.sort(key=lambda r: (r.get("confidence") is None,
                                           -(r.get("confidence") or 0.0)))
            for r in rot_active[MAX_ROTATION_WATCH:]:
                r["state"] = _EXPIRED
                r["expire_reason"] = "max_rotation_watch_evicted"
        return _write_state(rows)
    except Exception:  # noqa: BLE001 — enrollment is additive; never break the build
        return False


def _advance_rotation(call_id: str, asof: str, *, state=None, confidence: float | None = None,
                      trigger=None, promote: bool = False) -> dict | None:
    """Advance a rotation park row (WATCH→ARMED on TURNING/evidence; ARMED→promote on CONFIRMED).

    A thin, documented hook for the (later-step) enrollment call sites: it moves an existing
    rotation row along the state ladder and, on ``promote=True`` (the CONFIRMED turn), flags it for
    re-entry into the funnel the same way the timing loop marks a cleared name (``_cleared_today``).
    Resets ``days_in_state`` on a state CHANGE (a new state gets fresh TTL runway). Idempotent
    enough for hand calls; the full CONFIRMED→promote wiring lands with the enrollment step.
    Returns the updated row, or None if the call_id is not an active rotation park. Never raises."""
    cid = str(call_id or "").strip()
    if not cid:
        return None
    try:
        rows = _read_state()
        target_row = None
        for r in rows:
            if _origin(r) == ORIGIN_ROTATION and str(r.get("call_id") or "").strip() == cid \
                    and r.get("state") != _EXPIRED:
                target_row = r
                break
        if target_row is None:
            return None
        if state in (_WATCH, _ARMED) and state != target_row.get("state"):
            target_row["state"] = state
            target_row["days_in_state"] = 0   # fresh TTL runway on a state change
        if confidence is not None:
            target_row["confidence"] = confidence
        if trigger is not None:
            target_row["review_trigger"] = trigger
        if promote:
            target_row["last_review"] = str(asof)[:10]
            target_row["_cleared_today"] = True
        _write_state(rows)
        return target_row
    except Exception:  # noqa: BLE001
        return None


def review(asof: str, *, still_withheld) -> dict:
    """Run the once-per-build-day re-review over every ACTIVE parked name. IDEMPOTENT per
    (ticker, asof): a re-run on the same build day does not double-age or re-promote a name.

    ``still_withheld(ticker) -> reason_or_None`` is supplied by the caller (it re-runs the
    timing/gate check). For each active name:
      * reason CLEARED (predicate → None)  → marked for PROMOTION, state stays (the funnel decides);
      * still withheld                     → age by one trading day; EXPIRE past TTL
                                             (20 td WATCH / 10 td ARMED) with state="expired".
    ``MAX_WATCH`` is enforced AFTER aging by EXPIRING the lowest-``combined`` active names.

    Returns ``{"promote": [...], "expired": [...], "active": [...]}`` (each a list of state rows).
    ROTATION-origin rows are OUT OF SCOPE here — they are neither aged by ``still_withheld`` nor
    counted in the ``MAX_WATCH`` eviction; they are carried through the snapshot untouched (their
    own advancement runs via ``_advance_rotation`` / the enrollment step). Timing behaviour is
    byte-identical to before this lane existed. Best-effort; NEVER raises — returns an empty result
    on any failure."""
    try:
        asof10 = str(asof)[:10]
        all_state = _read_state()
        # Carry ROTATION rows through untouched; only TIMING rows flow through the re-review below.
        rotation_rows = [r for r in all_state if _origin(r) == ORIGIN_ROTATION]
        state_by_ticker = {(r.get("ticker") or "").upper().strip(): r
                           for r in all_state
                           if r.get("ticker") and _origin(r) != ORIGIN_ROTATION}
        state_by_ticker = _seed_from_log(state_by_ticker)

        promote: list[dict] = []
        expired: list[dict] = []
        active: list[dict] = []

        for t, row in state_by_ticker.items():
            if row.get("state") == _EXPIRED:
                expired.append(row)
                continue
            # IDEMPOTENT per (ticker, asof): if we already reviewed this name today, replay its
            # prior outcome WITHOUT re-aging or re-promoting it.
            already_reviewed = str(row.get("last_review") or "")[:10] == asof10
            try:
                reason = None if already_reviewed and row.get("_cleared_today") \
                    else still_withheld(t)
            except Exception:  # noqa: BLE001 — a bad predicate must never break the loop
                reason = row.get("reason")

            if reason is None:
                # the withhold reason CLEARED → promote for re-entry into the funnel this cycle.
                row["last_review"] = asof10
                row["_cleared_today"] = True
                promote.append(row)
                active.append(row)
                continue

            row["_cleared_today"] = False
            row["reason"] = reason
            # age by one trading day only ONCE per build day (idempotent).
            if not already_reviewed:
                row["days_in_state"] = int(row.get("days_in_state") or 0) + 1
            row["last_review"] = asof10
            ttl = _TTL_ARMED if row.get("state") == _ARMED else _TTL_WATCH
            if int(row.get("days_in_state") or 0) > ttl:
                row["state"] = _EXPIRED
                row["expire_reason"] = "decayed"
                expired.append(row)
            else:
                active.append(row)

        # MAX_WATCH eviction — keep the top-MAX_WATCH active names by `combined`, expire the rest
        # (lowest-scored eviction, §3.6.2). A None combined sorts lowest (evicted first).
        if len(active) > MAX_WATCH:
            active.sort(key=lambda r: (r.get("combined") is None,
                                       -(r.get("combined") or 0.0)))
            for row in active[MAX_WATCH:]:
                row["state"] = _EXPIRED
                row["expire_reason"] = "max_watch_evicted"
                expired.append(row)
            active = active[:MAX_WATCH]
            promote = [r for r in promote if r.get("state") != _EXPIRED]

        # persist: active rows + this build's expiries (expired rows are retained one snapshot so
        # the API/grading can see them, then drop on the next review since they're no longer active)
        # + the carried-through ROTATION rows (untouched — a separate lane the timing loop never
        # ages or evicts). Rotation rows are preserved across every timing review.
        snapshot = active + [r for r in expired if r.get("state") == _EXPIRED] + rotation_rows
        _write_state(snapshot)
        return {"promote": promote, "expired": expired, "active": active}
    except Exception:  # noqa: BLE001 — the re-review loop is additive; never break the build
        return {"promote": [], "expired": [], "active": []}


def promote_candidates(asof: str) -> list[dict]:
    """The parked names cleared for re-entry on ``asof`` — each a candidate dict the funnel can feed
    back to the Strategist/PM (skipping re-sourcing, §3.6). Reads the persisted state (so it reflects
    the most recent ``review``). Returns ``[{ticker, combined, thesis, reason, asof}]``. Never raises."""
    try:
        asof10 = str(asof)[:10]
        out: list[dict] = []
        for r in _read_state():
            if r.get("state") == _EXPIRED:
                continue
            if str(r.get("last_review") or "")[:10] == asof10 and r.get("_cleared_today"):
                out.append({
                    "ticker": (r.get("ticker") or "").upper().strip(),
                    "combined": r.get("combined"),
                    "thesis": r.get("thesis") or r.get("reason"),
                    "reason": r.get("reason"),
                    "asof": r.get("asof"),
                })
        return [c for c in out if c["ticker"]]
    except Exception:  # noqa: BLE001
        return []


def state_rows() -> list[dict]:
    """The current re-review state snapshot (one row per tracked ticker) — the source for the
    /api/desk/watchlist surface (state + days-in-state). Never raises."""
    return _read_state()


def origin_of(row: dict | None) -> str:
    """Public accessor for a row's origin lane (ORIGIN_TIMING for a legacy row with no field).

    The byte-compat read contract in one place: any consumer that wants to know a row's lane calls
    this rather than reading the raw key, so a LEGACY row (no `origin`) is uniformly seen as
    ORIGIN_TIMING. Never raises."""
    return _origin(row)


def rotation_rows() -> list[dict]:
    """The ACTIVE rotation-origin park rows (state != expired). The rotation-lane counterpart of the
    timing snapshot: one row per rotation call_id, each carrying call_id + review_trigger + state.
    Never raises."""
    try:
        return [r for r in _read_state()
                if _origin(r) == ORIGIN_ROTATION and r.get("state") != _EXPIRED]
    except Exception:  # noqa: BLE001
        return []
