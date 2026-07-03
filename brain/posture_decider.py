"""brain/posture_decider.py — THE POSTURE DECIDER (SHADOW) — W-E.2 task E2.1.

DESIGN (judged_posture.md — Design 3 winner)
--------------------------------------------
The single posture object decided from market_view BEFORE name selection, subsuming
budget()/fragility_signal()/W-I damp.  Publishes ``data/posture/latest.json`` + a
dated copy EVERY BUILD, regardless of the arming flag, so the shadow window accrues
from day one.

WAVE CONTRACT — MASTERMIND_POSTURE_DECIDER defaults '0' throughout.
* Flag OFF (default / shadow): the artifact publishes, NO sizing path reads it.
  ``regime_frame.budget()`` returns the same value as today; ``fragility_signal()``
  is still the def_sleeve sizer; ``derisk.eff_cap`` composes exactly two terms.
* Flag ON (armed — E3.3, a Fable decision, NOT this wave): confidence/transition move
  INSIDE decide(); the W-I evidence damp is REMOVED from budget(); fragility_signal
  retires to the fallback; eff_cap gains the posture_notch_cap via min().

ARCHITECTURE (the split brain)
-------------------------------
OFFENSE side: the W2 budget equation clamp(0.40+0.20·conf·T·F, .40, .60) lives
VERBATIM inside decide() — byte-identical to today's budget() on every calm tape,
so the existing golden tests stay green.

DEFENSE/CLASS side: defense_pressure D = availability-renormalizing EQUAL-WEIGHT
MEAN over the shrink-biased planes. Missing planes drop out of the denominator
(renormalize = missing plane never defaults to 0-risk). Equal-weight PINNED in v1 —
adaptive per-plane weights are amputated from v1 (cannot clear P3 in 4 weeks;
posture_governor stays the only learner).

Planes of D (eight sources, each tri-state 0=benign / 0.5=caution / 1=max-pressure):
  P1. regime_fragility  — (1 - confidence), capped [0,1]
  P2. transition_tilt   — {WEAKENING:0.6, ROLLING/DETERIORATING:0.4, else 0.0}
  P3. flip_fragility    — 1.0 if flip_margin < 0.15 else 0.0
  P4. dwell_level       — from risk_state {risk_on:0, caution:0.5, risk_off:1}
  P5. distribution_tells — from evidence nowcast_doubt
  P6. liquidity_quality  — from evidence liquidity_stress
  P7. regime_nowcast     — from evidence (shrink-biased; ADVISORY)
  P8. defensive_rs_cross — from evidence defensive_rs_cross

D = mean(available planes).  Available = not None.  If ALL planes absent → D = 0.5
(degrade to BALANCED, never to 0-risk).

CLASS BANDS (from D):
  D < 0.25   → OFFENSE
  D < 0.50   → BALANCED
  D < 0.75   → ROTATE_DEFENSIVE
  D >= 0.75  → PRESERVE

HYSTERESIS (mirrors macro_risk dwell + tripwire clamp):
  Escalate same-session.
  De-escalate = 3 consecutive lower-band builds AND no sev>=2 tripwire in 2 sessions.
  Max-dwell auto-release: 15 sessions (PRESERVE cannot become a trap).
  State in data/posture/state.json; degrade-to-stateless on corruption.

ANTI-COMPOUNDING (W2's lesson, the reason the subsumption exists):
  Every signal consumed EXACTLY ONCE.  When armed: the evidence planes feed D and
  ONLY D; the offense equation reads conf/transition exactly once (inside decide());
  fragility_signal is retired.  The grep-gate CI test (test_posture_migration.py
  TestRow4GrepGate) is the ship-blocker.

NEVER RAISES: every path degrades gracefully to BALANCED / midpoint.

PUBLIC API
----------
* ``posture_flag() -> bool``          — True when MASTERMIND_POSTURE_DECIDER = 1.
* ``decide(region, *, evidence, risk_state) -> dict``  — compute the posture (never raises).
* ``build(region, *, evidence, risk_state, write=True) -> dict`` — decide + atomic publish.
* ``latest() -> dict | None``         — read the last published artifact (never raises).
* ``render_directive(asof) -> str``   — the POSTURE block for LLM prompts (E2.5).

ARTIFACT SCHEMA (posture.v1)
-----------------------------
{
  schema_version: "posture.v1",
  asof:           "YYYY-MM-DD",
  built_at:       "ISO-8601",
  shadow:         bool,          # True when the flag is OFF (shadow mode)
  posture_class:  "OFFENSE" | "BALANCED" | "ROTATE_DEFENSIVE" | "PRESERVE",
  offense_budget: float,         # the clamped W2 equation result [0.40, 0.60]
  defense_floor:  float,         # DEF_SLEEVE_MAX * D (0.0 when armed ceiling not set)
  cash_floor:     float,
  conviction_appetite: float,    # [0, 1] — high D lowers appetite
  posture_notch_cap:  float | None,  # eff_cap ceiling when flag is ON (None in shadow)
  defense_pressure: float,       # D [0, 1]
  planes:         {name: {value, available}},
  offense_inputs: {confidence, T, F, lead_budget_raw},
  hysteresis:     {class_raw, class_held, sessions_in_class, deescalate_count, ...},
  why:            str,           # one-line evidence trail for the POSTURE block
  shrink_provenance: str,        # "defense_D" (the single pathway token, row 5)
  evidence_trail: list[str],
}
"""
from __future__ import annotations

import json
import os
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Optional

# ─────────────────────────────────────────────────────────────────────────────
# geometry + constants
# ─────────────────────────────────────────────────────────────────────────────

_ROOT: Path = Path(__file__).resolve().parent.parent
_ARTIFACT_DIR: Path = _ROOT / "data" / "posture"
_LATEST_PATH: Path = _ARTIFACT_DIR / "latest.json"
_STATE_PATH: Path = _ARTIFACT_DIR / "state.json"

_SCHEMA_VERSION = "posture.v1"

# Class bands (D thresholds — pinned equal-weight, from judged_posture.md §synthesis)
_BAND_OFFENSE = 0.25
_BAND_BALANCED = 0.50
_BAND_ROTATE_DEF = 0.75

# Offense equation constants (verbatim from doctrine budget: block / W2 regime_frame.budget())
_BUDGET_BASE = 0.40
_BUDGET_SLOPE = 0.20
_BUDGET_FLOOR = 0.40
_BUDGET_CEIL = 0.60
_BUDGET_MIDPOINT = 0.50
_FLIP_MARGIN_MIN = 0.15
_TRANSITION_MULT = {"WEAKENING": 0.6, "ROLLING": 0.4, "DETERIORATING": 0.4}

# Defense plane P2 tilt values
_TRANS_TILT = {"WEAKENING": 0.6, "ROLLING": 0.4, "DETERIORATING": 0.4}

# Risk_state dwell → plane value
_DWELL_LEVEL = {"risk_on": 0.0, "caution": 0.5, "risk_off": 1.0}

# DEF_SLEEVE_MAX for the defense_floor computation (0.0 = inert, the default control arm)
_DEF_SLEEVE_MAX_DEFAULT = 0.0
# the armed-sleeve ceiling (mirror of def_sleeve.armed_ceiling) — used ONLY to record the WOULD-BE
# defense_floor (armed_ceiling · D) in the shadow trail while the live sleeve is inert (max 0.0).
_DEF_SLEEVE_CEILING = 0.35

# Hysteresis parameters (mirrors macro_risk dwell)
_DEESCALATE_SESSIONS = 3       # consecutive lower-band builds required to step down
_TRIPWIRE_CLAMP_SESSIONS = 2   # sev>=2 tripwire within this many sessions BLOCKS de-escalation
_MAX_DWELL_SESSIONS = 15       # auto-release one step if dwell exceeds this

# Per-class OFFENSE CEILING — the max leadership budget a class permits.  MIN-COMPOSED with the
# verbatim W2 equation (a class can only SHRINK the equation's output toward the floor, NEVER raise
# it — the anti-compounding law, ledger row 5: when the flag arms the damp is removed from the
# equation and the evidence's shrink lives ONCE on the defense side, so a defensive class re-imposes
# the shrink here as a CEILING rather than double-counting the damp).  OFFENSE's ceiling is the W2
# clamp ceiling (0.60) so a calm tape's budget is byte-identical to today; the defensive classes'
# ceilings pin the incident-replay band (ROTATE-DEFENSIVE offense 0.40-0.45, PRESERVE at the floor).
# All values (unverified-prior) live in doctrine posture.class_map.offense_cap.
_OFFENSE_CAP = {
    "OFFENSE": 0.60,
    "BALANCED": 0.55,
    "ROTATE_DEFENSIVE": 0.43,
    "PRESERVE": 0.40,
}

# Conviction appetite per class
_APPETITE = {
    "OFFENSE": 1.0,
    "BALANCED": 0.75,
    "ROTATE_DEFENSIVE": 0.50,
    "PRESERVE": 0.25,
}

# Cash floor per class
_CASH_FLOOR = {
    "OFFENSE": 0.05,
    "BALANCED": 0.10,
    "ROTATE_DEFENSIVE": 0.15,
    "PRESERVE": 0.25,
}

# Posture notch cap per class — the eff_cap CEILING min-composed into bot/derisk.py ONLY when the flag
# is ON (that wiring is E2.2).  ALWAYS recorded in the shadow artifact (build_plan §4.4 asserts a
# notch cap of 0.70 on the 06-26 SHADOW build).  OFFENSE/BALANCED map to 1.00 = a no-op ceiling
# (min(x, 1.00) == x → no notch); the defensive classes impose a real ceiling.
_NOTCH_CAP = {
    "OFFENSE": 1.00,
    "BALANCED": 0.90,
    "ROTATE_DEFENSIVE": 0.70,
    "PRESERVE": 0.55,
}


# ─────────────────────────────────────────────────────────────────────────────
# arming flag
# ─────────────────────────────────────────────────────────────────────────────

def posture_flag() -> bool:
    """True when MASTERMIND_POSTURE_DECIDER=1 (armed). Default OFF throughout W-E.2."""
    return os.environ.get("MASTERMIND_POSTURE_DECIDER", "0").strip().lower() in (
        "1", "true", "yes", "on"
    )


# ─────────────────────────────────────────────────────────────────────────────
# doctrine config (degrade-safe)
# ─────────────────────────────────────────────────────────────────────────────

def _cfg() -> dict:
    """Read doctrine's posture / budget / def_sleeve blocks; degrade-safe to priors."""
    try:
        from bot.doctrine_config import load_doctrine
        doc = load_doctrine() or {}
        return {
            "budget": doc.get("budget") or {},
            "def_sleeve": doc.get("def_sleeve") or {},
            "posture": doc.get("posture") or {},
        }
    except Exception:  # noqa: BLE001
        return {"budget": {}, "def_sleeve": {}, "posture": {}}


def _class_lever(posture_class: str, key: str, prior_map: dict[str, Any]) -> Any:
    """Read one per-class lever from doctrine posture.class_map.<class>.<key>, degrade-safe to the
    mirrored prior.  A missing/malformed value degrades to the prior (the conservative shipped value)
    — a bad config can never LOOSEN a defensive class's lever below its prior discipline."""
    prior = prior_map.get(posture_class)
    try:
        cm = (_cfg()["posture"].get("class_map") or {}).get(posture_class) or {}
        v = cm.get(key)
        if v is not None:
            return float(v)
    except Exception:  # noqa: BLE001
        pass
    return prior


def _offense_cap(posture_class: str) -> float:
    """The class's OFFENSE CEILING (doctrine posture.class_map.<class>.offense_cap; prior fallback)."""
    v = _class_lever(posture_class, "offense_cap", _OFFENSE_CAP)
    return float(v) if v is not None else _BUDGET_CEIL


def _bands() -> tuple[float, float, float]:
    """The class band edges (balanced / rotate_defensive / preserve) from doctrine, degrade-safe to the
    pinned priors.  A malformed band degrades to its prior — a bad config can never WIDEN the OFFENSE
    band (the priors are the conservative shipped edges)."""
    b_balanced, b_rot, b_pres = _BAND_OFFENSE, _BAND_BALANCED, _BAND_ROTATE_DEF
    try:
        bands = _cfg()["posture"].get("bands") or {}
        if bands.get("balanced") is not None:
            b_balanced = float(bands["balanced"])
        if bands.get("rotate_defensive") is not None:
            b_rot = float(bands["rotate_defensive"])
        if bands.get("preserve") is not None:
            b_pres = float(bands["preserve"])
    except Exception:  # noqa: BLE001
        pass
    return b_balanced, b_rot, b_pres


def _hysteresis_cfg() -> dict[str, int]:
    """The hysteresis params (deescalate_consecutive / tripwire_clamp_sessions / max_dwell_sessions)
    from doctrine posture.hysteresis, degrade-safe to the mirrored priors."""
    cfg = {
        "deescalate_consecutive": _DEESCALATE_SESSIONS,
        "tripwire_clamp_sessions": _TRIPWIRE_CLAMP_SESSIONS,
        "max_dwell_sessions": _MAX_DWELL_SESSIONS,
    }
    try:
        hy = _cfg()["posture"].get("hysteresis") or {}
        for k in cfg:
            if hy.get(k) is not None:
                cfg[k] = int(hy[k])
    except Exception:  # noqa: BLE001
        pass
    return cfg


def _tripwire_clamp_severity() -> int:
    """The minimum derisk tripwire severity that CLAMPS de-escalation (doctrine
    posture.hysteresis.tripwire_clamp_severity; prior fallback 2 — mirrors macro_risk)."""
    try:
        hy = _cfg()["posture"].get("hysteresis") or {}
        if hy.get("tripwire_clamp_severity") is not None:
            return int(hy["tripwire_clamp_severity"])
    except Exception:  # noqa: BLE001
        pass
    return 2


def _def_sleeve_max() -> float:
    """DEF_SLEEVE_MAX from doctrine (0.0 default = inert control arm)."""
    try:
        v = _cfg()["def_sleeve"].get("max")
        if v is not None:
            return float(v)
    except Exception:  # noqa: BLE001
        pass
    return _DEF_SLEEVE_MAX_DEFAULT


# ─────────────────────────────────────────────────────────────────────────────
# offense side — verbatim W2 equation (never changes, flag ON or OFF)
# ─────────────────────────────────────────────────────────────────────────────

def _offense_budget(conf: Optional[float], transition_state: Optional[str],
                    flip_margin: Optional[float]) -> tuple[float, dict]:
    """THE ONE EQUATION — byte-identical to regime_frame.budget() on every calm tape.

    Returns (lead_budget, inputs_dict).  Missing any of {conf, transition, flip_margin}
    degrades to midpoint 0.50 (the W2 degrade-to-neutral invariant).
    """
    cfg = _cfg()["budget"]

    def _f(key: str, default: float) -> float:
        try:
            v = cfg.get(key)
            return float(v) if v is not None else default
        except Exception:  # noqa: BLE001
            return default

    base = _f("base", _BUDGET_BASE)
    slope = _f("slope", _BUDGET_SLOPE)
    floor_ = _f("floor", _BUDGET_FLOOR)
    ceil_ = _f("ceil", _BUDGET_CEIL)
    midpoint = _f("midpoint", _BUDGET_MIDPOINT)
    flip_min = _f("flip_margin_min", _FLIP_MARGIN_MIN)

    # degrade to midpoint if any load-bearing field is missing
    if conf is None or transition_state is None or flip_margin is None:
        return midpoint, {"confidence": conf, "transition_state": transition_state,
                          "flip_margin": flip_margin, "T": None, "F": None,
                          "lead_budget_raw": midpoint}

    conf_c = min(max(float(conf), 0.0), 1.0)

    # T — transition multiplier (unlisted states → 1.0)
    t_map = dict(_TRANSITION_MULT)
    try:
        for k, v in (cfg.get("transition_mult") or {}).items():
            t_map[k] = float(v)
    except Exception:  # noqa: BLE001
        pass
    T = t_map.get(str(transition_state).upper(), 1.0)

    # F — flip-fragility multiplier
    F = 0.75 if float(flip_margin) < flip_min else 1.0

    raw = base + slope * conf_c * T * F
    lead = min(max(raw, floor_), ceil_)
    return lead, {"confidence": conf_c, "transition_state": transition_state,
                  "flip_margin": float(flip_margin), "T": T, "F": F,
                  "lead_budget_raw": raw}


# ─────────────────────────────────────────────────────────────────────────────
# defense side — renormalizing multi-plane D (equal-weight pinned, v1)
# ─────────────────────────────────────────────────────────────────────────────

def _plane_value(name: str, *, conf: Optional[float], transition_state: Optional[str],
                 flip_margin: Optional[float], risk_state: Optional[dict],
                 evidence: Optional[dict]) -> Optional[float]:
    """Return the plane's pressure value [0,1] or None (not available).

    Shrink-biased: a higher value = more defensive pressure.
    None = plane not available (missing data) → drops out of the mean (renormalize).
    """
    ev_src = (evidence or {}).get("sources") or {}

    if name == "regime_fragility":
        if conf is None:
            return None
        return min(max(1.0 - float(conf), 0.0), 1.0)

    if name == "transition_tilt":
        if transition_state is None:
            return None
        return _TRANS_TILT.get(str(transition_state).upper(), 0.0)

    if name == "flip_fragility":
        if flip_margin is None:
            return None
        return 1.0 if float(flip_margin) < _FLIP_MARGIN_MIN else 0.0

    if name == "dwell_level":
        st = (risk_state or {}).get("state")
        if st is None:
            return None
        return _DWELL_LEVEL.get(str(st).lower(), 0.0)

    if name == "distribution_tells":
        v = ev_src.get("nowcast_doubt")
        if v is None:
            return None
        return 1.0 if v is True else 0.0

    if name == "liquidity_quality":
        v = ev_src.get("liquidity_stress")
        if v is None:
            return None
        return 1.0 if v is True else 0.0

    if name == "regime_nowcast":
        # ADVISORY-ONLY (gate FAILED); maps from evidence but can only raise D
        v = ev_src.get("nowcast_doubt")  # shares the nowcast_doubt source
        if v is None:
            return None
        return 0.5 if v is True else 0.0

    if name == "defensive_rs_cross":
        v = ev_src.get("defensive_rs_cross")
        if v is None:
            return None
        return 1.0 if v is True else 0.0

    return None


_D_PLANE_NAMES = (
    "regime_fragility",
    "transition_tilt",
    "flip_fragility",
    "dwell_level",
    "distribution_tells",
    "liquidity_quality",
    "regime_nowcast",
    "defensive_rs_cross",
)


def _defense_pressure(conf: Optional[float], transition_state: Optional[str],
                      flip_margin: Optional[float], risk_state: Optional[dict],
                      evidence: Optional[dict]) -> tuple[float, dict]:
    """Compute D = availability-renormalizing equal-weight mean over shrink-biased planes.

    Returns (D, planes_dict).  If ALL planes absent → 0.5 (degrade to BALANCED).
    Missing plane drops out of the denominator (renormalize = missing never defaults to 0-risk).
    D is shrink-biased: a MISSING plane can only remove a shrink, never impose one.
    """
    planes: dict[str, dict] = {}
    values: list[float] = []
    for name in _D_PLANE_NAMES:
        v = _plane_value(name, conf=conf, transition_state=transition_state,
                         flip_margin=flip_margin, risk_state=risk_state, evidence=evidence)
        planes[name] = {"value": v, "available": v is not None}
        if v is not None:
            values.append(v)

    if not values:
        D = 0.5  # degrade to BALANCED midpoint — missing data never manufactures 0-risk
    else:
        D = sum(values) / len(values)
    return D, planes


# ─────────────────────────────────────────────────────────────────────────────
# class bands + hysteresis
# ─────────────────────────────────────────────────────────────────────────────

def _class_from_d(D: float) -> str:
    b_balanced, b_rot, b_pres = _bands()
    if D >= b_pres:
        return "PRESERVE"
    if D >= b_rot:
        return "ROTATE_DEFENSIVE"
    if D >= b_balanced:
        return "BALANCED"
    return "OFFENSE"


_CLASS_RANK = {"OFFENSE": 0, "BALANCED": 1, "ROTATE_DEFENSIVE": 2, "PRESERVE": 3}
_RANK_CLASS = {v: k for k, v in _CLASS_RANK.items()}


def _load_state() -> dict:
    """Load the hysteresis state (data/posture/state.json). Degrade to stateless on any error."""
    try:
        if _STATE_PATH.exists():
            s = json.loads(_STATE_PATH.read_text())
            if isinstance(s, dict):
                return s
    except Exception:  # noqa: BLE001
        pass
    return {}


def _save_state(state: dict) -> None:
    """Atomic save of the hysteresis state. Never raises."""
    try:
        _STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _STATE_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, indent=2, default=str))
        tmp.replace(_STATE_PATH)
    except Exception:  # noqa: BLE001
        pass


def _apply_hysteresis(class_raw: str, state: dict,
                      last_severity: Optional[int] = None) -> tuple[str, dict, list[str]]:
    """Apply the dwell-based hysteresis to the raw class.

    Escalate same-session (raw class higher → immediate).
    De-escalate: 3 consecutive lower-band builds AND no sev>=2 tripwire in 2 sessions.
    Max-dwell auto-release: 15 sessions.

    Returns (class_held, new_state, trail_lines).
    """
    hy = _hysteresis_cfg()
    deescalate_sessions = int(hy["deescalate_consecutive"])
    tripwire_clamp_sessions = int(hy["tripwire_clamp_sessions"])
    max_dwell_sessions = int(hy["max_dwell_sessions"])

    trail: list[str] = []
    current_class = state.get("class") or class_raw
    current_rank = _CLASS_RANK.get(current_class, 0)
    raw_rank = _CLASS_RANK.get(class_raw, 0)
    sessions = int(state.get("sessions_in_class") or 0)
    deesc_count = int(state.get("deescalate_count") or 0)
    last_sev = int(last_severity or state.get("last_severity") or 0)
    sev_build_age = int(state.get("sev_build_age") or 999)  # builds since last sev>=clamp tripwire
    # a hot tripwire on THIS build resets the age to 0 immediately (so it clamps this de-escalation) —
    # mirrors macro_risk._recent_tripwire_severity, which reads the CURRENT session's derisk artifact.
    if last_severity is not None and int(last_severity) >= _tripwire_clamp_severity():
        sev_build_age = 0

    # always escalate immediately
    if raw_rank > current_rank:
        class_held = class_raw
        sessions = 1
        deesc_count = 0
        trail.append(f"escalated {current_class} → {class_raw} (raw D-driven)")
    elif raw_rank < current_rank:
        # de-escalation check
        tripwire_clamp = last_sev >= _tripwire_clamp_severity() and sev_build_age <= tripwire_clamp_sessions
        if tripwire_clamp:
            class_held = current_class
            deesc_count = 0
            trail.append(f"de-escalation BLOCKED (sev {last_sev} tripwire within "
                         f"{sev_build_age} builds)")
        else:
            deesc_count += 1
            if deesc_count >= deescalate_sessions:
                # step down ONE band only
                new_rank = max(raw_rank, current_rank - 1)
                class_held = _RANK_CLASS.get(new_rank, current_class)
                if class_held != current_class:
                    trail.append(f"de-escalated {current_class} → {class_held} "
                                 f"({deesc_count} consecutive lower sessions)")
                    sessions = 1
                    deesc_count = 0
                else:
                    class_held = current_class
                    sessions += 1
            else:
                class_held = current_class
                sessions += 1
                trail.append(f"de-escalation dwell-blocked ({deesc_count}/{deescalate_sessions})")
    else:
        # same band — just accumulate
        class_held = current_class
        sessions += 1
        deesc_count = 0

    # max-dwell auto-release (PRESERVE cannot become a trap)
    if sessions > max_dwell_sessions and _CLASS_RANK.get(class_held, 0) > raw_rank:
        new_rank = _CLASS_RANK.get(class_held, 0) - 1
        released_class = _RANK_CLASS.get(max(0, new_rank), "OFFENSE")
        trail.append(f"max-dwell auto-release {class_held} → {released_class} "
                     f"(held {sessions} sessions)")
        class_held = released_class
        sessions = 1
        deesc_count = 0

    # update sev_build_age
    if last_sev is not None and last_sev >= _tripwire_clamp_severity():
        sev_build_age = 0
    else:
        sev_build_age = min(sev_build_age + 1, 999)

    new_state = {
        "class": class_held,
        "sessions_in_class": sessions,
        "deescalate_count": deesc_count,
        "last_severity": last_sev,
        "sev_build_age": sev_build_age,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    return class_held, new_state, trail


# ─────────────────────────────────────────────────────────────────────────────
# the one decide() function
# ─────────────────────────────────────────────────────────────────────────────

def decide(
    region: str = "us",
    *,
    evidence: Optional[dict] = None,
    risk_state: Optional[dict] = None,
    last_severity: Optional[int] = None,
    write_state: bool = True,
) -> dict:
    """Compute the posture for *region*.  Never raises — degrades to BALANCED/midpoint.

    Parameters
    ----------
    region:         'us' (others degrade gracefully — no CN/HK posture yet).
    evidence:       rotation_evidence() dict from regime_frame (the W-I agreement count).
                    None → all evidence planes absent (no-op, equal to 0 agreeing sources).
    risk_state:     dict with at least {'state': str} from macro_risk.risk_state().
                    None → dwell_level plane absent (renormalized out).
    last_severity:  the latest derisk tripwire severity (for the hysteresis tripwire clamp).
    write_state:    True = persist the hysteresis state (False for tests / replay).

    Returns
    -------
    The full posture artifact dict (see module docstring schema).
    """
    shadow = not posture_flag()
    asof = date.today().isoformat()
    built_at = datetime.now(timezone.utc).isoformat()

    # ── read the frame (regime_frame is always available; degrade gracefully) ──
    try:
        from brain import regime_frame as _rf
        fr = _rf.frame(region) or {}
    except Exception:  # noqa: BLE001
        fr = {}

    conf = _safe_float(fr.get("confidence"))
    transition_state = fr.get("transition_state")
    flip_margin = _safe_float(fr.get("flip_margin"))

    # ── offense side (verbatim W2 equation) ──
    offense_equation, offense_inputs = _offense_budget(conf, transition_state, flip_margin)

    # ── defense side (renormalizing multi-plane D) ──
    D, planes = _defense_pressure(conf, transition_state, flip_margin, risk_state, evidence)

    # ── class + hysteresis ──
    class_raw = _class_from_d(D)
    state = _load_state()
    class_held, new_state, hyst_trail = _apply_hysteresis(class_raw, state, last_severity)

    # ── MIN-COMPOSE the offense budget with the HELD class's ceiling (anti-compounding, row 5) ──
    # The verbatim equation moved off the damp (the damp's shrink now lives ONCE on the defense side
    # of D).  A defensive class re-imposes that shrink here as a CEILING — min(equation, cap) — so the
    # evidence is consumed exactly once: it raises D, D picks the class, the class caps offense.  On a
    # calm tape the class is OFFENSE (cap 0.60 = the W2 ceil) so min() is a no-op and the budget is
    # byte-identical to today; on the incident tape ROTATE-DEFENSIVE's cap pins offense into 0.40-0.45.
    offense_budget = min(offense_equation, _offense_cap(class_held))
    if write_state:
        # persist ALWAYS (flag ON or OFF) so the shadow hysteresis window accrues from day one — the
        # de-escalation dwell that blocks the 2026-07-01 un-cap must be tracked even in shadow.
        _save_state(new_state)

    # ── sizing outputs (the class's derived levers, doctrine-sourced with prior fallbacks) ──
    dsm = _def_sleeve_max()
    defense_floor = round(dsm * D, 4)                         # 0 until the sleeve is armed (dsm 0.0)
    defense_floor_at_max = round(_DEF_SLEEVE_CEILING * D, 4)  # the WOULD-BE floor (armed-ceiling · D)
    cash_fl = float(_class_lever(class_held, "cash_floor", _CASH_FLOOR))
    appetite = float(_class_lever(class_held, "conviction_appetite", _APPETITE))
    # posture_notch_cap: the class's eff_cap ceiling.  ALWAYS recorded in the artifact (the shadow
    # trail must show the WOULD-BE notch — build_plan §4.4 asserts notch cap 0.70 on 06-26, a shadow
    # build).  It is only APPLIED to bot/derisk.py eff_cap when the flag is ON (that wiring is E2.2);
    # OFFENSE/BALANCED map to 1.00 (a no-op ceiling — no notch).
    notch_cap = float(_class_lever(class_held, "posture_notch_cap", _NOTCH_CAP))

    # ── evidence trail (one-line why for the POSTURE block) ──
    n_agree = (evidence or {}).get("n_agree", 0)
    ev_src = ((evidence or {}).get("sources") or {})
    trail: list[str] = []
    if conf is not None:
        trail.append(f"conf {conf:.3f}")
    if transition_state:
        trail.append(f"transition {transition_state}")
    if flip_margin is not None and flip_margin < _FLIP_MARGIN_MIN:
        trail.append(f"flip fragile ({flip_margin:.2f})")
    dwell_st = (risk_state or {}).get("state")
    if dwell_st:
        trail.append(f"dwell {dwell_st}")
    if n_agree > 0:
        trail.append(f"{n_agree}/4 evidence agree")
    trail.extend(hyst_trail)

    why = "; ".join(trail) if trail else "no evidence"

    return {
        "schema_version": _SCHEMA_VERSION,
        "asof": asof,
        "built_at": built_at,
        "shadow": shadow,
        "posture_class": class_held,
        "offense_budget": round(offense_budget, 4),
        "offense_budget_equation": round(offense_equation, 4),
        "offense_cap": _offense_cap(class_held),
        "defense_floor": defense_floor,
        "defense_floor_at_max": defense_floor_at_max,
        "cash_floor": cash_fl,
        "conviction_appetite": appetite,
        "posture_notch_cap": notch_cap,
        "defense_pressure": round(D, 4),
        "planes": planes,
        "offense_inputs": offense_inputs,
        "hysteresis": {
            "class_raw": class_raw,
            "class_held": class_held,
            "sessions_in_class": new_state.get("sessions_in_class"),
            "deescalate_count": new_state.get("deescalate_count"),
            "last_severity": last_severity,
        },
        "why": why,
        "shrink_provenance": "defense_D",  # row 5 contract: single pathway token
        "evidence_trail": trail,
    }


# ─────────────────────────────────────────────────────────────────────────────
# build + publish (shadow every build, regardless of flag)
# ─────────────────────────────────────────────────────────────────────────────

def build(
    region: str = "us",
    *,
    evidence: Optional[dict] = None,
    risk_state: Optional[dict] = None,
    last_severity: Optional[int] = None,
    write: bool = True,
) -> dict:
    """decide() + atomic write of latest.json + a dated copy.

    Always publishes regardless of the arming flag (the wave contract: the shadow window
    accrues from day one).  ``write=False`` is for tests / replay.  Never raises.
    """
    try:
        payload = decide(region, evidence=evidence, risk_state=risk_state,
                         last_severity=last_severity, write_state=write)
    except Exception:  # noqa: BLE001
        payload = _degrade_artifact()

    if write:
        _atomic_write(_LATEST_PATH, payload)
        dated = _ARTIFACT_DIR / f"{payload.get('asof', 'unknown')}.json"
        _atomic_write(dated, payload)

    return payload


def _degrade_artifact() -> dict:
    """Degrade payload on total failure (never raises)."""
    return {
        "schema_version": _SCHEMA_VERSION,
        "asof": date.today().isoformat(),
        "built_at": datetime.now(timezone.utc).isoformat(),
        "shadow": True,
        "posture_class": "BALANCED",
        "offense_budget": _BUDGET_MIDPOINT,
        "defense_floor": 0.0,
        "cash_floor": 0.10,
        "conviction_appetite": 0.75,
        "posture_notch_cap": None,
        "defense_pressure": 0.5,
        "planes": {},
        "offense_inputs": {},
        "hysteresis": {},
        "why": "DEGRADED — all planes absent",
        "shrink_provenance": "defense_D",
        "evidence_trail": ["DEGRADED"],
    }


def _atomic_write(path: Path, payload: dict) -> None:
    """Atomic tmp + os.replace write. Never raises."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2, default=str))
        tmp.replace(path)
    except Exception:  # noqa: BLE001
        pass


# ─────────────────────────────────────────────────────────────────────────────
# latest() — read the last published artifact
# ─────────────────────────────────────────────────────────────────────────────

def latest() -> Optional[dict]:
    """Return the last published posture artifact, or None if absent/corrupt. Never raises."""
    try:
        if _LATEST_PATH.exists():
            d = json.loads(_LATEST_PATH.read_text())
            return d if isinstance(d, dict) else None
    except Exception:  # noqa: BLE001
        pass
    return None


# ─────────────────────────────────────────────────────────────────────────────
# render_directive() — the POSTURE block injected into LLM prompts (E2.5)
# ─────────────────────────────────────────────────────────────────────────────

def render_directive(asof: Optional[str] = None) -> str:
    """Render the POSTURE block for LLM book prompts (E2.5 directive rendering).

    Flag-independent: the seats see the shadow posture (read-only prompt enrichment —
    seeing it is part of the shadow experiment).  Missing artifact → empty string (degrade).
    Never raises.

    The block is prefixed with '## POSTURE (SHADOW — ADVISORY)' when the flag is OFF,
    and '## DESK POSTURE (BINDING GUIDANCE)' when the flag is ON (armed).  The binding
    phrase matches the E2.5 spec: 'the desk posture is ROTATE-DEFENSIVE: offense budget
    0.42, defense floor 0.24 — deviations are logged and graded'.
    """
    try:
        art = latest()
        if not art or not isinstance(art, dict):
            return ""
        posture_class = art.get("posture_class") or "BALANCED"
        offense_budget = art.get("offense_budget")
        defense_floor = art.get("defense_floor")
        why = art.get("why") or ""
        shadow = art.get("shadow", True)

        if offense_budget is None or defense_floor is None:
            return ""

        if shadow:
            header = "## POSTURE (SHADOW — ADVISORY)"
            binding = "advisory (shadow mode — flag OFF; observing how this would steer)"
        else:
            header = "## DESK POSTURE (BINDING GUIDANCE)"
            binding = "BINDING — deviations are logged and graded"

        lines = [
            header,
            (f"the desk posture is {posture_class}: offense budget {offense_budget:.2f}, "
             f"defense floor {defense_floor:.2f} — {binding}"),
            f"Evidence: {why}",
            "",
        ]
        return "\n".join(lines)
    except Exception:  # noqa: BLE001
        return ""


# ─────────────────────────────────────────────────────────────────────────────
# helpers
# ─────────────────────────────────────────────────────────────────────────────

def _safe_float(v: Any) -> Optional[float]:
    try:
        f = float(v)
        return f if f == f else None  # NaN → None
    except (TypeError, ValueError):
        return None
