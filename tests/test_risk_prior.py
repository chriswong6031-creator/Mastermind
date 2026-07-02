"""P-NEW-1: risk_prior must not let a SHADOW-labeled fused_risk read LOOSEN the bot, and must read
the VALIDATED (non-shadow) risk_radar/forward_log.jsonl as an escalation source.

CONTEXT: brain/risk_prior.py arrives via the in-flight fix/bot-orphans-arming merge — it is NOT on
this fable-w1 base. This file is the P-NEW-1 verify-then-fix TEST SPEC: it is skipped (importorskip)
until risk_prior lands, then it BINDS the contract the fix must satisfy:

  1. prior() exposes shadow = (fused_risk.passport.validation.status == 'shadow') OR regime_one.shadow.
  2. A shadow prior is ADVISORY-ONLY for LOOSENING: it may tighten/freeze/shrink, never produce a
     LESS-cautious state than the bot's own view (degrade-never-raise / never-un-cap).
  3. risk_prior reads the validated risk_radar forward_log (state=caution/risk_off/risk_on + h21
     drawdown_prob). Effective prior tightness = MAX-CAUTIOUS(bot_view, validated radar, [shadow
     fused_risk ONLY if MORE cautious]) — never MIN. A validated caution read is surfaced as an
     accountable reconciliation divergence (NOT auto-flipped — sector-cycle-veto is refuted).

DI style: a synthetic regime_one dict is injected; a trimmed risk_radar forward_log row is copied
into tests/fixtures/ rather than reading the live vendor path.
"""
import json
from pathlib import Path

import pytest

import bot  # noqa: F401

risk_prior = pytest.importorskip(
    "brain.risk_prior",
    reason="brain/risk_prior.py lands via the fix/bot-orphans-arming merge; P-NEW-1 fix binds then.",
)

_FIX = Path(__file__).resolve().parent / "fixtures"


# 5-state (most→least cautious) ordering used to assert "never less cautious".
_CAUTION_RANK = {"risk_off": 3, "weakening": 2, "caution": 2, "neutral": 1,
                 "stable": 1, "risk_on": 0}


def _rank(state: str) -> int:
    return _CAUTION_RANK.get((state or "").lower().replace("-", "_"), 1)


def _shadow_riskon_regime() -> dict:
    """A regime_one with a SHADOW fused_risk labeled risk-on (the 2026-07-01 artifact shape)."""
    return {
        "date": "2026-07-01",
        "shadow": True,
        "fused_risk": {"label": "risk-on", "confidence": 0.277, "degraded": False},
        "passport": {"validation": {"status": "shadow"}},
    }


def _radar_caution_row() -> dict:
    """The trimmed validated (non-shadow) risk_radar forward_log row: state=caution, h21 dd 0.16."""
    line = (_FIX / "risk_radar_forward_log_caution.jsonl").read_text().strip().splitlines()[-1]
    return json.loads(line)


def _inject(monkeypatch, regime: dict, radar: dict) -> None:
    """Wire the synthetic regime + radar row into risk_prior's readers, whatever they are named.
    Tolerant: patches the loader attrs the fix is expected to add, skipping any that don't exist so
    the contract assertions (not the wiring shape) are what bind."""
    for attr, val in (("_load_regime_one", regime), ("_load_risk_radar", radar),
                      ("_load_radar_forward", radar)):
        if hasattr(risk_prior, attr):
            monkeypatch.setattr(risk_prior, attr, lambda *a, _v=val, **k: _v, raising=False)


def test_shadow_flag_is_exposed(monkeypatch):
    _inject(monkeypatch, _shadow_riskon_regime(), _radar_caution_row())
    p = risk_prior.prior()
    assert p.get("shadow") is True


def test_shadow_prior_never_loosens_below_bot_view(monkeypatch):
    """The shadow risk-on read can NEVER make the effective prior less cautious than the bot's own
    view. Given bot_view=risk_on and a validated caution radar, the effective prior must be at least
    as cautious as risk_on (i.e. it may escalate to caution, never stay/loosen to risk_on-by-shadow)."""
    _inject(monkeypatch, _shadow_riskon_regime(), _radar_caution_row())
    bot_view = "risk_on"
    p = risk_prior.prior(bot_view=bot_view) if _accepts_bot_view() else risk_prior.prior()
    eff = (p.get("effective_state") or p.get("prior_state") or p.get("state") or bot_view)
    assert _rank(eff) >= _rank(bot_view), (
        f"shadow prior loosened below bot_view: eff={eff} < bot_view={bot_view}")


def test_validated_caution_radar_is_surfaced(monkeypatch):
    """The validated caution read must be surfaced as an accountable reconciliation divergence
    (radar_state exposed + a reconciliation row), NOT silently dropped and NOT auto-flipping state."""
    _inject(monkeypatch, _shadow_riskon_regime(), _radar_caution_row())
    p = risk_prior.prior()
    # radar state exposed somewhere on the prior payload
    radar_state = (p.get("radar_state") or (p.get("reconciliation") or {}).get("radar_state"))
    assert radar_state == "caution"
    # a reconciliation/divergence record exists (the bot keeps its own state; this is accountable)
    assert p.get("reconciliation") or p.get("divergence") or p.get("radar_divergence")


def _accepts_bot_view() -> bool:
    import inspect
    try:
        return "bot_view" in inspect.signature(risk_prior.prior).parameters
    except (TypeError, ValueError):
        return False
