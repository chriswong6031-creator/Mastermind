"""control_plane.governance — append-only authority-change ledger for Mastermind.

Writes to data/governance/governance.jsonl (one JSON object per line).

DISTINCTION FROM run_events.jsonl
----------------------------------
``run_events.jsonl``  — operational telemetry: job starts/finishes, guardrail
    triggers, lock conflicts, step failures.  Written by every scheduled job,
    every guardrail check, every lock contention.  High-frequency; records WHAT
    THE SYSTEM DID.

``governance.jsonl``  — authority-change audit trail: flag-state diffs, doctrine
    hash changes, experiment maturations, book lifecycle recommendations, posture
    governor arming, operator-script actions.  Low-frequency; records WHY THE
    SYSTEM'S AUTHORITY OR CONFIGURATION CHANGED.  Schema is a strict superset of
    the NW governance event contract so the two systems remain compatible.

NEVER-RAISE CONTRACT
--------------------
``append()`` catches all exceptions, logs a warning, and returns None.
A governance-log failure must never abort the operation that triggered it.
This mirrors the never-raise contract in
``control_plane.run_events`` and ``vendor/macro_src/engine/neuralweb/governance.py``.

SCHEMA (NW-schema-compatible — field names match the Neural Web governance contract)
-------------------------------------------------------------------------------------
Required:
    ts              (str)  UTC ISO-8601 isoformat(timespec="seconds") — injected
    event_id        (str)  SHA-256[:16] of canonical key tuple — injected
    event_type      (str)  stable event kind, e.g. "flag_changed", "doctrine_changed"
    target          (str)  subject of the authority change (flag name, experiment id, book …)
    actor           (str)  who/what made the change: "system", "operator-script", …
    reason          (str)  human-readable rationale

Optional:
    before          (Any)  prior state / value (masked secrets appear as "<set>")
    after           (Any)  new state / value
    rollback        (str)  how to undo this change (REQUIRED by program spec; "" when N/A)
    source_artifact (str)  path / job / script that generated the event

EVENT-ID CONTRACT
-----------------
``event_id`` is the first 16 hex digits of SHA-256 over the canonical-JSON
representation of (ts, event_type, target).  Deterministic given those three
fields.  Two same-second events with the same (ts, event_type, target) collide
— both rows are still written (append-only ledger); consumers must not dedupe.

SECRETS LAW
-----------
Secret-bearing flag values must never reach the ledger.  ``flags.py`` masks them
to ``"<set>"``.  A ``"<set>"``-to-``"<set>"`` transition on the same key is NOT
a change; ``append_flag_diff()`` skips it.
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# path resolution
# ---------------------------------------------------------------------------

def _ledger_path(root: str | Path | None = None) -> Path:
    """Resolve data/governance/governance.jsonl, creating parent dirs as needed."""
    if root is None:
        base = Path(__file__).resolve().parent.parent / "data"
    else:
        base = Path(root) / "data"
    p = base / "governance" / "governance.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


# ---------------------------------------------------------------------------
# event-id
# ---------------------------------------------------------------------------

def _event_id(ts: str, event_type: str, target: str) -> str:
    """SHA-256[:16] of canonical (ts|event_type|target) — deterministic."""
    raw = f"{ts}|{event_type}|{target}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------

def append(
    event: dict[str, Any],
    *,
    root: str | Path | None = None,
) -> str | None:
    """Append one governance event to data/governance/governance.jsonl.

    Parameters
    ----------
    event : dict
        Caller-supplied payload.  ``ts`` and ``event_id`` are injected
        automatically; any caller-supplied ``ts`` is overridden.
        Required keys: event_type, target, actor, reason.
        Optional: before, after, rollback, source_artifact.
    root : str | Path | None
        Project root override (None = resolve relative to this file).
        Used in tests to redirect writes to a tmp dir.

    Returns
    -------
    str | None
        The ``event_id`` of the appended record, or None on any error.
        NEVER raises.
    """
    try:
        ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
        event_type = str(event.get("event_type", "") or "")
        target = str(event.get("target", "") or "")
        eid = _event_id(ts, event_type, target)

        row: dict[str, Any] = {
            "ts":             ts,
            "event_id":       eid,
            "event_type":     event_type,
            "target":         target,
            "actor":          str(event.get("actor", "system") or "system"),
            "reason":         str(event.get("reason", "") or ""),
            "rollback":       str(event.get("rollback", "") or ""),
            "source_artifact": str(event.get("source_artifact", "") or ""),
        }
        # Optional before/after — include when key is explicitly present in the caller's event
        # (None is a valid value meaning "was absent/unset"; preserve it as null in the JSONL).
        _sentinel = object()
        b = event.get("before", _sentinel)
        a = event.get("after", _sentinel)
        if b is not _sentinel:
            row["before"] = b
        if a is not _sentinel:
            row["after"] = a

        # Remove empty-string optionals (keep ts/event_id/event_type/target/actor/reason always,
        # and before/after are kept verbatim — even None — when they were supplied).
        _always_keep = {"ts", "event_id", "event_type", "target", "actor", "reason",
                        "before", "after"}
        row = {
            k: v for k, v in row.items()
            if k in _always_keep or (v is not None and v != "")
        }
        row["ts"] = ts
        row["event_id"] = eid

        p = _ledger_path(root)
        line = json.dumps(row, separators=(",", ":"), default=str)
        with p.open("a") as fh:
            fh.write(line + "\n")
        return eid

    except Exception as exc:  # noqa: BLE001
        log.warning("control_plane.governance.append failed: %s", exc)
        return None


# ---------------------------------------------------------------------------
# flag-diff helper
# ---------------------------------------------------------------------------

def append_flag_diff(
    before: dict[str, str],
    after: dict[str, str],
    *,
    root: str | Path | None = None,
    source_artifact: str = "startup",
) -> list[str]:
    """Diff two flag snapshots and emit one ``flag_changed`` event per change.

    Rules:
      - A key present in ``after`` but not ``before`` → added.
      - A key present in ``before`` but not ``after`` → removed.
      - A key present in both with a different value → changed.
      - A ``"<set>"``-to-``"<set>"`` transition on the same key is NOT a change
        (the value was masked in both snapshots; we can't tell if it truly changed).

    Returns the list of event_ids emitted (one per changed flag, or [] if none).
    Never raises.
    """
    try:
        all_keys = set(before) | set(after)
        emitted: list[str] = []
        for key in sorted(all_keys):
            b = before.get(key)
            a = after.get(key)
            if b == a:
                continue
            # Skip "<set>"-to-"<set>": value was masked in both snapshots
            if b == "<set>" and a == "<set>":
                continue
            if b is None:
                reason = f"flag {key} added at startup"
                rollback = f"unset env var {key}"
            elif a is None:
                reason = f"flag {key} removed (not present in current env)"
                rollback = f"restore env var {key}={b}"
            else:
                reason = f"flag {key} changed value"
                rollback = f"restore env var {key}={b}"
            eid = append(
                {
                    "event_type": "flag_changed",
                    "target": key,
                    "actor": "system",
                    "reason": reason,
                    "before": b,
                    "after": a,
                    "rollback": rollback,
                    "source_artifact": source_artifact,
                },
                root=root,
            )
            if eid:
                emitted.append(eid)
        return emitted
    except Exception as exc:  # noqa: BLE001
        log.warning("control_plane.governance.append_flag_diff failed: %s", exc)
        return []


# ---------------------------------------------------------------------------
# run_events tail reader — used by the flag-diff emitter at startup
# ---------------------------------------------------------------------------

def _last_startup_flags(root: str | Path | None = None) -> dict[str, str] | None:
    """Read the ``flags`` dict from the most recent ``app_started`` event in
    ``run_events.jsonl``.  Returns None when no prior startup event is found or
    on any read error (first boot, corrupt ledger, …).  Never raises."""
    try:
        from control_plane.run_events import _ledger_path as _re_path
        p = _re_path(root)
        if not p.exists():
            return None
        lines = p.read_text().splitlines()
        for line in reversed(lines):
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
                if ev.get("kind") == "app_started":
                    flags = (ev.get("extra") or {}).get("flags")
                    if isinstance(flags, dict):
                        return flags
            except Exception:  # noqa: BLE001
                continue
        return None
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# doctrine-hash helper
# ---------------------------------------------------------------------------

_DOCTRINE_HASH_PATH_REL = Path("data") / "governance" / "doctrine_hash.json"


def _doctrine_hash_path(root: str | Path | None = None) -> Path:
    base = Path(root) if root is not None else Path(__file__).resolve().parent.parent
    return base / _DOCTRINE_HASH_PATH_REL


def check_doctrine_hash(
    *,
    root: str | Path | None = None,
    doctrine_path: str | Path | None = None,
) -> str | None:
    """Hash config/doctrine.yml; compare vs the last recorded hash; emit
    ``doctrine_changed`` if different.  Persists the new hash regardless.

    Returns the event_id if a change was detected and emitted, or None when
    the hash is unchanged / on any error.  Never raises."""
    try:
        import hashlib as _hl

        base = Path(root) if root is not None else Path(__file__).resolve().parent.parent
        doc = Path(doctrine_path) if doctrine_path is not None else (base / "config" / "doctrine.yml")

        if not doc.exists():
            return None

        text = doc.read_bytes()
        current_hash = _hl.sha256(text).hexdigest()

        hp = _doctrine_hash_path(root)
        hp.parent.mkdir(parents=True, exist_ok=True)

        prior_hash: str | None = None
        if hp.exists():
            try:
                data = json.loads(hp.read_text())
                prior_hash = str(data.get("hash") or "")
            except Exception:  # noqa: BLE001
                prior_hash = None

        # Always persist the current hash (even if unchanged)
        hp.write_text(json.dumps({"hash": current_hash, "ts": datetime.now(timezone.utc).isoformat(timespec="seconds")}, indent=2))

        if prior_hash and prior_hash == current_hash:
            return None

        if prior_hash is None:
            return None  # first boot — no prior to diff against; just record

        eid = append(
            {
                "event_type": "doctrine_changed",
                "target": "config/doctrine.yml",
                "actor": "system",
                "reason": "doctrine.yml SHA-256 hash changed since last startup",
                "before": prior_hash,
                "after": current_hash,
                "rollback": "restore the prior config/doctrine.yml from git",
                "source_artifact": "startup",
            },
            root=root,
        )
        return eid
    except Exception as exc:  # noqa: BLE001
        log.warning("control_plane.governance.check_doctrine_hash failed: %s", exc)
        return None
