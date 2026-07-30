"""Typed, point-in-time perception contract for Mastermind reasoning seats.

``market_view.v1`` remains the display/compatibility artifact.  This module builds the
loss-minimizing ``decision_context.v2`` contract used by AI seats: regime probabilities and
trajectory are kept separate from the hard label; every plane carries availability, authority,
units, source, and freshness; Neural Web lobes are admitted individually only when fresh.

This artifact is read-only.  It grants no new sizing authority and never converts an advisory
plane into a governor vote.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from brain import regime_frame as _rf

_ROOT = Path(__file__).resolve().parent.parent
_ARTIFACT_DIR = _ROOT / "data" / "decision_context"
_LATEST_PATH = _ARTIFACT_DIR / "latest.json"
_MARKET_VIEW_PATH = _ROOT / "data" / "market_view" / "latest.json"
_SCHEMA_VERSION = "decision_context.v2"

# Unit and horizon metadata prevents incomparable magnitudes from masquerading as one scale.
_SIGNAL_META: dict[str, tuple[str, str | None]] = {
    "risk_radar": ("probability", "21_sessions"),
    "mtf_signals": ("normalized_strength", "multi_horizon"),
    "froth_fragility": ("score_0_100", "tactical"),
    "gross_factor": ("gross_multiplier", "current"),
    "turning_point": ("event_state", "turning_point"),
    "vol_shock": ("score_0_100", "forward"),
    "cross_asset": ("percentile_0_1", "cross_asset"),
    "market_drivers": ("signed_direction", "5_sessions"),
    "dislocation": ("score", "tactical"),
    "macro_risk": ("score_0_1", "current"),
    "cycles": ("late_cycle_share", "medium_term"),
    "distribution_tells": ("book_weight_share", "tactical"),
    "liquidity_quality": ("categorical_state", "20_sessions"),
    "regime_nowcast": ("doubting_legs_count", "tactical"),
    "rotation_tensor": ("basis_points_per_day", "rotation"),
    "anticipation": ("categorical_state", "forward"),
    "rrg": ("categorical_state", "rotation"),
    "group_flow": ("categorical_state", "tactical"),
    "event_calendar": ("event_count", "calendar"),
    "intl_spillover": ("categorical_state", "cross_asset"),
    "neural_web": ("categorical_state", "context"),
}

_SHRINK_ONLY = {
    "risk_radar",
    "mtf_signals",
    "cycles",
    "distribution_tells",
    "liquidity_quality",
    "regime_nowcast",
    "rotation_tensor",
    "anticipation",
}


def _read_json(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text())
        return raw if isinstance(raw, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _as_float(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _artifact_present(rec: dict[str, Any]) -> bool:
    raw = rec.get("raw") or {}
    if not isinstance(raw, dict):
        return True
    if "artifact_present" in raw:
        return raw.get("artifact_present") is not False
    return raw.get("present", True) is not False


def _signal_active(rec: dict[str, Any]) -> bool | None:
    raw = rec.get("raw") or {}
    if isinstance(raw, dict) and "signal_active" in raw:
        return bool(raw.get("signal_active"))
    direction = rec.get("direction")
    if direction is None:
        return None
    return direction != "neutral"


def _missing_reason(rec: dict[str, Any]) -> str | None:
    raw = rec.get("raw") or {}
    if not isinstance(raw, dict):
        return None
    reason = raw.get("note")
    return str(reason)[:240] if reason else None


def _evidence(rec: dict[str, Any]) -> list[str]:
    raw = rec.get("raw") or {}
    if not isinstance(raw, dict):
        return []
    candidates: list[Any] = []
    for key in ("evidence", "drivers", "contradictions"):
        value = raw.get(key)
        if isinstance(value, list):
            candidates.extend(value)
    out: list[str] = []
    for value in candidates[:8]:
        if isinstance(value, str):
            out.append(value[:180])
        elif isinstance(value, dict):
            text = value.get("detail_en") or value.get("label") or value.get("source")
            if text:
                out.append(str(text)[:180])
    return out


def _signal_record(
    name: str,
    rec: dict[str, Any],
    *,
    context_asof: str | None = None,
) -> dict[str, Any]:
    present = _artifact_present(rec)
    fresh = rec.get("freshness") if isinstance(rec.get("freshness"), dict) else {}
    source_asof = str(fresh.get("asof") or "")[:10] or None
    future_relative = bool(context_asof and source_asof and source_asof > context_asof)
    stale = bool(fresh.get("stale", True)) or future_relative
    validated = rec.get("status") == "validated" and present and not stale
    unit, horizon = _SIGNAL_META.get(name, ("untyped", None))
    if validated:
        allowed_effect = "governor_vote"
        layer = "governor"
    elif name in _SHRINK_ONLY:
        allowed_effect = "shrink_only"
        layer = "context"
    else:
        allowed_effect = "annotate_only"
        layer = "context"
    return {
        "name": name,
        "layer": layer,
        "allowed_effect": allowed_effect,
        "direction": rec.get("direction"),
        "reading": str(rec.get("reading") or "")[:240] or None,
        "value": {
            "value": _as_float(rec.get("magnitude")),
            "unit": unit,
            "horizon": horizon,
        },
        "confidence": _as_float(rec.get("confidence")),
        "freshness": {
            "market_asof": source_asof,
            "age_sessions": fresh.get("age_sessions"),
            "stale": stale,
            "future_dated": bool(fresh.get("future_dated")) or future_relative,
            "stale_after_sessions": fresh.get("stale_after_sessions"),
            "valid_until": None,
        },
        "availability": {
            "artifact_present": present,
            "signal_active": _signal_active(rec) if present else False,
            "missing_reason": _missing_reason(rec) if not present else None,
        },
        "source": str(rec.get("source_contract") or "")[:240] or None,
        "evidence": _evidence(rec),
    }


def _label_direction(regime: dict[str, Any]) -> str | None:
    quad = str(regime.get("quad") or "").upper()
    name = str(regime.get("quad_name") or "").lower()
    if quad in ("Q1", "Q2") or name in ("goldilocks", "reflation"):
        return "risk_on"
    if quad in ("Q3", "Q4") or name in ("deflation", "stagflation"):
        return "risk_off"
    return None


def _regime_state(regime: dict[str, Any]) -> dict[str, Any]:
    vector = regime.get("quad_vector") if isinstance(regime.get("quad_vector"), dict) else {}
    probabilities = vector.get("p") if isinstance(vector.get("p"), dict) else {}
    transition_momentum = (
        vector.get("transition_momentum")
        if isinstance(vector.get("transition_momentum"), dict)
        else {}
    )
    flip = regime.get("flip_condition") if isinstance(regime.get("flip_condition"), dict) else {}
    liquidity_quality = (
        regime.get("liquidity_quality")
        if isinstance(regime.get("liquidity_quality"), dict)
        else {}
    )
    risk = regime.get("risk_state") if isinstance(regime.get("risk_state"), dict) else {}
    driver = (
        regime.get("market_drivers")
        if isinstance(regime.get("market_drivers"), dict)
        else {}
    )
    business_cycle = (
        regime.get("business_cycle")
        if isinstance(regime.get("business_cycle"), dict)
        else {}
    )
    regime_asof = str(regime.get("date") or "")[:10] or None
    cycle_asof = str(business_cycle.get("asof") or "")[:10] or None
    cycle_future_dated = bool(
        regime_asof and cycle_asof and cycle_asof > regime_asof
    )
    cycle_detail = {
        "asof": cycle_asof,
        "available": business_cycle.get("available"),
        "calibrated": business_cycle.get("calibrated"),
        "phase": business_cycle.get("phase"),
        "recession_now": business_cycle.get("recession_now"),
        "recession_signal": business_cycle.get("recession_signal"),
        "caveat": business_cycle.get("caveat"),
        "future_dated": cycle_future_dated,
        "admitted": bool(business_cycle) and not cycle_future_dated,
    }
    if cycle_future_dated:
        # Preserve the provenance defect, but quarantine every forward-dated value.
        cycle_detail = {
            "asof": cycle_asof,
            "future_dated": True,
            "admitted": False,
            "degrade_reason": "source_asof_after_regime_market_asof",
        }
    return {
        "market_asof": regime_asof,
        "hard_label": {
            "quad": regime.get("quad"),
            "name": regime.get("quad_name"),
            "direction": _label_direction(regime),
            "confidence": _as_float(regime.get("confidence")),
        },
        "probabilistic_state": {
            "probabilities": {str(k): _as_float(v) for k, v in probabilities.items()},
            "hard_label_agrees": vector.get("hard_label_agrees"),
            "confidence": _as_float(vector.get("confidence")),
            "source": vector.get("source"),
            "degraded": bool(vector.get("degraded")) if vector else None,
        },
        "trajectory": {
            "transition_state": regime.get("transition_state"),
            "transition_state_raw": regime.get("transition_state_raw"),
            "flip_margin": _as_float(
                regime.get("flip_margin")
                if regime.get("flip_margin") is not None
                else flip.get("margin")
            ),
            "flip_axis": flip.get("axis"),
            "flip_component": flip.get("component"),
            "gaining_quad": transition_momentum.get("gaining"),
            "gaining_rate_5s": _as_float(transition_momentum.get("gaining_rate")),
            "losing_quad": transition_momentum.get("losing"),
            "losing_rate_5s": _as_float(transition_momentum.get("losing_rate")),
            "window_sessions": transition_momentum.get("window_sessions"),
            "confirming": list(regime.get("confirming") or [])[:20],
            "contradicting": list(regime.get("contradicting") or [])[:20],
        },
        "axes": {
            "growth": {
                "level": _as_float(regime.get("growth_score")),
                "confidence": _as_float(regime.get("growth_confidence")),
                "slope": _as_float(regime.get("growth_slope")),
                "acceleration": _as_float(regime.get("growth_acceleration")),
            },
            "inflation": {
                "level": _as_float(regime.get("inflation_score")),
                "confidence": _as_float(regime.get("inflation_confidence")),
                "slope": _as_float(regime.get("inflation_slope")),
                "acceleration": _as_float(regime.get("inflation_acceleration")),
            },
        },
        "cycle": {
            "tag": regime.get("cycle_tag"),
            "business_cycle": cycle_detail,
        },
        "liquidity": {
            "quantity_overlay": regime.get("liquidity_overlay"),
            "quality": liquidity_quality,
        },
        "risk": {
            "state": risk.get("state"),
            "score": _as_float(risk.get("score")),
            "gross_factor": _as_float(risk.get("gross_factor")),
            "headline": risk.get("headline_en"),
        },
        "market_driver": {
            "primary": driver.get("primary"),
            "label": driver.get("primary_label"),
            "direction": driver.get("direction"),
            "confidence": driver.get("confidence"),
            "strength": _as_float(driver.get("strength")),
            "evidence": list(driver.get("evidence") or [])[:8],
            "invalidation": driver.get("invalidation"),
        },
    }


def _pick(source: Any, keys: tuple[str, ...]) -> dict[str, Any]:
    if not isinstance(source, dict):
        return {}
    return {key: source.get(key) for key in keys if source.get(key) is not None}


def _neural_web_state(
    neural_web: dict[str, Any],
    *,
    context_asof: str | None = None,
) -> dict[str, Any]:
    if not neural_web:
        return {
            "artifact_asof": None,
            "lobe_health": {},
            "health_summary": {"fresh": 0, "stale": 0, "unknown": 0},
            "contexts": {},
        }
    try:
        from brain import neural_web_context as _nwc

        lobes = neural_web.get("lobes") or {}
        names = sorted(lobes) if isinstance(lobes, dict) else []
        health = {name: _nwc.lobe_freshness(name, neural_web) for name in names}
        for row in health.values():
            lobe_asof = str(row.get("asof") or "")[:10] or None
            future_relative = bool(
                context_asof and lobe_asof and lobe_asof > context_asof
            )
            row["future_dated"] = future_relative
            if future_relative:
                row["stale"] = True
                row["degrade_reason"] = "lobe_asof_after_decision_market_asof"
        fresh_n = sum(1 for row in health.values() if not row.get("stale", True))
        stale_n = sum(1 for row in health.values() if row.get("stale", True))
        unknown_n = sum(1 for row in health.values() if row.get("age_days") is None)

        def fresh_lobe(name: str) -> dict[str, Any]:
            if health.get(name, {}).get("stale", True):
                return {}
            row = lobes.get(name) if isinstance(lobes, dict) else None
            return row if isinstance(row, dict) else {}

        macro = fresh_lobe("macro_weather")
        rotation = fresh_lobe("theme_rotation")
        structure = fresh_lobe("market_structure")
        rates = fresh_lobe("rates_command")
        transmission = fresh_lobe("transmission_chains")
        contradictions = fresh_lobe("contradictions")
        contradiction_rows = contradictions.get("records") or []
        contexts = {
            "macro_weather": _pick(
                macro,
                ("us_quad", "china_quad", "hk_quad", "canada_quad",
                 "fx", "rates", "credit", "cross_asset", "contradiction_note"),
            ),
            "theme_rotation": _pick(
                rotation,
                ("leadership_state", "days_in_state", "stance_en",
                 "trailing_leader_name", "trailing_leader_health",
                 "trailing_leader_breadth", "trailing_leader_r10",
                 "strength_names", "migration_absorbing", "migration_bleeding",
                 "sector_rotation_agrees"),
            ),
            "market_structure": _pick(
                structure,
                ("gamma_regime", "net_gex_bn", "dist_to_flip_pct", "cta_state",
                 "cta_z", "vc_state", "agreement", "cor1m_regime",
                 "cor1m_pctile_2y", "honesty_note"),
            ),
            "rates": _pick(
                rates,
                ("net_state", "state_label_en", "hawk_score", "ease_score",
                 "implied_m12", "policy_rate", "path_plain_en", "honesty_note"),
            ),
            "transmission": _pick(
                transmission,
                ("n_active", "n_dormant", "summary", "honesty_note"),
            ),
            "contradictions": [
                _pick(row, ("source", "severity", "summary", "detail", "pair"))
                for row in contradiction_rows[:12]
                if isinstance(row, dict)
            ],
        }
        return {
            "artifact_asof": neural_web.get("as_of"),
            "lobe_health": health,
            "health_summary": {
                "fresh": fresh_n,
                "stale": stale_n,
                "unknown": unknown_n,
            },
            # Cortex prose and raw candidate matrices are structurally excluded.
            "contexts": {k: v for k, v in contexts.items() if v},
        }
    except Exception:  # noqa: BLE001
        return {
            "artifact_asof": neural_web.get("as_of"),
            "lobe_health": {},
            "health_summary": {"fresh": 0, "stale": 0, "unknown": 0},
            "contexts": {},
        }


def assemble(
    regime: dict[str, Any] | None,
    market_view: dict[str, Any] | None,
    *,
    neural_web: dict[str, Any] | None = None,
    region: str = "us",
    seq: int = 0,
    built_at: str | None = None,
) -> dict[str, Any]:
    """Assemble ``decision_context.v2`` without writing."""
    regime = regime if isinstance(regime, dict) else {}
    market_view = market_view if isinstance(market_view, dict) else {}
    context_asof = str(regime.get("date") or market_view.get("asof") or "")[:10] or None
    planes = market_view.get("planes") if isinstance(market_view.get("planes"), dict) else {}
    signals = [
        _signal_record(name, rec, context_asof=context_asof)
        for name, rec in planes.items()
        if isinstance(rec, dict)
    ]
    lvp = (
        market_view.get("label_vs_planes")
        if isinstance(market_view.get("label_vs_planes"), dict)
        else {}
    )
    tilt = (
        market_view.get("net_posture_tilt")
        if isinstance(market_view.get("net_posture_tilt"), dict)
        else {}
    )
    assembly = (
        market_view.get("assembly")
        if isinstance(market_view.get("assembly"), dict)
        else {}
    )
    present = sum(1 for row in signals if row["availability"]["artifact_present"])
    fresh = sum(
        1 for row in signals
        if row["availability"]["artifact_present"] and not row["freshness"]["stale"]
    )
    regime_state = _regime_state(regime)
    neural_state = _neural_web_state(neural_web or {}, context_asof=context_asof)
    cycle_detail = (regime_state.get("cycle") or {}).get("business_cycle") or {}
    temporal_anomalies = []
    if cycle_detail.get("future_dated"):
        temporal_anomalies.append("business_cycle_asof_after_regime_market_asof")
    future_signals = [
        row["name"] for row in signals if row["freshness"].get("future_dated")
    ]
    if future_signals:
        temporal_anomalies.append(
            "signal_asof_after_decision_market_asof:" + ",".join(future_signals)
        )
    future_lobes = [
        name for name, row in (neural_state.get("lobe_health") or {}).items()
        if row.get("future_dated")
    ]
    if future_lobes:
        temporal_anomalies.append(
            "neural_lobe_asof_after_decision_market_asof:" + ",".join(future_lobes)
        )
    decision_coverage = assembly.get("decision_coverage")
    decision_degraded = (
        decision_coverage is None
        or _as_float(decision_coverage) is None
        or float(decision_coverage) < 1.0
    )
    return {
        "schema_version": _SCHEMA_VERSION,
        "region": region,
        "market_asof": context_asof,
        "built_at": built_at or datetime.now(tz=timezone.utc).isoformat(),
        "seq": int(seq),
        "regime": regime_state,
        "governor": {
            "relationship": lvp.get("relationship"),
            "confirmed": bool(lvp.get("confirmed")),
            "conflict": bool(lvp.get("conflict")),
            "label_direction": lvp.get("label_direction"),
            "consensus_direction": lvp.get("plane_consensus_direction"),
            "net_tilt": _as_float(tilt.get("tilt")),
            "contributors": list(tilt.get("contributors") or []),
            "validated_present": int(tilt.get("n_validated") or 0),
            "validated_total": assembly.get("decision_total"),
            "decision_coverage": assembly.get("decision_coverage"),
            "degraded": decision_degraded,
        },
        "signals": signals,
        "neural_web": neural_state,
        "data_quality": {
            "signals_total": len(signals),
            "present": present,
            "fresh": fresh,
            "stale": sum(
                1 for row in signals
                if row["availability"]["artifact_present"] and row["freshness"]["stale"]
            ),
            "missing": sum(1 for row in signals if not row["availability"]["artifact_present"]),
            "availability_coverage": round(present / len(signals), 4) if signals else 0.0,
            "fresh_coverage": round(fresh / len(signals), 4) if signals else 0.0,
            "temporal_anomalies": temporal_anomalies,
            "degraded": (
                bool(assembly.get("degraded"))
                or not signals
                or decision_degraded
                or bool(temporal_anomalies)
            ),
        },
        "authority_note": (
            "Read-only perception contract. Only fresh validated governor signals may vote; "
            "advisory context may annotate or shrink where separately authorized, never loosen risk."
        ),
    }


def latest() -> dict[str, Any]:
    """Read the latest published decision context, or ``{}``."""
    return _read_json(_LATEST_PATH)


def prompt_summary(ctx: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return the bounded, typed subset pushed into reasoning prompts."""
    ctx = ctx if isinstance(ctx, dict) else latest()
    if not ctx:
        return {}
    regime = ctx.get("regime") or {}
    neural = ctx.get("neural_web") or {}
    return {
        "schema_version": ctx.get("schema_version"),
        "market_asof": ctx.get("market_asof"),
        "hard_label": regime.get("hard_label"),
        "probabilistic_state": regime.get("probabilistic_state"),
        "trajectory": regime.get("trajectory"),
        "axes": regime.get("axes"),
        "liquidity": regime.get("liquidity"),
        "risk": regime.get("risk"),
        "market_driver": regime.get("market_driver"),
        "governor": ctx.get("governor"),
        "data_quality": ctx.get("data_quality"),
        "neural_web_health": neural.get("health_summary"),
        "neural_web_contexts": neural.get("contexts"),
    }


def build(
    region: str = "us",
    *,
    regime: dict[str, Any] | None = None,
    market_view: dict[str, Any] | None = None,
    neural_web: dict[str, Any] | None = None,
    write: bool = True,
    seq: int = 0,
) -> dict[str, Any]:
    """Build and optionally atomically publish the latest and dated v2 artifacts."""
    if regime is None:
        regime = _rf._read_raw(region)
    if market_view is None:
        market_view = _read_json(_MARKET_VIEW_PATH)
    if neural_web is None:
        try:
            from brain import neural_web_context as _nwc

            neural_web = _nwc.context()
        except Exception:  # noqa: BLE001
            neural_web = {}
    out = assemble(
        regime,
        market_view,
        neural_web=neural_web,
        region=region,
        seq=seq,
    )
    if not write:
        return out
    try:
        _ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(out, indent=2, default=str)
        tmp = _LATEST_PATH.with_suffix(".json.tmp")
        tmp.write_text(payload)
        os.replace(tmp, _LATEST_PATH)
        asof = out.get("market_asof")
        if asof:
            dated = _ARTIFACT_DIR / f"{str(asof)[:10]}.json"
            dated_tmp = dated.with_suffix(".json.tmp")
            dated_tmp.write_text(payload)
            os.replace(dated_tmp, dated)
    except Exception:  # noqa: BLE001 — perception publication never blocks the book
        pass
    return out
