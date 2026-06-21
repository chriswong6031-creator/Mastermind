"""The material-change gate (3-tier cadence; never a continuous loop).

Generalizes the desks' interval_days skip: the brain regenerates a thesis only when
something it cares about moved (regime quad flip, macro_risk band cross, a tracked
prediction coming due, a theme threshold) — otherwise carry yesterday's view forward.
"""
from __future__ import annotations

from datetime import date


def state_signature(regime: dict, top_sector: str) -> str:
    band = "lo" if _risk(regime) < 0.34 else "hi" if _risk(regime) > 0.66 else "mid"
    return f"{regime['quad']}|{band}|{regime['liquidity_overlay']}|{top_sector}"


def _risk(regime: dict) -> float:
    mr = regime.get("macro_risk")
    return mr.get("score", 0.4) if isinstance(mr, dict) else 0.4


def should_run(sig: str, prev_run: dict | None, *, interval_days: int = 1, force: bool = False,
               asof: str | None = None) -> dict:
    """Decide whether to wake the brain. Returns {run, triggers, carried}.

    Triggers: force/event, first_run, state_change (regime/risk-band/liquidity/leadership moved), and
    the INTERVAL cadence — rebuild at least every `interval_days` even when the regime is unchanged, so
    a stable-regime stretch still refreshes confluence / candidates / sizing (the book used to carry
    forward indefinitely). The interval also skips redundant SAME-day re-runs (0 days < interval)."""
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
    return {"run": bool(triggers), "triggers": triggers, "carried": prev_run is not None and not triggers}
