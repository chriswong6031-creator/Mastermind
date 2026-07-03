"""tests/test_posture_decider.py — the W-E.2 E2.1 posture-decider unit + incident-replay suite.

WHAT THIS FILE PROVES (charter P6 — every mistake becomes machinery)
-------------------------------------------------------------------
The 2026-07-02 semis breakdown made executable.  These asserts are the permanent CI fixtures
build_plan.md §4.4 pre-registers — the stack must pass them forever:

  * D≈0.74 on the 06-26-shape tape  ⇒  ROTATE_DEFENSIVE by 06-26 (offense 0.40-0.45,
    defense_floor 0.22-0.27 at armed max 0.35, notch cap 0.70, appetite ~0.5).
  * 07-01's regressed STABLE print drops raw D  ⇒  de-escalation is DWELL-BLOCKED, class HELD.
  * The ROSY-PLANE stress variant (regime planes zeroed)  ⇒  D≈0.50, STILL ROTATE_DEFENSIVE
    (the band edge is pinned by the fixture, not hand-tuned).
  * The CALM tape  ⇒  OFFENSE, offense_budget BYTE-IDENTICAL to today (0.52) — zero drift.

Plus the unit contracts of every organ: the verbatim offense equation, the renormalizing
multi-plane D (missing plane excluded, never a fabricated zero-risk read), the class bands, the
hysteresis machine (escalate-instantly / de-escalate-slowly / tripwire-clamp / max-dwell), the
degrade-to-stateless invariant, the shadow-always-publishes contract, and the anti-compounding
single-consumption (shrink_provenance == the ONE defense pathway).

THE WAVE CONTRACT — SHADOW BY DEFAULT
------------------------------------
MASTERMIND_POSTURE_DECIDER defaults '0'.  The decider PUBLISHES its shadow artifact every build
regardless of the flag; NO sizing path reads it this wave.  Arming is E3.3 — not here.

ISOLATION: every test monkeypatches ``_STATE_PATH`` / ``_ARTIFACT_DIR`` / ``_LATEST_PATH`` onto a
tmp dir so no live data/posture/ file is read or written by the suite, and the hysteresis state
never bleeds between tests.  The tapes are the self-contained fixtures under tests/fixtures/posture/.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import bot  # noqa: F401  -> puts vendor/macro on sys.path

from brain import posture_decider as PD
from brain import regime_frame as RF

_POSTURE_FIX = Path(__file__).resolve().parent / "fixtures" / "posture"
_FLAG = "MASTERMIND_POSTURE_DECIDER"


# ── fixtures ─────────────────────────────────────────────────────────────────────────────────────
def _fixture(name: str) -> dict:
    return json.loads((_POSTURE_FIX / f"{name}.json").read_text())


def _disagreeing() -> dict:
    return _fixture("disagreeing_tape")


def _calm() -> dict:
    return _fixture("calm_tape")


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    """Isolate the artifact + state paths onto a tmp dir (no live data/posture/ touched; no state
    bleed between tests).  Returns a helper that writes a regime tape to the region path and returns
    the loaded fixture, and defaults the flag OFF (the shadow / control arm)."""
    art = tmp_path / "_posture"
    art.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(PD, "_ARTIFACT_DIR", art, raising=False)
    monkeypatch.setattr(PD, "_LATEST_PATH", art / "latest.json", raising=False)
    monkeypatch.setattr(PD, "_STATE_PATH", art / "state.json", raising=False)
    monkeypatch.setenv(_FLAG, "0")
    reg_path = tmp_path / "regime_latest.json"
    monkeypatch.setitem(RF._REGION_PATHS, "us", reg_path)

    def _load_tape(tape: dict) -> dict:
        reg_path.write_text(json.dumps(tape["regime"]))
        return tape

    return _load_tape


def _evidence(tape: dict) -> dict:
    return RF.rotation_evidence(**tape["evidence"])


# ══════════════════════════════════════════════════════════════════════════════════════════════════
# OFFENSE side — the W2 equation moved VERBATIM (ledger row 1 discipline)
# ══════════════════════════════════════════════════════════════════════════════════════════════════
class TestOffenseEquationVerbatim:
    def test_calm_tape_offense_byte_identical_to_budget(self, isolated):
        """The decider's offense equation is byte-identical to regime_frame.budget() on the calm tape:
        clamp(0.40 + 0.20·0.60·1.0·1.0) == 0.52 (the same value test_regime_frame pins)."""
        tape = isolated(_calm())
        post = PD.decide("us", evidence=_evidence(tape), risk_state=tape["risk_state"],
                         write_state=False)
        assert post["offense_budget_equation"] == pytest.approx(0.52)
        # and on the calm tape the class is OFFENSE (cap 0.60) so the min() no-op keeps 0.52.
        assert post["posture_class"] == "OFFENSE"
        assert post["offense_budget"] == pytest.approx(0.52)

    def test_offense_degrades_to_midpoint_on_missing_field(self, isolated):
        """Any load-bearing field missing → the equation degrades to the 0.50 midpoint (verbatim W2)."""
        tape = _calm()
        tape["regime"].pop("confidence", None)
        isolated(tape)
        post = PD.decide("us", risk_state=tape["risk_state"], write_state=False)
        assert post["offense_budget_equation"] == pytest.approx(0.50)

    def test_offense_clamp_band(self, isolated):
        """The clamp keeps the offense equation in [0.40, 0.60] for every conf/transition combo."""
        for conf in (-1.0, 0.0, 0.5, 1.0, 9.0):
            for ts in ("STABLE", "WEAKENING", "ROLLING", "DETERIORATING", "MADE_UP"):
                tape = {"regime": {"confidence": conf, "transition_state": ts,
                                   "flip_condition": {"margin": 0.01}},
                        "evidence": {}, "risk_state": {"state": "risk_on"}}
                isolated(tape)
                post = PD.decide("us", risk_state=tape["risk_state"], write_state=False)
                assert 0.40 <= post["offense_budget_equation"] <= 0.60


# ══════════════════════════════════════════════════════════════════════════════════════════════════
# DEFENSE side — the renormalizing multi-plane D (missing plane excluded, never fabricated 0-risk)
# ══════════════════════════════════════════════════════════════════════════════════════════════════
class TestDefensePressureRenormalizes:
    def test_all_planes_absent_degrades_to_balanced_midpoint(self, isolated):
        """No frame fields, no evidence, no risk_state → every plane absent → D degrades to 0.5
        (BALANCED), NEVER to 0-risk (charter P2 — missing data can never manufacture an all-clear)."""
        isolated({"regime": {}, "evidence": {}, "risk_state": {}})
        post = PD.decide("us", write_state=False)
        assert post["defense_pressure"] == pytest.approx(0.5)
        assert post["posture_class"] in ("BALANCED", "ROTATE_DEFENSIVE")  # never OFFENSE on a blind read

    def test_missing_plane_is_excluded_not_zero(self, isolated):
        """A missing evidence source is EXCLUDED from the mean (renormalize), not counted as 0.  With
        only the regime-fragility plane present (conf 0.0 → pressure 1.0) and no other planes, D == 1.0
        — proving the absent planes did not dilute it toward 0."""
        isolated({"regime": {"confidence": 0.0}, "evidence": {}, "risk_state": {}})
        post = PD.decide("us", write_state=False)
        present = {k: v["value"] for k, v in post["planes"].items() if v["available"]}
        assert present == {"regime_fragility": 1.0}
        assert post["defense_pressure"] == pytest.approx(1.0)

    def test_advisory_nowcast_raises_not_lowers(self, isolated):
        """regime_nowcast is ADVISORY (gate failed 0.354): its plane value is shrink-biased (0.5 on a
        doubt) and it enters the mean as a source that can only RAISE the read — a doubt is never a 0."""
        tape = _disagreeing()
        isolated(tape)
        post = PD.decide("us", evidence=_evidence(tape), risk_state=tape["risk_state"],
                         write_state=False)
        nc = post["planes"]["regime_nowcast"]
        assert nc["available"] is True
        assert nc["value"] == pytest.approx(0.5)  # doubt → 0.5 (shrink-biased, never a 0 all-clear)


# ══════════════════════════════════════════════════════════════════════════════════════════════════
# THE INCIDENT REPLAY — build_plan.md §4.4 (the permanent CI battery)
# ══════════════════════════════════════════════════════════════════════════════════════════════════
class TestIncidentReplay:
    def test_06_26_rotate_defensive(self, isolated):
        """D≈0.74 on the 06-26-shape tape ⇒ ROTATE_DEFENSIVE by 06-26, with the pinned levers:
        offense 0.40-0.45, defense_floor 0.22-0.27 at armed max 0.35, notch cap 0.70, appetite ~0.5."""
        tape = isolated(_disagreeing())
        post = PD.decide("us", evidence=_evidence(tape), risk_state=tape["risk_state"],
                         write_state=False)
        assert post["posture_class"] == "ROTATE_DEFENSIVE"
        # D is high (multi-plane defensive) — the band edge, not hand-tuned.
        assert post["defense_pressure"] >= 0.50
        # offense pinned into the incident band (build_plan §4.4: 0.40-0.45)
        assert 0.40 <= post["offense_budget"] <= 0.45
        # the WOULD-BE defensive floor at the armed ceiling (0.35·D) lands in 0.22-0.27
        assert 0.22 <= post["defense_floor_at_max"] <= 0.27
        # the class notch cap + appetite
        assert post["posture_notch_cap"] == pytest.approx(0.70)
        assert post["conviction_appetite"] == pytest.approx(0.50)

    def test_defense_floor_inert_until_armed(self, isolated):
        """The LIVE defense_floor is 0.0 while the sleeve is inert (def_sleeve.max 0.0, the control arm)
        — the WOULD-BE floor is only the shadow's defense_floor_at_max.  (Arming is E3.3.)"""
        tape = isolated(_disagreeing())
        post = PD.decide("us", evidence=_evidence(tape), risk_state=tape["risk_state"],
                         write_state=False)
        assert post["defense_floor"] == pytest.approx(0.0)          # inert (def_sleeve.max 0.0)
        assert post["defense_floor_at_max"] > 0.0                    # the shadow records the would-be

    def test_07_01_deescalation_dwell_blocked(self, isolated):
        """06-26 escalates to ROTATE_DEFENSIVE; 07-01's regressed STABLE print drops raw D to OFFENSE
        but de-escalation is DWELL-BLOCKED — the class is HELD.  (The 2026-07-01 un-cap made
        structurally impossible.)"""
        dis = isolated(_disagreeing())
        # 06-26 — escalate, persist state
        p1 = PD.decide("us", evidence=_evidence(dis), risk_state=dis["risk_state"], write_state=True)
        assert p1["posture_class"] == "ROTATE_DEFENSIVE"
        # 07-01 — the regressed rosy print (calm tape) drops raw D; the class must HOLD
        calm = isolated(_calm())
        p2 = PD.decide("us", evidence=_evidence(calm), risk_state=calm["risk_state"], write_state=True)
        assert p2["hysteresis"]["class_raw"] == "OFFENSE"       # the raw read regressed
        assert p2["posture_class"] == "ROTATE_DEFENSIVE"        # ...but the class is HELD (dwell-blocked)
        assert p2["hysteresis"]["deescalate_count"] == 1        # one lower session accrued, not 3

    def test_07_01_stays_blocked_until_three_lower_sessions(self, isolated):
        """De-escalation requires 3 CONSECUTIVE lower sessions — the class holds through builds 1 & 2
        and only steps DOWN on build 3 (escalate-instantly / de-escalate-slowly)."""
        dis = isolated(_disagreeing())
        PD.decide("us", evidence=_evidence(dis), risk_state=dis["risk_state"], write_state=True)
        calm = isolated(_calm())
        held = []
        for _ in range(3):
            p = PD.decide("us", evidence=_evidence(calm), risk_state=calm["risk_state"],
                          write_state=True)
            held.append(p["posture_class"])
        # builds 1,2 held ROTATE_DEFENSIVE; build 3 steps DOWN one band (→ BALANCED)
        assert held[0] == "ROTATE_DEFENSIVE"
        assert held[1] == "ROTATE_DEFENSIVE"
        assert held[2] == "BALANCED"

    def test_rosy_plane_stress_still_rotate_defensive(self, isolated):
        """The ROSY-PLANE stress variant: regime planes forced rosy (conf 1.0 / STABLE / wide flip →
        P1,P2,P3 all 0) while the evidence planes still fire ⇒ D≈0.50, STILL ROTATE_DEFENSIVE.  The
        band edge is pinned by the fixture, not hand-tuned — one regressing label cannot suppress the
        multi-plane read."""
        tape = _disagreeing()
        tape["regime"]["confidence"] = 1.0
        tape["regime"]["transition_state"] = "STABLE"
        tape["regime"]["flip_condition"] = {"margin": 0.40}
        isolated(tape)
        post = PD.decide("us", evidence=_evidence(tape), risk_state=tape["risk_state"],
                         write_state=False)
        # the regime planes are zeroed (rosy) ...
        assert post["planes"]["regime_fragility"]["value"] == pytest.approx(0.0)
        assert post["planes"]["transition_tilt"]["value"] == pytest.approx(0.0)
        assert post["planes"]["flip_fragility"]["value"] == pytest.approx(0.0)
        # ... yet the class HOLDS ROTATE_DEFENSIVE on the surviving evidence planes (D at the band edge)
        assert post["defense_pressure"] == pytest.approx(0.50, abs=0.02)
        assert post["posture_class"] == "ROTATE_DEFENSIVE"

    def test_calm_tape_offense_zero_drift(self, isolated):
        """The CALM-tape control: OFFENSE class, offense_budget byte-identical to today (0.52), the
        defensive levers off — ZERO drift (the shrink-only/degrade-to-today discipline)."""
        tape = isolated(_calm())
        post = PD.decide("us", evidence=_evidence(tape), risk_state=tape["risk_state"],
                         write_state=False)
        assert post["posture_class"] == "OFFENSE"
        assert post["offense_budget"] == pytest.approx(0.52)
        assert post["defense_floor"] == pytest.approx(0.0)
        assert post["posture_notch_cap"] == pytest.approx(1.0)  # no-op ceiling → no notch
        assert post["defense_pressure"] < PD._BAND_OFFENSE      # D below the OFFENSE band edge


# ══════════════════════════════════════════════════════════════════════════════════════════════════
# HYSTERESIS — the state machine (escalate instantly / tripwire clamp / max-dwell / degrade-stateless)
# ══════════════════════════════════════════════════════════════════════════════════════════════════
class TestHysteresis:
    def test_escalates_instantly(self, isolated):
        """A more-defensive raw class jumps up THIS build (no dwell required to escalate)."""
        # seed OFFENSE
        calm = isolated(_calm())
        PD.decide("us", evidence=_evidence(calm), risk_state=calm["risk_state"], write_state=True)
        # a defensive tape escalates immediately
        dis = isolated(_disagreeing())
        p = PD.decide("us", evidence=_evidence(dis), risk_state=dis["risk_state"], write_state=True)
        assert p["posture_class"] == "ROTATE_DEFENSIVE"
        assert p["hysteresis"]["class_raw"] == "ROTATE_DEFENSIVE"

    def test_tripwire_clamp_blocks_deescalation(self, isolated):
        """A sev>=2 derisk tripwire (last_severity=2) BLOCKS de-escalation even past the 3-session
        streak — a hot tripwire holds the defensive class (the 06-26..07-01 clamp)."""
        dis = isolated(_disagreeing())
        PD.decide("us", evidence=_evidence(dis), risk_state=dis["risk_state"], write_state=True)
        calm = isolated(_calm())
        # feed 4 lower sessions but with a hot sev-2 tripwire each build → never de-escalates
        for _ in range(4):
            p = PD.decide("us", evidence=_evidence(calm), risk_state=calm["risk_state"],
                          last_severity=2, write_state=True)
        assert p["posture_class"] == "ROTATE_DEFENSIVE"

    def test_max_dwell_auto_release(self, isolated, monkeypatch):
        """A class held past max_dwell_sessions whose raw read is lower auto-releases one step so
        PRESERVE cannot become a trap (still one step at a time)."""
        # shrink the max-dwell horizon for a fast test via the doctrine-sourced helper
        monkeypatch.setattr(PD, "_hysteresis_cfg", lambda: {
            "deescalate_consecutive": 999,   # de-escalation streak effectively disabled
            "tripwire_clamp_sessions": 2,
            "max_dwell_sessions": 3,
        })
        dis = isolated(_disagreeing())
        PD.decide("us", evidence=_evidence(dis), risk_state=dis["risk_state"], write_state=True)
        calm = isolated(_calm())
        cls = None
        for _ in range(6):
            p = PD.decide("us", evidence=_evidence(calm), risk_state=calm["risk_state"],
                          write_state=True)
            cls = p["posture_class"]
        # the streak path is disabled, but max-dwell releases one step once dwell > 3
        assert cls in ("BALANCED", "OFFENSE")

    def test_degrade_to_stateless_on_corrupt_state(self, isolated):
        """A corrupt/absent state file degrades to STATELESS — the class == the raw read this build
        (no history → held == raw), never a looser class than the raw read."""
        # write garbage to the state path
        PD._STATE_PATH.write_text("{ this is not json")
        dis = isolated(_disagreeing())
        p = PD.decide("us", evidence=_evidence(dis), risk_state=dis["risk_state"], write_state=False)
        # stateless: the held class equals the raw class (the defensive read stands on its own)
        assert p["posture_class"] == p["hysteresis"]["class_raw"] == "ROTATE_DEFENSIVE"


# ══════════════════════════════════════════════════════════════════════════════════════════════════
# THE SHADOW CONTRACT — publishes every build regardless of the flag (ledger row 6)
# ══════════════════════════════════════════════════════════════════════════════════════════════════
class TestShadowContract:
    def test_publishes_latest_and_dated_with_flag_off(self, isolated, monkeypatch):
        """build() writes latest.json + a dated copy EVERY build with the flag OFF (the shadow window
        accrues immediately; arming is the separate E3.3 decision)."""
        monkeypatch.setenv(_FLAG, "0")
        tape = isolated(_disagreeing())
        art = PD.build("us", evidence=_evidence(tape), risk_state=tape["risk_state"], write=True)
        assert art["shadow"] is True
        assert PD._LATEST_PATH.exists()
        payload = json.loads(PD._LATEST_PATH.read_text())
        # the shadow carries the full evidence trail
        for k in ("posture_class", "offense_budget", "defense_pressure", "planes", "why",
                  "shrink_provenance"):
            assert k in payload
        dated = list(PD._ARTIFACT_DIR.glob("2026-*.json"))
        assert dated, "the shadow publish must write a dated copy alongside latest.json"

    def test_shadow_flag_marks_shadow_true(self, isolated, monkeypatch):
        """Flag OFF ⇒ shadow=True in the artifact; flag ON ⇒ shadow=False (the arming semantics)."""
        tape = isolated(_disagreeing())
        monkeypatch.setenv(_FLAG, "0")
        assert PD.decide("us", evidence=_evidence(tape), risk_state=tape["risk_state"],
                         write_state=False)["shadow"] is True
        monkeypatch.setenv(_FLAG, "1")
        assert PD.decide("us", evidence=_evidence(tape), risk_state=tape["risk_state"],
                         write_state=False)["shadow"] is False

    def test_build_never_raises_on_write_failure(self, isolated, monkeypatch):
        """A publish failure degrades to returning the posture (charter P10 — a perception organ that
        fails to publish protects nothing but breaks nothing)."""
        tape = isolated(_disagreeing())
        # point the artifact dir at an unwritable path
        monkeypatch.setattr(PD, "_LATEST_PATH", Path("/proc/does/not/exist/latest.json"),
                            raising=False)
        monkeypatch.setattr(PD, "_ARTIFACT_DIR", Path("/proc/does/not/exist"), raising=False)
        art = PD.build("us", evidence=_evidence(tape), risk_state=tape["risk_state"], write=True)
        assert art["posture_class"] == "ROTATE_DEFENSIVE"  # returned even though the write failed


# ══════════════════════════════════════════════════════════════════════════════════════════════════
# ANTI-COMPOUNDING — the single-consumption pathway (ledger row 5)
# ══════════════════════════════════════════════════════════════════════════════════════════════════
class TestAntiCompounding:
    def test_shrink_provenance_is_single_defense_pathway(self, isolated):
        """The evidence is consumed ONCE, on the defense side: shrink_provenance names a SINGLE
        pathway (defense_D), never a compounded {offense_damp, defense_D} pair."""
        tape = isolated(_disagreeing())
        post = PD.decide("us", evidence=_evidence(tape), risk_state=tape["risk_state"],
                         write_state=False)
        assert post["shrink_provenance"] == "defense_D"

    def test_offense_carries_no_evidence_damp(self, isolated):
        """The offense equation reads NO evidence damp (the · D factor is absent) — the same evidence
        that raises D does NOT also shrink offense.  On the disagreeing tape the offense EQUATION is
        the un-damped clamp(0.40 + 0.20·0.327·1.0·0.75) == 0.44905 (the damp would have made it lower;
        the shrink lives on the defense side instead)."""
        tape = isolated(_disagreeing())
        post = PD.decide("us", evidence=_evidence(tape), risk_state=tape["risk_state"],
                         write_state=False)
        expected_undamped = 0.40 + 0.20 * 0.327 * 1.0 * 0.75
        assert post["offense_budget_equation"] == pytest.approx(expected_undamped, abs=1e-4)


# ══════════════════════════════════════════════════════════════════════════════════════════════════
# DENYLIST + doctrine block presence (E2.1 ship-blockers)
# ══════════════════════════════════════════════════════════════════════════════════════════════════
class TestDoctrineBlock:
    def test_posture_block_present_and_read(self):
        """The doctrine posture: block exists and the decider reads its bands (the config is the source
        of truth; the module priors are only the fallback)."""
        b_balanced, b_rot, b_pres = PD._bands()
        assert b_balanced == pytest.approx(0.25)
        assert b_rot == pytest.approx(0.50)
        assert b_pres == pytest.approx(0.75)

    def test_class_map_offense_caps_read_from_doctrine(self):
        """The class offense ceilings are read from doctrine posture.class_map (min-composed with the
        equation)."""
        assert PD._offense_cap("OFFENSE") == pytest.approx(0.60)
        assert PD._offense_cap("ROTATE_DEFENSIVE") == pytest.approx(0.43)
        assert PD._offense_cap("PRESERVE") == pytest.approx(0.40)

    def test_posture_denylisted_for_self_tune_code_and_doctrine(self):
        """The posture: block is DENYLISTED for self_tune in BOTH the code _HARD_DENYLIST and the
        doctrine denylist (gate-adjacent config; a tuner could gerrymander its own posture — P8)."""
        from brain import self_tune as ST
        assert any("posture" in t.lower() for t in ST._HARD_DENYLIST)
        assert ST._denied("posture.bands.rotate_defensive") is True
        text = (Path(__file__).resolve().parent.parent / "config" / "doctrine.yml").read_text()
        import yaml
        denyl = ((yaml.safe_load(text) or {}).get("self_tune") or {}).get("denylist") or []
        assert any("posture" in str(x).lower() for x in denyl)
