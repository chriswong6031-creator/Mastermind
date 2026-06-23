"""P3 self-mirror — offline tests for brain.self_mirror (digest + flag-gated inject).

No LLM, no vendor data. We stub the calibration block + the seat detail rows. We prove:
  * flag OFF → inject returns the SAME prompt object (byte-identical, all four seats),
  * flag ON + status=="building" (n<MIN_N) → digest "" → inject unchanged,
  * flag ON + scoring → digest carries the multiplier + a characteristic-miss line,
  * the aggregate PATTERN line is contradiction-checked (sign must agree with reliability),
  * digest never raises and never says "raise conviction".
"""
from __future__ import annotations

import types

import pytest

from brain import self_mirror


def _stub_calib(monkeypatch, agent, block):
    monkeypatch.setattr(self_mirror._calib, "load",
                        lambda: {"agents": {agent: block}}, raising=False)


def _stub_rows(monkeypatch, agent, detail):
    monkeypatch.setitem(self_mirror._ROW_BUILDERS, agent, lambda asof: detail)


# ───────────────────────────── flag gating ─────────────────────────────
@pytest.mark.parametrize("agent", ["strategist", "pm", "gate", "risk"])
def test_inject_off_is_byte_identical(monkeypatch, agent):
    monkeypatch.delenv("MASTERMIND_SELF_MIRROR", raising=False)
    prompt = "SYSTEM PROMPT for " + agent
    assert self_mirror.inject(prompt, agent) is prompt   # same object, unchanged


def test_inject_on_but_building_returns_unchanged(monkeypatch):
    monkeypatch.setenv("MASTERMIND_SELF_MIRROR", "1")
    _stub_calib(monkeypatch, "gate", {"status": "building", "n": 5,
                                      "reliability": 0.4, "multiplier": 1.0})
    prompt = "GATE SYS"
    assert self_mirror.inject(prompt, "gate") is prompt   # empty digest → unchanged


# ───────────────────────────── digest content ─────────────────────────────
def test_digest_scoring_carries_multiplier_and_miss(monkeypatch):
    _stub_calib(monkeypatch, "gate", {"status": "scoring", "n": 18,
                                      "reliability": 0.40, "multiplier": 0.62})
    _stub_rows(monkeypatch, "gate", [
        {"date": "2026-05-02", "ticker": "NVDA", "outcome": 0, "conf": 1.0, "rel": 0.083, "verb": "VETO"},
        {"date": "2026-05-09", "ticker": "AAPL", "outcome": 1, "conf": 1.0, "rel": -0.02, "verb": "VETO"},
    ])
    d = self_mirror.digest("gate")
    assert "0.40" in d                                    # reliability surfaced
    assert "0.62" in d                                    # multiplier surfaced
    assert "NVDA" in d and "VETO" in d                    # characteristic miss quoted
    assert "+8.3%" in d
    assert "raise" not in d.lower()                       # de-confidence only, never inflates


def test_digest_building_is_empty(monkeypatch):
    _stub_calib(monkeypatch, "pm", {"status": "building", "n": 4,
                                    "reliability": 0.3, "multiplier": 1.0})
    assert self_mirror.digest("pm") == ""


def test_digest_pattern_line_gated_on_effective_n(monkeypatch):
    # < _PATTERN_EFFECTIVE_N independent clusters → no aggregate pattern line, but misses still show.
    _stub_calib(monkeypatch, "risk", {"status": "scoring", "n": 14,
                                      "reliability": 0.30, "multiplier": 0.55})
    _stub_rows(monkeypatch, "risk", [
        {"date": "2026-05-02", "ticker": "T1", "outcome": 0, "conf": 1.0, "rel": 0.05, "verb": "EXIT"},
        {"date": "2026-05-03", "ticker": "T2", "outcome": 0, "conf": 1.0, "rel": 0.04, "verb": "EXIT"},
    ])  # two adjacent dates → thinned to 1 cluster, below the floor of 8
    d = self_mirror.digest("risk")
    assert "Pattern" not in d
    assert "T1" in d                                      # miss still quoted


def test_digest_pattern_contradiction_dropped(monkeypatch):
    # aggregate reliability says CALIBRATED (0.8) but the supplied rows are mostly wrong — the
    # "frequently wrong" pattern sign disagrees with reliability >= 0.5 → must be dropped.
    _stub_calib(monkeypatch, "strategist", {"status": "scoring", "n": 20,
                                            "reliability": 0.80, "multiplier": 1.0})
    detail = [{"date": f"2026-{m:02d}-01", "ticker": f"T{m}", "outcome": 0,
               "conf": 0.5, "rel": 0.05, "verb": "lead-call"} for m in range(1, 11)]
    _stub_rows(monkeypatch, "strategist", detail)        # 10 monthly → ≥8 clusters, but conflicting
    d = self_mirror.digest("strategist")
    assert "WRONG" not in d                               # contradictory pattern suppressed


def test_digest_never_raises_on_garbage(monkeypatch):
    monkeypatch.setattr(self_mirror._calib, "load", lambda: {"agents": {"gate": None}}, raising=False)
    assert self_mirror.digest("gate") == ""


def test_inject_on_scoring_appends(monkeypatch):
    monkeypatch.setenv("MASTERMIND_SELF_MIRROR", "1")
    _stub_calib(monkeypatch, "gate", {"status": "scoring", "n": 18,
                                      "reliability": 0.40, "multiplier": 0.62})
    _stub_rows(monkeypatch, "gate", [
        {"date": "2026-05-02", "ticker": "NVDA", "outcome": 0, "conf": 1.0, "rel": 0.083, "verb": "VETO"}])
    prompt = "GATE SYS"
    out = self_mirror.inject(prompt, "gate")
    assert out.startswith("GATE SYS\n\n")                 # original preserved, digest appended
    assert "NVDA" in out
