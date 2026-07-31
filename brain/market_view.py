"""brain/market_view.py — THE one market view (architecture W-E.0, task E0.3).

WHY THIS MODULE EXISTS
----------------------
The 2026-07-02 semis breakdown taught the firm that the bot could hold a rosy
``label`` (Q1 Goldilocks, confidence 0.327, transition_state STABLE) while its OWN
already-loaded regime file embedded a caution radar, a distributing-froth read, and a
top-0%-concentrated cross-asset print — all of which the bot never looked at.  The
coverage audit's structural finding: the perception gap is a *parsing gap, not a data
gap*.  This module is the single, deterministic, freshness+confidence-stamped view (P7)
assembled from those already-embedded planes plus the W-I evidence planes plus the E0.1/
E0.2 organs (lazy, degrade-to-absent while unbuilt).

THE WAVE CONTRACT — ZERO BEHAVIOR CHANGE
----------------------------------------
This is a NEW read-only artifact.  It sizes NOTHING.  ``view()`` composes published
planes into one dict + writes ``data/market_view/latest.json``.  No sizing path reads it
in W-E.0; the posture decider (E2.x) is a later wave behind Fable sign-off.  The
disagreement layer is the wave's *point* but it only annotates — advisory/absent/stale
planes weight 0 and can NEVER lower disagreement (P2).

INVARIANT (governs every path here)
-----------------------------------
Missing / stale / corrupt data may only shrink the view's confidence, mark a plane
``stale``/``absent``, and be EXCLUDED from the validated tilt — it may NEVER manufacture
consensus, lower disagreement, raise authority, or flip a direction.  Every plane
degrades independently; the whole assembler NEVER raises.

PUBLIC API
----------
* ``view(region='us')``       -> the full market_view.v1 dict (deterministic, zero LLM).
* ``build(region='us', write=True)`` -> ``view()`` + atomic write of latest.json + a
                              dated copy under data/market_view/.
* ``PLANE_ORDER`` / ``TOP_LEVEL_ORDER`` -> the golden key orders (the artifact feeds
                              prompts; key-order drift silently changes LLM output).

PER-PLANE FRESHNESS
-------------------
Each plane's freshness is computed from THAT block's OWN ``asof`` (never the file mtime,
never the top-level regime date) via ``regime_frame._trading_days_since``.  A plane older
than ``market_view.stale_after_trading_days`` (doctrine, fallback 5) is ``stale:true`` and
excluded from the validated tilt (fail-closed).

VALIDATED vs ADVISORY (P3)
--------------------------
Only planes with on-disk forward grading sign the tilt: ``risk_radar`` (forward_log.jsonl,
2006-2026 calibration), ``mtf_signals`` (MTF walk-forward), and ``cycles`` (entry-tilt
survives the walk-forward; veto refuted).  Everything else — froth_fragility, gross_factor,
turning_point, vol_shock, cross_asset, market_drivers, dislocation, macro_risk, the W-I
planes, rotation_tensor, anticipation, and every null stub — is ``advisory`` and can only
annotate/shrink, never sign ``net_posture_tilt``.  ``regime_nowcast`` is ADVISORY-ONLY: its
walk-forward gate FAILED (hit-rate 0.354) — it is a plane of the view but never validated.
"""
from __future__ import annotations

import json
import os
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Optional

from brain import regime_frame as _rf

# The module lives at brain/market_view.py; the repo root is two levels up.
_ROOT: Path = Path(__file__).resolve().parent.parent

# The one artifact this module publishes (P7).  Atomic tmp+os.replace writes.
_ARTIFACT_DIR: Path = _ROOT / "data" / "market_view"
_LATEST_PATH: Path = _ARTIFACT_DIR / "latest.json"

# The E0.1/E0.2 organs — read from their own contracts when present, degrade to absent.
_ROTATION_TENSOR_PATH: Path = _ARTIFACT_DIR / "rotation_tensor.json"
_ANTICIPATION_DIR: Path = _ROOT / "data" / "anticipation"
_RRG_PATH: Path = _ROOT / "vendor" / "macro" / "site" / "marketdata" / "subsector_rotation.json"
_GROUP_FLOW_PATH: Path = _ROOT / "vendor" / "macro" / "site" / "basketdata" / "flow.json"

_SCHEMA_VERSION = "market_view.v1"

# ---- the golden key orders (load-bearing: the artifact is injected into LLM prompts) ----
# The Tier-A embedded-key planes, in coverage-audit priority order, then cycles(), then the
# W-I planes, then the E0.1/E0.2 organs, then the H4-handoff null stubs.
PLANE_ORDER: tuple[str, ...] = (
    # Tier-A embedded keys (coverage_audit.md priorities in the comment)
    "risk_radar",        # pri 97
    "mtf_signals",       # pri 94
    "froth_fragility",   # pri 88
    "gross_factor",      # pri 85 (risk_state.gross_factor)
    "turning_point",     # pri 66
    "vol_shock",         # pri 61
    "cross_asset",
    "market_drivers",
    "dislocation",
    "macro_risk",
    # regime_frame.cycles() — the existing one-reader (never re-read the json)
    "cycles",
    # W-I planes
    "distribution_tells",
    "liquidity_quality",
    "regime_nowcast",    # ADVISORY-ONLY (its walk-forward gate FAILED, hit-rate 0.354)
    # E0.1 / E0.2 organs (lazy-import; degrade to absent while unbuilt)
    "rotation_tensor",
    "anticipation",
    # H4-handoff null-advisory stubs (schema complete from day one)
    "rrg",
    "group_flow",
    "event_calendar",
    "intl_spillover",
    # W-NW.1 Neural Web bridge (ADVISORY-ONLY; dark-ship; validated=False per §3.3.1)
    "neural_web",
)

# The validated set — the ONLY planes that may sign net_posture_tilt (P3).
_VALIDATED_PLANES: frozenset[str] = frozenset({"risk_radar", "mtf_signals", "cycles"})

TOP_LEVEL_ORDER: tuple[str, ...] = (
    "schema_version",
    "region",
    "asof",
    "built_at",
    "seq",
    "planes",
    "net_posture_tilt",
    "coverage",
    "coherence",
    "disagreements",
    "label_vs_planes",
    "posture_floor_defense",
    "assembly",
    "budget_ref",
    "brief",
)

_PLANE_RECORD_ORDER: tuple[str, ...] = (
    "reading", "direction", "magnitude", "freshness", "confidence",
    "status", "source_contract", "raw",
)

_BRIEF_ORDER: tuple[str, ...] = (
    "what_changed", "whats_rotating", "wheres_the_risk", "posture_implication",
)

# Discrete direction tokens — directions NEVER average into mush (a graft from the judged
# design); a plane is risk_off / neutral / risk_on / None (undeterminable).
_RISK_OFF = "risk_off"
_NEUTRAL = "neutral"
_RISK_ON = "risk_on"

# Fallback staleness horizon (mirrors doctrine market_view.stale_after_trading_days).
_STALE_TD_FALLBACK: int = 5
# Fallback coverage floor: below this the view is "degraded" (may only refuse to loosen).
_COVERAGE_FLOOR_FALLBACK: float = 0.30


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------

def _cfg() -> dict[str, Any]:
    """Read doctrine's ``market_view:`` block, or {} on any error (fallbacks below win)."""
    try:
        block = _rf._doctrine().get("market_view") or {}
        return block if isinstance(block, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _stale_after_td() -> int:
    try:
        v = _cfg().get("stale_after_trading_days")
        if v is not None:
            return int(v)
    except Exception:  # noqa: BLE001
        pass
    return _STALE_TD_FALLBACK


def _coverage_floor() -> float:
    try:
        v = _cfg().get("coverage_floor")
        if v is not None:
            return float(v)
    except Exception:  # noqa: BLE001
        pass
    return _COVERAGE_FLOOR_FALLBACK


# ---------------------------------------------------------------------------
# freshness — per-plane, from each block's OWN asof (never the file mtime)
# ---------------------------------------------------------------------------

def _freshness(asof: Any) -> dict[str, Any]:
    """Build a freshness sub-record from a plane's OWN asof.

    ``age_sessions`` reuses regime_frame's trading-day arithmetic (the one calendar).
    Unknown age (asof absent/unparseable) is fail-closed: ``stale=True`` (an unknown-age
    plane can never sign the tilt).
    """
    age = _rf._trading_days_since(str(asof) if asof is not None else None)
    stale_after = _stale_after_td()
    stale = age is None or age < 0 or age > stale_after
    return {
        "asof": str(asof) if asof is not None else None,
        "age_sessions": age,
        "stale": bool(stale),
        "future_dated": age is not None and age < 0,
        "stale_after_sessions": stale_after,
    }


def _plane_record(
    *,
    reading: Any,
    direction: Optional[str],
    magnitude: Any,
    asof: Any,
    confidence: Any,
    validated: bool,
    source_contract: str,
    raw: Any,
) -> dict[str, Any]:
    """Assemble one PlaneRecord in golden key order.

    ``status`` is 'validated' only for the three forward-graded planes AND only when the
    plane is not stale — a stale validated plane degrades to advisory (it cannot sign the
    tilt).  ``confidence`` is coerced to a float in [0,1] or None.
    """
    fresh = _freshness(asof)
    conf = _rf._as_float(confidence)
    if conf is not None:
        conf = min(max(conf, 0.0), 1.0)
    status = "validated" if (validated and not fresh["stale"]) else "advisory"
    raw_out = dict(raw) if isinstance(raw, dict) else {"value": raw}
    # Availability and activation are separate concepts.  ``artifact_present`` describes
    # whether the source record exists; ``signal_active`` describes whether it is firing.
    # Keep legacy ``present`` compatible for the current UI, but never infer absence from a
    # detector's domain-specific "present" field.
    raw_out.setdefault("artifact_present", True)
    raw_out.setdefault("present", True)
    raw_out.setdefault("signal_active", direction is not None)
    return {
        "reading": reading,
        "direction": direction,
        "magnitude": _rf._as_float(magnitude) if magnitude is not None else None,
        "freshness": fresh,
        "confidence": conf,
        "status": status,
        "source_contract": source_contract,
        "raw": raw_out,
    }


def _absent_record(source_contract: str, note: str = "") -> dict[str, Any]:
    """A null-advisory PlaneRecord for a plane that is absent (missing block / unbuilt organ /
    H4-handoff stub).  Absent planes weight 0 and can never lower disagreement (P2)."""
    return {
        "reading": None,
        "direction": None,
        "magnitude": None,
        "freshness": {
            "asof": None,
            "age_sessions": None,
            "stale": True,
            "future_dated": False,
            "stale_after_sessions": _stale_after_td(),
        },
        "confidence": None,
        "status": "advisory",
        "source_contract": source_contract,
        "raw": {
            "present": False,
            "artifact_present": False,
            "signal_active": False,
            **({"note": note} if note else {}),
        },
    }


# ---------------------------------------------------------------------------
# Tier-A embedded-key adapters (over the ALREADY-loaded regime file — one read)
# ---------------------------------------------------------------------------
# Each adapter takes the whole raw regime dict (read once) and returns a PlaneRecord.  A
# missing block returns an absent record — NEVER a fabricated reading.

def _block(raw: dict[str, Any], key: str) -> Optional[dict[str, Any]]:
    v = raw.get(key)
    return v if isinstance(v, dict) else None


def _adapt_risk_radar(raw: dict[str, Any]) -> dict[str, Any]:
    """risk_radar (pri 97) — dominant-scare classifier + drawdown_prob.h21 directive.

    VALIDATED (forward_log.jsonl, 2006-2026 SPY-pullback calibration).  A caution / risk_off
    state or a firing alert reads risk_off; an all-clear reads risk_on.  drawdown_prob.h21 is
    the magnitude (probability of >=5% pullback, 21-session horizon).
    """
    b = _block(raw, "risk_radar")
    if b is None:
        return _absent_record("regime.risk_radar")
    state = b.get("state")
    alert = bool(b.get("alert"))
    ddp = b.get("drawdown_prob")
    h21 = ddp.get("h21") if isinstance(ddp, dict) else None
    st = str(state).strip().lower().replace("-", "_") if state is not None else None
    if st in ("caution", "risk_off") or alert:
        direction = _RISK_OFF
    elif st in ("calm", "clear", "risk_on", "normal", "all_clear"):
        direction = _RISK_ON
    else:
        direction = _NEUTRAL if st is not None else None
    return _plane_record(
        reading=b.get("headline_en") or b.get("dominant_label_en") or state,
        direction=direction,
        magnitude=h21,
        asof=b.get("asof"),
        confidence=_rf._as_float(b.get("top_score")) / 100.0
        if _rf._as_float(b.get("top_score")) is not None else None,
        validated=True,
        source_contract="regime.risk_radar (forward_log.jsonl)",
        raw={"state": state, "alert": alert, "dominant_scare": b.get("dominant_scare"),
             "top_score": b.get("top_score"), "drawdown_prob_h21": h21,
             "cap_leadership": b.get("cap_leadership"), "favor_entries": b.get("favor_entries")},
    )


def _adapt_mtf_signals(raw: dict[str, Any]) -> dict[str, Any]:
    """mtf_signals (pri 94) — per-ticker 3D-MACD state + trail_breach (the price-action tell).

    VALIDATED (MTF walk-forward on record).  Direction from the cross-sectional balance of
    short-bias vs long-bias states and the trail-breach share.  Magnitude = share of the
    universe in short-bias.
    """
    b = _block(raw, "mtf_signals")
    if b is None:
        return _absent_record("regime.mtf_signals")
    sigs = b.get("signals")
    if not isinstance(sigs, list) or not sigs:
        return _absent_record("regime.mtf_signals", "no per-ticker signals")
    n = 0
    short_bias = long_bias = breaches = 0
    for s in sigs:
        if not isinstance(s, dict):
            continue
        n += 1
        st = str(s.get("state") or "").strip().lower()
        if st == "short-bias":
            short_bias += 1
        elif st == "long-bias":
            long_bias += 1
        if s.get("trail_breach"):
            breaches += 1
    if n == 0:
        return _absent_record("regime.mtf_signals", "no parseable signals")
    short_frac = short_bias / n
    breach_frac = breaches / n
    # discrete direction: a distribution-flavoured tape (more short-bias than long-bias, or a
    # high trail-breach share) reads risk_off.
    if short_bias > long_bias or breach_frac >= 0.5:
        direction = _RISK_OFF
    elif long_bias > short_bias and breach_frac < 0.4:
        direction = _RISK_ON
    else:
        direction = _NEUTRAL
    return _plane_record(
        reading=(f"{short_bias} short-bias / {long_bias} long-bias of {n}; "
                 f"{breaches} trail-breach"),
        direction=direction,
        magnitude=round(short_frac, 4),
        asof=b.get("asof"),
        confidence=None,  # per-name MTF has no single scalar confidence
        validated=True,
        source_contract="regime.mtf_signals (MTF walk-forward)",
        raw={"n": n, "short_bias": short_bias, "long_bias": long_bias,
             "trail_breach": breaches, "short_frac": round(short_frac, 4),
             "breach_frac": round(breach_frac, 4), "tf": b.get("tf")},
    )


def _adapt_froth_fragility(raw: dict[str, Any]) -> dict[str, Any]:
    """froth_fragility (pri 88) — crowding/distribution quadrant + alert.  ADVISORY."""
    b = _block(raw, "froth_fragility")
    if b is None:
        return _absent_record("regime.froth_fragility")
    alert = bool(b.get("alert"))
    quadrant = b.get("quadrant")
    stealth_fire = bool(b.get("stealth_fire"))
    unwind = bool(b.get("unwind_risk"))
    # distribution/narrowing-top/euphoric reads risk_off; a calm band reads risk_on.
    q = str(quadrant or "").strip().lower()
    if alert or stealth_fire or unwind or "narrowing" in q or "distribut" in q or "euphoric" in q:
        direction = _RISK_OFF
    elif quadrant is not None:
        direction = _NEUTRAL
    else:
        direction = None
    return _plane_record(
        reading=b.get("quadrant_en") or quadrant,
        direction=direction,
        magnitude=b.get("headline"),
        asof=b.get("asof"),
        confidence=None,
        validated=False,
        source_contract="regime.froth_fragility",
        raw={"headline": b.get("headline"), "band": b.get("band"), "quadrant": quadrant,
             "alert": alert, "stealth_fire": stealth_fire, "unwind_risk": unwind},
    )


def _adapt_gross_factor(raw: dict[str, Any]) -> dict[str, Any]:
    """risk_state.gross_factor (pri 85) — the dashboard's own pre-computed gross multiplier.

    ADVISORY (is_context_only).  gross_factor < 1.0 is a caution shrink (risk_off);
    == 1.0 is neutral.  Magnitude = the factor itself.
    """
    b = _block(raw, "risk_state")
    if b is None:
        return _absent_record("regime.risk_state.gross_factor")
    gf = _rf._as_float(b.get("gross_factor"))
    if gf is None:
        return _absent_record("regime.risk_state.gross_factor", "no gross_factor")
    if gf < 1.0:
        direction = _RISK_OFF
    else:
        direction = _NEUTRAL
    return _plane_record(
        reading=b.get("label_en") or b.get("state"),
        direction=direction,
        magnitude=gf,
        asof=b.get("asof"),
        confidence=None,
        validated=False,
        source_contract="regime.risk_state.gross_factor (is_context_only)",
        raw={"gross_factor": gf, "state": b.get("state"), "score": b.get("score"),
             "cap_leadership": b.get("cap_leadership")},
    )


def _adapt_turning_point(raw: dict[str, Any]) -> dict[str, Any]:
    """turning_point (pri 66) — regime-turn detector.  ADVISORY."""
    b = _block(raw, "turning_point")
    if b is None:
        return _absent_record("regime.turning_point")
    present = bool(b.get("present"))
    active = bool(b.get("active"))
    raw_fire = bool(b.get("raw_fire"))
    if present or active or raw_fire:
        direction = _RISK_OFF
    else:
        direction = _NEUTRAL
    return _plane_record(
        reading=b.get("headline_en") or b.get("state"),
        direction=direction,
        magnitude=None,
        asof=b.get("asof"),
        confidence=None,
        validated=False,
        source_contract="regime.turning_point",
        raw={"detector_present": present, "active": active, "raw_fire": raw_fire,
             "signal_active": bool(present or active or raw_fire),
             "state": b.get("state"), "put_state": b.get("put_state")},
    )


def _adapt_vol_shock(raw: dict[str, Any]) -> dict[str, Any]:
    """vol_shock (pri 61) — forward vol-shock score + GEX-gate.  ADVISORY."""
    b = _block(raw, "vol_shock")
    if b is None:
        return _absent_record("regime.vol_shock")
    score = _rf._as_float(b.get("score"))
    band = str(b.get("band") or "").strip().lower()
    if band in ("elevated", "high", "extreme") or (score is not None and score >= 50.0):
        direction = _RISK_OFF
    elif band:
        direction = _NEUTRAL
    else:
        direction = None
    return _plane_record(
        reading=b.get("band"),
        direction=direction,
        magnitude=score,
        asof=b.get("asof"),
        confidence=None,
        validated=False,
        source_contract="regime.vol_shock (gex_gate_scored)",
        raw={"score": score, "band": b.get("band"),
             "gex_gate_scored": b.get("gex_gate_scored")},
    )


def _adapt_cross_asset(raw: dict[str, Any]) -> dict[str, Any]:
    """cross_asset — correlation structure (absorption pctile + dominant cluster).  ADVISORY."""
    b = _block(raw, "cross_asset")
    if b is None:
        return _absent_record("regime.cross_asset")
    verdict = str(b.get("verdict") or "").strip().lower()
    pctile = _rf._as_float(b.get("absorption_pctile_5y"))
    # "concentrated" / a top-of-5y absorption pctile = one-bet fragility → risk_off.
    if verdict == "concentrated" or (pctile is not None and pctile >= 0.90):
        direction = _RISK_OFF
    elif verdict:
        direction = _NEUTRAL
    else:
        direction = None
    return _plane_record(
        reading=b.get("headline") or b.get("verdict"),
        direction=direction,
        magnitude=pctile,
        asof=b.get("asof"),
        confidence=None,
        validated=False,
        source_contract="regime.cross_asset",
        raw={"verdict": b.get("verdict"), "absorption_pctile_5y": pctile,
             "dominant_cluster": b.get("dominant_cluster")},
    )


def _adapt_market_drivers(raw: dict[str, Any]) -> dict[str, Any]:
    """market_drivers — what's driving markets (primary driver + sign).  ADVISORY / neutral."""
    b = _block(raw, "market_drivers")
    if b is None:
        return _absent_record("regime.market_drivers")
    return _plane_record(
        reading=b.get("headline") or b.get("direction") or b.get("primary_label") or b.get("primary"),
        direction=_NEUTRAL if b.get("verdict") else None,  # attribution, not a risk vote
        magnitude=b.get("dir_sign"),
        asof=b.get("asof"),
        confidence=None,
        validated=False,
        source_contract="regime.market_drivers",
        raw={"verdict": b.get("verdict"), "primary": b.get("primary"),
             "direction": b.get("direction"), "dir_sign": b.get("dir_sign"),
             "confidence": b.get("confidence"), "strength": b.get("strength"),
             "evidence": list(b.get("evidence") or [])[:8],
             "invalidation": b.get("invalidation")},
    )


def _adapt_dislocation(raw: dict[str, Any]) -> dict[str, Any]:
    """dislocation — acute cross-asset stress washout.  ADVISORY."""
    b = _block(raw, "dislocation")
    if b is None:
        return _absent_record("regime.dislocation")
    active = bool(b.get("dislocation_active"))
    return _plane_record(
        reading=b.get("headline") or b.get("verdict"),
        direction=_RISK_OFF if active else (_NEUTRAL if b.get("verdict") else None),
        magnitude=None,
        asof=b.get("asof"),
        confidence=None,
        validated=False,
        source_contract="regime.dislocation",
        raw={"verdict": b.get("verdict"), "dislocation_active": active,
             "put_state": b.get("put_state")},
    )


def _adapt_macro_risk(raw: dict[str, Any]) -> dict[str, Any]:
    """macro_risk — the composite recession/drawdown/liquidity risk score.  ADVISORY."""
    b = _block(raw, "macro_risk")
    if b is None:
        return _absent_record("regime.macro_risk")
    score = _rf._as_float(b.get("score"))
    label = str(b.get("label") or "").strip().lower()
    if label in ("high", "elevated") or (score is not None and score >= 0.5):
        direction = _RISK_OFF
    elif label:
        direction = _NEUTRAL
    else:
        direction = None
    return _plane_record(
        reading=b.get("label"),
        direction=direction,
        magnitude=score,
        # Embedded macro_risk is produced as part of this exact regime snapshot.  When it lacks
        # its own timestamp, inherit the regime snapshot's market date with explicit provenance.
        asof=b.get("asof") or raw.get("date"),
        confidence=None,
        validated=False,
        source_contract="regime.macro_risk",
        raw={"score": score, "label": b.get("label"), "components": b.get("components"),
             "asof_inherited_from_regime": not bool(b.get("asof"))},
    )


# ---------------------------------------------------------------------------
# cycles() plane — call the ONE reader, never re-read the json
# ---------------------------------------------------------------------------

def _adapt_cycles(raw: dict[str, Any]) -> dict[str, Any]:
    """The sector-cycle plane — VALIDATED (entry-tilt survives the walk-forward; veto refuted).

    Calls ``regime_frame.cycles()`` (the sole reader) — does NOT re-read sector_cycles.json.
    A stale/absent cycle file makes cycles() return {} → this plane is absent.  Direction:
    a board dominated by late_cycle sectors and starved of entry_favored ones reads risk_off;
    an entry-favored-dominated board reads risk_on.
    """
    try:
        snapshot = _rf.cycles_with_meta()
        cyc = snapshot.get("sectors") if isinstance(snapshot, dict) else {}
        source_asof = snapshot.get("asof") if isinstance(snapshot, dict) else None
    except Exception:  # noqa: BLE001
        cyc, source_asof = {}, None
    if not cyc:
        return _absent_record("regime_frame.cycles (sector_cycles.json)",
                              "cycles() empty (stale/absent)")
    n = len(cyc)
    late = sum(1 for r in cyc.values() if isinstance(r, dict) and r.get("late_cycle"))
    entry = sum(1 for r in cyc.values() if isinstance(r, dict) and r.get("entry_favored"))
    if late > entry:
        direction = _RISK_OFF
    elif entry > late:
        direction = _RISK_ON
    else:
        direction = _NEUTRAL
    return _plane_record(
        reading=f"{late} late-cycle / {entry} entry-favored of {n} sectors",
        direction=direction,
        magnitude=round(late / n, 4) if n else None,
        asof=source_asof,
        confidence=None,
        validated=True,
        source_contract="regime_frame.cycles (walk-forward entry-tilt)",
        raw={"n": n, "late_cycle": late, "entry_favored": entry,
             "source_asof": source_asof},
    )


# ---------------------------------------------------------------------------
# W-I planes — lazy, injected series never live-read here
# ---------------------------------------------------------------------------

def _adapt_distribution_tells(dt_out: Any) -> dict[str, Any]:
    """distribution_tells summary plane.  ADVISORY.

    ``dt_out`` is a pre-computed ``distribution_tells.score(...)`` dict, injected by the caller
    (phase2 passes the real book scoring; tests inject a pure dict).  None → absent.  A ``hot``
    escalation or a book-level defensive-RS crossover reads risk_off.
    """
    if not isinstance(dt_out, dict):
        return _absent_record("portfolio.distribution_tells.score", "not supplied")
    hot = bool(dt_out.get("hot"))
    cross = dt_out.get("def_rs_cross")
    frac = _rf._as_float(dt_out.get("distributing_weight_frac"))
    if hot or cross is True:
        direction = _RISK_OFF
    else:
        direction = _NEUTRAL
    if dt_out.get("reason"):
        reading = dt_out.get("reason")
    elif hot:
        reading = "distribution escalation hot"
    elif cross is True:
        reading = "defensive relative-strength cross"
    else:
        reading = "no distribution"
    return _plane_record(
        reading=reading,
        direction=direction,
        magnitude=frac,
        asof=dt_out.get("asof"),
        confidence=None,
        validated=False,
        source_contract="portfolio.distribution_tells.score",
        raw={"hot": hot, "def_rs_cross": cross,
             "distributing_weight_frac": frac,
             "escalate_severity": dt_out.get("escalate_severity")},
    )


def _adapt_liquidity_quality(lq_out: Any) -> dict[str, Any]:
    """liquidity_quality label plane.  ADVISORY.

    ``lq_out`` is a pre-computed ``liquidity_quality.classify(...)`` dict (injected).  None →
    absent.  A stress-expansion / neutral-hollow / contracting label reads risk_off; a
    benign-expansion reads risk_on; unknown → absent-direction.
    """
    if not isinstance(lq_out, dict):
        return _absent_record("brain.liquidity_quality.classify", "not supplied")
    label = str(lq_out.get("label") or "").strip().lower()
    if label in ("stress-expansion", "neutral-hollow", "contracting"):
        direction = _RISK_OFF
    elif label == "benign-expansion":
        direction = _RISK_ON
    elif label == "unknown" or not label:
        direction = None
    else:
        direction = _NEUTRAL
    return _plane_record(
        reading=lq_out.get("label"),
        direction=direction,
        magnitude=None,
        asof=lq_out.get("asof"),
        confidence=None,
        validated=False,
        source_contract="brain.liquidity_quality.classify",
        raw={"label": lq_out.get("label"), "components": lq_out.get("components")},
    )


def _adapt_regime_nowcast(nc_out: Any) -> dict[str, Any]:
    """regime_nowcast plane — ADVISORY-ONLY (its walk-forward gate FAILED, hit-rate 0.354).

    ``nc_out`` is a pre-computed ``regime_nowcast.nowcast(...)`` dict (injected).  None →
    absent.  A doubt/strong-doubt stance reads risk_off; confirm reads risk_on.  NEVER
    validated — it is an advisory plane of the view, per the base contract.
    """
    if not isinstance(nc_out, dict):
        return _absent_record("brain.regime_nowcast.nowcast", "not supplied")
    if nc_out.get("applies") is False:
        # the nowcast no-ops on a defensive label — no directional read.
        return _plane_record(
            reading="no-op (label already defensive)",
            direction=None,
            magnitude=None,
            asof=(nc_out.get("legs") or {}).get("asof") if isinstance(nc_out.get("legs"), dict) else None,
            confidence=None,
            validated=False,
            source_contract="brain.regime_nowcast.nowcast (ADVISORY — gate failed 0.354)",
            raw={"applies": False, "stance": nc_out.get("stance")},
        )
    stance = str(nc_out.get("stance") or "").strip().lower()
    if stance in ("doubt", "strong-doubt"):
        direction = _RISK_OFF
    elif stance == "confirm":
        direction = _RISK_ON
    else:
        direction = None
    legs = nc_out.get("legs") if isinstance(nc_out.get("legs"), dict) else {}
    n_doubt = legs.get("n_doubt")
    return _plane_record(
        reading=nc_out.get("reason") or stance,
        direction=direction,
        magnitude=_rf._as_float(n_doubt),
        asof=legs.get("asof"),
        confidence=None,
        validated=False,  # ADVISORY-ONLY forever (gate failed) — never signs the tilt
        source_contract="brain.regime_nowcast.nowcast (ADVISORY — gate failed 0.354)",
        raw={"applies": nc_out.get("applies"), "stance": stance, "n_doubt": n_doubt},
    )


# ---------------------------------------------------------------------------
# Published Macro context planes — advisory/annotate-only
# ---------------------------------------------------------------------------

def _adapt_rrg() -> dict[str, Any]:
    """Adapt Macro's published subsector-rotation/RRG contract.

    RRG quadrants describe relative rotation, not portfolio risk.  The plane therefore
    remains neutral + advisory even when the source's emerging-score track record is
    validated.  It supplies precise leadership/migration context without inventing a
    risk-on/off vote from sector labels.
    """
    try:
        raw = json.loads(_RRG_PATH.read_text())
    except FileNotFoundError:
        return _absent_record(
            "site/marketdata/subsector_rotation.json (RRG)",
            "published RRG contract absent",
        )
    except Exception:  # noqa: BLE001
        return _absent_record(
            "site/marketdata/subsector_rotation.json (RRG)",
            "published RRG contract unreadable",
        )
    if not isinstance(raw, dict):
        return _absent_record(
            "site/marketdata/subsector_rotation.json (RRG)",
            "published RRG contract malformed",
        )
    sectors = [row for row in (raw.get("sectors") or []) if isinstance(row, dict)]
    if not sectors:
        return _absent_record(
            "site/marketdata/subsector_rotation.json (RRG)",
            "RRG contract has no sector rows",
        )

    by_quadrant: dict[str, list[str]] = {
        "leading": [],
        "improving": [],
        "weakening": [],
        "lagging": [],
    }
    for row in sectors:
        quadrant = str(row.get("quadrant") or "").lower()
        key = str(row.get("key") or row.get("name") or "").strip()
        if quadrant in by_quadrant and key:
            by_quadrant[quadrant].append(key)
    parts = [
        f"{name}={','.join(values)}"
        for name, values in by_quadrant.items()
        if values
    ]
    track = raw.get("track_record") if isinstance(raw.get("track_record"), dict) else {}
    horizons = track.get("horizons") if isinstance(track.get("horizons"), dict) else {}
    proven = [
        str(horizon)
        for horizon, passed in (track.get("proven") or {}).items()
        if passed
    ] if isinstance(track.get("proven"), dict) else []
    evidence = [f"RRG {part}" for part in parts]
    for horizon in proven[:4]:
        stats = horizons.get(horizon) if isinstance(horizons.get(horizon), dict) else {}
        evidence.append(
            f"{horizon}d emerging-score IC={stats.get('score_ic')} "
            f"HAC-t={stats.get('score_ic_t_hac')}"
        )
    return _plane_record(
        reading="sector RRG: " + "; ".join(parts),
        direction=_NEUTRAL,
        magnitude=None,
        asof=raw.get("asof") or raw.get("as_of"),
        confidence=None,
        validated=False,
        source_contract="site/marketdata/subsector_rotation.json (RRG; context-only)",
        raw={
            "quadrants": by_quadrant,
            "n_sectors": len(sectors),
            "track_record_verdict": track.get("verdict"),
            "proven_horizons": proven,
            "evidence": evidence,
            "advisory": True,
        },
    )


def _adapt_group_flow() -> dict[str, Any]:
    """Adapt the display-only group concentration/flow context.

    The producer explicitly says the score is uncalibrated and non-directional.  Preserve
    that honesty: expose emerging/cooling groups and cluster absorption, but never turn
    this concentration proxy into a risk vote or a sizing input.
    """
    try:
        raw = json.loads(_GROUP_FLOW_PATH.read_text())
    except FileNotFoundError:
        return _absent_record(
            "site/basketdata/flow.json (display-only concentration proxy)",
            "published group-flow context absent",
        )
    except Exception:  # noqa: BLE001
        return _absent_record(
            "site/basketdata/flow.json (display-only concentration proxy)",
            "published group-flow context unreadable",
        )
    if not isinstance(raw, dict):
        return _absent_record(
            "site/basketdata/flow.json (display-only concentration proxy)",
            "published group-flow context malformed",
        )

    def _names(block: Any) -> list[str]:
        if not isinstance(block, dict):
            return []
        rows = list(block.get("sectors") or []) + list(block.get("baskets") or [])
        return [
            str(row.get("name") or row.get("id"))
            for row in rows
            if isinstance(row, dict) and (row.get("name") or row.get("id"))
        ]

    emerging = _names(raw.get("emerging"))
    cooling = _names(raw.get("cooling"))
    cluster = raw.get("cluster") if isinstance(raw.get("cluster"), dict) else {}
    if not emerging and not cooling and not cluster:
        return _absent_record(
            "site/basketdata/flow.json (display-only concentration proxy)",
            "group-flow context has no group rows",
        )
    cluster_regime = cluster.get("regime")
    absorption = _rf._as_float(cluster.get("absorption"))
    reading = (
        "display-only concentration proxy"
        f" | emerging={','.join(emerging[:4]) or 'none'}"
        f" | cooling={','.join(cooling[:4]) or 'none'}"
        f" | cluster={cluster_regime or 'unknown'}"
        f" absorption={absorption if absorption is not None else 'unknown'}"
    )
    evidence = [
        f"emerging concentration: {', '.join(emerging[:8]) or 'none'}",
        f"cooling concentration: {', '.join(cooling[:8]) or 'none'}",
        f"cluster regime={cluster_regime} absorption={absorption}",
    ]
    return _plane_record(
        reading=reading,
        direction=_NEUTRAL,
        magnitude=None,
        asof=raw.get("as_of") or raw.get("asof"),
        confidence=None,
        validated=False,
        source_contract="site/basketdata/flow.json (display-only; uncalibrated)",
        raw={
            "emerging": emerging[:12],
            "cooling": cooling[:12],
            "cluster_regime": cluster_regime,
            "cluster_absorption": absorption,
            "calibrated": bool(raw.get("calibrated")),
            "directional": False,
            "evidence": evidence,
            "advisory": True,
        },
    )


# ---------------------------------------------------------------------------
# E0.1 / E0.2 organs — lazy-import, degrade to absent while unbuilt
# ---------------------------------------------------------------------------

def _adapt_rotation_tensor() -> dict[str, Any]:
    """rotation_tensor (E0.1) plane.  ADVISORY.  Reads data/market_view/rotation_tensor.json.

    The organ is built by a separate wave task; until it exists this plane is absent (P2).  A
    defensive headline episode reads risk_off; an offensive one reads risk_on.
    """
    try:
        if not _ROTATION_TENSOR_PATH.exists():
            return _absent_record("data/market_view/rotation_tensor.json", "organ unbuilt (E0.1)")
        t = json.loads(_ROTATION_TENSOR_PATH.read_text())
    except Exception:  # noqa: BLE001
        return _absent_record("data/market_view/rotation_tensor.json", "unreadable")
    if not isinstance(t, dict):
        return _absent_record("data/market_view/rotation_tensor.json", "malformed")
    ep = t.get("headline_episode") if isinstance(t.get("headline_episode"), dict) else {}
    ep_dir = str(ep.get("direction") or "").strip().lower()
    if ep_dir == "defensive":
        direction = _RISK_OFF
    elif ep_dir == "offensive":
        direction = _RISK_ON
    elif ep_dir:
        direction = _NEUTRAL
    else:
        direction = None
    return _plane_record(
        reading=(f"{ep.get('axis')} episode {ep.get('n_sessions')}s "
                 f"pct={ep.get('percentile')}" if ep else None),
        direction=direction,
        magnitude=ep.get("magnitude_bps"),
        asof=t.get("as_of"),
        confidence=t.get("confidence"),
        validated=False,
        source_contract="data/market_view/rotation_tensor.json",
        raw={"headline_episode": ep, "advisory": t.get("advisory")},
    )


def _adapt_anticipation() -> dict[str, Any]:
    """anticipation (E0.2) plane.  ADVISORY.  Reads the latest data/anticipation/<asof>.json.

    The organ is built by a separate wave task; until it exists this plane is absent (P2).  A
    CRASH-RISK or SECTOR-TOP alarm at ELEVATED+ reads risk_off.
    """
    try:
        if not _ANTICIPATION_DIR.exists():
            return _absent_record("data/anticipation/<asof>.json", "organ unbuilt (E0.2)")
        dated = sorted(p for p in _ANTICIPATION_DIR.glob("*.json") if p.name != "latest.json")
        latest = _ANTICIPATION_DIR / "latest.json"
        path = latest if latest.exists() else (dated[-1] if dated else None)
        if path is None:
            return _absent_record("data/anticipation/<asof>.json", "no dated artifact")
        a = json.loads(path.read_text())
    except Exception:  # noqa: BLE001
        return _absent_record("data/anticipation/<asof>.json", "unreadable")
    if not isinstance(a, dict):
        return _absent_record("data/anticipation/<asof>.json", "malformed")
    # anticipation.battery() shape: {top_level, sector_top: [...], bubble_formation: [...],
    # crash_risk: {...}} with lowercase levels calm|watch|elevated|critical.  A crash_risk or
    # any sector_top/bubble alarm at elevated+ reads risk_off.  Missing → absent.
    def _lvl(x: Any) -> Optional[str]:
        return str(x).strip().lower() if x is not None else None

    top_level = _lvl(a.get("top_level"))
    crash = a.get("crash_risk") if isinstance(a.get("crash_risk"), dict) else {}
    crash_level = _lvl(crash.get("level"))
    tops = a.get("sector_top") if isinstance(a.get("sector_top"), list) else []
    bubbles = a.get("bubble_formation") if isinstance(a.get("bubble_formation"), list) else []
    levels = [l for l in (crash_level,) if l]
    for lst in (tops, bubbles):
        for rec in lst:
            lv = _lvl(rec.get("level")) if isinstance(rec, dict) else None
            if lv:
                levels.append(lv)
    if top_level is None and not levels:
        return _absent_record("data/anticipation/<asof>.json", "no alarm levels")
    fired = any(l in ("elevated", "critical") for l in ([top_level] + levels) if l)
    direction = _RISK_OFF if fired else (_NEUTRAL if (top_level or levels) else None)
    n_top_fired = sum(1 for r in tops
                      if isinstance(r, dict) and _lvl(r.get("level")) in ("elevated", "critical"))
    return _plane_record(
        reading=(f"top_level={top_level} crash_risk={crash_level} "
                 f"sector_top_fired={n_top_fired}"),
        direction=direction,
        magnitude=None,
        asof=a.get("asof") or a.get("as_of"),
        confidence=None,
        validated=False,
        source_contract="data/anticipation/<asof>.json (battery)",
        raw={"top_level": top_level, "crash_risk_level": crash_level,
             "sector_top_fired": n_top_fired},
    )


# ---------------------------------------------------------------------------
# W-NW.1 Neural Web advisory plane adapter
# ---------------------------------------------------------------------------

def _adapt_neural_web(nw_out: Any) -> dict[str, Any]:
    """Adapt neural_web_context.market_plane() output to a PlaneRecord.

    ALWAYS passes validated=False (ruling §3.3.1): the tilt guard is status-based,
    so this plane is advisory-only and can never sign net_posture_tilt.
    Absent / stale input → _absent_record.

    Direction mapping (advisory caution only, per §3.3.1):
      NW verdict label that implies caution → risk_off; neutral/absent → None.
    """
    if not isinstance(nw_out, dict) or nw_out.get("stale") or not nw_out.get("asof"):
        return _absent_record(
            "vendor/macro/site/neuralwebdata/mastermind_context.json",
            "neural_web context absent or stale (W-NW.1 dark-ship)",
        )
    try:
        verdict = nw_out.get("verdict") or {}
        # Only map to risk_off or neutral; never risk_on (advisory caution only)
        direction: Optional[str] = None
        verdict_label = str(verdict.get("label_en") or verdict.get("verdict") or "").lower()
        if any(kw in verdict_label for kw in ("caution", "risk-off", "risk_off",
                                               "defensive", "bearish", "deteriorat")):
            direction = _RISK_OFF
        elif verdict_label:
            direction = _NEUTRAL

        regime = nw_out.get("regime") or {}
        conf_raw = regime.get("confidence")
        quad_name = regime.get("quad_name") or ""
        cycle_tag = regime.get("cycle_tag") or ""
        contr_count = nw_out.get("contradiction_count", 0)
        asof = nw_out.get("asof")

        reading = (
            f"NW: {quad_name} cycle={cycle_tag} contradictions={contr_count}"
            if (quad_name or cycle_tag) else None
        )

        return _plane_record(
            reading=reading,
            direction=direction,
            magnitude=None,
            asof=asof,
            confidence=conf_raw,
            validated=False,   # §3.3.1: NEVER validated; advisory-only
            source_contract="vendor/macro/site/neuralwebdata/mastermind_context.json",
            raw={
                "verdict": verdict,
                "regime": regime,
                "contradiction_count": contr_count,
                "advisory": True,
            },
        )
    except Exception:  # noqa: BLE001
        return _absent_record(
            "vendor/macro/site/neuralwebdata/mastermind_context.json",
            "neural_web adapter error",
        )


# ---------------------------------------------------------------------------
# label plane — the regime label direction (what the tilt is measured AGAINST)
# ---------------------------------------------------------------------------

def _label_direction(raw: dict[str, Any]) -> tuple[Optional[str], Any, Any]:
    """Return (direction, quad_name, confidence) for the regime LABEL itself.

    Goldilocks / Reflation (Q1/Q2) risk-on labels read risk_on; Deflation / Stagflation
    (Q3/Q4) read risk_off; anything else neutral.  The label is NOT a plane in ``planes`` —
    it is the thing ``label_vs_planes`` measures the validated-plane consensus against.
    """
    quad = raw.get("quad")
    quad_name = raw.get("quad_name")
    conf = _rf._as_float(raw.get("confidence"))
    q = str(quad or "").strip().upper()
    qn = str(quad_name or "").strip().lower()
    if q in ("Q1", "Q2") or qn in ("goldilocks", "reflation"):
        direction = _RISK_ON
    elif q in ("Q3", "Q4") or qn in ("deflation", "stagflation"):
        direction = _RISK_OFF
    elif quad is not None or quad_name is not None:
        direction = _NEUTRAL
    else:
        direction = None
    return direction, quad_name, conf


# ---------------------------------------------------------------------------
# the disagreement layer — the wave's point (validated-only tilt)
# ---------------------------------------------------------------------------

def _tilt_score(direction: Optional[str]) -> int:
    """Map a discrete direction to a signed vote: risk_off -1, neutral 0, risk_on +1, None 0."""
    if direction == _RISK_OFF:
        return -1
    if direction == _RISK_ON:
        return 1
    return 0


def _net_posture_tilt(planes: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """The net posture tilt from VALIDATED, FRESH planes ONLY (P3).

    Advisory/absent/stale planes weight 0 — they can annotate but never sign the tilt.  A
    stale validated plane has already been downgraded to status=='advisory' by _plane_record,
    so it is excluded here automatically.  Returns a signed float in [-1, 1] (mean vote over
    the validated planes) plus the contributing plane names.
    """
    votes: list[int] = []
    contributors: list[str] = []
    for name in PLANE_ORDER:
        rec = planes.get(name)
        if not isinstance(rec, dict):
            continue
        if rec.get("status") != "validated":
            continue
        if (rec.get("freshness") or {}).get("stale"):
            continue
        d = rec.get("direction")
        if d is None:
            continue
        votes.append(_tilt_score(d))
        contributors.append(name)
    if not votes:
        return {"tilt": 0.0, "direction": None, "n_validated": 0, "contributors": []}
    mean = sum(votes) / len(votes)
    if mean < -1e-9:
        direction = _RISK_OFF
    elif mean > 1e-9:
        direction = _RISK_ON
    else:
        direction = _NEUTRAL
    return {
        "tilt": round(mean, 6),
        "direction": direction,
        "n_validated": len(votes),
        "contributors": contributors,
    }


def _disagreements(planes: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Named pairwise splits between planes with OPPOSITE discrete directions.

    Only planes with a determinable direction participate; absent/None-direction planes
    cannot create OR lower a disagreement (P2).  Each pair is emitted once, in PLANE_ORDER,
    with the validated flag on each side so a consumer can weight validated splits higher.
    """
    directional = [
        (name, planes[name])
        for name in PLANE_ORDER
        if isinstance(planes.get(name), dict)
        and planes[name].get("direction") in (_RISK_OFF, _RISK_ON)
    ]
    out: list[dict[str, Any]] = []
    for i in range(len(directional)):
        ni, ri = directional[i]
        for j in range(i + 1, len(directional)):
            nj, rj = directional[j]
            if ri["direction"] != rj["direction"]:
                out.append({
                    "a": ni, "a_direction": ri["direction"],
                    "a_validated": ri.get("status") == "validated",
                    "b": nj, "b_direction": rj["direction"],
                    "b_validated": rj.get("status") == "validated",
                })
    return out


def _coherence(planes: dict[str, dict[str, Any]]) -> Optional[float]:
    """A 0-1 coherence scalar = agreement among FRESH directional planes.

    1.0 = every directional plane agrees; 0.0 = a perfect even split.  Absent/None-direction
    and stale planes are excluded (they cannot manufacture coherence).  None when no fresh
    plane is directional.  This is descriptive only — it never signs the tilt.
    """
    dirs = [
        planes[name]["direction"]
        for name in PLANE_ORDER
        if isinstance(planes.get(name), dict)
        and planes[name].get("direction") in (_RISK_OFF, _RISK_ON, _NEUTRAL)
        and not (planes[name].get("freshness") or {}).get("stale")
        and (planes[name].get("raw") or {}).get("artifact_present", True) is not False
    ]
    dirs = [d for d in dirs if d is not None]
    if not dirs:
        return None
    off = dirs.count(_RISK_OFF)
    on = dirs.count(_RISK_ON)
    neu = dirs.count(_NEUTRAL)
    total = off + on + neu
    if total == 0:
        return None
    # coherence = the modal share (how dominant the winning direction is).
    return round(max(off, on, neu) / total, 4)


def _label_vs_planes(
    label_dir: Optional[str],
    tilt: dict[str, Any],
    planes: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """THE first-class field: does the regime LABEL conflict with the validated-plane consensus?

    conflict=True iff the label reads one risk direction and the validated consensus reads the
    OPPOSITE (risk_on-label vs risk_off-consensus, or vice-versa).  A neutral label or a
    neutral/absent consensus is NOT a conflict.  ``dissenting_planes`` names the validated,
    fresh planes whose direction opposes the label — the incident's "3 validated planes
    dissent" made auditable.  ``magnitude`` = |tilt| (how strong the dissent is).
    """
    consensus = tilt.get("direction")
    conflict = (
        label_dir in (_RISK_ON, _RISK_OFF)
        and consensus in (_RISK_ON, _RISK_OFF)
        and label_dir != consensus
    )
    dissenting: list[str] = []
    if label_dir in (_RISK_ON, _RISK_OFF):
        for name in tilt.get("contributors", []):
            rec = planes.get(name)
            if isinstance(rec, dict) and rec.get("direction") not in (label_dir, None, _NEUTRAL):
                dissenting.append(name)
    if conflict:
        relationship = "conflict"
    elif (
        label_dir in (_RISK_ON, _RISK_OFF)
        and consensus in (_RISK_ON, _RISK_OFF)
        and label_dir == consensus
    ):
        relationship = "confirmed"
    elif label_dir is None or consensus is None:
        relationship = "unavailable"
    else:
        # A neutral label or consensus is an abstention, never confirmation.
        relationship = "unconfirmed"
    return {
        "label_direction": label_dir,
        "plane_consensus_direction": consensus,
        "conflict": bool(conflict),
        "relationship": relationship,
        "confirmed": relationship == "confirmed",
        "magnitude": abs(_rf._as_float(tilt.get("tilt")) or 0.0),
        "dissenting_planes": dissenting,
    }


# ---------------------------------------------------------------------------
# deterministic brief — zero LLM
# ---------------------------------------------------------------------------

def _brief(
    planes: dict[str, dict[str, Any]],
    label_vs_planes: dict[str, Any],
    tilt: dict[str, Any],
    prev_view: Optional[dict[str, Any]],
) -> dict[str, Any]:
    """A fully templated brief from the planes — zero LLM.  Golden key-order tested.

    ``what_changed`` diffs the label-vs-planes conflict vs yesterday's artifact (if supplied);
    ``whats_rotating`` names the rotation/cycle planes; ``wheres_the_risk`` names the risk_off
    directional planes; ``posture_implication`` states the shrink-only implication (never a
    sizing instruction — this is display).
    """
    def _reading(name: str) -> Optional[str]:
        rec = planes.get(name)
        r = rec.get("reading") if isinstance(rec, dict) else None
        return str(r) if r is not None else None

    conflict = label_vs_planes.get("conflict")
    relationship = label_vs_planes.get("relationship")
    # what_changed
    prev_conflict = None
    if isinstance(prev_view, dict):
        prev_conflict = (prev_view.get("label_vs_planes") or {}).get("conflict")
    if conflict and prev_conflict is not True:
        what_changed = ("Label-vs-planes conflict OPENED: the regime label "
                        f"({label_vs_planes.get('label_direction')}) now disagrees with the "
                        f"validated-plane consensus ({label_vs_planes.get('plane_consensus_direction')}).")
    elif conflict and prev_conflict is True:
        what_changed = ("Label-vs-planes conflict PERSISTS: validated planes continue to "
                        f"dissent from the {label_vs_planes.get('label_direction')} label.")
    elif not conflict and prev_conflict is True:
        if relationship == "confirmed":
            what_changed = "Label-vs-planes conflict RESOLVED: validated planes now confirm the label."
        else:
            what_changed = (
                "Label-vs-planes conflict CLOSED, but confirmation is absent: "
                f"relationship={relationship}."
            )
    elif relationship == "confirmed":
        what_changed = "Validated planes confirm the regime label."
    elif relationship == "unconfirmed":
        what_changed = (
            "Regime label is UNCONFIRMED: the validated-plane consensus is neutral or abstaining."
        )
    else:
        what_changed = "Label-vs-planes confirmation is unavailable."

    # whats_rotating — cycles + rotation_tensor readings
    rot_bits = [b for b in (_reading("cycles"), _reading("rotation_tensor")) if b]
    whats_rotating = "; ".join(rot_bits) if rot_bits else "No rotation plane available."

    # wheres_the_risk — the risk_off directional planes, validated first
    risk_names = [
        name for name in PLANE_ORDER
        if isinstance(planes.get(name), dict) and planes[name].get("direction") == _RISK_OFF
    ]
    risk_names.sort(key=lambda n: 0 if planes[n].get("status") == "validated" else 1)
    if risk_names:
        def _risk_label(name: str) -> str:
            rec = planes[name]
            tier = "V" if rec.get("status") == "validated" else "A"
            freshness = "stale" if (rec.get("freshness") or {}).get("stale") else "fresh"
            return f"{name}({tier},{freshness}): {_reading(name)}"

        wheres_the_risk = "; ".join(
            _risk_label(name) for name in risk_names[:5]
        )
    else:
        wheres_the_risk = "No plane reads risk_off."

    # posture_implication — shrink-only, display language; mentions defense on conflict
    if conflict and label_vs_planes.get("plane_consensus_direction") == _RISK_OFF:
        posture_implication = (
            "Validated planes dissent risk_off while the label reads risk_on — a "
            "defensive tilt is warranted; on this view the perception layer would lean toward "
            "raising the defensive floor and shrinking offense (shrink-only; no cap release).")
    elif tilt.get("direction") == _RISK_OFF:
        posture_implication = (
            "Validated consensus reads risk_off — the perception layer leans defensive "
            "(shrink-only).")
    elif tilt.get("direction") == _RISK_ON and relationship == "confirmed":
        posture_implication = (
            "Validated consensus reads risk_on and confirms the label — no defensive "
            "shrink implied by the view.")
    elif tilt.get("direction") == _RISK_ON:
        posture_implication = (
            "Validated consensus reads risk_on but does not confirm the regime label; "
            "the view does not authorize loosening risk."
        )
    else:
        posture_implication = (
            "No validated directional consensus — the view neither loosens nor tightens.")

    return {
        "what_changed": what_changed,
        "whats_rotating": whats_rotating,
        "wheres_the_risk": wheres_the_risk,
        "posture_implication": posture_implication,
    }


# ---------------------------------------------------------------------------
# budget_ref — read-only audit embed (what budget() currently reads; NO wiring)
# ---------------------------------------------------------------------------

def _budget_ref(region: str) -> dict[str, Any]:
    """Embed ``regime_frame.budget(region)`` read-only for a complete posture audit record.

    This is an AUDIT field — the view does NOT feed budget() and budget() does NOT read the
    view (the wave contract).  A budget() failure degrades to an absent ref (never raises).
    """
    try:
        b = _rf.budget(region)
        if isinstance(b, dict):
            return {"lead_budget": b.get("lead_budget"), "inputs": b.get("inputs")}
    except Exception:  # noqa: BLE001
        pass
    return {"lead_budget": None, "inputs": None}


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------

def view(
    region: str = "us",
    *,
    distribution_tells_out: Any = None,
    liquidity_quality_out: Any = None,
    regime_nowcast_out: Any = None,
    neural_web_out: Any = None,
    prev_view: Any = None,
    seq: int = 0,
) -> dict[str, Any]:
    """Assemble THE market view for *region* (deterministic, zero LLM).

    The W-I planes are INJECTED (pre-computed dicts) so this function never live-reads the
    shared mutating series stores — ``phase2`` passes the production reads; tests inject pure
    dicts.  A ``None`` W-I arg → that plane is absent (P2), never fabricated.  Every path
    degrades — this function NEVER raises.

    Returns the market_view.v1 dict in golden key order (TOP_LEVEL_ORDER); the artifact feeds
    LLM prompts so key-order is load-bearing.
    """
    raw = _rf._read_raw(region)
    if not isinstance(raw, dict):
        raw = {}

    # ---- assemble every plane (each degrades to an absent record independently) ----
    planes: dict[str, dict[str, Any]] = {}
    planes["risk_radar"] = _adapt_risk_radar(raw)
    planes["mtf_signals"] = _adapt_mtf_signals(raw)
    planes["froth_fragility"] = _adapt_froth_fragility(raw)
    planes["gross_factor"] = _adapt_gross_factor(raw)
    planes["turning_point"] = _adapt_turning_point(raw)
    planes["vol_shock"] = _adapt_vol_shock(raw)
    planes["cross_asset"] = _adapt_cross_asset(raw)
    planes["market_drivers"] = _adapt_market_drivers(raw)
    planes["dislocation"] = _adapt_dislocation(raw)
    planes["macro_risk"] = _adapt_macro_risk(raw)
    planes["cycles"] = _adapt_cycles(raw)
    planes["distribution_tells"] = _adapt_distribution_tells(distribution_tells_out)
    planes["liquidity_quality"] = _adapt_liquidity_quality(liquidity_quality_out)
    planes["regime_nowcast"] = _adapt_regime_nowcast(regime_nowcast_out)
    planes["rotation_tensor"] = _adapt_rotation_tensor()
    planes["anticipation"] = _adapt_anticipation()
    # Published, context-only Macro contracts.  Both remain advisory and non-directional.
    planes["rrg"] = _adapt_rrg()
    planes["group_flow"] = _adapt_group_flow()
    # Remaining H4 handoffs stay explicit nulls until their point-in-time contracts exist.
    planes["event_calendar"] = _absent_record("engine.event_calendar",
                                              "H4 handoff — pending")
    planes["intl_spillover"] = _absent_record("intl spillover",
                                              "H4 handoff — pending")
    # W-NW.1 Neural Web advisory plane — dark-ship (always-on reader; flag gates prompt/sizing)
    planes["neural_web"] = _adapt_neural_web(neural_web_out)

    # keep planes in golden order (dict insertion order == PLANE_ORDER above)
    ordered_planes = {k: planes[k] for k in PLANE_ORDER}

    # ---- the disagreement layer ----
    label_dir, _quad_name, _label_conf = _label_direction(raw)
    tilt = _net_posture_tilt(ordered_planes)
    disagreements = _disagreements(ordered_planes)
    coherence = _coherence(ordered_planes)
    lvp = _label_vs_planes(label_dir, tilt, ordered_planes)

    # posture_floor_defense: the top-level "this view warrants a defensive floor" flag — True iff
    # the label conflicts with a risk_off validated consensus (P2: annotate/shrink only).
    posture_floor_defense = bool(
        lvp["conflict"] and lvp.get("plane_consensus_direction") == _RISK_OFF
    )

    # ---- coverage + assembly bookkeeping ----
    total = len(PLANE_ORDER)
    def _artifact_present(rec: dict[str, Any]) -> bool:
        raw_rec = rec.get("raw") or {}
        if "artifact_present" in raw_rec:
            return raw_rec.get("artifact_present") is not False
        return raw_rec.get("present", True) is not False

    fresh = sum(
        1 for r in ordered_planes.values()
        if _artifact_present(r)
        and not (r.get("freshness") or {}).get("stale")
    )
    stale = sum(
        1 for r in ordered_planes.values()
        if _artifact_present(r)
        and (r.get("freshness") or {}).get("stale")
    )
    missing = sum(
        1 for r in ordered_planes.values()
        if not _artifact_present(r)
    )
    present = total - missing
    coverage = round(present / total, 4) if total else 0.0
    validated = int(tilt.get("n_validated") or 0)
    decision_coverage = round(validated / len(_VALIDATED_PLANES), 4)
    fresh_coverage = round(fresh / total, 4) if total else 0.0
    degrade_reasons: list[str] = []
    if coverage < _coverage_floor():
        degrade_reasons.append("availability_coverage_below_floor")
    if decision_coverage < 1.0:
        degrade_reasons.append("validated_decision_plane_incomplete")
    degraded = bool(degrade_reasons)

    assembly = {
        "total": total,
        "present": present,
        "fresh": fresh,
        "stale": stale,
        "missing": missing,
        "validated": validated,
        "decision_total": len(_VALIDATED_PLANES),
        "decision_coverage": decision_coverage,
        "fresh_coverage": fresh_coverage,
        "degrade_reasons": degrade_reasons,
        "degraded": bool(degraded),
    }

    # ---- deterministic brief ----
    prev = prev_view if isinstance(prev_view, dict) else None
    brief = _brief(ordered_planes, lvp, tilt, prev)

    # ---- asof = the regime file's date (the base plane's own date) ----
    asof = raw.get("date")

    out: dict[str, Any] = {
        "schema_version": _SCHEMA_VERSION,
        "region": region,
        "asof": asof,
        "built_at": datetime.now(tz=timezone.utc).isoformat(),
        "seq": int(seq),
        "planes": ordered_planes,
        "net_posture_tilt": tilt,
        "coverage": coverage,
        "coherence": coherence,
        "disagreements": disagreements,
        "label_vs_planes": lvp,
        "posture_floor_defense": posture_floor_defense,
        "assembly": assembly,
        "budget_ref": _budget_ref(region),
        "brief": brief,
    }
    # enforce golden top-level order (defensive: build a fresh ordered dict)
    return {k: out[k] for k in TOP_LEVEL_ORDER}


def _read_prev_latest() -> Optional[dict[str, Any]]:
    """Read the previously-published latest.json for the brief's what_changed diff, or None."""
    try:
        if _LATEST_PATH.exists():
            d = json.loads(_LATEST_PATH.read_text())
            return d if isinstance(d, dict) else None
    except Exception:  # noqa: BLE001
        pass
    return None


def build(
    region: str = "us",
    *,
    write: bool = True,
    distribution_tells_out: Any = None,
    liquidity_quality_out: Any = None,
    regime_nowcast_out: Any = None,
    neural_web_out: Any = None,
    seq: int | None = None,
) -> dict[str, Any]:
    """Build the view and (optionally) atomically publish latest.json + a dated copy.

    The what_changed diff reads the CURRENT latest.json BEFORE overwriting it.  Writes are
    atomic (tmp + os.replace) so a reader never sees a half-written artifact.  A write failure
    degrades to returning the view without publishing (never raises — the wave contract is that
    a perception organ that fails to publish protects nothing but breaks nothing).
    """
    prev = _read_prev_latest()
    if seq is None:
        try:
            resolved_seq = int((prev or {}).get("seq") or 0) + 1
        except (TypeError, ValueError):
            resolved_seq = 1
    else:
        resolved_seq = int(seq)
    v = view(
        region,
        distribution_tells_out=distribution_tells_out,
        liquidity_quality_out=liquidity_quality_out,
        regime_nowcast_out=regime_nowcast_out,
        neural_web_out=neural_web_out,
        prev_view=prev,
        seq=resolved_seq,
    )
    if not write:
        return v
    try:
        _ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(v, indent=2, default=str)
        # atomic: write tmp then os.replace onto the final path
        tmp = _LATEST_PATH.with_suffix(".json.tmp")
        tmp.write_text(payload)
        os.replace(tmp, _LATEST_PATH)
        # dated copy (keyed on the view's own asof, falling back to today)
        asof = v.get("asof") or date.today().isoformat()
        dated = _ARTIFACT_DIR / f"{asof}.json"
        dated_tmp = dated.with_suffix(".json.tmp")
        dated_tmp.write_text(payload)
        os.replace(dated_tmp, dated)
    except Exception:  # noqa: BLE001 — publish failure never breaks a build
        pass
    return v
