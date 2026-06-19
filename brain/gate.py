"""The material-change gate (3-tier cadence; never a continuous loop).

Generalizes the desks' interval_days skip: the brain regenerates a thesis only when
something it cares about moved (regime quad flip, macro_risk band cross, a tracked
prediction coming due, a theme threshold) — otherwise carry yesterday's view forward.
"""
from __future__ import annotations


def state_signature(regime: dict, top_sector: str) -> str:
    band = "lo" if _risk(regime) < 0.34 else "hi" if _risk(regime) > 0.66 else "mid"
    return f"{regime['quad']}|{band}|{regime['liquidity_overlay']}|{top_sector}"


def _risk(regime: dict) -> float:
    mr = regime.get("macro_risk")
    return mr.get("score", 0.4) if isinstance(mr, dict) else 0.4


def should_run(sig: str, prev_run: dict | None, *, interval_days: int = 1, force: bool = False) -> dict:
    """Decide whether to wake the brain. Returns {run, triggers, carried}."""
    triggers = []
    if force:
        triggers.append("event")
    if prev_run is None:
        triggers.append("first_run")
    elif prev_run.get("state_sig") != sig:
        triggers.append("state_change")  # regime / risk-band / liquidity / leadership moved
    return {"run": bool(triggers), "triggers": triggers, "carried": prev_run is not None and not triggers}
