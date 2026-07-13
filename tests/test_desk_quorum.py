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


# ─────────────────────────────────────────────────────────────────────────────
# 5. the FULL desk quorum — nexus(strategist/pm_conviction/gate)=None is byte-identical
# ─────────────────────────────────────────────────────────────────────────────
def test_nexus_all_new_seats_none_is_byte_identical():
    """The HARD LAW: nexus(bd, sent) MUST be byte-identical to nexus(bd, sent, technician=None,
    strategist=None, pm_conviction=None, gate=None) for EVERY synthesis branch — a fully-defaulted
    quorum call is a pure no-op that reproduces the 2-arg path exactly."""
    for bd, sent in _CASES:
        two_arg = C.nexus(bd, sent)
        all_kw = C.nexus(bd, sent, technician=None, strategist=None,
                         pm_conviction=None, gate=None)
        assert two_arg == all_kw, f"nexus diverged for {sent!r}: {two_arg} != {all_kw}"


def test_nexus_each_new_seat_none_individually_is_byte_identical():
    """Passing any single new seat kwarg as None (others absent) is also a pure no-op."""
    for bd, sent in _CASES:
        base = C.nexus(bd, sent)
        assert C.nexus(bd, sent, strategist=None) == base
        assert C.nexus(bd, sent, pm_conviction=None) == base
        assert C.nexus(bd, sent, gate=None) == base


# ─────────────────────────────────────────────────────────────────────────────
# 6. apply_strategist — HOSTILE parks; supportive/neutral/None pass (subtract-only)
# ─────────────────────────────────────────────────────────────────────────────
def test_apply_strategist_hostile_parks():
    """hostile → force ("drop", 0.0) — the §2.5 row-1 hard withhold, regardless of incoming size."""
    for action, scale in (("confirm", 1.0), ("trim", 0.66), ("trim", 0.3)):
        assert C.apply_strategist(action, scale, "hostile") == ("drop", 0.0)


def test_apply_strategist_supportive_neutral_none_unchanged():
    """supportive / neutral / None / any unrecognised backdrop → (action, scale) UNCHANGED."""
    for verdict in ("supportive", "neutral", None, "SUPPORTIVE", "garbage", ""):
        for action, scale in (("confirm", 1.0), ("trim", 0.66), ("drop", 0.0)):
            assert C.apply_strategist(action, scale, verdict) == (action, scale), verdict


def test_apply_strategist_ratchets_down_only():
    for verdict in ("hostile", "supportive", "neutral", None, "garbage"):
        for scale in (0.0, 0.3, 0.5, 0.7, 1.0):
            _, new_scale = C.apply_strategist("confirm", scale, verdict)
            assert new_scale <= scale + 1e-9, f"{verdict} raised scale {scale}→{new_scale}"


# ─────────────────────────────────────────────────────────────────────────────
# 7. apply_pm — no champion (pass/no_slot) parks; add/None pass (intent only)
# ─────────────────────────────────────────────────────────────────────────────
def test_apply_pm_no_champion_parks():
    """pass / no_slot / decline → force ("drop", 0.0) — §2.5 row 6: no champion → withhold to WL."""
    for verdict in ("pass", "no_slot", "decline", "NO_SLOT"):
        for action, scale in (("confirm", 1.0), ("trim", 0.66)):
            assert C.apply_pm(action, scale, verdict) == ("drop", 0.0), verdict


def test_apply_pm_add_none_unchanged():
    """add (intent only) / None / unknown → UNCHANGED — the champion NEVER raises the scale."""
    for verdict in ("add", "ADD", None, "garbage", ""):
        for action, scale in (("confirm", 1.0), ("trim", 0.66), ("drop", 0.0)):
            assert C.apply_pm(action, scale, verdict) == (action, scale), verdict


def test_apply_pm_ratchets_down_only():
    for verdict in ("add", "pass", "no_slot", None, "garbage"):
        for scale in (0.0, 0.3, 0.7, 1.0):
            _, new_scale = C.apply_pm("confirm", scale, verdict)
            assert new_scale <= scale + 1e-9, f"{verdict} raised scale {scale}→{new_scale}"


# ─────────────────────────────────────────────────────────────────────────────
# 8. apply_gate — veto/withhold park; approve passes; approve+downsize caps (subtract-only)
# ─────────────────────────────────────────────────────────────────────────────
def test_apply_gate_veto_withhold_parks():
    """veto / reject / withhold → force ("drop", 0.0) — §2.5 row 4: veto wins over any quorum."""
    for verdict in ("veto", "reject", "withhold", "VETO", "WITHHOLD"):
        for action, scale in (("confirm", 1.0), ("trim", 0.66)):
            assert C.apply_gate(action, scale, verdict) == ("drop", 0.0), verdict


def test_apply_gate_approve_none_unchanged():
    """approve (no downsize) / None / unknown → UNCHANGED — final APPROVE passes at engine size."""
    for verdict in ("approve", "APPROVE", None, "garbage"):
        for action, scale in (("confirm", 1.0), ("trim", 0.66)):
            assert C.apply_gate(action, scale, verdict) == (action, scale), verdict


def test_apply_gate_approve_downsize_caps_only():
    """approve + downsize_to → §2.3 fn1 / §2.5 row 5 smallest-size-wins: caps DOWN, never up."""
    # legal downsize below the current scale → trim to the cap
    assert C.apply_gate("confirm", 1.0, "approve", downsize_to=0.5) == ("trim", 0.5)
    # downsize_to at/above the scale → no change (a min() cap can never raise)
    assert C.apply_gate("confirm", 1.0, "approve", downsize_to=1.0) == ("confirm", 1.0)
    assert C.apply_gate("trim", 0.4, "approve", downsize_to=0.9) == ("trim", 0.4)
    # downsize_to == 0 → parks
    assert C.apply_gate("confirm", 1.0, "approve", downsize_to=0.0) == ("drop", 0.0)
    # bad / None downsize_to → ignored (pure no-op)
    assert C.apply_gate("confirm", 1.0, "approve", downsize_to=None) == ("confirm", 1.0)
    assert C.apply_gate("confirm", 1.0, "approve", downsize_to="oops") == ("confirm", 1.0)


def test_apply_gate_ratchets_down_only():
    for verdict in ("approve", "veto", "withhold", None, "garbage"):
        for scale in (0.0, 0.3, 0.7, 1.0):
            _, new_scale = C.apply_gate("confirm", scale, verdict)
            assert new_scale <= scale + 1e-9, f"{verdict} raised scale {scale}→{new_scale}"


# ─────────────────────────────────────────────────────────────────────────────
# 9. the ORDERED conjunctive quorum through nexus — strictest cut wins, subtract-only
# ─────────────────────────────────────────────────────────────────────────────
def test_nexus_strategist_hostile_drops():
    """A HOSTILE backdrop parks a name FORGE confirmed with no adversary objection."""
    bd = _bd(confirmed=True)
    out = C.nexus(bd, {"stance": "SUPPORT", "confidence": 0.9},
                  strategist={"backdrop": "hostile"})
    assert out["action"] == "drop" and out["scale"] == 0.0 and out["lean"] == "watch"


def test_nexus_pm_no_champion_withholds():
    """No champion (pm_conviction=no_slot) → the name is parked (§2.5 row 6)."""
    out = C.nexus(_bd(confirmed=True), {"stance": "SUPPORT", "confidence": 0.9},
                  pm_conviction={"proposal": "no_slot"})
    assert out["action"] == "drop" and out["scale"] == 0.0


def test_nexus_gate_veto_wins_over_full_quorum():
    """Even a fully-positive quorum cannot override a Gate-Officer VETO (§2.5 row 4)."""
    out = C.nexus(_bd(confirmed=True), {"stance": "SUPPORT", "confidence": 0.9},
                  strategist={"backdrop": "supportive"}, technician={"verdict": "now"},
                  pm_conviction={"proposal": "add"}, gate={"decision": "veto"})
    assert out["action"] == "drop" and out["scale"] == 0.0


def test_nexus_full_positive_quorum_passes_through():
    """All seats positive → the SENTINEL synthesis is left UNCHANGED (byte-identical to no seats)."""
    bd, sent = _bd(confirmed=True), {"stance": "SUPPORT", "confidence": 0.9}
    base = C.nexus(bd, sent)
    out = C.nexus(bd, sent, strategist={"backdrop": "supportive"},
                  technician={"verdict": "now"}, pm_conviction={"proposal": "add"},
                  gate={"decision": "approve"})
    assert out == base


def test_nexus_strictest_cut_wins():
    """Multiple cuts → the STRICTEST wins regardless of order (all are min()-caps)."""
    # staged_starter caps at 0.7, but a hostile strategist parks → the drop (0.0) dominates.
    out = C.nexus(_bd(confirmed=True), {"stance": "SUPPORT", "confidence": 0.9},
                  strategist={"backdrop": "hostile"}, technician={"verdict": "staged_starter"})
    assert out["action"] == "drop" and out["scale"] == 0.0
    # only a staged_starter → caps at 0.7 (the milder cut, no park)
    out2 = C.nexus(_bd(confirmed=True), {"stance": "SUPPORT", "confidence": 0.9},
                   technician={"verdict": "staged_starter"})
    assert out2["action"] == "trim" and out2["scale"] == 0.7


def test_nexus_quorum_never_rescues_unconfirmed():
    """SUBTRACT-ONLY: no combination of positive seats can rescue a FORGE-unconfirmed name."""
    out = C.nexus(_bd(confirmed=False), {"stance": "SUPPORT", "confidence": 0.9},
                  strategist={"backdrop": "supportive"}, technician={"verdict": "now"},
                  pm_conviction={"proposal": "add"}, gate={"decision": "approve"})
    assert out["action"] == "drop" and out["scale"] == 0.0


def test_nexus_quorum_never_raises_scale():
    """INVARIANT: for EVERY seat combination the final scale ≤ the synthesis scale (subtract-only)."""
    bd, sent = _bd(confirmed=True), {"stance": "CONDITIONAL", "confidence": 0.5}
    base_scale = C.nexus(bd, sent)["scale"]        # 0.66 (CONDITIONAL trim)
    for strat in (None, {"backdrop": "supportive"}, {"backdrop": "neutral"}, {"backdrop": "hostile"}):
        for tech in (None, {"verdict": "now"}, {"verdict": "staged_starter"}, {"verdict": "wait"}):
            for pm in (None, {"proposal": "add"}, {"proposal": "no_slot"}):
                for g in (None, {"decision": "approve"}, {"decision": "veto"}):
                    out = C.nexus(bd, sent, strategist=strat, technician=tech,
                                  pm_conviction=pm, gate=g)
                    assert out["scale"] <= base_scale + 1e-9, (strat, tech, pm, g, out)


# ─────────────────────────────────────────────────────────────────────────────
# PHASE2 CONTRACT — the exact seat-verdict → nexus mapping bot/phase2.py builds, exercised
# through committee.nexus with the SAME seat-dict shapes the desk-quorum block emits (backdrop /
# verdict / proposal / decision keys). No LLM, no full build — just the mapping contract.
# ─────────────────────────────────────────────────────────────────────────────
def _phase2_seat_dicts(*, strat=None, tech=None, pm=None, gate=None):
    """Reproduce the phase2 seat-kwarg construction (None verdict → None seat dict)."""
    return {
        "technician": {"verdict": tech} if tech is not None else None,
        "strategist": {"backdrop": strat} if strat is not None else None,
        "pm_conviction": {"proposal": pm} if pm is not None else None,
        "gate": {"decision": gate} if gate is not None else None,
    }


def _phase2_quorum(*, strat=None, tech=None, pm=None, gate=None, combined=78):
    """The exact nexus call phase2 makes: fresh confirmed base, SENTINEL None, the four seat dicts."""
    sk = _phase2_seat_dicts(strat=strat, tech=tech, pm=pm, gate=gate)
    return C.nexus({"confirmed": True, "combined": combined}, None,
                   technician=sk["technician"], strategist=sk["strategist"],
                   pm_conviction=sk["pm_conviction"], gate=sk["gate"])


def test_phase2_all_seats_none_confirms_full():
    """OFF-equivalent: every seat None (no LLM / off) → confirm at full size (pass-through)."""
    out = _phase2_quorum()
    assert out["action"] == "confirm" and out["scale"] == 1.0


def test_phase2_full_positive_quorum_confirms_full():
    """A fully-positive quorum (neutral backdrop / now / add / approve) → confirm at full size."""
    out = _phase2_quorum(strat="neutral", tech="now", pm="add", gate="approve")
    assert out["action"] == "confirm" and out["scale"] == 1.0


def test_phase2_strategist_hostile_parks():
    """Row 1: a hostile backdrop hard-withholds even with every other seat positive."""
    out = _phase2_quorum(strat="hostile", tech="now", pm="add", gate="approve")
    assert out["action"] == "drop" and out["scale"] == 0.0


def test_phase2_pm_no_champion_parks():
    """Row 6: no champion (PM 'pass') → withhold, even with a supportive backdrop + approve gate."""
    out = _phase2_quorum(strat="supportive", tech="now", pm="pass", gate="approve")
    assert out["action"] == "drop" and out["scale"] == 0.0


def test_phase2_gate_veto_beats_full_quorum():
    """Row 4: the Gate Officer veto wins over an otherwise-full positive quorum."""
    out = _phase2_quorum(strat="supportive", tech="now", pm="add", gate="veto")
    assert out["action"] == "drop" and out["scale"] == 0.0


def test_phase2_staged_starter_caps_scale():
    """A STAGED_STARTER technician verdict caps the entry tranche (scale ≤ 0.7), never parks."""
    out = _phase2_quorum(strat="neutral", tech="staged_starter", pm="add", gate="approve")
    assert out["action"] == "trim" and out["scale"] <= 0.7


def test_phase2_gate_downsize_applied_via_apply_gate():
    """A gate 'trim' maps to approve+downsize_to; phase2 applies it via apply_gate (min-cap only)."""
    # phase2 receives gate decision {action: trim, scale: 0.4}; the quorum stays confirm/1.0
    # (nexus's 3-arg loop ignores downsize), then apply_gate caps it to 0.4 — never above.
    q = _phase2_quorum(strat="neutral", tech="now", pm="add", gate="approve")
    assert q["action"] == "confirm" and q["scale"] == 1.0
    capped_action, capped_scale = C.apply_gate(q["action"], q["scale"], "approve", downsize_to=0.4)
    assert capped_action == "trim" and capped_scale == 0.4
    # a downsize ABOVE the current scale can never raise it (subtract-only)
    same_action, same_scale = C.apply_gate("confirm", 0.5, "approve", downsize_to=0.9)
    assert (same_action, same_scale) == ("confirm", 0.5)


def test_phase2_never_rescues_unconfirmed():
    """FORGE-unconfirmed can never be rescued by any positive seat combo (phase2 base is confirmed=True,
    but a defensive check: an unconfirmed base drops regardless of the quorum)."""
    sk = _phase2_seat_dicts(strat="supportive", tech="now", pm="add", gate="approve")
    out = C.nexus({"confirmed": False, "combined": 78}, None,
                  technician=sk["technician"], strategist=sk["strategist"],
                  pm_conviction=sk["pm_conviction"], gate=sk["gate"])
    assert out["action"] == "drop" and out["scale"] == 0.0


def test_phase2_final_action_mapping():
    """The phase2 _final_action derivation: drop→park, scale<1→trim, else confirm."""
    def final_action(q_action, q_scale):
        return "park" if q_action == "drop" else ("trim" if q_scale < 1.0 else "confirm")
    assert final_action("drop", 0.0) == "park"
    assert final_action("trim", 0.7) == "trim"
    assert final_action("confirm", 1.0) == "confirm"
