"""control_plane.packet_gate — DecisionPacket boundary wiring for Brain books (ruling R6).

This module provides the public API every bot book calls at its submission boundary.

Charter constraints
-------------------
- P2:  a rejected packet NEVER increases exposure vs the Brain-errored path.
- P3:  the packet binds at the SUBMISSION BOUNDARY; Brains stay free-form inside.
- P8:  shadow-first — accrue data before growing teeth; enforce-flip requires governance
       event recorded BEFORE the flag is changed (A6).

Three gate modes (MASTERMIND_PACKET_GATE env flag)
---------------------------------------------------
  "off"     Zero behavior change — no packets built, no ledger entries.
  "shadow"  DEFAULT. Build + validate + ledger but NEVER reject. Invalid packets
            are logged only; the book always proceeds as if no packet existed.
  "enforce" Build + validate. Valid → proceed (attach packet_id + packet to the
            decision record). Invalid → record_rejection + run_event(packet_rejected)
            + fall back to the book's existing conservative no-proposal path.

Public API
----------
  gate_mode() → str          "off" | "shadow" | "enforce"
  process(book, submission_dict, prior_book, *, extras=None, run_events_root=None,
          rejections_root=None) → PacketResult

PacketResult
------------
  .ok          bool   True when gate passes (always True in shadow/off; gate logic in enforce)
  .packet      DecisionPacket | None   the built packet (None in off mode or on build error)
  .packet_id   str | None   SHA of packet (from record_rejection ID or derived)
  .errors      list[str]    validation errors (empty when ok)
  .skipped     bool   True in "off" mode (no processing)
  .shadowed    bool   True when mode is "shadow" and the packet was invalid (logged only)
  .rejection_id str | None  from record_rejection (non-None only when enforce + invalid)

Usage (per book)
----------------
    from control_plane.packet_gate import process as _packet_process

    # at the submission boundary, after reading the submission but BEFORE sizing/clamps:
    _pgr = _packet_process(
        PORTFOLIO_ID, submission_dict, prior_book,
        extras={"run_id": brain.get("run_id"), "asof": asof,
                "mandate": "...", "falsifiers": [...], "expected_failure_mode": "..."},
    )
    if not _pgr.ok:
        # enforce mode + invalid packet → fall back to no-proposal path
        # (this branch is NEVER reached in shadow or off mode)
        decided = False
    else:
        # attach packet_id + packet to the decision record (match each book's existing pattern)
        out["packet_id"]   = _pgr.packet_id
        out["packet_meta"] = _pgr.packet.to_dict() if _pgr.packet else None
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger(__name__)

_VALID_MODES = frozenset({"off", "shadow", "enforce"})
_DEFAULT_MODE = "shadow"


def gate_mode() -> str:
    """Return the current gate mode: 'off' | 'shadow' | 'enforce'.

    Reads MASTERMIND_PACKET_GATE env var (case-insensitive).  Any value outside
    {'off', 'shadow', 'enforce'} — including unset — returns the default 'shadow'.
    """
    raw = os.environ.get("MASTERMIND_PACKET_GATE", _DEFAULT_MODE).strip().lower()
    if raw in _VALID_MODES:
        return raw
    return _DEFAULT_MODE


# ---------------------------------------------------------------------------
# PacketResult — the object every bot caller receives
# ---------------------------------------------------------------------------

@dataclasses.dataclass
class PacketResult:
    """Result of gate processing for one submission boundary call."""
    ok:            bool
    packet:        Any          # DecisionPacket | None
    packet_id:     str | None
    errors:        list[str]
    skipped:       bool = False  # True only in "off" mode
    shadowed:      bool = False  # True in "shadow" mode when packet was invalid (logged only)
    rejection_id:  str | None = None

    def to_meta(self) -> dict[str, Any]:
        """Compact dict for embedding in a decisions.jsonl / latest.json meta field."""
        return {
            "packet_id":    self.packet_id,
            "ok":           self.ok,
            "skipped":      self.skipped,
            "shadowed":     self.shadowed,
            "errors":       self.errors[:10] if self.errors else [],  # cap for JSON size
            "rejection_id": self.rejection_id,
        }


# ---------------------------------------------------------------------------
# process — the single entry point for all bot books
# ---------------------------------------------------------------------------

def process(
    book: str,
    submission_dict: dict | None,
    prior_book: dict | None,
    *,
    extras: dict | None = None,
    run_events_root=None,
    rejections_root=None,
) -> "PacketResult":
    """Build, validate, and route one submission boundary packet.

    Parameters
    ----------
    book            : portfolio_id, e.g. "autonomous"
    submission_dict : the raw submission dict (from read_submission() — has 'holdings' key)
    prior_book      : the book's last-known state (paper_account._load_account shape)
    extras          : optional narrative fields for build_packet_from_submission
                      (run_id, asof, mandate, falsifiers, expected_failure_mode, ...)
    run_events_root : redirect run_events writes (for tests)
    rejections_root : redirect record_rejection writes (for tests)

    Returns
    -------
    PacketResult    NEVER raises.  mode='off' → skipped=True, ok=True, packet=None.
    """
    mode = gate_mode()

    # ── OFF: zero behavior change ──────────────────────────────────────────
    if mode == "off":
        return PacketResult(ok=True, packet=None, packet_id=None, errors=[], skipped=True)

    # ── Build packet ───────────────────────────────────────────────────────
    packet = None
    try:
        from control_plane.decision_packet import build_packet_from_submission
        packet = build_packet_from_submission(
            book,
            submission_dict or {},
            prior_book or {},
            extras,
        )
    except Exception as exc:  # noqa: BLE001 — never-raise contract
        log.warning("packet_gate.process build_packet_from_submission failed for %s: %s", book, exc)
        # Shadow: log-only; Enforce: treat as invalid (no packet → all fields missing)
        _evt_id = _append_event({
            "kind": "packet_rejected",
            "book": book,
            "step": "build_packet",
            "status": "error",
            "err": repr(exc)[:300],
            "extra": {"mode": mode, "reason": "build_failed"},
        }, root=run_events_root)
        return PacketResult(ok=(mode == "shadow"), packet=None, packet_id=None,
                            errors=[f"build_packet_from_submission failed: {exc!r}"[:300]],
                            shadowed=(mode == "shadow"))

    # ── Validate ───────────────────────────────────────────────────────────
    ok_validate = False
    errors: list[str] = []
    try:
        from control_plane.decision_packet import validate as _validate
        ok_validate, errors = _validate(packet, prior_book or {})
    except Exception as exc:  # noqa: BLE001
        log.warning("packet_gate.process validate failed for %s: %s", book, exc)
        errors = [f"validate raised: {exc!r}"[:300]]
        ok_validate = False

    packet_id = _derive_packet_id(packet)

    # ── VALID ──────────────────────────────────────────────────────────────
    if ok_validate:
        _append_event({
            "kind": "packet_accepted",
            "book": book,
            "step": "submission_boundary",
            "status": "ok",
            "extra": {
                "mode":       mode,
                "packet_id":  packet_id,
                "run_id":     getattr(packet, "run_id", "") or "",
                "asof":       getattr(packet, "asof", "") or "",
                "risk_direction": getattr(packet, "risk_direction", "") or "",
                "_risk_direction_overridden": getattr(packet, "_risk_direction_overridden", False),
            },
        }, root=run_events_root)
        return PacketResult(ok=True, packet=packet, packet_id=packet_id, errors=[])

    # ── INVALID ────────────────────────────────────────────────────────────
    rejection_id: str | None = None
    try:
        from control_plane.decision_packet import record_rejection
        rejection_id = record_rejection(
            packet.to_dict() if packet is not None else {},
            errors,
            root=rejections_root,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("packet_gate.process record_rejection failed for %s: %s", book, exc)

    _append_event({
        "kind":   "packet_rejected",
        "book":   book,
        "step":   "submission_boundary",
        "status": "warn" if mode == "shadow" else "error",
        "extra": {
            "mode":         mode,
            "packet_id":    packet_id,
            "rejection_id": rejection_id,
            "errors":       errors[:5],   # cap event size
            "n_errors":     len(errors),
            "run_id":       getattr(packet, "run_id", "") if packet else "",
        },
    }, root=run_events_root)

    # SHADOW: log only — never reject (the book ALWAYS proceeds unchanged)
    if mode == "shadow":
        return PacketResult(
            ok=True,
            packet=packet,
            packet_id=packet_id,
            errors=errors,
            shadowed=True,
            rejection_id=rejection_id,
        )

    # ENFORCE: rejection — caller falls back to the conservative no-proposal path (P2)
    return PacketResult(
        ok=False,
        packet=packet,
        packet_id=packet_id,
        errors=errors,
        rejection_id=rejection_id,
    )


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _derive_packet_id(packet) -> str | None:
    """Derive a stable packet_id from its key fields (book + asof + submitted_at)."""
    if packet is None:
        return None
    try:
        raw = "|".join([
            str(getattr(packet, "book",         "") or ""),
            str(getattr(packet, "asof",         "") or ""),
            str(getattr(packet, "submitted_at", "") or ""),
            str(getattr(packet, "run_id",       "") or ""),
        ])
        return hashlib.sha256(raw.encode()).hexdigest()[:16]
    except Exception:  # noqa: BLE001
        return None


def _append_event(event: dict, *, root=None) -> str | None:
    """Append a run_event; never raises."""
    try:
        from control_plane import run_events
        return run_events.append(event, root=root)
    except Exception as exc:  # noqa: BLE001
        log.warning("packet_gate._append_event failed: %s", exc)
        return None
