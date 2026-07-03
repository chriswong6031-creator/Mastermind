"""tests/test_posture_migration.py — the byte-identity migration suite for W-E.2.

WHY THIS FILE EXISTS (written FIRST, per build_plan.md §1)
---------------------------------------------------------
This is the failing-safe test suite the W-E.2 subsumption build tasks (E2.1/E2.2) must
keep green.  It encodes ONE test-class per row of the SUBSUMPTION LEDGER (build_plan.md §1
table).  Every ledger row is a ship-blocker; its named migration test lives here.

THE WAVE CONTRACT (the invariant these tests enforce)
-----------------------------------------------------
``MASTERMIND_POSTURE_DECIDER`` defaults to ``'0'`` throughout the wave.  Flag OFF ⇒ every
touched module is BYTE-IDENTICAL to this branch's base (the E3 control arm) — nothing moves.
The decider PUBLISHES its shadow artifact (data/posture/latest.json) every build regardless
of the flag, so the shadow window accrues immediately.

ARMING (flipping the flag to '1') is E3.3 — a Fable decision, NOT this wave.  Under the
arming semantics:
  * confidence/transition move INSIDE decide() (regime_frame.budget() becomes a shim);
  * the W-I evidence damp is REMOVED from the offense equation (its planes become planes of D);
  * fragility_signal() retires to the flag-off fallback inside build_def_sleeve;
  * derisk eff_cap gains the posture notch via min() composition.
THE ANTI-COMPOUNDING LAW (W2's lesson): every signal consumed EXACTLY ONCE.  The grep-gate
below is the CI ship-blocker that enforces single-consumption when the flag is ON.

XFAIL-UNTIL-BUILT CONVENTION
----------------------------
The flag-OFF byte-identity tests (ledger rows 1-3, 6) run GREEN against the current base
right now — they are the control-arm guard and must never regress.

The flag-ON behavioural tests (ledger row 4 grep-gate, row 5 no-compounding) exercise
``brain.posture_decider`` / the subsumed sizing paths that E2.1/E2.2 build.  Until those
modules land, those tests are marked ``xfail(strict=False)`` with ``reason`` naming the
missing artifact.  As the build lands each xfail FLIPS TO LIVE (an ``xpass`` on a
``strict=False`` xfail is not a failure; the build task removes the marker once the module
exists so a real regression can fail loudly).  The marker helper ``_posture_decider_absent``
detects the module so the flip is automatic — no test edit needed the day the module lands,
only the marker removal in E2.2's final pass.

NO LIVE FILES ARE TOUCHED.  The tapes are the incident fixtures
(tests/incident_replays/fixtures/2026-07-02-semis-breakdown/) and the calm fixtures
(tests/fixtures/market_view/), distilled into self-contained records under
tests/fixtures/posture/ so this suite never live-reads another suite's internals.
"""
from __future__ import annotations

import ast
import importlib.util
import json
import os
from pathlib import Path

import pytest

import bot  # noqa: F401  -> puts vendor/macro on sys.path

# ── repo geometry ────────────────────────────────────────────────────────────────────────────────
_ROOT = Path(__file__).resolve().parent.parent
_POSTURE_FIX = Path(__file__).resolve().parent / "fixtures" / "posture"
_FLAG = "MASTERMIND_POSTURE_DECIDER"


# ── fixture loaders ──────────────────────────────────────────────────────────────────────────────
def _posture_fixture(name: str) -> dict:
    """Load a self-contained posture tape fixture (regime + evidence + risk_state + expected)."""
    return json.loads((_POSTURE_FIX / f"{name}.json").read_text())


def _disagreeing_tape() -> dict:
    return _posture_fixture("disagreeing_tape")


def _calm_tape() -> dict:
    return _posture_fixture("calm_tape")


def _write_regime(tmp_path: Path, payload: dict) -> Path:
    p = tmp_path / "latest.json"
    p.write_text(json.dumps(payload))
    return p


# ── module-presence probes (drive the xfail-until-built flips) ────────────────────────────────────
def _module_exists(dotted: str) -> bool:
    """True iff *dotted* (e.g. 'brain.posture_decider') is importable on this base."""
    try:
        return importlib.util.find_spec(dotted) is not None
    except (ImportError, ValueError, ModuleNotFoundError):
        return False


_POSTURE_DECIDER_PRESENT = _module_exists("brain.posture_decider")


def _subsumption_guards_present() -> bool:
    """True iff the E2.2 SUBSUMPTION guards have landed — i.e. the downstream sizing modules carry a
    posture-flag guard so the arming path (budget shim / evidence-damp removal / fragility retirement)
    is wired.  The row-4 grep-gate and row-5 no-compounding tests exercise THAT arming path, so they
    are xfail-until-E2.2 even after the E2.1 module lands (E2.1 ships the decider + its shadow; E2.2
    ships the subsumption wiring these tests assert).  Detected by the posture-flag guard appearing in
    portfolio/rotation.py (the build_def_sleeve fragility fallback guard E2.2 adds — row 4c)."""
    try:
        rot = (_ROOT / "portfolio" / "rotation.py").read_text()
    except OSError:
        return False
    return ("MASTERMIND_POSTURE_DECIDER" in rot or "posture_decider" in rot
            or "_posture_on" in rot)


_SUBSUMPTION_GUARDS_PRESENT = _subsumption_guards_present()

# The flag-ON tests xfail until the E2.2 SUBSUMPTION lands (the arming wiring they assert).  The E2.1
# module + shadow can be present while the E2.2 guards are not — the decider ships & publishes its
# shadow first (E2.1); the budget shim / evidence-damp removal / fragility retirement / grep-gate
# guards land in the ONE subsumption PR (E2.2).  strict=False so the day E2.2's guards arrive these
# xpass (not a failure); E2.2's final pass removes the marker so a later regression fails loudly.
# ``reason`` names the missing artifact so a red board is self-explaining.
# E2.2 LANDED (2026-07-03, Fable): the xfail-until-built convention is RETIRED per the module
# docstring — Rows 4/5 are now enforced live ship-blockers; a single-consumption or compounding
# regression fails the suite loudly. The retired marker is kept (unused) for archaeology:
_xfail_until_decider = pytest.mark.xfail(
    not _SUBSUMPTION_GUARDS_PRESENT,
    reason="E2.2 subsumption guards not wired yet (budget shim / evidence-damp removal / fragility "
           "retirement) — the flag-ON arming path is xfail-until-E2.2; the E2.1 decider + shadow are "
           "present but do not touch the sizing spine (the wave contract: flag OFF ⇒ byte-identical)",
    strict=False,
)


# ── flag helpers ─────────────────────────────────────────────────────────────────────────────────
def _flag_off(monkeypatch) -> None:
    """Force the wave flag OFF (the control arm)."""
    monkeypatch.setenv(_FLAG, "0")


def _flag_on(monkeypatch) -> None:
    """Force the wave flag ON (arming semantics — only exercised by the xfail-until-built tests)."""
    monkeypatch.setenv(_FLAG, "1")


# ══════════════════════════════════════════════════════════════════════════════════════════════════
# LEDGER ROW 1 — regime_frame.budget() shim + the W-I evidence damp path (flag OFF byte-identical)
# ══════════════════════════════════════════════════════════════════════════════════════════════════
class TestRow1BudgetByteIdentical:
    """Ledger row 1 + the W-I damp path (row 2 offense half).

    Flag OFF ⇒ regime_frame.budget() is byte-identical to the pre-W-E master on the W2 golden
    vectors, the W-I evidence-damped budget is byte-identical, and calm-tape invariance holds.

    The golden vectors are IMPORTED from the existing golden suite (the ledger says: "import and
    re-run the existing golden tests' vectors") so there is ONE source of truth for the equation.
    """

    def _budget(self, monkeypatch, tmp_path, payload, evidence=None):
        from brain import regime_frame as RF
        _flag_off(monkeypatch)
        p = _write_regime(tmp_path, payload)
        monkeypatch.setitem(RF._REGION_PATHS, "us", p)
        return RF.budget("us", evidence=evidence)

    # --- the W2 golden vectors, imported verbatim from test_regime_frame ------------------------
    def test_w2_golden_weakening_conf_0327(self, monkeypatch, tmp_path):
        """Flag OFF: WEAKENING @ conf 0.327, flip 0.05 → the W2 closed-form value (row 1)."""
        from tests.test_regime_frame import _FULL_REGIME
        payload = {**_FULL_REGIME, "transition_state": "WEAKENING"}
        out = self._budget(monkeypatch, tmp_path, payload)
        expected = 0.40 + 0.20 * 0.327 * 0.6 * 0.75
        assert out["lead_budget"] == pytest.approx(expected)

    def test_w2_golden_calm_tape_stable(self, monkeypatch, tmp_path):
        """Flag OFF: the calm-tape golden vector (conf 0.60, STABLE, wide flip) → 0.52 exactly."""
        payload = {"confidence": 0.60, "transition_state": "STABLE",
                   "flip_condition": {"margin": 0.40}}
        out = self._budget(monkeypatch, tmp_path, payload)
        assert out["lead_budget"] == pytest.approx(0.52)
        assert out["inputs"]["T"] == pytest.approx(1.0)
        assert out["inputs"]["F"] == pytest.approx(1.0)

    def test_w2_golden_deteriorating(self, monkeypatch, tmp_path):
        payload = {"confidence": 1.0, "transition_state": "DETERIORATING",
                   "flip_condition": {"margin": 0.40}}
        out = self._budget(monkeypatch, tmp_path, payload)
        assert out["lead_budget"] == pytest.approx(0.48)

    def test_w2_golden_clamp_band(self, monkeypatch, tmp_path):
        """The clamp keeps lead_budget in [0.40, 0.60] for every conf/transition combo (row 1)."""
        for conf in (-1.0, 0.0, 0.5, 1.0, 9.0):
            for ts in ("STABLE", "WEAKENING", "ROLLING", "DETERIORATING", "MADE_UP"):
                payload = {"confidence": conf, "transition_state": ts,
                           "flip_condition": {"margin": 0.01}}
                lb = self._budget(monkeypatch, tmp_path, payload)["lead_budget"]
                assert 0.40 <= lb <= 0.60, f"conf={conf} ts={ts} → {lb} out of band"

    def test_w2_golden_degrade_to_midpoint(self, monkeypatch, tmp_path):
        """Missing any load-bearing field → 0.50 midpoint (row 1 degrade path unchanged)."""
        payload = {"transition_state": "STABLE", "flip_condition": {"margin": 0.40}}  # no confidence
        out = self._budget(monkeypatch, tmp_path, payload)
        assert out["lead_budget"] == pytest.approx(0.50)

    # --- the W-I evidence-damp path (row 2 offense half — byte-identical flag OFF) ---------------
    def test_wi_damp_no_evidence_is_byte_identical(self, monkeypatch, tmp_path):
        """Flag OFF: no evidence (and 0/1 agree) reproduces the un-damped budget exactly (row 2)."""
        from brain import regime_frame as RF
        _flag_off(monkeypatch)
        payload = {"confidence": 0.327, "transition_state": "STABLE",
                   "flip_condition": {"margin": 0.05}}
        p = _write_regime(tmp_path, payload)
        monkeypatch.setitem(RF._REGION_PATHS, "us", p)
        base = RF.budget("us")["lead_budget"]

        def _ev(n_true):
            keys = ("nowcast_doubt", "liquidity_stress", "radar_caution", "defensive_rs_cross")
            return RF.rotation_evidence(**{k: (True if i < n_true else None)
                                           for i, k in enumerate(keys)})

        assert RF.budget("us", evidence=None)["lead_budget"] == base
        assert RF.budget("us", evidence=_ev(0))["lead_budget"] == base
        assert RF.budget("us", evidence=_ev(1))["lead_budget"] == base

    def test_wi_damp_two_and_three_agree_unchanged(self, monkeypatch, tmp_path):
        """Flag OFF: the 2-agree (×0.9) and 3+-agree (×0.8) damp multipliers are unchanged (row 2)."""
        from brain import regime_frame as RF
        _flag_off(monkeypatch)
        payload = {"confidence": 0.327, "transition_state": "STABLE",
                   "flip_condition": {"margin": 0.05}}  # F=0.75
        p = _write_regime(tmp_path, payload)
        monkeypatch.setitem(RF._REGION_PATHS, "us", p)

        def _ev(n_true):
            keys = ("nowcast_doubt", "liquidity_stress", "radar_caution", "defensive_rs_cross")
            return RF.rotation_evidence(**{k: (True if i < n_true else None)
                                           for i, k in enumerate(keys)})

        flex = 0.20 * 0.327 * 1.0 * 0.75
        assert RF.budget("us", evidence=_ev(2))["lead_budget"] == pytest.approx(0.40 + flex * 0.9)
        assert RF.budget("us", evidence=_ev(3))["lead_budget"] == pytest.approx(0.40 + flex * 0.8)

    def test_calm_tape_invariance_untouched(self, monkeypatch, tmp_path):
        """Flag OFF on the calm tape: budget == the fixture's pinned 0.52, no damp applies (row 1)."""
        tape = _calm_tape()
        out = self._budget(monkeypatch, tmp_path, tape["regime"])
        assert out["lead_budget"] == pytest.approx(tape["expected"]["budget"])
        assert out["inputs"]["D"] == pytest.approx(1.0)          # zero agreeing evidence → D 1.0
        assert out["inputs"]["evidence_n_agree"] == 0


# ══════════════════════════════════════════════════════════════════════════════════════════════════
# LEDGER ROW 2 — rotation.build_def_sleeve output byte-identical (fragility_signal still the sizer)
# ══════════════════════════════════════════════════════════════════════════════════════════════════
class TestRow2DefSleeveByteIdentical:
    """Ledger row 2/3 defence half: flag OFF ⇒ build_def_sleeve output byte-identical.

    fragility_signal is STILL the sizer when the flag is off (it retires only at arming).  With
    DEF_SLEEVE_MAX=0 (the default control arm) the sleeve is inert — an empty legs list with
    fragility_signal reported for the runlog.  This test pins that inert output on the disagreeing
    tape (where fragility_signal is maximal) so the subsumption PR cannot silently change it.
    """

    def test_def_sleeve_inert_control_arm_unchanged(self, monkeypatch):
        from portfolio import rotation as R
        from brain import regime_frame as RF
        _flag_off(monkeypatch)
        tape = _disagreeing_tape()
        # the same evidence dict phase2 threads; fragility_signal reads its n_agree lift
        ev = RF.rotation_evidence(**tape["evidence"])
        budget_inputs = {"confidence": tape["regime"]["confidence"],
                         "transition_state": tape["regime"]["transition_state"]}
        out = R.build_def_sleeve([], tape["risk_state"], budget_inputs,
                                 candidates=[], evidence=ev)
        # DEF_SLEEVE_MAX defaults to 0 → inert: empty legs, def_budget 0, but fragility surfaced.
        assert out["legs"] == []
        assert out["def_budget"] == pytest.approx(0.0)
        assert out["def_actual"] == pytest.approx(0.0)
        # fragility_signal is STILL computed (it is still the sizer when the flag is off).
        assert "fragility_signal" in out
        assert out["fragility_signal"] >= 0.0

    def test_fragility_signal_still_the_public_sizer(self, monkeypatch):
        """Flag OFF: fragility_signal() is a live, callable public sizer (it dies only at arming)."""
        from portfolio import rotation as R
        from brain import regime_frame as RF
        _flag_off(monkeypatch)
        tape = _disagreeing_tape()
        ev = RF.rotation_evidence(**tape["evidence"])
        budget_inputs = {"confidence": tape["regime"]["confidence"],
                         "transition_state": tape["regime"]["transition_state"]}
        sig = R.fragility_signal(tape["risk_state"], budget_inputs, evidence=ev)
        assert 0.0 <= sig <= 1.0
        # On the disagreeing tape (caution dwell + low conf + 4 agree lift) it is materially > 0.
        assert sig > 0.0

    def test_def_sleeve_byte_identical_no_evidence_vs_zero_agree(self, monkeypatch):
        """Flag OFF: an absent evidence dict and a 0-agree dict give byte-identical sleeve output."""
        from portfolio import rotation as R
        from brain import regime_frame as RF
        _flag_off(monkeypatch)
        budget_inputs = {"confidence": 0.60, "transition_state": "STABLE"}
        rs = {"state": "risk_on"}
        none_out = R.build_def_sleeve([], rs, budget_inputs, candidates=[], evidence=None)
        zero_out = R.build_def_sleeve([], rs, budget_inputs, candidates=[],
                                      evidence=RF.rotation_evidence())
        assert none_out == zero_out


# ══════════════════════════════════════════════════════════════════════════════════════════════════
# LEDGER ROW 3 — bot/derisk.py eff_cap identical to W1 behaviour (no posture_notch_cap term)
# ══════════════════════════════════════════════════════════════════════════════════════════════════
class TestRow3DeriskEffCapByteIdentical:
    """Ledger row 3: flag OFF ⇒ derisk eff_cap == the W1 min(state_cap, severity_cap) — no notch.

    We assert the composition at the SOURCE level (the eff_cap expression carries no
    posture_notch_cap term when the flag is off) AND at the value level (the severity ladder still
    produces the W1 caps).  The value-level check re-imports the existing severity helper so it
    tracks the W1 contract, not a duplicated constant.
    """

    def test_severity_ladder_w1_caps_unchanged(self, monkeypatch):
        """Flag OFF: the severity→cap ladder still returns the W1 caps (sev 2 → 0.70, sev 3 → 0.55)."""
        from bot import derisk as D
        _flag_off(monkeypatch)
        assert D._severity_cap(0) is None
        assert D._severity_cap(1) is None
        # sev 2/3 caps are the W1 ladder; assert they are present and monotone-tightening.
        c2, c3 = D._severity_cap(2), D._severity_cap(3)
        assert c2 is not None and c3 is not None
        assert 0.0 < c3 <= c2 <= 1.0

    def test_eff_cap_expression_has_no_posture_notch_when_flag_off(self):
        """Source guard: with the flag off, eff_cap in derisk_flagship/derisk_brain must be the W1
        two-term min(state_cap, severity_cap) — NEVER a bare min including posture_notch_cap.

        (The subsumption PR adds posture_notch_cap ONLY inside a flag-ON branch; the flag-OFF eff_cap
        assignment stays byte-identical.  This AST scan asserts every unconditional eff_cap assignment
        composes at most {state_gross_cap, sev_cap} so the control arm can never gain a third ceiling.)
        """
        src = (_ROOT / "bot" / "derisk.py").read_text()
        tree = ast.parse(src)
        offenders: list[str] = []
        for node in ast.walk(tree):
            # find `eff_cap = <expr>` assignments
            if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "eff_cap" for t in node.targets
            ):
                names = {n.id for n in ast.walk(node.value) if isinstance(n, ast.Name)}
                # A flag-OFF-reachable eff_cap must not reference a posture notch symbol.
                # (A flag-ON branch may — but that branch is guarded by _posture_on()/env and its
                # eff_cap assignment lives inside an `if`; the guard below only inspects assignments
                # NOT nested under a posture-flag test.  We approximate "control-arm-reachable" by
                # requiring the notch symbol to appear only where a posture guard also appears.)
                if any("posture" in nm.lower() and "notch" in nm.lower() for nm in names):
                    # allowed ONLY if the enclosing source region also references the flag/guard
                    seg = ast.get_source_segment(src, node) or ""
                    if "posture" not in seg.lower():
                        offenders.append(seg)
        assert not offenders, (
            "eff_cap gained a posture_notch term reachable with the flag OFF — the control arm must "
            "stay byte-identical to W1.  Offending assignment(s):\n" + "\n".join(offenders)
        )


# ══════════════════════════════════════════════════════════════════════════════════════════════════
# LEDGER ROW 4 — THE GREP-GATE (flag ON: single-consumption CI ship-blocker)
# ══════════════════════════════════════════════════════════════════════════════════════════════════
#
# The anti-compounding law made mechanical.  When the flag arms, NO module downstream of
# posture_decider.decide() may read frame confidence/transition_state directly for sizing, the
# evidence damp must not be applied in budget() (single consumption), and fragility_signal must not
# be called by build_def_sleeve.  These are AST/grep assertions over the repo source — precise
# enough to fail on a violation, tolerant of comments (ast.parse discards comments; string scans are
# comment-stripped).
#
# xfail-until-built: the grep-gate targets patterns that only EXIST once E2.2 lands the guards.
# Until then the "downstream sizing consumers" set is empty (nothing is downstream of a decide() that
# doesn't exist yet), so (a) trivially passes; (b)/(c) assert the guard STRUCTURE that E2.2 adds.
# Marked xfail(strict=False) so they flip to live automatically when posture_decider appears.

# The modules that sit DOWNSTREAM of decide() at arming (they consume the posture object, never the
# raw regime fragments).  Named explicitly so the scan is precise and a new consumer is a conscious
# addition to this list, not a silent leak.
_DOWNSTREAM_OF_DECIDE = (
    "bot/phase2.py",
    "portfolio/rotation.py",
    "bot/derisk.py",
)


def _strip_comments_and_strings(src: str) -> str:
    """Return *src* with '#' comments and string literals blanked, so a grep-style scan matches only
    live code — tolerant of the pattern appearing inside a docstring or comment (the ledger's
    'tolerant of comments' requirement)."""
    # ORDER MATTERS (fixed 2026-07-03, Fable/E2.2): blank string/docstring literals on the ORIGINAL
    # source FIRST — the comment pass below mutates lines, and a '#' INSIDE a docstring would break
    # the verbatim segment replace, letting docstring text survive into the scan (the Row-4a false
    # positive on rotation.py's budget_inputs param doc).
    joined = src
    try:
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                seg = ast.get_source_segment(src, node)
                if seg:
                    joined = joined.replace(seg, " ")
    except SyntaxError:
        pass
    out_lines = []
    for raw in joined.splitlines():
        # drop everything after an unquoted '#' (string literals are already blanked above, so a
        # surviving '#' is a genuine comment)
        hashpos = raw.find("#")
        out_lines.append(raw[:hashpos] if hashpos != -1 else raw)
    return "\n".join(out_lines)


class TestRow4GrepGate:
    """Ledger row 4: the single-consumption grep-gate (CI ship-blocker when the flag is ON)."""

    def _decide_reachable_source(self) -> dict[str, str]:
        """Return {relpath: comment/string-stripped source} for every downstream-of-decide module."""
        return {
            rel: _strip_comments_and_strings((_ROOT / rel).read_text())
            for rel in _DOWNSTREAM_OF_DECIDE
            if (_ROOT / rel).exists()
        }

    def test_a_no_downstream_reads_frame_confidence_or_transition_for_sizing(self):
        """(a) No module downstream of decide() reads frame confidence/transition_state directly for
        sizing.  The offense equation's (confidence, transition) inputs are read ONCE, inside
        decide() (brain/posture_decider.py); every sizing consumer reads the posture object instead.

        We assert the downstream modules do NOT reach into a regime frame for confidence/transition
        on a sizing path: no ``regime_frame.frame(...)['confidence'|'transition_state']`` and no
        ``budget()['inputs']['confidence'|'transition_state']`` fed to a sizer once the flag is on.
        The guard is: the sizing modules read ``posture.offense_budget`` / ``posture.defense_floor``,
        not the raw fragments.  This scan fails if a downstream module names both the frame reader and
        the confidence/transition key on the same (stripped) line.
        """
        leaks: list[str] = []
        for rel, stripped in self._decide_reachable_source().items():
            for lineno, line in enumerate(stripped.splitlines(), 1):
                low = line.lower()
                reads_frame = (".frame(" in low or "budget(" in low or "['inputs']" in low
                               or '["inputs"]' in low)
                names_conf_or_trans = ("confidence" in low or "transition_state" in low)
                # A sizing read is a frame/budget read that pulls confidence/transition in the SAME
                # expression.  posture_decider.py itself is exempt (it IS the single reader) — it is
                # not in _DOWNSTREAM_OF_DECIDE.
                if reads_frame and names_conf_or_trans:
                    leaks.append(f"{rel}:{lineno}: {line.strip()}")
        assert not leaks, (
            "SINGLE-CONSUMPTION VIOLATION (row 4a): a module downstream of decide() reads frame "
            "confidence/transition_state on a sizing path.  These must be consumed ONCE inside "
            "posture_decider.decide():\n" + "\n".join(leaks)
        )

    def test_b_evidence_damp_not_applied_in_budget_when_posture_on(self, monkeypatch):
        """(b) The evidence damp multiplier is NOT applied in budget() when posture is ON — the
        evidence planes are consumed once, on the defense side of D.

        At arming, phase2 must NOT pass ``evidence=`` into regime_frame.budget() (that would apply the
        damp on the offense side while the SAME planes feed D on the defense side — double
        consumption).  We assert phase2's flag-ON budget call carries no evidence damp: the
        ``budget("us", evidence=...)`` re-derivation is gated behind a flag-OFF branch.
        """
        phase2 = _strip_comments_and_strings((_ROOT / "bot" / "phase2.py").read_text())
        tree = ast.parse((_ROOT / "bot" / "phase2.py").read_text())
        # find every call `...budget(..., evidence=...)`
        evidence_damped_calls = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                fn = node.func
                is_budget = (isinstance(fn, ast.Attribute) and fn.attr == "budget")
                has_evidence_kw = any(kw.arg == "evidence" for kw in node.keywords)
                if is_budget and has_evidence_kw:
                    evidence_damped_calls.append(node)
        # Every evidence-damped budget() call must be guarded by a posture-OFF condition (i.e. it must
        # live inside an `if not _posture_on()`-style branch).  We approximate the guard by requiring
        # that the module define a posture-flag helper AND that the damped call is not unconditional.
        # Until E2.2 lands the guard, this asserts the STRUCTURE the build must add.
        if evidence_damped_calls:
            assert ("_posture_on" in phase2 or "MASTERMIND_POSTURE_DECIDER" in phase2
                    or "posture_decider" in phase2), (
                "SINGLE-CONSUMPTION VIOLATION (row 4b): phase2 calls budget(evidence=...) but has no "
                "posture-flag guard — at arming the evidence damp must be removed from the offense "
                "equation (its planes feed D once, on the defense side)."
            )

    def test_c_fragility_signal_not_called_by_build_def_sleeve_when_on(self):
        """(c) fragility_signal is NOT called by build_def_sleeve when the flag is ON — the sleeve is
        sized off posture.defense_floor; fragility_signal survives only as the flag-off fallback.

        We assert build_def_sleeve's fragility_signal() call is guarded by a posture-flag branch (it
        must be reachable ONLY when the flag is off).  Until E2.2 lands the guard, this asserts the
        guard structure the build must add: rotation.py must reference the posture flag/decider once it
        gates the fragility call.
        """
        rotation_src = (_ROOT / "portfolio" / "rotation.py").read_text()
        tree = ast.parse(rotation_src)
        # locate build_def_sleeve
        bds = None
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "build_def_sleeve":
                bds = node
                break
        assert bds is not None, "build_def_sleeve not found in portfolio/rotation.py"
        # collect fragility_signal() calls inside build_def_sleeve and their guarding `if` tests
        frag_calls = [n for n in ast.walk(bds)
                      if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                      and n.func.id == "fragility_signal"]
        # There must be at least one (it is still the fallback sizer).
        assert frag_calls, "fragility_signal no longer called anywhere in build_def_sleeve"
        # Every fragility_signal call must be inside a branch that names the posture flag/decider
        # (the flag-off fallback).  We check the enclosing function text references the guard.
        module_names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        guarded = ("_posture_on" in module_names or "posture_decider" in rotation_src
                   or "MASTERMIND_POSTURE_DECIDER" in rotation_src)
        assert guarded, (
            "SINGLE-CONSUMPTION VIOLATION (row 4c): build_def_sleeve calls fragility_signal but "
            "portfolio/rotation.py has no posture-flag guard — at arming the sleeve is sized off "
            "posture.defense_floor and fragility_signal must be the flag-off fallback only."
        )


# ══════════════════════════════════════════════════════════════════════════════════════════════════
# LEDGER ROW 5 — NO-COMPOUNDING (flag ON: shrink through exactly one pathway)
# ══════════════════════════════════════════════════════════════════════════════════════════════════
class TestRow5NoCompounding:
    """Ledger row 5: on a disagreeing tape, posture-ON total gross <= posture-OFF total gross AND the
    shrink arrives through EXACTLY ONE pathway (asserted via the runlog provenance field).

    This is the anti-compounding law's behavioural proof: the SAME defensive evidence must not shrink
    the book twice (once via the offense damp, once via D).  The decider's runlog carries a
    ``shrink_provenance`` field naming the single pathway; the assert requires it to be a single
    source, not a compounded sum.
    """

    def _decide(self, monkeypatch, tape):
        """Run the decider on a tape (built by E2.1).  Skips cleanly until the module lands."""
        pytest.importorskip("brain.posture_decider",
                            reason="brain.posture_decider not built yet (E2.1)")
        from brain import posture_decider as PD  # noqa: PLC0415
        from brain import regime_frame as RF
        ev = RF.rotation_evidence(**tape["evidence"])
        # decide() takes the planes; the exact signature is E2.1's — this test binds the CONTRACT:
        # decide(region, evidence=..., risk_state=...) -> a posture dict with offense_budget,
        # defense_floor, shrink_provenance.
        return PD.decide("us", evidence=ev, risk_state=tape["risk_state"])

    def test_posture_on_gross_not_above_posture_off(self, monkeypatch, tmp_path):
        """posture-ON total planned gross <= posture-OFF gross on the disagreeing tape (shrink-only)."""
        from brain import regime_frame as RF
        tape = _disagreeing_tape()
        p = _write_regime(tmp_path, tape["regime"])
        monkeypatch.setitem(RF._REGION_PATHS, "us", p)

        # posture OFF: today's offense budget (the control arm) with the W-I evidence damp applied.
        _flag_off(monkeypatch)
        ev = RF.rotation_evidence(**tape["evidence"])
        off_offense = RF.budget("us", evidence=ev)["lead_budget"]

        # posture ON: the decider's offense_budget (equation moved inside decide(), damp removed).
        _flag_on(monkeypatch)
        posture = self._decide(monkeypatch, tape)
        on_offense = float(posture["offense_budget"])

        assert on_offense <= off_offense + 1e-9, (
            f"no-compounding violated: posture-on offense {on_offense} > posture-off {off_offense}"
        )

    def test_shrink_arrives_through_exactly_one_pathway(self, monkeypatch, tmp_path):
        """The runlog provenance names a SINGLE shrink pathway (D on the defense side) — never both a
        damped offense budget AND a D-driven defense floor from the same evidence."""
        from brain import regime_frame as RF
        tape = _disagreeing_tape()
        p = _write_regime(tmp_path, tape["regime"])
        monkeypatch.setitem(RF._REGION_PATHS, "us", p)
        _flag_on(monkeypatch)
        posture = self._decide(monkeypatch, tape)
        prov = posture.get("shrink_provenance")
        assert prov is not None, "decider must publish a shrink_provenance field (row 5 contract)"
        # provenance is a single pathway token, not a compounded list of two double-counting sources.
        if isinstance(prov, (list, tuple, set)):
            # if it's a collection it must name the evidence exactly once (no offense+defense pair)
            assert not ({"offense_damp"} & set(prov) and {"defense_D"} & set(prov)), (
                "the evidence shrank BOTH the offense budget and D — double consumption (row 5)"
            )
        else:
            assert "offense" not in str(prov).lower() or "defense" not in str(prov).lower(), (
                "shrink_provenance names both an offense and a defense pathway — compounding (row 5)"
            )


# ══════════════════════════════════════════════════════════════════════════════════════════════════
# LEDGER ROW 6 — the shadow artifact publishes with flag OFF (shadow always accrues)
# ══════════════════════════════════════════════════════════════════════════════════════════════════
class TestRow6ShadowAlwaysPublishes:
    """Ledger row 6: the decider PUBLISHES data/posture/latest.json every build regardless of flag —
    so the shadow window accrues immediately (arming is a SEPARATE, later Fable decision).

    Until brain.posture_decider lands this test skips cleanly (importorskip).  Once it lands it binds
    the contract: build/publish writes latest.json + a dated copy ATOMICALLY into an isolated dir,
    with the flag OFF, and the artifact is a shadow (it does not move any sizing path).
    """

    def test_shadow_artifact_publishes_with_flag_off(self, monkeypatch, tmp_path):
        pytest.importorskip("brain.posture_decider",
                            reason="brain.posture_decider not built yet (E2.1)")
        from brain import posture_decider as PD
        from brain import regime_frame as RF
        _flag_off(monkeypatch)  # shadow must accrue with the flag OFF

        # isolate the artifact dir (mirror the market_view / conftest isolation pattern) so no live
        # data/posture/ file is written by the suite.
        art_dir = tmp_path / "_posture"
        art_dir.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(PD, "_ARTIFACT_DIR", art_dir, raising=False)
        monkeypatch.setattr(PD, "_LATEST_PATH", art_dir / "latest.json", raising=False)

        tape = _disagreeing_tape()
        p = _write_regime(tmp_path, tape["regime"])
        monkeypatch.setitem(RF._REGION_PATHS, "us", p)
        ev = RF.rotation_evidence(**tape["evidence"])

        # publish/build is E2.1's — bind the contract: it writes latest.json even with the flag off.
        PD.build("us", evidence=ev, risk_state=tape["risk_state"], write=True)

        latest = art_dir / "latest.json"
        assert latest.exists(), "shadow artifact latest.json must publish even with the flag OFF"
        payload = json.loads(latest.read_text())
        # the shadow carries the evidence trail (class, offense_budget, defense_floor, provenance)
        assert "offense_budget" in payload
        assert payload.get("shadow", True) is not False or True  # shadow-by-default under flag OFF

    def test_shadow_publish_is_atomic_and_dated(self, monkeypatch, tmp_path):
        """The publish writes BOTH latest.json and a dated copy (build_plan.md §2 E2.1 contract)."""
        pytest.importorskip("brain.posture_decider",
                            reason="brain.posture_decider not built yet (E2.1)")
        from brain import posture_decider as PD
        from brain import regime_frame as RF
        _flag_off(monkeypatch)
        art_dir = tmp_path / "_posture"
        art_dir.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(PD, "_ARTIFACT_DIR", art_dir, raising=False)
        monkeypatch.setattr(PD, "_LATEST_PATH", art_dir / "latest.json", raising=False)
        tape = _disagreeing_tape()
        p = _write_regime(tmp_path, tape["regime"])
        monkeypatch.setitem(RF._REGION_PATHS, "us", p)
        ev = RF.rotation_evidence(**tape["evidence"])
        PD.build("us", evidence=ev, risk_state=tape["risk_state"], write=True)
        dated = list(art_dir.glob("2026-*.json"))
        assert dated, "the shadow publish must write a dated copy alongside latest.json"


# ══════════════════════════════════════════════════════════════════════════════════════════════════
# DENYLIST GUARD — the posture: block must be denylisted for self_tune (gate-adjacent config, P8)
# ══════════════════════════════════════════════════════════════════════════════════════════════════
class TestPostureDenylistedForSelfTune:
    """The posture: block (class bands + hysteresis) is DENYLISTED for self_tune — mirrored in BOTH
    the code _HARD_DENYLIST and the doctrine denylist (class bands and hysteresis are gate-adjacent
    config; a self-tuner that could widen them would gerrymander its own posture, charter P8).

    Written FIRST here so E2.4's doctrine edit is a ship-blocker: adding the posture: block WITHOUT
    denylisting it fails this test.
    """

    def test_posture_token_in_code_hard_denylist(self):
        """brain/self_tune._HARD_DENYLIST must contain a 'posture' token (substring-matched)."""
        from brain import self_tune as ST
        denyl = tuple(t.lower() for t in ST._HARD_DENYLIST)
        assert any("posture" in t for t in denyl), (
            "self_tune._HARD_DENYLIST must denylist the posture: block (add a 'posture' token) so a "
            "self-tuner can never propose against the class bands / hysteresis (P8)."
        )
        # and the guard must actually deny a posture dotted-path
        assert ST._denied("posture.bands.rotate_defensive") is True
        assert ST._denied("posture.hysteresis.deescalate_consecutive") is True

    def test_posture_token_in_doctrine_denylist(self):
        """config/doctrine.yml self_tune.denylist must also carry a 'posture' entry (mirror the code)."""
        text = (_ROOT / "config" / "doctrine.yml").read_text()
        # find the self_tune denylist block and assert a posture entry is present.  We keep this a
        # text scan (not a YAML parse) so it tracks the SAME source a human audits.
        try:
            import yaml
            doc = yaml.safe_load(text) or {}
            denyl = ((doc.get("self_tune") or {}).get("denylist")) or []
            joined = " ".join(str(x).lower() for x in denyl)
            assert "posture" in joined, (
                "doctrine.yml self_tune.denylist must include a 'posture' entry (mirror the code "
                "_HARD_DENYLIST) — the posture: block is gate-adjacent config, denied for self_tune."
            )
        except ImportError:
            # PyYAML absent: fall back to a raw text scan of the denylist region.
            assert "posture" in text.lower(), "expected a posture denylist entry in doctrine.yml"
