"""Guards for the RISK GOVERNOR (brain/risk_lens) + its wiring into the Brain books.

Offline only: no vendor engine, no LLM, no network. We monkeypatch the dashboard `_load` helper to
feed synthetic vol/etf-pulse/gex dicts and assert:
  * the injectors are byte-identical no-ops when MASTERMIND_RISK_GOVERNOR is OFF (the default),
  * the heuristic posture rises with risk-off tells and stays NEUTRAL on a calm tape,
  * a published Macro Risk Officer posture OVERRIDES the heuristic,
  * the book's overlap with crowded leadership is surfaced (the 2026-06-23 failure mode),
  * the persona mandate governs GROSS for free-form books and CONCENTRATION for Heavyweight,
  * with the flag ON the brain prompts/personas actually carry the governor (and not when OFF).
"""
from __future__ import annotations

import pytest

import bot  # noqa: F401  -> vendor/macro onto sys.path
from brain import risk_lens as R


# ── synthetic tapes ───────────────────────────────────────────────────────────────────────
def _risk_off_load(rel):
    if rel == "site/basketdata/vol_sentiment.json":
        return {"vix": 28.0, "vix_pctile": 88, "term_structure": "backwardation", "sentiment_en": "fear"}
    if rel == "site/basketdata/etf_pulse.json":
        return {"risk": {"label_en": "risk-off"}, "credit": {"label_en": "risk-off widening"},
                "leaders": ["SMH", "XLK"], "laggards": ["XLU"]}
    if rel == "site/gex/SPY.json":
        return {"summary": {"regime": "short", "net_gex_bn": -2.1}}
    return None


def _calm_load(rel):
    if rel == "site/basketdata/vol_sentiment.json":
        return {"vix": 13.0, "vix_pctile": 25, "term_structure": "contango", "sentiment_en": "neutral"}
    if rel == "site/basketdata/etf_pulse.json":
        return {"risk": {"label_en": "risk-on"}, "leaders": ["SMH"]}
    return None


@pytest.fixture(autouse=True)
def _no_officer(monkeypatch):
    """Default: no sibling Macro Risk Officer file (heuristic owns the posture). Tests opt in."""
    monkeypatch.setattr(R, "_macro_officer", lambda: None)


# ── flag-gating: OFF is a byte-identical no-op ─────────────────────────────────────────────
def test_off_is_byte_identical(monkeypatch):
    monkeypatch.delenv("MASTERMIND_RISK_GOVERNOR", raising=False)
    persona = "BASE PERSONA"
    assert R.govern_persona(persona, "autonomous") is persona       # same object, unchanged
    assert R.govern_persona(persona, "heavyweight") is persona
    assert R.briefing("autonomous", regime={"liquidity_overlay": "contracting"}, held=["SMH"]) == ""


# ── heuristic posture ──────────────────────────────────────────────────────────────────────
def test_posture_risk_off_on_stress_tape(monkeypatch):
    monkeypatch.setattr(R, "_load", _risk_off_load)
    st = R.risk_state("autonomous", regime={"liquidity_overlay": "contracting"},
                      asof="2026-06-23", held=["SMH", "NVDA"])
    assert st["posture"] == "RISK-OFF" and st["source"] == "heuristic"
    assert st["score"] >= 4
    assert st["gamma"]["benchmark_short_gamma"] is True
    # every major tell shows up in the evidence
    blob = " ".join(st["evidence"]).lower()
    assert "liquidity" in blob and "risk-off" in blob and "credit" in blob and "gamma" in blob


def test_posture_neutral_on_calm_tape(monkeypatch):
    monkeypatch.setattr(R, "_load", _calm_load)
    st = R.risk_state("autonomous", regime={"liquidity_overlay": "expanding"}, held=["SMH"])
    assert st["posture"] == "NEUTRAL" and st["source"] == "heuristic"
    assert st["score"] < 2


def test_posture_caution_on_contracting_liquidity_alone(monkeypatch):
    monkeypatch.setattr(R, "_load", lambda rel: None)   # no vol/pulse/gex files
    st = R.risk_state("china", regime={"liquidity_overlay": "contracting"}, held=[])
    assert st["posture"] == "CAUTION"                   # liquidity contraction alone = +2 → CAUTION


def test_posture_unknown_when_tape_unreadable(monkeypatch):
    monkeypatch.setattr(R, "_load", lambda rel: None)
    st = R.risk_state("hk", regime={}, held=[])
    assert st["posture"] == "UNKNOWN" and st["source"] == "no_signal"


# ── crowded-leadership overlap (the 2026-06-23 failure mode) ───────────────────────────────
def test_held_overlap_with_crowded_leaders(monkeypatch):
    monkeypatch.setattr(R, "_load", _risk_off_load)
    st = R.risk_state("autonomous", regime={"liquidity_overlay": "contracting"},
                      held=["SMH", "AAPL"])
    assert st["held_overlap"] == ["SMH"]
    assert any("crowd" in e.lower() for e in st["evidence"])


# ── Macro Risk Officer override ────────────────────────────────────────────────────────────
def test_macro_officer_overrides_heuristic(monkeypatch):
    monkeypatch.setattr(R, "_load", _calm_load)         # heuristic would say NEUTRAL
    monkeypatch.setattr(R, "_macro_officer", lambda: {
        "posture": "risk_off", "headline": "Liquidity air-pocket; de-gross",
        "directive": "Cut gross to 50%.", "drivers": ["MOVE spike"], "asof": "2026-06-23"})
    st = R.risk_state("autonomous", regime={"liquidity_overlay": "expanding"}, held=[])
    assert st["posture"] == "RISK-OFF" and st["source"] == "macro_risk_officer"
    brief = R._format_briefing(st)
    assert "Macro Risk Officer" in brief and "Cut gross to 50%." in brief


# ── briefing format (flag ON) ──────────────────────────────────────────────────────────────
def test_briefing_renders_when_on(monkeypatch):
    monkeypatch.setenv("MASTERMIND_RISK_GOVERNOR", "1")
    monkeypatch.setattr(R, "_load", _risk_off_load)
    out = R.briefing("autonomous", regime={"liquidity_overlay": "contracting"},
                     asof="2026-06-23", held=["SMH"])
    assert "RISK GOVERNOR" in out and "RISK-OFF" in out
    assert "Directive:" in out and "CAPITAL PROTECTION" in out


# ── persona mandates ───────────────────────────────────────────────────────────────────────
def test_freeform_mandate_governs_gross(monkeypatch):
    monkeypatch.setenv("MASTERMIND_RISK_GOVERNOR", "1")
    p = R.govern_persona("BASE", "autonomous")
    assert "RISK GOVERNOR" in p and "CAPITAL PROTECTION" in p
    assert "gross + correlation" in p          # governs the WHOLE book, not one position
    assert "token 5% hedge" in p               # explicitly rejects the sidecar-hedge mistake
    assert "overrides your act" in p.lower()


def test_heavyweight_mandate_governs_concentration(monkeypatch):
    monkeypatch.setenv("MASTERMIND_RISK_GOVERNOR", "1")
    p = R.govern_persona("BASE", "heavyweight")
    assert "RISK GOVERNOR" in p and "DE-CONCENTRATE" in p
    assert "maximally concentrated" in p.lower()
    assert "concentrate-and-press" in p.lower()
    # the free-form gross-trim language is NOT the heavyweight mandate
    assert "act / lean-in imperative" not in p


# ── wiring into the bots (flag-gated) ──────────────────────────────────────────────────────
def test_autonomous_prompt_gated(monkeypatch):
    from bot import autonomous
    # OFF (default) → no governor block in the prompt
    monkeypatch.delenv("MASTERMIND_RISK_GOVERNOR", raising=False)
    assert "RISK GOVERNOR" not in autonomous._build_prompt("2026-06-23", inaugural=True)
    # ON → the live risk block is injected
    monkeypatch.setenv("MASTERMIND_RISK_GOVERNOR", "1")
    monkeypatch.setattr(R, "_load", _risk_off_load)
    assert "RISK GOVERNOR" in autonomous._build_prompt("2026-06-23", inaugural=True)


def test_heavyweight_prompt_gated(monkeypatch):
    from bot import heavyweight
    monkeypatch.delenv("MASTERMIND_RISK_GOVERNOR", raising=False)
    assert "RISK GOVERNOR" not in heavyweight._build_prompt("2026-06-23", inaugural=True)
    monkeypatch.setenv("MASTERMIND_RISK_GOVERNOR", "1")
    monkeypatch.setattr(R, "_load", _risk_off_load)
    assert "RISK GOVERNOR" in heavyweight._build_prompt("2026-06-23", inaugural=True)
