"""Tests for brain/experiment_registry.py — W-L / L6.

Tests:
  1. Maturity math: date-triggered promotion OPEN → MATURED works correctly.
  2. Status transition guard: invalid transitions are rejected.
  3. Summary: counts add up; matured_items list is consistent.
  4. Resolve: terminal state; verdict note is stored; re-resolve fails.
  5. Add: duplicate id rejected; missing id rejected.
  6. Real seed: registry.json parses clean (every required field present, no unknown statuses).
  7. MAINTENANCE.md paths: every artifact path mentioned in MAINTENANCE.md that is not a
     runtime-generated file actually exists on disk.
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import pytest

# ── helpers ──────────────────────────────────────────────────────────────────

_WORKTREE = Path(__file__).resolve().parent.parent


def _make_exp(eid: str, comeback_date: str | None = None, status: str = "open") -> dict:
    return {
        "id": eid,
        "what": f"Test experiment {eid}",
        "gate": "some gate condition",
        "comeback_date": comeback_date,
        "maturity_condition": "description of maturity",
        "status": status,
        "owner": "opus-session",
        "artifact_paths": [],
        "notes": "",
    }


@pytest.fixture()
def registry(tmp_path, monkeypatch):
    """An isolated registry backed by a tmp file."""
    reg_path = tmp_path / "experiments" / "registry.json"
    reg_path.parent.mkdir(parents=True, exist_ok=True)

    import brain.experiment_registry as er
    monkeypatch.setattr(er, "_REGISTRY_PATH", reg_path)
    yield er


# ── 1. Maturity math ─────────────────────────────────────────────────────────

class TestMaturityMath:
    def test_not_yet_matured(self, registry):
        future = (date.today() + timedelta(days=10)).isoformat()
        registry.add(_make_exp("future-1", comeback_date=future))
        result = registry.matured()
        assert all(e["id"] != "future-1" for e in result), "future item must not be matured yet"

    def test_matured_today(self, registry):
        today = date.today().isoformat()
        registry.add(_make_exp("today-1", comeback_date=today))
        result = registry.matured()
        ids = [e["id"] for e in result]
        assert "today-1" in ids

    def test_matured_past(self, registry):
        past = (date.today() - timedelta(days=5)).isoformat()
        registry.add(_make_exp("past-1", comeback_date=past))
        result = registry.matured()
        ids = [e["id"] for e in result]
        assert "past-1" in ids

    def test_status_promoted_to_matured(self, registry):
        past = (date.today() - timedelta(days=1)).isoformat()
        registry.add(_make_exp("promo-1", comeback_date=past))
        registry.matured()
        exp = registry.get("promo-1")
        assert exp["status"] == "matured", "status must be promoted to matured after matured() call"

    def test_no_comeback_date_stays_open(self, registry):
        registry.add(_make_exp("no-date-1", comeback_date=None))
        registry.matured()
        exp = registry.get("no-date-1")
        assert exp["status"] == "open"

    def test_already_matured_stays_in_list(self, registry):
        registry.add(_make_exp("already-matured", status="matured"))
        result = registry.matured()
        ids = [e["id"] for e in result]
        assert "already-matured" in ids

    def test_judged_not_in_matured(self, registry):
        past = (date.today() - timedelta(days=1)).isoformat()
        registry.add(_make_exp("judged-one", comeback_date=past))
        registry.matured()                              # promotes open → matured
        registry.resolve("judged-one", verdict="resolved")  # matured → judged
        result = registry.matured()
        ids = [e["id"] for e in result]
        assert "judged-one" not in ids

    def test_sort_order_earliest_first(self, registry):
        earlier = (date.today() - timedelta(days=5)).isoformat()
        later = (date.today() - timedelta(days=1)).isoformat()
        registry.add(_make_exp("later-exp", comeback_date=later))
        registry.add(_make_exp("earlier-exp", comeback_date=earlier))
        result = registry.matured()
        ids = [e["id"] for e in result]
        assert ids.index("earlier-exp") < ids.index("later-exp"), "earliest deadline must sort first"


# ── 2. Status transitions ─────────────────────────────────────────────────────

class TestTransitions:
    def test_open_to_matured_allowed(self, registry):
        registry.add(_make_exp("t1"))
        ok = registry.update("t1", status="matured")
        assert ok
        assert registry.get("t1")["status"] == "matured"

    def test_open_to_cancelled_allowed(self, registry):
        registry.add(_make_exp("t2"))
        ok = registry.update("t2", status="cancelled")
        assert ok

    def test_open_to_judged_denied(self, registry):
        registry.add(_make_exp("t3"))
        ok = registry.update("t3", status="judged")
        assert not ok, "open → judged must be rejected; must go via matured first"

    def test_judged_is_terminal(self, registry):
        registry.add(_make_exp("t4"))
        registry.update("t4", status="matured")
        registry.update("t4", status="judged")
        ok = registry.update("t4", status="open")
        assert not ok

    def test_cancelled_is_terminal(self, registry):
        registry.add(_make_exp("t5"))
        registry.update("t5", status="cancelled")
        ok = registry.update("t5", status="open")
        assert not ok

    def test_immutable_fields_not_updatable(self, registry):
        registry.add(_make_exp("t6"))
        original_gate = registry.get("t6")["gate"]
        registry.update("t6", gate="new gate text")
        assert registry.get("t6")["gate"] == original_gate, "gate field must be immutable"


# ── 3. Summary ────────────────────────────────────────────────────────────────

class TestSummary:
    def test_counts(self, registry):
        registry.add(_make_exp("s1"))
        registry.add(_make_exp("s2", status="matured"))
        past = (date.today() - timedelta(days=1)).isoformat()
        registry.add(_make_exp("s3", comeback_date=past))       # will be promoted to matured
        registry.add(_make_exp("s4"))
        registry.update("s4", status="cancelled")
        s = registry.summary()
        assert s["total"] >= 4
        assert s["cancelled"] >= 1
        # after matured() call inside summary, s3 is matured
        assert s["matured"] >= 1

    def test_matured_items_in_summary(self, registry):
        past = (date.today() - timedelta(days=2)).isoformat()
        registry.add(_make_exp("s5", comeback_date=past))
        s = registry.summary()
        ids = [e["id"] for e in s["matured_items"]]
        assert "s5" in ids

    def test_empty_registry_returns_valid_stub(self, registry):
        s = registry.summary()
        assert s["total"] == 0
        assert s["matured_items"] == []
        assert "as_of" in s


# ── 4. Resolve ────────────────────────────────────────────────────────────────

class TestResolve:
    def test_resolve_records_verdict(self, registry):
        registry.add(_make_exp("r1"))
        registry.update("r1", status="matured")
        ok = registry.resolve("r1", verdict="promoted to live")
        assert ok
        exp = registry.get("r1")
        assert exp["status"] == "judged"
        assert "promoted to live" in exp["notes"]

    def test_resolve_from_open_fails(self, registry):
        registry.add(_make_exp("r2"))
        ok = registry.resolve("r2", verdict="skip matured step")
        assert not ok, "must be matured before resolve"

    def test_resolve_twice_fails(self, registry):
        registry.add(_make_exp("r3"))
        registry.update("r3", status="matured")
        registry.resolve("r3", verdict="first verdict")
        ok = registry.resolve("r3", verdict="second verdict")
        assert not ok, "resolved experiment is terminal"

    def test_verdict_date_is_stamped(self, registry):
        registry.add(_make_exp("r4"))
        registry.update("r4", status="matured")
        registry.resolve("r4", verdict="check date stamp")
        notes = registry.get("r4")["notes"]
        assert date.today().isoformat() in notes


# ── 5. Add ────────────────────────────────────────────────────────────────────

class TestAdd:
    def test_add_works(self, registry):
        ok = registry.add(_make_exp("a1"))
        assert ok
        assert registry.get("a1") is not None

    def test_duplicate_id_rejected(self, registry):
        registry.add(_make_exp("a2"))
        ok = registry.add(_make_exp("a2"))
        assert not ok

    def test_missing_id_rejected(self, registry):
        ok = registry.add({"what": "no id", "gate": "x", "status": "open"})
        assert not ok


# ── 6. Real seed parses clean ────────────────────────────────────────────────

_SEED_PATH = _WORKTREE / "data" / "experiments" / "registry.json"
_REQUIRED_FIELDS = {"id", "what", "gate", "comeback_date", "maturity_condition",
                    "status", "owner", "artifact_paths", "notes"}
_VALID_STATUSES = {"open", "matured", "judged", "cancelled"}
_VALID_OWNERS = {"opus-session", "fable-review", "self-tune", "self-tunable"}


@pytest.mark.skipif(not _SEED_PATH.exists(), reason="registry.json seed not found")
class TestRealSeed:
    def test_parses_as_list(self):
        data = json.loads(_SEED_PATH.read_text())
        assert isinstance(data, list), "registry.json must be a JSON array"

    def test_all_required_fields_present(self):
        data = json.loads(_SEED_PATH.read_text())
        for i, exp in enumerate(data):
            missing = _REQUIRED_FIELDS - set(exp.keys())
            assert not missing, f"experiment[{i}] (id={exp.get('id')!r}) missing fields: {missing}"

    def test_all_ids_unique(self):
        data = json.loads(_SEED_PATH.read_text())
        ids = [e.get("id") for e in data]
        assert len(ids) == len(set(ids)), "experiment ids must be unique"

    def test_valid_statuses(self):
        data = json.loads(_SEED_PATH.read_text())
        for exp in data:
            assert exp.get("status") in _VALID_STATUSES, (
                f"experiment {exp.get('id')!r} has invalid status {exp.get('status')!r}")

    def test_valid_owners(self):
        data = json.loads(_SEED_PATH.read_text())
        for exp in data:
            assert exp.get("owner") in _VALID_OWNERS, (
                f"experiment {exp.get('id')!r} has invalid owner {exp.get('owner')!r}")

    def test_artifact_paths_are_lists(self):
        data = json.loads(_SEED_PATH.read_text())
        for exp in data:
            assert isinstance(exp.get("artifact_paths", []), list), (
                f"experiment {exp.get('id')!r} artifact_paths must be a list")

    def test_comeback_dates_are_iso_or_null(self):
        data = json.loads(_SEED_PATH.read_text())
        for exp in data:
            cd = exp.get("comeback_date")
            if cd is not None:
                # must parse as ISO date
                try:
                    date.fromisoformat(str(cd)[:10])
                except ValueError:
                    pytest.fail(f"experiment {exp.get('id')!r} has non-ISO comeback_date: {cd!r}")


# ── 7. MAINTENANCE.md paths exist ────────────────────────────────────────────

_MAINTENANCE_PATH = _WORKTREE / "MAINTENANCE.md"

# Paths listed in MAINTENANCE.md that must exist as real files or directories on disk.
# These are the load-bearing references (code files, critical docs, test dirs) that a
# maintenance session would look up.  We skip auto-generated data dirs and runtime files.
_MAINTENANCE_PATHS_TO_CHECK = [
    "research/MASTERMIND_FIX_MASTERPLAN.md",
    "research/MASTERMIND_CHARTER_V2.md",
    "research/mastermind_problem_register.json",
    "research/MASTERMIND_LEARNING_DESIGN.md",
    "research/MASTERMIND_V2_ARCHITECTURE.md",
    "brain/experiment_registry.py",
    "brain/improvement_agenda.py",
    "brain/cio.py",
    "brain/calibration.py",
    "brain/benchmark_ledger.py",
    "data/experiments/registry.json",
    "tests/incident_replays",
    "app/scheduler.py",
    "scripts/check_deploy_lag.py",
    "loop/harness.py",
    "loop/pbo.py",
    "loop/holdout.py",
    "config/doctrine.yml",
    "config/clusters.yml",
    "research/incidents/2026-07-02-semis-breakdown",
    "MAINTENANCE.md",
]


@pytest.mark.skipif(not _MAINTENANCE_PATH.exists(), reason="MAINTENANCE.md not found")
class TestMaintenancePaths:
    @pytest.mark.parametrize("rel_path", _MAINTENANCE_PATHS_TO_CHECK)
    def test_referenced_path_exists(self, rel_path):
        target = _WORKTREE / rel_path
        assert target.exists(), (
            f"MAINTENANCE.md references {rel_path!r} but it does not exist at {target}")
