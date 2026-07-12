"""Desk-quorum ladder + technician ratchet guards (brain/committee).

Covers the committee-side additions that admit the entry-timing TECHNICIAN seat into the buy gate:

  1. nexus REFACTOR is byte-identical: nexus(bd, sent) == nexus(bd, sent, technician=None) for a
     representative set of (confirm / trim / drop) cases — proving _nexus_synthesis reproduces the
     pre-refactor behaviour and the None technician-path is a pure no-op.
  2. apply_technician: the SUBTRACT-ONLY ratchet (wait→drop/0; staged_starter→cap 0.7;
     now/None/garbage→unchanged) + the ratchet-DOWN invariant (returned scale ≤ input scale for
     EVERY verdict; a "now" verdict never raises a scale).
  3. desk_quorum_mode: default off, ladder parsing (off|shadow|enforce), fail-soft on a bad value.
  4. technician_gate: off/shadow are book no-ops; enforce applies the verdict subtract-only; a None
     verdict (seat failure) never parks.

All pure — no LLM, no I/O.
"""
from __future__ import annotations

import bot  # noqa: F401  — package import side-effects (matches tests/test_committee.py)
from brain import committee as C


# Representative (breakdown, sentinel) pairs spanning every _nexus_synthesis branch.
def _bd(confirmed=True, combined=78):
    return {"confirmed": confirmed, "combined": combined, "engine_score": 80,
            "research_score": 76, "viability": "compelling", "size_mult": 1.0}


# (breakdown, sentinel) → covers: no-adversary confirm, SUPPORT confirm, CONDITIONAL trim,
# weak-OPPOSE trim, strong-OPPOSE drop, and the FORGE-not-confirmed drop.
_CASES = [
    (_bd(confirmed=True), None),                                    # confirm (no adversary)
    (_bd(confirmed=True), {"stance": "SUPPORT", "confidence": 0.9}),    # confirm
    (_bd(confirmed=True), {"stance": "CONDITIONAL", "confidence": 0.5}),  # trim 0.66
    (_bd(confirmed=True), {"stance": "OPPOSE", "confidence": 0.4}),   # weak → trim
    (_bd(confirmed=True), {"stance": "OPPOSE", "confidence": 0.85}),  # strong → drop
    (_bd(confirmed=False), {"stance": "SUPPORT", "confidence": 0.9}),   # blocked → drop
]


# ─────────────────────────────────────────────────────────────────────────────
# 1. the refactor is byte-identical — nexus(bd, sent) == nexus(bd, sent, technician=None)
# ─────────────────────────────────────────────────────────────────────────────
def test_nexus_technician_none_is_byte_identical():
    """Passing technician=None must reproduce the original 2-arg nexus EXACTLY, for every branch."""
    for bd, sent in _CASES:
        two_arg = C.nexus(bd, sent)
        none_kw = C.nexus(bd, sent, technician=None)
        assert two_arg == none_kw, f"nexus diverged for {sent!r}: {two_arg} != {none_kw}"


def test_nexus_synthesis_matches_nexus_default():
    """_nexus_synthesis IS the body the default nexus path returns (proves the extraction is clean)."""
    for bd, sent in _CASES:
        assert C._nexus_synthesis(bd, sent) == C.nexus(bd, sent)


def test_nexus_covers_all_synthesis_actions():
    """Sanity: the representative cases actually exercise confirm/trim/drop (not all one branch)."""
    actions = {C.nexus(bd, sent)["action"] for bd, sent in _CASES}
    assert {"confirm", "trim", "drop"} <= actions


# ─────────────────────────────────────────────────────────────────────────────
# 2. apply_technician — the SUBTRACT-ONLY ratchet
# ─────────────────────────────────────────────────────────────────────────────
def test_apply_technician_wait_parks():
    """wait → force ("drop", 0.0) regardless of the incoming action/scale."""
    for action, scale in (("confirm", 1.0), ("trim", 0.66), ("trim", 0.3)):
        assert C.apply_technician(action, scale, "wait") == ("drop", 0.0)


def test_apply_technician_staged_starter_caps_at_0_7():
    """staged_starter caps the scale at 0.7; when it lowers the scale the action becomes 'trim'."""
    a, s = C.apply_technician("confirm", 1.0, "staged_starter")
    assert a == "trim" and s == 0.7
    # already at/under 0.7 → no change (cap is a min(), never raises)
    assert C.apply_technician("trim", 0.5, "staged_starter") == ("trim", 0.5)
    assert C.apply_technician("trim", 0.7, "staged_starter") == ("trim", 0.7)


def test_apply_technician_now_none_garbage_unchanged():
    """now / None / any unrecognised verdict → the (action, scale) pair is returned UNCHANGED."""
    for verdict in ("now", None, "garbage", "", "MAYBE"):
        for action, scale in (("confirm", 1.0), ("trim", 0.66), ("drop", 0.0)):
            assert C.apply_technician(action, scale, verdict) == (action, scale), verdict


def test_apply_technician_ratchets_down_only():
    """INVARIANT: for EVERY verdict the returned scale is ≤ the input scale, and a 'now' verdict
    can NEVER raise a scale (the cap for 'now' is 1.0 → min(x, 1.0) == x)."""
    for verdict in ("wait", "staged_starter", "now", None, "garbage"):
        for scale in (0.0, 0.3, 0.5, 0.7, 0.66, 1.0):
            _, new_scale = C.apply_technician("confirm", scale, verdict)
            assert new_scale <= scale + 1e-9, f"{verdict} raised scale {scale}→{new_scale}"
    # a "now" verdict is a strict no-op on the scale for every legal scale
    for scale in (0.0, 0.25, 0.7, 1.0):
        assert C.apply_technician("confirm", scale, "now") == ("confirm", scale)


# ─────────────────────────────────────────────────────────────────────────────
# 3. desk_quorum_mode — default off, ladder parsing, fail-soft
# ─────────────────────────────────────────────────────────────────────────────
def test_desk_quorum_mode_default_off(monkeypatch):
    monkeypatch.delenv("MASTERMIND_DESK_QUORUM", raising=False)
    assert C.desk_quorum_mode() == "off"


def test_desk_quorum_mode_ladder_parsing(monkeypatch):
    for val, expected in (("off", "off"), ("shadow", "shadow"), ("enforce", "enforce"),
                          ("SHADOW", "shadow"), ("  Enforce  ", "enforce")):
        monkeypatch.setenv("MASTERMIND_DESK_QUORUM", val)
        assert C.desk_quorum_mode() == expected, val


def test_desk_quorum_mode_fail_soft_on_bad_value(monkeypatch):
    """A typo / empty / garbage value degrades to the safe default 'off' — never accidentally arms."""
    for val in ("enfroce", "on", "1", "yes", "", "   ", "banana"):
        monkeypatch.setenv("MASTERMIND_DESK_QUORUM", val)
        assert C.desk_quorum_mode() == "off", val


# ─────────────────────────────────────────────────────────────────────────────
# 4. technician_gate — the mode-aware phase2 decision helper
# ─────────────────────────────────────────────────────────────────────────────
def test_technician_gate_off_is_noop():
    """off (or any unrecognised mode) → inert no-op: input (action, scale) returned unchanged."""
    for mode in ("off", "garbage", ""):
        for verdict in ("wait", "staged_starter", "now", None):
            g = C.technician_gate(mode, "confirm", 1.0, verdict)
            assert g == {"park": False, "scale": 1.0, "action": "confirm"}, (mode, verdict)


def test_technician_gate_shadow_never_changes_book():
    """shadow → the verdict is NOT applied: no park, scale unchanged, even for a 'wait'."""
    for verdict in ("wait", "staged_starter", "now", None):
        g = C.technician_gate("shadow", "confirm", 1.0, verdict)
        assert g == {"park": False, "scale": 1.0, "action": "confirm"}, verdict


def test_technician_gate_enforce_applies_subtract_only():
    """enforce → apply the verdict subtract-only: wait parks, staged_starter caps at 0.7."""
    assert C.technician_gate("enforce", "confirm", 1.0, "wait") == {
        "park": True, "scale": 0.0, "action": "drop"}
    assert C.technician_gate("enforce", "confirm", 1.0, "staged_starter") == {
        "park": False, "scale": 0.7, "action": "trim"}
    # now / unknown → unchanged
    assert C.technician_gate("enforce", "confirm", 1.0, "now") == {
        "park": False, "scale": 1.0, "action": "confirm"}


def test_technician_gate_enforce_none_verdict_never_parks():
    """A None verdict (a seat/LLM FAILURE) is a no-op even in enforce — a failed seat NEVER parks."""
    g = C.technician_gate("enforce", "confirm", 1.0, None)
    assert g == {"park": False, "scale": 1.0, "action": "confirm"}


def test_technician_gate_enforce_never_raises_scale():
    """enforce is subtract-only: for every verdict the returned scale is ≤ the input scale."""
    for verdict in ("wait", "staged_starter", "now", None, "garbage"):
        for scale in (0.3, 0.5, 0.7, 1.0):
            g = C.technician_gate("enforce", "confirm", scale, verdict)
            assert g["scale"] <= scale + 1e-9, (verdict, scale, g)
