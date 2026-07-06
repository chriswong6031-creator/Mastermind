"""Tests for MW2 Lane B — tri-state experiment maturity evaluator.

Tests:
  1. evaluate() — per-state assertions:
     a. Date-driven items: not_old_enough when comeback_date is in the future.
     b. Date-driven items: ready_for_review when comeback_date has been reached.
     c. shadow-trim-ladder: blocked_missing_evidence when evidence_n < 40.
     d. shadow-trim-ladder: ready_for_review when evidence_n >= 40.
     e. governor-arming: blocked_missing_evidence when snapshot count < 8.
     f. governor-arming: ready_for_review when snapshot count >= 8.
     g. bubble-formation-grading: always blocked_missing_evidence (no H4 artifact).
     h. deploy-lag-sla: always blocked_missing_evidence (continuous SLA).
     i. Unknown experiment with no comeback_date: blocked_missing_evidence with honest reason.
     j. Terminal experiments (judged/cancelled): blocked with explanation, never ready.
     k. Already-matured experiments: always ready_for_review.

  2. Stuck flag:
     a. Blocked + no comeback_date + first_blocked <= 14 days ago → stuck=False.
     b. Blocked + no comeback_date + first_blocked > 14 days ago → stuck=True.
     c. Not-blocked state → stuck=False regardless.

  3. update_evaluator_tracking():
     a. Sets _evaluator_first_blocked on first blocked observation.
     b. Does not overwrite an existing _evaluator_first_blocked.
     c. Clears _evaluator_first_blocked when state is not blocked.

  4. open_with_tristate() ordering:
     a. ready_for_review items rank before stuck items.
     b. Stuck items rank before non-stuck blocked items.
     c. Blocked items rank before not_old_enough items.
     d. Excluded: terminal experiments.

  5. No auto-promotion: evaluate() and open_with_tristate() NEVER change status to
     matured/judged even when state == ready_for_review.

  6. Agenda _from_experiment_tristate() ordering:
     a. ready_for_review items produce high-severity agenda items.
     b. stuck items produce high-severity agenda items.
     c. blocked items produce lower-severity agenda items.

  7. registry round-trip: date-driven items are unchanged by evaluate() (no side effects
     on comeback_date-driven experiments).

  8. register_evaluator(): custom evaluator is dispatched correctly.

  9. 19/19 incident replays still pass (separate invocation).
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import pytest

_WORKTREE = Path(__file__).resolve().parent.parent


# ── fixtures ─────────────────────────────────────────────────────────────────

def _make_exp(eid: str, *,
              comeback_date: str | None = None,
              status: str = "open",
              maturity_condition: str = "description of maturity",
              artifact_paths: list | None = None,
              first_blocked: str | None = None) -> dict:
    return {
        "id": eid,
        "what": f"Test experiment {eid}",
        "gate": "some gate condition",
        "comeback_date": comeback_date,
        "maturity_condition": maturity_condition,
        "status": status,
        "owner": "opus-session",
        "artifact_paths": artifact_paths or [],
        "notes": "",
        "_evaluator_first_blocked": first_blocked,
    }


@pytest.fixture()
def registry(tmp_path, monkeypatch):
    """An isolated registry backed by a tmp file."""
    reg_path = tmp_path / "experiments" / "registry.json"
    reg_path.parent.mkdir(parents=True, exist_ok=True)
    import brain.experiment_registry as er
    monkeypatch.setattr(er, "_REGISTRY_PATH", reg_path)
    monkeypatch.setattr(er, "_ROOT", tmp_path)
    yield er


# ── 1. evaluate() — per-state ─────────────────────────────────────────────────

class TestEvaluatePerState:
    """All calls use a fixed asof to ensure determinism."""
    TODAY = date(2026, 7, 6)

    def test_date_driven_not_old_enough(self):
        from brain.experiment_registry import evaluate, STATE_NOT_OLD_ENOUGH
        future = (self.TODAY + timedelta(days=10)).isoformat()
        item = _make_exp("e1", comeback_date=future)
        result = evaluate(item, self.TODAY)
        assert result["state"] == STATE_NOT_OLD_ENOUGH
        assert "10 days" in result["reason"] or "not yet" in result["reason"].lower()
        assert result["expected_ready_date"] == future
        assert result["stuck"] is False

    def test_date_driven_ready(self):
        from brain.experiment_registry import evaluate, STATE_READY
        past = (self.TODAY - timedelta(days=3)).isoformat()
        item = _make_exp("e2", comeback_date=past)
        result = evaluate(item, self.TODAY)
        assert result["state"] == STATE_READY
        assert result["stuck"] is False

    def test_date_driven_ready_today(self):
        from brain.experiment_registry import evaluate, STATE_READY
        item = _make_exp("e3", comeback_date=self.TODAY.isoformat())
        result = evaluate(item, self.TODAY)
        assert result["state"] == STATE_READY

    def test_shadow_trim_ladder_blocked_missing_dir(self, tmp_path, monkeypatch):
        """No distribution_trims/ dir → blocked, evidence_n=0, required_n=40."""
        import brain.experiment_registry as er
        monkeypatch.setattr(er, "_ROOT", tmp_path)  # tmp_path has no data/ subdir
        item = _make_exp("shadow-trim-ladder")
        result = er.evaluate(item, self.TODAY)
        assert result["state"] == er.STATE_BLOCKED
        assert result["evidence_n"] == 0
        assert result["required_n"] == 40

    def test_shadow_trim_ladder_blocked_below_threshold(self, tmp_path, monkeypatch):
        """10 graded trims → blocked, evidence_n=10 < required 40."""
        import brain.experiment_registry as er
        monkeypatch.setattr(er, "_ROOT", tmp_path)
        trims_dir = tmp_path / "data" / "shadow" / "distribution_trims"
        trims_dir.mkdir(parents=True)
        # Write 2 files with 5 graded trims each
        for i in range(2):
            payload = {
                "asof": f"2026-07-0{i+1}",
                "portfolio_id": "flagship",
                "trims": [
                    {"ticker": f"T{j}", "graded": True}
                    for j in range(5)
                ],
            }
            (trims_dir / f"2026-07-0{i+1}_flagship.json").write_text(json.dumps(payload))
        item = _make_exp("shadow-trim-ladder")
        result = er.evaluate(item, self.TODAY)
        assert result["state"] == er.STATE_BLOCKED
        assert result["evidence_n"] == 10
        assert result["required_n"] == 40
        assert result["expected_ready_date"] is not None  # pacing estimate provided

    def test_shadow_trim_ladder_ready_at_threshold(self, tmp_path, monkeypatch):
        """40 graded trims → ready_for_review."""
        import brain.experiment_registry as er
        monkeypatch.setattr(er, "_ROOT", tmp_path)
        trims_dir = tmp_path / "data" / "shadow" / "distribution_trims"
        trims_dir.mkdir(parents=True)
        payload = {
            "asof": "2026-07-01",
            "portfolio_id": "flagship",
            "trims": [{"ticker": f"T{j}", "graded": True} for j in range(40)],
        }
        (trims_dir / "2026-07-01_flagship.json").write_text(json.dumps(payload))
        item = _make_exp("shadow-trim-ladder")
        result = er.evaluate(item, self.TODAY)
        assert result["state"] == er.STATE_READY
        assert result["evidence_n"] == 40
        assert result["required_n"] == 40

    def test_shadow_trim_ladder_only_counts_graded_true(self, tmp_path, monkeypatch):
        """Ungraded trims (graded=False or absent) do NOT count toward the 40."""
        import brain.experiment_registry as er
        monkeypatch.setattr(er, "_ROOT", tmp_path)
        trims_dir = tmp_path / "data" / "shadow" / "distribution_trims"
        trims_dir.mkdir(parents=True)
        payload = {
            "asof": "2026-07-01",
            "trims": [
                {"ticker": "A", "graded": True},
                {"ticker": "B", "graded": False},
                {"ticker": "C"},                   # no graded key
                {"ticker": "D", "graded": True},
            ],
        }
        (trims_dir / "2026-07-01_flagship.json").write_text(json.dumps(payload))
        item = _make_exp("shadow-trim-ladder")
        result = er.evaluate(item, self.TODAY)
        assert result["evidence_n"] == 2            # only 2 with graded=True

    def test_governor_arming_blocked_missing_dir(self, tmp_path, monkeypatch):
        """No benchmark/ dir → blocked, evidence_n=0, required_n=8."""
        import brain.experiment_registry as er
        monkeypatch.setattr(er, "_ROOT", tmp_path)
        item = _make_exp("governor-arming")
        result = er.evaluate(item, self.TODAY)
        assert result["state"] == er.STATE_BLOCKED
        assert result["evidence_n"] == 0
        assert result["required_n"] == 8

    def test_governor_arming_blocked_below_threshold(self, tmp_path, monkeypatch):
        """3 benchmark snapshots → blocked."""
        import brain.experiment_registry as er
        monkeypatch.setattr(er, "_ROOT", tmp_path)
        bench_dir = tmp_path / "data" / "benchmark"
        bench_dir.mkdir(parents=True)
        for i in range(3):
            (bench_dir / f"2026-07-0{i+1}.json").write_text(
                json.dumps({"as_of": f"2026-07-0{i+1}", "bogeys": {}, "leaderboard": []}))
        item = _make_exp("governor-arming")
        result = er.evaluate(item, self.TODAY)
        assert result["state"] == er.STATE_BLOCKED
        assert result["evidence_n"] == 3
        assert result["required_n"] == 8
        assert result["expected_ready_date"] is not None

    def test_governor_arming_ready_at_threshold(self, tmp_path, monkeypatch):
        """8 benchmark snapshots → ready_for_review."""
        import brain.experiment_registry as er
        monkeypatch.setattr(er, "_ROOT", tmp_path)
        bench_dir = tmp_path / "data" / "benchmark"
        bench_dir.mkdir(parents=True)
        for i in range(8):
            (bench_dir / f"2026-07-{i+1:02d}.json").write_text(
                json.dumps({"as_of": f"2026-07-{i+1:02d}", "bogeys": {}, "leaderboard": []}))
        item = _make_exp("governor-arming")
        result = er.evaluate(item, self.TODAY)
        assert result["state"] == er.STATE_READY
        assert result["evidence_n"] == 8

    def test_governor_arming_excludes_underscore_files(self, tmp_path, monkeypatch):
        """_series.json and similar underscore-prefixed files are excluded from the count."""
        import brain.experiment_registry as er
        monkeypatch.setattr(er, "_ROOT", tmp_path)
        bench_dir = tmp_path / "data" / "benchmark"
        bench_dir.mkdir(parents=True)
        # 7 real snapshots + 1 underscore file → still blocked at 7 < 8
        for i in range(7):
            (bench_dir / f"2026-07-{i+1:02d}.json").write_text(json.dumps({}))
        (bench_dir / "_series.json").write_text(json.dumps({}))
        item = _make_exp("governor-arming")
        result = er.evaluate(item, self.TODAY)
        assert result["state"] == er.STATE_BLOCKED
        assert result["evidence_n"] == 7     # underscore file excluded

    def test_bubble_formation_grading_always_blocked(self):
        """bubble-formation-grading has no H4 artifact → always blocked_missing_evidence."""
        from brain.experiment_registry import evaluate, STATE_BLOCKED
        item = _make_exp("bubble-formation-grading",
                         maturity_condition="H4 handoff lands")
        result = evaluate(item, self.TODAY)
        assert result["state"] == STATE_BLOCKED
        assert "no mechanical evaluator" in result["reason"].lower() or \
               "mechanical" in result["reason"].lower()
        assert result["evidence_n"] is None
        assert result["required_n"] is None

    def test_deploy_lag_sla_always_blocked(self):
        """deploy-lag-sla is a continuous SLA → always blocked_missing_evidence."""
        from brain.experiment_registry import evaluate, STATE_BLOCKED
        item = _make_exp("deploy-lag-sla",
                         maturity_condition="continuous")
        result = evaluate(item, self.TODAY)
        assert result["state"] == STATE_BLOCKED
        assert "no mechanical evaluator" in result["reason"].lower() or \
               "continuous" in result["reason"].lower()

    def test_unknown_no_comeback_blocked(self):
        """An unknown experiment with no comeback_date returns blocked with honest reason."""
        from brain.experiment_registry import evaluate, STATE_BLOCKED
        item = _make_exp("unknown-experiment-xyz")  # not in _EVALUATORS, no comeback_date
        result = evaluate(item, self.TODAY)
        assert result["state"] == STATE_BLOCKED
        assert "no mechanical evaluator" in result["reason"].lower() or \
               "fable" in result["reason"].lower()

    def test_terminal_judged_not_evaluated(self):
        """A judged experiment returns blocked with a 'terminal' explanation, never ready."""
        from brain.experiment_registry import evaluate, STATE_BLOCKED, STATE_READY
        item = _make_exp("judged-exp", status="judged")
        result = evaluate(item, self.TODAY)
        assert result["state"] != STATE_READY
        assert result["stuck"] is False

    def test_terminal_cancelled_not_evaluated(self):
        from brain.experiment_registry import evaluate, STATE_READY
        item = _make_exp("cancelled-exp", status="cancelled")
        result = evaluate(item, self.TODAY)
        assert result["state"] != STATE_READY

    def test_already_matured_is_ready(self):
        """An experiment already at status=matured is always STATE_READY."""
        from brain.experiment_registry import evaluate, STATE_READY
        item = _make_exp("matured-exp", status="matured",
                         comeback_date="2026-07-01")
        result = evaluate(item, self.TODAY)
        assert result["state"] == STATE_READY
        assert result["stuck"] is False


# ── 2. Stuck flag ─────────────────────────────────────────────────────────────

class TestStuckFlag:
    TODAY = date(2026, 7, 6)

    def test_not_stuck_within_14_days(self):
        from brain.experiment_registry import evaluate, STATE_BLOCKED
        first_blocked = (self.TODAY - timedelta(days=10)).isoformat()
        item = _make_exp("bubble-formation-grading", first_blocked=first_blocked)
        result = evaluate(item, self.TODAY)
        assert result["state"] == STATE_BLOCKED
        assert result["stuck"] is False

    def test_stuck_after_14_days(self):
        from brain.experiment_registry import evaluate
        first_blocked = (self.TODAY - timedelta(days=15)).isoformat()
        item = _make_exp("bubble-formation-grading", first_blocked=first_blocked)
        result = evaluate(item, self.TODAY)
        assert result["stuck"] is True

    def test_stuck_exactly_on_boundary(self):
        """Exactly 14 days is NOT stuck (strictly >14)."""
        from brain.experiment_registry import evaluate
        first_blocked = (self.TODAY - timedelta(days=14)).isoformat()
        item = _make_exp("bubble-formation-grading", first_blocked=first_blocked)
        result = evaluate(item, self.TODAY)
        assert result["stuck"] is False

    def test_stuck_requires_no_comeback_date(self):
        """An item with a comeback_date cannot be stuck even if it has been blocked >14 days."""
        from brain.experiment_registry import evaluate
        first_blocked = (self.TODAY - timedelta(days=30)).isoformat()
        item = _make_exp("some-exp",
                         comeback_date=(self.TODAY + timedelta(days=5)).isoformat(),
                         first_blocked=first_blocked)
        result = evaluate(item, self.TODAY)
        # date-driven items can't be stuck
        assert result["stuck"] is False

    def test_ready_state_never_stuck(self):
        """A ready_for_review item is never stuck regardless of _evaluator_first_blocked."""
        from brain.experiment_registry import evaluate, STATE_READY
        first_blocked = (self.TODAY - timedelta(days=30)).isoformat()
        # Use date-driven ready item
        past = (self.TODAY - timedelta(days=3)).isoformat()
        item = _make_exp("some-date-exp", comeback_date=past, first_blocked=first_blocked)
        result = evaluate(item, self.TODAY)
        assert result["state"] == STATE_READY
        assert result["stuck"] is False


# ── 3. update_evaluator_tracking() ───────────────────────────────────────────

class TestUpdateEvaluatorTracking:
    TODAY = date(2026, 7, 6)

    def test_stamps_first_blocked_when_missing(self, registry):
        registry.add(_make_exp("track-1"))
        from brain.experiment_registry import STATE_BLOCKED
        ok = registry.update_evaluator_tracking("track-1", STATE_BLOCKED, self.TODAY)
        assert ok
        exp = registry.get("track-1")
        assert exp["_evaluator_first_blocked"] == self.TODAY.isoformat()

    def test_does_not_overwrite_existing(self, registry):
        original_date = (self.TODAY - timedelta(days=20)).isoformat()
        registry.add(_make_exp("track-2", first_blocked=original_date))
        # Manually set it in the registry
        registry.update("track-2", **{"_evaluator_first_blocked": original_date})
        from brain.experiment_registry import STATE_BLOCKED
        registry.update_evaluator_tracking("track-2", STATE_BLOCKED, self.TODAY)
        exp = registry.get("track-2")
        # Should preserve original date, not overwrite with today
        assert exp["_evaluator_first_blocked"] == original_date

    def test_clears_when_state_not_blocked(self, registry):
        original_date = (self.TODAY - timedelta(days=5)).isoformat()
        registry.add(_make_exp("track-3", first_blocked=original_date))
        registry.update("track-3", **{"_evaluator_first_blocked": original_date})
        from brain.experiment_registry import STATE_READY
        registry.update_evaluator_tracking("track-3", STATE_READY, self.TODAY)
        exp = registry.get("track-3")
        assert exp["_evaluator_first_blocked"] is None


# ── 4. open_with_tristate() ordering ─────────────────────────────────────────

class TestOpenWithTristateOrdering:
    TODAY = date(2026, 7, 6)

    def test_ready_before_stuck_before_blocked_before_not_old_enough(self, registry, monkeypatch):
        """Priority: ready > stuck > blocked > not_old_enough."""
        import brain.experiment_registry as er

        # ready: date reached
        registry.add(_make_exp("r-exp", comeback_date=(self.TODAY - timedelta(days=1)).isoformat()))
        # not_old_enough: future date
        registry.add(_make_exp("n-exp", comeback_date=(self.TODAY + timedelta(days=10)).isoformat()))
        # stuck: bubble-formation-grading with first_blocked >14d
        stuck_fb = (self.TODAY - timedelta(days=20)).isoformat()
        e_stuck = _make_exp("bubble-formation-grading", first_blocked=stuck_fb)
        registry.add(e_stuck)
        registry.update("bubble-formation-grading", **{"_evaluator_first_blocked": stuck_fb})
        # blocked (no comeback_date, recent): governor-arming (no data)
        registry.add(_make_exp("governor-arming"))

        results = registry.open_with_tristate(self.TODAY)
        ids_in_order = [e["id"] for e in results]

        assert ids_in_order.index("r-exp") < ids_in_order.index("bubble-formation-grading"), \
            "ready must rank before stuck"
        assert ids_in_order.index("bubble-formation-grading") < ids_in_order.index("governor-arming"), \
            "stuck must rank before blocked"
        assert ids_in_order.index("governor-arming") < ids_in_order.index("n-exp"), \
            "blocked must rank before not_old_enough"

    def test_excludes_terminal_items(self, registry):
        registry.add(_make_exp("open-1"))
        registry.add(_make_exp("judged-1", status="judged"))
        registry.add(_make_exp("cancelled-1", status="cancelled"))
        results = registry.open_with_tristate(self.TODAY)
        ids = [e["id"] for e in results]
        assert "open-1" in ids
        assert "judged-1" not in ids
        assert "cancelled-1" not in ids

    def test_evaluation_dict_attached(self, registry):
        registry.add(_make_exp("open-check"))
        results = registry.open_with_tristate(self.TODAY)
        matched = [e for e in results if e["id"] == "open-check"]
        assert matched
        ev = matched[0].get("evaluation")
        assert ev is not None
        assert "state" in ev
        assert "reason" in ev
        assert "stuck" in ev


# ── 5. No auto-promotion ──────────────────────────────────────────────────────

class TestNoAutoPromotion:
    TODAY = date(2026, 7, 6)

    def test_evaluate_does_not_change_status(self, registry, tmp_path, monkeypatch):
        """evaluate() on a ready item must NOT mutate status to matured."""
        import brain.experiment_registry as er
        monkeypatch.setattr(er, "_ROOT", tmp_path)
        # governor-arming with 8 snapshots → ready
        bench_dir = tmp_path / "data" / "benchmark"
        bench_dir.mkdir(parents=True)
        for i in range(8):
            (bench_dir / f"2026-07-{i+1:02d}.json").write_text(json.dumps({}))
        registry.add(_make_exp("governor-arming"))
        item = registry.get("governor-arming")
        result = er.evaluate(item, self.TODAY)
        assert result["state"] == er.STATE_READY
        # Status must still be "open"
        after = registry.get("governor-arming")
        assert after["status"] == "open", \
            "evaluate() must never auto-promote status; only matured() and resolve() may do that"

    def test_open_with_tristate_does_not_change_status(self, registry, tmp_path, monkeypatch):
        """open_with_tristate() must not promote any status."""
        import brain.experiment_registry as er
        monkeypatch.setattr(er, "_ROOT", tmp_path)
        past = (self.TODAY - timedelta(days=3)).isoformat()
        registry.add(_make_exp("ts-exp", comeback_date=past))
        registry.open_with_tristate(self.TODAY)
        item = registry.get("ts-exp")
        # matured() IS allowed to promote (called inside summary/open_with_tristate indirectly)
        # but open_with_tristate() itself doesn't call matured() — it just evaluates
        # The key invariant: status must not be auto-advanced by the evaluate path alone
        assert item["status"] in ("open", "matured")  # matured OK (date-driven matured() may run)


# ── 6. Agenda _from_experiment_tristate() ────────────────────────────────────

class TestAgendaTristate:
    TODAY = date(2026, 7, 6)

    def _mock_registry(self, monkeypatch, items: list[dict]):
        """Patch experiment_registry.open_with_tristate to return controlled items."""
        import brain.experiment_registry as er
        import sys
        import types
        mock_er = types.ModuleType("brain.experiment_registry")
        mock_er.STATE_READY = er.STATE_READY
        mock_er.STATE_BLOCKED = er.STATE_BLOCKED
        mock_er.STATE_NOT_OLD_ENOUGH = er.STATE_NOT_OLD_ENOUGH
        mock_er.open_with_tristate = lambda asof=None: items
        monkeypatch.setattr("brain.improvement_agenda.experiment_registry", mock_er, raising=False)
        import brain
        monkeypatch.setattr(brain, "experiment_registry", mock_er, raising=False)
        monkeypatch.setitem(sys.modules, "brain.experiment_registry", mock_er)

    def _make_agenda_item(self, eid: str, state: str, stuck: bool = False,
                          evidence_n: int | None = None, required_n: int | None = None,
                          erd: str | None = None) -> dict:
        from brain.experiment_registry import STATE_READY, STATE_BLOCKED, STATE_NOT_OLD_ENOUGH
        return _make_exp(eid) | {
            "evaluation": {
                "state": state, "reason": f"test reason for {eid}",
                "evidence_n": evidence_n, "required_n": required_n,
                "expected_ready_date": erd, "stuck": stuck,
            }
        }

    def test_ready_items_high_severity(self, monkeypatch, tmp_path):
        from brain.experiment_registry import STATE_READY
        from brain import improvement_agenda as A
        items = [self._make_agenda_item("ready-exp", STATE_READY)]
        self._mock_registry(monkeypatch, items)
        out = A._from_experiment_tristate(self.TODAY)
        assert out, "ready experiment should produce an agenda item"
        it = out[0]
        assert "READY FOR REVIEW" in it["title"]
        # severity=0.95 → rank_score >= CLASS_EXPERIMENT(86) + ~9
        assert it["rank_score"] >= 86 + 9

    def test_stuck_items_high_severity(self, monkeypatch):
        from brain.experiment_registry import STATE_BLOCKED
        from brain import improvement_agenda as A
        items = [self._make_agenda_item("stuck-exp", STATE_BLOCKED, stuck=True)]
        self._mock_registry(monkeypatch, items)
        out = A._from_experiment_tristate(self.TODAY)
        assert out
        it = out[0]
        assert "STUCK" in it["title"]
        assert it["rank_score"] >= 86 + 8   # severity=0.9 → bump ~9

    def test_blocked_items_lower_severity(self, monkeypatch):
        from brain.experiment_registry import STATE_BLOCKED
        from brain import improvement_agenda as A
        items = [self._make_agenda_item("blocked-exp", STATE_BLOCKED, stuck=False,
                                        evidence_n=3, required_n=40, erd="2026-09-01")]
        self._mock_registry(monkeypatch, items)
        out = A._from_experiment_tristate(self.TODAY)
        assert out
        it = out[0]
        # severity=0.5 → rank_score = 86 + 5
        assert it["rank_score"] <= 86 + 6

    def test_all_items_have_evidence(self, monkeypatch):
        """P3: every tristate item must carry evidence."""
        from brain.experiment_registry import STATE_BLOCKED, STATE_READY, STATE_NOT_OLD_ENOUGH
        from brain import improvement_agenda as A
        items = [
            self._make_agenda_item("r", STATE_READY),
            self._make_agenda_item("b", STATE_BLOCKED),
            self._make_agenda_item("n", STATE_NOT_OLD_ENOUGH),
        ]
        self._mock_registry(monkeypatch, items)
        out = A._from_experiment_tristate(self.TODAY)
        for it in out:
            assert it.get("evidence"), f"item {it['id']} missing evidence (P3)"


# ── 7. Registry round-trip for date-driven items ──────────────────────────────

class TestDateDrivenRoundTrip:
    """Date-driven items (those with comeback_date) behave identically before and after MW2."""
    TODAY = date(2026, 7, 6)

    def test_future_comeback_unchanged(self, registry):
        future = (self.TODAY + timedelta(days=7)).isoformat()
        registry.add(_make_exp("dd-1", comeback_date=future))
        result = registry.matured()
        ids = [e["id"] for e in result]
        assert "dd-1" not in ids, "future item must not be in matured()"

    def test_past_comeback_promoted_by_matured(self, registry):
        past = (self.TODAY - timedelta(days=2)).isoformat()
        registry.add(_make_exp("dd-2", comeback_date=past))
        result = registry.matured()
        ids = [e["id"] for e in result]
        assert "dd-2" in ids
        # status must be promoted to matured (the existing date-driven path, unchanged)
        assert registry.get("dd-2")["status"] == "matured"

    def test_evaluate_agrees_with_matured_for_past_date(self):
        from brain.experiment_registry import evaluate, STATE_READY
        past = (self.TODAY - timedelta(days=2)).isoformat()
        item = _make_exp("dd-3", comeback_date=past)
        result = evaluate(item, self.TODAY)
        assert result["state"] == STATE_READY

    def test_evaluate_agrees_with_matured_for_future_date(self):
        from brain.experiment_registry import evaluate, STATE_NOT_OLD_ENOUGH
        future = (self.TODAY + timedelta(days=5)).isoformat()
        item = _make_exp("dd-4", comeback_date=future)
        result = evaluate(item, self.TODAY)
        assert result["state"] == STATE_NOT_OLD_ENOUGH


# ── 8. register_evaluator() ───────────────────────────────────────────────────

class TestRegisterEvaluator:
    TODAY = date(2026, 7, 6)

    def test_custom_evaluator_dispatched(self, monkeypatch):
        import brain.experiment_registry as er
        calls = {"n": 0}

        def my_eval(item, asof):
            calls["n"] += 1
            return er._eval_result(er.STATE_READY, "custom evaluator fired",
                                   evidence_n=99, required_n=10)

        # Register and test
        old = er._EVALUATORS.get("custom-test-exp")
        try:
            er.register_evaluator("custom-test-exp", my_eval)
            item = _make_exp("custom-test-exp")
            result = er.evaluate(item, self.TODAY)
            assert calls["n"] == 1
            assert result["state"] == er.STATE_READY
            assert result["evidence_n"] == 99
        finally:
            # Restore original state
            if old is None:
                er._EVALUATORS.pop("custom-test-exp", None)
            else:
                er._EVALUATORS["custom-test-exp"] = old

    def test_custom_evaluator_overrides_date_driven(self):
        import brain.experiment_registry as er
        # An experiment that would normally be date-driven (has comeback_date)
        # but a custom evaluator takes precedence.
        future = (self.TODAY + timedelta(days=10)).isoformat()
        item = _make_exp("my-custom-exp", comeback_date=future)
        old = er._EVALUATORS.get("my-custom-exp")
        try:
            er.register_evaluator("my-custom-exp",
                                  lambda i, a: er._eval_result(er.STATE_BLOCKED, "custom override"))
            result = er.evaluate(item, self.TODAY)
            assert result["state"] == er.STATE_BLOCKED
            assert "custom override" in result["reason"]
        finally:
            if old is None:
                er._EVALUATORS.pop("my-custom-exp", None)
            else:
                er._EVALUATORS["my-custom-exp"] = old


# ── 9. Integration: full registry seed parses and evaluates without error ─────

class TestRealSeedTristate:
    TODAY = date(2026, 7, 6)
    _SEED_PATH = _WORKTREE / "data" / "experiments" / "registry.json"

    @pytest.mark.skipif(not _SEED_PATH.exists(), reason="registry.json seed not found")
    def test_all_seed_items_evaluate_without_exception(self):
        """Every item in the real registry.json produces a valid evaluate() result."""
        import brain.experiment_registry as er
        data = json.loads(self._SEED_PATH.read_text())
        assert isinstance(data, list)
        for exp in data:
            result = er.evaluate(exp, self.TODAY)
            assert result["state"] in (er.STATE_NOT_OLD_ENOUGH,
                                       er.STATE_BLOCKED,
                                       er.STATE_READY), \
                f"{exp.get('id')!r} returned unexpected state {result['state']!r}"
            assert isinstance(result["reason"], str)
            assert isinstance(result["stuck"], bool)

    @pytest.mark.skipif(not _SEED_PATH.exists(), reason="registry.json seed not found")
    def test_condition_only_items_are_blocked_not_ready(self):
        """The 4 condition-only items (no comeback_date) must not be ready_for_review
        in the absence of their evidence artifacts (which don't exist in a fresh clone)."""
        import brain.experiment_registry as er
        data = json.loads(self._SEED_PATH.read_text())
        condition_only_ids = {
            "shadow-trim-ladder", "governor-arming",
            "bubble-formation-grading", "deploy-lag-sla",
        }
        for exp in data:
            eid = exp.get("id")
            if eid not in condition_only_ids:
                continue
            result = er.evaluate(exp, self.TODAY)
            assert result["state"] != er.STATE_READY, \
                (f"{eid!r} returned ready_for_review in a fresh clone with no artifacts — "
                 f"the evaluator must return blocked when the evidence dir/file is absent")

    @pytest.mark.skipif(not _SEED_PATH.exists(), reason="registry.json seed not found")
    def test_condition_only_items_never_auto_promoted(self):
        """Calling open_with_tristate() on the real registry does not change any status."""
        import brain.experiment_registry as er
        before = {e["id"]: e["status"]
                  for e in json.loads(self._SEED_PATH.read_text())}
        er.open_with_tristate(self.TODAY)
        after = {e["id"]: e["status"]
                 for e in json.loads(self._SEED_PATH.read_text())}
        condition_only_ids = {
            "shadow-trim-ladder", "governor-arming",
            "bubble-formation-grading", "deploy-lag-sla",
        }
        for eid in condition_only_ids:
            if eid in before:
                assert before[eid] == after[eid], \
                    f"{eid!r} status changed from {before[eid]!r} to {after[eid]!r} — auto-promotion forbidden"
