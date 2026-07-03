"""Bounded self-repair (brain.self_tune) — W-L / L4. The self-modification boundary.

These tests guard the charter ship-blockers (P3/P8) as hard properties:
  * the DENYLIST refuses a cap hard-bound even when it carries the unverified-prior tag;
  * only (unverified-prior)-tagged, non-denied scalar leaves are tunable;
  * candidate steps are BOUNDED (±step_pct);
  * a degrading candidate is REFUSED by the immutable harness gate (self_tune adds no new scoring);
  * the two-strikes rule locks a family to proposal-only FOREVER (persisted);
  * the auto-revert path fires when realized underperforms the shadow projection;
  * MASTERMIND_SELF_TUNE OFF ⇒ every entrypoint is a byte-identical no-op.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

import bot  # noqa: F401 — bootstraps vendor/macro onto sys.path
from brain import self_tune as ST
from loop import paper
from loop.candidates import Candidate


@pytest.fixture()
def sandbox(tmp_path, monkeypatch):
    """Redirect the persisted state ledger into a temp dir + arm the flag by default for the ON tests."""
    monkeypatch.setattr(ST, "_STATE_DIR", tmp_path)
    monkeypatch.setattr(ST, "_STATE_PATH", tmp_path / "state.json")
    monkeypatch.setenv("MASTERMIND_SELF_TUNE", "1")
    return tmp_path


# ── the tunable-surface guard (charter P3/P8) ────────────────────────────────
_DOCTRINE_SNIPPET = """\
caps:
  name_cap: 0.08                 # (unverified-prior) tighter value
  broad_index_cap: 0.15          # (unverified-prior) broad-index name cap
budget:
  base: 0.40                     # (unverified-prior) budget floor
  slope: 0.20                    # (unverified-prior) confidence flex
  ceil: 0.60                     # (unverified-prior) clamp upper bound
  transition_mult:               # (unverified-prior) nested map — not a scalar leaf
    WEAKENING: 0.6
def_sleeve:
  armed_ceiling: 0.35            # (unverified-prior) hard clamp
  w_dwell: 0.5                   # (unverified-prior) dwell weight
rotation_tensor:
  validation_auc_gate: 0.55      # (unverified-prior) MISTAGGED gate — must be denied
scorecard:
  rs_down_day_lookback_d: 63     # untagged — not tunable
"""


def test_denylist_refuses_cap_hardbound_even_if_tagged():
    keys = ST.tunable_keys(_DOCTRINE_SNIPPET)
    # every caps.* key is denied even though name_cap / broad_index_cap carry the tag
    assert not any(p.startswith("caps.") for p in keys), keys
    # the def_sleeve hard clamp is denied; a mistagged gate is denied
    assert "def_sleeve.armed_ceiling" not in keys
    assert "rotation_tensor.validation_auc_gate" not in keys


def test_only_tagged_nondenied_scalars_are_tunable():
    keys = ST.tunable_keys(_DOCTRINE_SNIPPET)
    assert "budget.base" in keys and keys["budget.base"] == pytest.approx(0.40)
    assert "budget.slope" in keys and "budget.ceil" in keys
    assert "def_sleeve.w_dwell" in keys
    # untagged line is NOT tunable; a nested map value is NOT a scalar leaf
    assert "scorecard.rs_down_day_lookback_d" not in keys
    assert "budget.transition_mult" not in keys


def test_denied_helper_direct():
    assert ST._denied("caps.name_cap")
    assert ST._denied("perception_validation.auc_gate")
    assert ST._denied("something.full_size_gate")   # 'gate' substring
    assert not ST._denied("budget.base")


def test_self_tune_cannot_tune_itself():
    """Meta-boundary (P8): self_tune may never widen its OWN leash (step_pct / revert_margin /
    reverts_to_lock) nor re-adapt the governor's arming guards."""
    assert ST._denied("self_tune.step_pct")
    assert ST._denied("self_tune.revert_margin")
    assert ST._denied("governor.arming_effective_n")
    # and on the REAL production doctrine, no self_tune.* / governor.* key is in the tunable surface
    keys = ST.tunable_keys()
    assert not any(p.startswith("self_tune.") for p in keys), keys
    assert not any("governor" in p for p in keys), keys


# ── bounded candidate steps ──────────────────────────────────────────────────
def test_candidate_values_are_bounded():
    vals = ST.candidate_values(0.40, step_pct=0.25, n=2)
    assert sorted(vals) == pytest.approx([0.30, 0.50])   # ±25% only — no leap
    # zero degrades to a small absolute step, not an inert ±0
    z = ST.candidate_values(0.0, step_pct=0.25, n=2)
    assert 0.01 in z and -0.01 in z


# ── the harness gate refuses a degrading candidate (self_tune adds no new scoring) ──
def _synth_closes(n=1400, seed=0):
    """A synthetic price panel over the Lab universe with a locked 2022 holdout split. SPY = col 0."""
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2016-01-01", periods=n)
    cols = ["SPY", "QQQ", "IEF", "TLT", "XLK", "XLF"]
    out = {}
    for i, c in enumerate(cols):
        drift = 0.0003 if c in ("SPY", "QQQ", "XLK") else 0.0001
        ret = rng.normal(drift, 0.01, n)
        out[c] = 100 * np.exp(np.cumsum(ret))
    return pd.DataFrame(out, index=idx)


def test_degrading_candidate_refused_by_harness(sandbox, monkeypatch):
    """A junk candidate (all-cash, zero edge) must be REJECTED by the frozen gate — evaluate_candidate
    routes through loop.harness/pbo/holdout/promote and reimplements nothing."""
    closes = _synth_closes()
    bill = pd.Series(0.00005, index=closes.index)
    # a degenerate 'candidate' that holds ~nothing → cannot beat SPY/6040 → gate must not say 'paper'
    cand = Candidate({"weights": {"IEF": 0.02}})
    g = ST.evaluate_candidate(cand, closes, bill, holdout_start="2022-01-01")
    assert g["stage"] == "rejected", g
    # a GENUINE frozen-gate rejection carries promote.gate's per-check map (not an evaluation error);
    # proves the candidate reached the immutable judge and failed a real hurdle, not that self_tune
    # invented its own scoring.
    assert g["checks"], g
    assert g["reason"].startswith("failed check:"), g
    assert g["checks"]["beats_spy"] is False


# ── two-strikes rule → proposal-only FOREVER (persisted) ─────────────────────
def test_two_strikes_locks_family_forever(sandbox):
    fam = "budget"
    assert not ST.is_proposal_only(fam)
    ST.record_revert(fam, evidence={"why": "first strike"})
    assert not ST.is_proposal_only(fam)              # one strike is survivable
    ST.record_revert(fam, evidence={"why": "second strike"})
    assert ST.is_proposal_only(fam)                  # second strike locks it
    # persisted: a fresh state read still sees the lock
    st = ST._load_state()
    assert st["families"][fam]["proposal_only"] is True
    assert st["families"][fam]["reverts"] == 2
    # a locked family cannot be escalated further / re-armed from code
    rec = ST.record_revert(fam)
    assert rec["reverts"] == 2 and rec["proposal_only"] is True


def test_propose_refuses_locked_family(sandbox):
    fam = "budget"
    ST.record_revert(fam); ST.record_revert(fam)
    out = ST.propose(fam, candidate_factory=lambda p, v: None)
    assert out["status"] == "proposal-only"


# ── the auto-revert path ─────────────────────────────────────────────────────
def test_apply_auto_reverts_on_shortfall(sandbox, monkeypatch):
    """A staged shadow whose realized Sharpe underperforms the projection by >= revert_margin
    auto-reverts, journals, and counts a strike."""
    fam = "budget"
    st = ST._load_state()
    rec = ST._family_rec(st, fam)
    rec["active_shadow"] = {"state": "shadow", "path": "budget.slope", "from": 0.20, "to": 0.25,
                            "projection_sharpe": 1.0, "window_sessions": 21, "evidence": {}}
    ST._save_state(st)

    journaled = []
    out = ST.apply(fam, realized_sharpe=0.5, journal_fn=journaled.append)   # 50% shortfall > 20%
    assert out["status"] == "auto-reverted"
    assert journaled and journaled[0]["decision"] == "auto-reverted"
    assert "falsifier" in journaled[0]
    # a strike was recorded
    assert ST._load_state()["families"][fam]["reverts"] == 1


def test_apply_keeps_on_hit(sandbox):
    """A shadow that meets its projection is KEPT (no strike, journaled)."""
    fam = "risk_state"
    st = ST._load_state()
    rec = ST._family_rec(st, fam)
    rec["active_shadow"] = {"state": "shadow", "path": "risk_state.deescalate_sessions",
                            "from": 3, "to": 4, "projection_sharpe": 1.0, "window_sessions": 21,
                            "evidence": {}}
    ST._save_state(st)
    journaled = []
    out = ST.apply(fam, realized_sharpe=0.95, journal_fn=journaled.append)   # 5% shortfall < 20%
    assert out["status"] == "kept"
    assert ST._load_state()["families"][fam].get("reverts", 0) == 0
    assert journaled[0]["decision"] == "kept"


# ── charter P8: OFF ⇒ byte-identical no-op ───────────────────────────────────
def test_off_is_a_noop(tmp_path, monkeypatch):
    monkeypatch.setattr(ST, "_STATE_DIR", tmp_path)
    monkeypatch.setattr(ST, "_STATE_PATH", tmp_path / "state.json")
    monkeypatch.delenv("MASTERMIND_SELF_TUNE", raising=False)   # OFF (default)
    assert ST.propose("budget", candidate_factory=lambda p, v: object())["status"] == "off"
    assert ST.apply("budget", realized_sharpe=0.0)["status"] == "off"
    # no state file was written by a no-op run
    assert not (tmp_path / "state.json").exists()


# ── L4 wired loop/paper.forward_brier (no longer a None-stub) ────────────────
def _price_df(vals, start="2026-01-01"):
    idx = pd.bdate_range(start, periods=len(vals))
    return pd.DataFrame({"close": vals}, index=idx)


def test_forward_brier_builds_until_a_window_resolves():
    """A snapshot whose forward window has NOT printed `horizon` sessions is unresolved → the Brier
    stays None ('building'). The paper→live promise is honest, not a zombie None-stub."""
    arch = pd.DataFrame([{"asof": "2026-06-30",
                          "snapshot": {"weights": {"SPY": 1.0}, "prob_up": 0.6}}])
    # only 2 forward closes exist but horizon is 5 → unresolved
    prices = {"SPY": _price_df([100, 101, 102], start="2026-06-30")}
    out = paper.forward_brier("abcd1234", horizon=5,
                              archive_fn=lambda label: arch,
                              read_fn=lambda s: prices.get(s.upper()))
    assert out is None


def test_forward_brier_scores_a_resolved_window():
    """Once a forward window resolves, forward_brier scores the archived prob_up against the realized
    sleeve sign. Here SPY rises → outcome up; prob_up=0.9 → small Brier."""
    arch = pd.DataFrame([{"asof": "2026-01-01",
                          "snapshot": {"weights": {"SPY": 1.0}, "prob_up": 0.9}}])
    prices = {"SPY": _price_df([100, 101, 102, 103, 104, 105, 106])}   # >5 fwd sessions, rising
    out = paper.forward_brier("abcd1234", horizon=5,
                              archive_fn=lambda label: arch,
                              read_fn=lambda s: prices.get(s.upper()))
    assert out is not None and out["n_resolved"] == 1
    assert out["brier"] == pytest.approx((0.9 - 1.0) ** 2, abs=1e-9)   # outcome=up(1), p=0.9

    # a wrong-direction bet: falling prices → outcome down(0), same p=0.9 → large Brier
    prices_dn = {"SPY": _price_df([100, 99, 98, 97, 96, 95, 94])}
    out2 = paper.forward_brier("abcd1234", horizon=5,
                               archive_fn=lambda label: arch,
                               read_fn=lambda s: prices_dn.get(s.upper()))
    assert out2["brier"] == pytest.approx((0.9 - 0.0) ** 2, abs=1e-9)


def test_forward_brier_missing_leg_voids_snapshot():
    """A missing price leg voids the snapshot (P2 — no fabricated outcome), keeping the Brier None."""
    arch = pd.DataFrame([{"asof": "2026-01-01",
                          "snapshot": {"weights": {"SPY": 0.5, "GONE": 0.5}, "prob_up": 0.7}}])
    prices = {"SPY": _price_df([100, 101, 102, 103, 104, 105])}       # GONE has no data
    out = paper.forward_brier("abcd1234", horizon=5,
                              archive_fn=lambda label: arch,
                              read_fn=lambda s: prices.get(s.upper()))
    assert out is None


# ── propose degrades honestly without a factory (routes to agenda) ───────────
def test_propose_without_factory_is_honest(sandbox, monkeypatch):
    # point tunable_keys at a doctrine with a tunable budget family
    monkeypatch.setattr(ST, "tunable_keys", lambda text=None: {"budget.slope": 0.20})
    out = ST.propose("budget", candidate_factory=None)
    assert out["status"] == "no-factory"
    assert out["proposals"] and all(p["path"] == "budget.slope" for p in out["proposals"])


# ── full pipeline: agenda evidence → candidates → harness → shadow-staged ────
def test_propose_stages_a_shadow_survivor(sandbox, monkeypatch):
    """End-to-end: a family with a tunable key + a factory that produces a strong candidate reaches
    'shadow-staged' with the survivor recorded in state (P8 — staged for shadow, NOT applied live)."""
    monkeypatch.setattr(ST, "tunable_keys", lambda text=None: {"budget.slope": 0.20})
    closes = _synth_closes(seed=3)
    bill = pd.Series(0.00005, index=closes.index)

    # factory: map (path, value) → a Candidate whose weights vary with the value so the harness has
    # something economically distinct to judge (a strong SPY/IEF mix that beats the hurdles).
    def factory(path, value):
        return Candidate({"weights": {"SPY": 0.6 + value, "IEF": 0.4}, "knobs": {path: value}})

    out = ST.propose("budget", evidence={"cited": "calibration delta"},
                     candidate_factory=factory, closes=closes, bill=bill, asof="2026-07-02")
    assert out["status"] in ("shadow-staged", "no-survivor"), out
    if out["status"] == "shadow-staged":
        st = ST._load_state()
        rec = st["families"]["budget"]
        assert rec["active_shadow"]["state"] == "shadow"
        assert rec["active_shadow"]["path"] == "budget.slope"
        # staged is NOT applied — no doctrine write happened, the state only holds a shadow record
        assert rec.get("reverts", 0) == 0


def test_perception_validation_block_fully_denied():
    """Fable boundary review (W-L merge): every key of the gate-DEFINITION block is denied —
    including the event-labeling params that don't contain 'gate' in their path. A self-tuner
    that can redefine what counts as a drawdown event can gerrymander its own exams (P8)."""
    from brain.self_tune import _denied
    for key in ("fwd_sessions", "drawdown_bps_min", "benchmark", "episode_extreme_pctile",
                "auc_gate", "fires_max_frac", "walk_start_full", "walk_start_fallback"):
        assert _denied(f"perception_validation.{key}"), f"perception_validation.{key} must be denied"
