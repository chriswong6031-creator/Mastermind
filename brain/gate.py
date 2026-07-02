"""The material-change gate (3-tier cadence; never a continuous loop).

Generalizes the desks' interval_days skip: the brain regenerates a thesis only when
something it cares about moved (regime quad flip, macro_risk band cross, a tracked
prediction coming due, a theme threshold) — otherwise carry yesterday's view forward.

A6 — GATE WAKE TRIGGERS (W1)
-----------------------------
state_signature() now appends three new tokens after the original four-field base:
  5. quantized confidence bucket  — round(confidence / 0.15); coarse so routine noise
     does not thrash the gate, but a genuine WEAKENING/low-confidence flip (which may
     move two buckets) does cause a same-day rebuild.
  6. transition_state              — STABLE / WEAKENING / ROLLING / DETERIORATING / …
  7. stable hash of contradicting  — sorted-join of the contradicting-leg list; a new
     contradicting leg (evidence accumulating toward a flip) wakes the rebuild.

Missing fields → the literal token 'na' so old 3-field regime dicts produce a
stable signature that degrades to today's behaviour.

NOTE ON DEPLOY-DAY BEHAVIOUR: the first build after this code ships will compare the
NEW 7-field signature against the OLD 4-field signature stored in the run record — they
can never match, so exactly one extra rebuild fires on deploy day.  This is expected
and harmless; subsequent same-regime runs will match and carry forward as before.

should_run() gains one new trigger:
  'severity_tripwire' — force a run if any derisk artifact for TODAY has severity >= 2.
  The reader is injected (severity_reader kwarg) so tests are fully offline; the
  default reads data/macro_risk/<today>/.  This is purely additive — existing trigger
  logic is unchanged.
"""
from __future__ import annotations

import json
import os
from datetime import date
from pathlib import Path
from typing import Callable


# Repo root — two levels up from brain/gate.py.
_ROOT: Path = Path(__file__).resolve().parent.parent


def _confidence_bucket(regime: dict) -> str:
    """Quantize regime confidence into a coarse bucket to avoid thrashing the gate.

    round(confidence / 0.15) maps the [0, 1] range to buckets 0‥7.  A genuine
    WEAKENING flip (e.g. 0.55 → 0.28) moves two buckets and wakes the rebuild;
    routine ±0.01 noise stays in the same bucket and carries forward.
    Returns 'na' when the confidence field is absent or non-numeric — old 3-field
    regime dicts degrade to today's behaviour (no extra rebuild on schema mismatch).
    """
    val = regime.get("confidence")
    if val is None:
        return "na"
    try:
        return str(round(float(val) / 0.15))
    except (TypeError, ValueError):
        return "na"


def _transition_token(regime: dict) -> str:
    """Return transition_state as a token or 'na' when absent.

    A flip from STABLE → WEAKENING changes this token and wakes a same-day rebuild
    even when the quad/band/liquidity fields are unchanged.
    """
    val = regime.get("transition_state")
    return str(val) if val is not None else "na"


def _contradicting_token(regime: dict) -> str:
    """Return a stable, sorted representation of the contradicting-leg list.

    A new contradicting leg (evidence accumulating toward a regime flip) changes this
    token and wakes a rebuild; order-only changes to the same set do not.
    Returns 'na' when the field is absent so old regime dicts carry forward unchanged.
    """
    val = regime.get("contradicting")
    if val is None:
        return "na"
    if not isinstance(val, (list, tuple)):
        return "na"
    # Sort for stability (the API gives no ordering guarantee); join with '+' as a
    # readable, URL-safe delimiter that cannot appear in leg names.
    return "+".join(sorted(str(x) for x in val)) or "none"


def state_signature(regime: dict, top_sector: str) -> str:
    """Return a stable string that changes when any gate-relevant dimension moves.

    Format (7 pipe-delimited fields):
      <quad>|<risk_band>|<liquidity_overlay>|<top_sector>|<conf_bucket>|<transition>|<contradicting>

    The first four fields are unchanged from the pre-A6 format (backward compatible
    for the INTERVAL/first_run trigger paths).  The three new fields are appended so
    the only behaviour change on old callers is: the signature is longer, which causes
    exactly one extra rebuild on deploy day when the stored old signature mismatches.
    """
    band = "lo" if _risk(regime) < 0.34 else "hi" if _risk(regime) > 0.66 else "mid"
    base = f"{regime.get('quad')}|{band}|{regime.get('liquidity_overlay')}|{top_sector}"
    # A6 extensions — missing fields produce 'na' tokens so old regime files degrade
    # to a stable signature instead of raising KeyError.
    conf_bucket = _confidence_bucket(regime)
    transition  = _transition_token(regime)
    contradicting = _contradicting_token(regime)
    return f"{base}|{conf_bucket}|{transition}|{contradicting}"


def _risk(regime: dict) -> float:
    mr = regime.get("macro_risk")
    return mr.get("score", 0.4) if isinstance(mr, dict) else 0.4


# ---------------------------------------------------------------------------
# Severity tripwire reader (A6 — should_run 'severity_tripwire' trigger)
# ---------------------------------------------------------------------------

def _default_severity_reader(asof: str | None = None) -> int:
    """Return the max tripwire severity found in the macro_risk artifact dir for *asof*.

    Reads data/macro_risk/<asof>/derisk_*.json (read-only, never writes).  Returns 0
    when the directory does not exist, the date is missing, or any file is unparseable.
    Injected as a default so callers in tests can pass a stub without touching the
    filesystem.
    """
    day = asof or date.today().isoformat()
    artifact_dir = _ROOT / "data" / "macro_risk" / day
    if not artifact_dir.is_dir():
        return 0
    worst = 0
    for f in artifact_dir.glob("derisk_*.json"):
        try:
            blob = json.loads(f.read_text())
            sev = ((blob or {}).get("tripwire") or {}).get("severity") or 0
            worst = max(worst, int(sev))
        except Exception:
            pass
    return worst


def should_run(sig: str, prev_run: dict | None, *, interval_days: int = 1, force: bool = False,
               asof: str | None = None,
               severity_reader: Callable[[str | None], int] | None = None) -> dict:
    """Decide whether to wake the brain. Returns {run, triggers, carried}.

    Triggers: force/event, first_run, state_change (regime/risk-band/liquidity/leadership moved),
    the INTERVAL cadence — rebuild at least every `interval_days` even when the regime is unchanged,
    and (A6) 'severity_tripwire' — a same-day force-run if a severity>=2 derisk artifact exists.

    severity_reader: callable(asof) -> int  — injected for tests; defaults to reading the live
    data/macro_risk/<asof>/ directory.  The tripwire trigger is LABELED ('severity_tripwire') so
    run-log consumers can distinguish it from other trigger reasons.
    """
    _sev_reader = severity_reader if severity_reader is not None else _default_severity_reader
    triggers = []
    if force:
        triggers.append("event")
    if prev_run is None:
        triggers.append("first_run")
    else:
        if prev_run.get("state_sig") != sig:
            triggers.append("state_change")
        prev_asof = prev_run.get("asof")
        if prev_asof and "state_change" not in triggers:
            try:
                cur = date.fromisoformat(asof) if asof else date.today()
                if (cur - date.fromisoformat(prev_asof)).days >= max(1, interval_days):
                    triggers.append("interval")  # cadence floor — refresh a stable-regime book
            except Exception:
                pass
    # A6: severity tripwire — fire an extra rebuild when a severity>=2 derisk artifact exists
    # for today regardless of the other trigger conditions.  This is purely additive; it never
    # suppresses an already-scheduled run, and the label lets the run log surface WHY it woke.
    if "severity_tripwire" not in triggers:
        try:
            if _sev_reader(asof) >= 2:
                triggers.append("severity_tripwire")
        except Exception:
            pass  # reader errors must never block a run — degrade gracefully
    return {"run": bool(triggers), "triggers": triggers, "carried": prev_run is not None and not triggers}
