"""Experiment registry — W-L / L6.

Tracks every long-running, accruing experiment the system has open, each with a
comeback date or maturity condition, current status, owner, and artifact paths. The
weekly improvement agenda consumes `matured()` to surface the highest-priority items
that are ready for review but have not yet been judged.

Data lives in ``data/experiments/registry.json``.  The file is the authoritative
source; this module is the read/write contract (never edit the JSON by hand outside
the add/update/resolve functions here).

Design invariants (charter P3/P8):
  - REGISTRY IS APPEND-ONLY for statuses — an experiment can only go OPEN →
    MATURED → JUDGED (or OPEN → CANCELLED), never backwards.
  - Gate thresholds (``gate``) are declared by the program, not by the experiment
    itself.  They are never self-modifiable.
  - ``matured()`` is the ONLY function the agenda calls — it returns items that are
    ready for a human or Opus session to judge, in priority order (earliest
    comeback_date first, then by id).
  - P2: any failure in load/save degrades to an empty list, never raises.

Experiment schema (all fields required in seed; optional fields may be absent in
the JSON and will be filled with safe defaults):

  {
    "id":               str,          # stable slug, e.g. "shadow-trim-ladder"
    "what":             str,          # one-sentence description
    "gate":             str,          # the condition that flips status → matured
    "comeback_date":    str | null,   # ISO date: when to re-check; null = condition-only
    "maturity_condition": str,        # prose description of the objective test
    "status":           str,          # open | matured | judged | cancelled
    "owner":            str,          # opus-session | fable-review | self-tune
    "artifact_paths":  [str],         # relative paths where evidence lives
    "notes":            str           # free-form (may be "")
  }
"""
from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent.parent
_REGISTRY_PATH = _ROOT / "data" / "experiments" / "registry.json"

# Valid status transitions (OPEN is the initial state).
_TRANSITIONS: dict[str, set[str]] = {
    "open":     {"matured", "cancelled"},
    "matured":  {"judged", "open"},        # re-open if judged prematurely
    "judged":   set(),                     # terminal
    "cancelled": set(),                    # terminal
}


# ── load / save ───────────────────────────────────────────────────────────────

def _load() -> list[dict]:
    """Load registry.json → list[dict].  Returns [] on any failure (P2)."""
    try:
        if not _REGISTRY_PATH.exists():
            return []
        raw = _REGISTRY_PATH.read_text()
        data = json.loads(raw)
        if isinstance(data, list):
            return data
        # support {experiments: [...]} envelope
        if isinstance(data, dict):
            return data.get("experiments") or []
        return []
    except Exception:  # noqa: BLE001
        return []


def _save(experiments: list[dict]) -> bool:
    """Persist the registry.  Returns True on success."""
    try:
        _REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
        _REGISTRY_PATH.write_text(json.dumps(experiments, indent=2, default=str))
        return True
    except Exception:  # noqa: BLE001
        return False


def _defaults(exp: dict) -> dict:
    """Fill missing optional fields with safe defaults so callers never KeyError."""
    exp.setdefault("comeback_date", None)
    exp.setdefault("maturity_condition", exp.get("gate", ""))
    exp.setdefault("status", "open")
    exp.setdefault("owner", "opus-session")
    exp.setdefault("artifact_paths", [])
    exp.setdefault("notes", "")
    return exp


# ── public API ────────────────────────────────────────────────────────────────

def load() -> list[dict]:
    """Return all experiments (with safe defaults filled in)."""
    return [_defaults(dict(e)) for e in _load()]


def get(experiment_id: str) -> dict | None:
    """Return one experiment by id, or None if not found."""
    for exp in load():
        if exp.get("id") == experiment_id:
            return exp
    return None


def add(experiment: dict) -> bool:
    """Append a new experiment.  Returns False if id already exists or save fails."""
    try:
        eid = experiment.get("id")
        if not eid:
            return False
        all_exps = _load()
        if any(e.get("id") == eid for e in all_exps):
            return False
        all_exps.append(_defaults(dict(experiment)))
        return _save(all_exps)
    except Exception:  # noqa: BLE001
        return False


def update(experiment_id: str, **fields: Any) -> bool:
    """Update allowed fields on an existing experiment.  Status field is validated.

    NEVER allowed to update: id, gate, maturity_condition (these are set at creation
    time and are effectively immutable — only Fable-review sessions may change them
    by editing the seed directly).
    """
    _IMMUTABLE = {"id", "gate", "maturity_condition"}
    try:
        all_exps = _load()
        for i, exp in enumerate(all_exps):
            if exp.get("id") == experiment_id:
                for k, v in fields.items():
                    if k in _IMMUTABLE:
                        continue
                    if k == "status":
                        current = exp.get("status", "open")
                        allowed = _TRANSITIONS.get(current, set())
                        if v not in allowed:
                            return False          # invalid transition
                    exp[k] = v
                all_exps[i] = exp
                return _save(all_exps)
        return False                              # not found
    except Exception:  # noqa: BLE001
        return False


def resolve(experiment_id: str, verdict: str, notes: str = "") -> bool:
    """Mark an experiment JUDGED with a verdict note.

    ``verdict`` is free-form prose stored in the experiment's ``notes`` field
    (prepended with the date).  This is the official closure call; after this
    the experiment is terminal and cannot be re-opened by the registry API.
    """
    try:
        dated_note = f"[{date.today().isoformat()} JUDGED] {verdict}"
        all_exps = _load()
        for i, exp in enumerate(all_exps):
            if exp.get("id") == experiment_id:
                current = exp.get("status", "open")
                if "judged" not in _TRANSITIONS.get(current, set()):
                    return False                  # already terminal or wrong path
                exp["status"] = "judged"
                prior = (exp.get("notes") or "").strip()
                exp["notes"] = f"{prior}\n{dated_note}".strip()
                all_exps[i] = exp
                return _save(all_exps)
        return False
    except Exception:  # noqa: BLE001
        return False


def matured(as_of: date | None = None) -> list[dict]:
    """Return all experiments that are ready for review, in priority order.

    An experiment is *matured* when:
      (a) its status == "matured" already (previously flagged), OR
      (b) its status == "open" AND its comeback_date is not null AND the date
          has been reached (as_of >= comeback_date).

    Items where (b) applies are promoted to status="matured" and saved before
    being returned, so the agenda never re-surface a newly-matured item twice
    without human acknowledgement.

    Priority order: matured items first, sorted by comeback_date ASC (earliest
    deadline first); then by id as a tiebreaker.  Items with no comeback_date
    sort last within their status tier.
    """
    as_of = as_of or date.today()
    all_exps = _load()
    changed = False
    # promote open items whose comeback_date has arrived
    for i, exp in enumerate(all_exps):
        if exp.get("status") != "open":
            continue
        cd = exp.get("comeback_date")
        if not cd:
            continue
        try:
            cd_date = date.fromisoformat(str(cd)[:10])
        except Exception:  # noqa: BLE001
            continue
        if as_of >= cd_date:
            all_exps[i]["status"] = "matured"
            changed = True
    if changed:
        _save(all_exps)
    # collect all matured (not yet judged/cancelled)
    results = [_defaults(dict(e)) for e in all_exps if e.get("status") == "matured"]
    # sort: by comeback_date ASC (None sorts last), then by id
    def _sort_key(e: dict) -> tuple:
        cd = e.get("comeback_date")
        if cd:
            try:
                return (0, str(cd)[:10], e.get("id") or "")
            except Exception:  # noqa: BLE001
                pass
        return (1, "", e.get("id") or "")
    return sorted(results, key=_sort_key)


def summary() -> dict:
    """A compact summary suitable for the agenda and the /api/experiments endpoint.

    Returns {total, open, matured, judged, cancelled, matured_items: [...],
             as_of: str}.  Never raises.
    """
    try:
        all_exps = load()
        by_status: dict[str, int] = {}
        for e in all_exps:
            s = e.get("status", "open")
            by_status[s] = by_status.get(s, 0) + 1
        mat = matured()
        return {
            "as_of": date.today().isoformat(),
            "total": len(all_exps),
            "open": by_status.get("open", 0),
            "matured": by_status.get("matured", 0),
            "judged": by_status.get("judged", 0),
            "cancelled": by_status.get("cancelled", 0),
            "matured_items": mat,
        }
    except Exception:  # noqa: BLE001
        return {"as_of": date.today().isoformat(), "total": 0, "open": 0,
                "matured": 0, "judged": 0, "cancelled": 0, "matured_items": []}
