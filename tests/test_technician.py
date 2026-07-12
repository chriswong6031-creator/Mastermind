"""Guards for the entry-timing TECHNICIAN seat (brain/technician).

The two safety-critical properties:
  1. BLINDNESS — the Technician's input rules ONLY on the chart/timing; it must NEVER contain the
     bull thesis, the combined/conviction score, viability, fair value, recommend, or size_mult
     (mirrors the SENTINEL blindness invariant in tests/test_committee.py).
  2. FAIL-CONSERVATIVE — with no LLM (cli_bridge unavailable) OR on any error/garbage, the verdict
     is "wait". The seat can only ever WITHHOLD or stage, never force a buy (the INVERSE of the old
     SENTINEL "no-LLM → CONFIRM" hole). It is subtract-only.

No live LLM is required: every path is exercised offline by monkeypatching cli_bridge.available().
"""
from __future__ import annotations

import json

import pytest

import bot  # noqa: F401
from brain import technician as T


def _rich_input_kwargs():
    """A DELIBERATELY over-stuffed set of caller kwargs: the entry-timing fields the seat SHOULD
    see, PLUS forbidden thesis/score fields smuggled into the tech/entry_signal dicts, to prove the
    blindness filter strips them."""
    return dict(
        entry_signal={
            "stop": 141.0, "buy_zone": {"low": 150, "high": 155, "pct_from_spot": 1.2},
            "entry_grade": "B", "chase_above": 160.0,
            # smuggled forbidden fields — must be stripped
            "fair_value": 200.0, "combined": 78,
        },
        tech={
            "pct_vs_50dma": 6.0, "pct_vs_200dma": 18.0, "rsi14": 61.0,
            "off_52w_high_pct": -4.0, "rs": 82.0, "urgency": "soon",
            "eq_grade": "solid", "parabolic": False,
            # smuggled forbidden fields — must be stripped
            "viability": "compelling", "research_score": 76, "size_mult": 1.0,
            "recommend": True, "bull_thesis": "AI capex supercycle",
        },
        anticipation={"next_date": "2026-07-20", "days_to_event": 6, "sue_z": 1.1,
                      "vol_cone": {"p50": 4.2}, "horizon": "21d"},
        options={"gamma_flip": 148.0, "expected_move": 5.5,
                 "magnets": [150, 160], "walls": [{"strike": 165, "oi": 12000}]},
    )


# ─────────────────────────────────────────────────────────────────────────────
# 1. BLINDNESS — no thesis/score field can leak into the seat's input
# ─────────────────────────────────────────────────────────────────────────────

def test_technician_input_is_blind_to_thesis():
    """The Technician input must never carry the bull thesis / combined / viability / fair_value /
    recommend / size_mult — even when a caller smuggles them into the tech/entry_signal dicts."""
    payload = T.technician_input("NVDA", **_rich_input_kwargs())
    blob = json.dumps(payload).lower()
    for leaked in ("combined", "conviction", "viability", "fair_value", "recommend",
                   "research_score", "engine_score", "size_mult", "bull", "thesis", "confirmed"):
        assert leaked not in blob, f"Technician input leaked a thesis/score field: {leaked}"
    # ...and it DID keep the legitimate entry-timing fields
    assert payload["ticker"] == "NVDA"
    assert payload["entry_signal"]["stop"] == 141.0
    assert payload["tech"]["rsi14"] == 61.0
    assert payload["tech"]["pct_vs_50dma"] == 6.0
    assert payload["anticipation"]["days_to_event"] == 6
    assert payload["options"]["gamma_flip"] == 148.0


def test_technician_input_handles_none_and_missing():
    """None/missing slices degrade to empty; never raises."""
    payload = T.technician_input("aapl", entry_signal=None, tech=None)
    assert payload["ticker"] == "AAPL"
    assert payload["entry_signal"]["stop"] is None
    assert payload["tech"]["rsi14"] is None
    assert payload["anticipation"] == {} and payload["options"] == {}


# ─────────────────────────────────────────────────────────────────────────────
# 2. FAIL-CONSERVATIVE — no LLM / error / garbage → "wait"
# ─────────────────────────────────────────────────────────────────────────────

def test_assess_no_llm_defaults_to_wait(monkeypatch):
    """cli_bridge unavailable → verdict 'wait' (fail-conservative; the seat never forces a buy)."""
    monkeypatch.setattr(T.cli_bridge, "available", lambda: False)
    out = T.technician_assess(T.technician_input("NVDA", entry_signal={}, tech={}))
    assert out["verdict"] == "wait"
    assert out["agent"] == "technician"
    assert 0.0 <= out["confidence"] <= 1.0


def test_assess_availability_probe_failure_is_wait(monkeypatch):
    """Even the availability probe raising is a reason to WITHHOLD, not to proceed."""
    def _boom():
        raise RuntimeError("probe blew up")
    monkeypatch.setattr(T.cli_bridge, "available", _boom)
    out = T.technician_assess({"ticker": "NVDA"})
    assert out["verdict"] == "wait"


def test_assess_llm_call_failure_is_wait(monkeypatch):
    """LLM reachable but the call raises → fail-conservative 'wait' (never breaks the caller)."""
    monkeypatch.setattr(T.cli_bridge, "available", lambda: True)
    monkeypatch.setattr(T.cli_bridge, "reason_sync",
                        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("call failed")))
    out = T.technician_assess({"ticker": "NVDA"})
    assert out["verdict"] == "wait"


def test_assess_unusable_result_is_wait(monkeypatch):
    """LLM returns ok=False / non-dict → 'wait'."""
    monkeypatch.setattr(T.cli_bridge, "available", lambda: True)
    monkeypatch.setattr(T.cli_bridge, "reason_sync",
                        lambda *a, **kw: {"ok": False, "text": None, "error": "x"})
    assert T.technician_assess({"ticker": "NVDA"})["verdict"] == "wait"


def test_assess_unparseable_reply_is_wait(monkeypatch):
    """LLM returns prose (no JSON) → 'wait'."""
    monkeypatch.setattr(T.cli_bridge, "available", lambda: True)
    monkeypatch.setattr(T.cli_bridge, "reason_sync",
                        lambda *a, **kw: {"ok": True, "text": "The chart looks extended to me."})
    assert T.technician_assess({"ticker": "NVDA"})["verdict"] == "wait"


def test_assess_unknown_verdict_coerces_to_wait(monkeypatch):
    """SUBTRACT-ONLY: a garbage verdict can only degrade to the conservative floor, never to a buy."""
    monkeypatch.setattr(T.cli_bridge, "available", lambda: True)
    monkeypatch.setattr(T.cli_bridge, "reason_sync",
                        lambda *a, **kw: {"ok": True,
                                          "text": '{"verdict": "STRONG_BUY", "confidence": 0.9}'})
    assert T.technician_assess({"ticker": "NVDA"})["verdict"] == "wait"


# ─────────────────────────────────────────────────────────────────────────────
# 3. Verdict ∈ the allowed set (happy paths + alias normalisation)
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("reply,expected", [
    ('{"verdict": "now", "confidence": 0.8, "rationale": "clean base breakout"}', "now"),
    ('{"verdict": "staged_starter", "confidence": 0.6}', "staged_starter"),
    ('{"verdict": "wait", "confidence": 0.3}', "wait"),
    # design-doc TECH_* spellings + enum aliases normalise onto the canonical three
    ('{"verdict": "TECH_NOW"}', "now"),
    ('{"verdict": "ENTER_NOW"}', "now"),
    ('{"verdict": "TECH_STAGE"}', "staged_starter"),
    ('{"verdict": "STAGE-STARTER"}', "staged_starter"),
    ('{"verdict": "TECH_WAIT"}', "wait"),
])
def test_assess_verdict_in_allowed_set(monkeypatch, reply, expected):
    monkeypatch.setattr(T.cli_bridge, "available", lambda: True)
    monkeypatch.setattr(T.cli_bridge, "reason_sync", lambda *a, **kw: {"ok": True, "text": reply})
    out = T.technician_assess({"ticker": "NVDA"})
    assert out["verdict"] in T._VERDICTS
    assert out["verdict"] == expected


# ─────────────────────────────────────────────────────────────────────────────
# 4. Persistence round-trip — write then read the technician.json artifact
# ─────────────────────────────────────────────────────────────────────────────

def test_persistence_round_trip(monkeypatch, tmp_path):
    """run() writes data/committee/<asof>/<TICKER>/technician.json; read it back."""
    monkeypatch.setattr(T, "_ARTIFACTS", tmp_path)
    monkeypatch.setattr(T.cli_bridge, "available", lambda: True)
    monkeypatch.setattr(T.cli_bridge, "reason_sync",
                        lambda *a, **kw: {"ok": True,
                                          "text": '{"verdict": "staged_starter", "confidence": 0.6, '
                                                  '"rationale": "imperfect base — starter only"}'})
    verdict = T.run("nvda", entry_signal={"stop": 141.0}, tech={"rsi14": 61.0}, asof="2026-06-22")
    assert verdict["verdict"] == "staged_starter"

    art = tmp_path / "2026-06-22" / "NVDA" / "technician.json"
    assert art.exists(), "technician.json artifact was not written"
    loaded = json.loads(art.read_text())
    assert loaded["agent"] == "technician"
    assert loaded["verdict"]["verdict"] == "staged_starter"
    # the persisted input carries the entry-timing fields but NOT any thesis/score field
    blob = json.dumps(loaded["input"]).lower()
    for leaked in ("combined", "viability", "fair_value", "size_mult"):
        assert leaked not in blob


def test_write_artifact_never_raises_on_bad_path(monkeypatch):
    """A persistence failure must never break the seat."""
    from pathlib import Path
    monkeypatch.setattr(T, "_ARTIFACTS", Path("/impossible/__mm_tech__"))
    monkeypatch.setattr(T.cli_bridge, "available", lambda: False)
    # run() swallows the write failure and still returns the (conservative) verdict
    out = T.run("NVDA", entry_signal={}, tech={}, asof="2026-06-22")
    assert out["verdict"] == "wait"


# ─────────────────────────────────────────────────────────────────────────────
# 5. Gating flag — DARK by default
# ─────────────────────────────────────────────────────────────────────────────

def test_enabled_default_off(monkeypatch):
    monkeypatch.delenv("MASTERMIND_TECHNICIAN", raising=False)
    assert T.technician_enabled() is False


def test_enabled_falsy_off(monkeypatch):
    for v in ("0", "false", "no", "off", ""):
        monkeypatch.setenv("MASTERMIND_TECHNICIAN", v)
        assert T.technician_enabled() is False


def test_enabled_truthy_on(monkeypatch):
    for v in ("1", "true", "yes", "on"):
        monkeypatch.setenv("MASTERMIND_TECHNICIAN", v)
        assert T.technician_enabled() is True


# ─────────────────────────────────────────────────────────────────────────────
# 6. Fail-soft on garbage input (no raise)
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("garbage", [None, 123, "a string", [], {"ticker": None}])
def test_input_fail_soft_on_garbage(garbage):
    """technician_input tolerates non-dict slices without raising."""
    payload = T.technician_input("NVDA", entry_signal=garbage, tech=garbage,
                                 anticipation=garbage, options=garbage)
    assert payload["ticker"] == "NVDA"
    assert isinstance(payload["entry_signal"], dict)


def test_assess_fail_soft_on_garbage_input(monkeypatch):
    """technician_assess tolerates a garbage input dict — still returns a valid conservative verdict."""
    monkeypatch.setattr(T.cli_bridge, "available", lambda: False)
    for garbage in (None, {}, {"ticker": 123}, {"junk": object()}):
        out = T.technician_assess(garbage)
        assert out["verdict"] in T._VERDICTS
        assert out["verdict"] == "wait"


def test_flag_registered_in_known_flags_and_authority_map():
    """MASTERMIND_TECHNICIAN is registered so conformance (test_governance_ledger) stays green."""
    from control_plane.flags import KNOWN_FLAGS
    assert "MASTERMIND_TECHNICIAN" in KNOWN_FLAGS
    import yaml
    from pathlib import Path
    amap = yaml.safe_load((Path(T.__file__).resolve().parent.parent
                           / "config" / "authority_map.yml").read_text())
    entry = (amap.get("flags") or {}).get("MASTERMIND_TECHNICIAN")
    assert entry is not None and entry.get("authority_level") == "A4"
