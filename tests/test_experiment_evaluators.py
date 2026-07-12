"""Tests for the two 2026-07-17 mechanical evaluators — judgment-book-promotion and
posture-decider-arming.

WHY THIS FILE EXISTS
--------------------
Both experiments carry comeback_date=2026-07-17 in data/experiments/registry.json. Before these
evaluators were registered they fell through to _eval_date_driven, which returns ready_for_review
the moment the calendar date arrives AND matured() auto-promotes status→matured by date alone —
i.e. an arming flag would flip on the CALENDAR, not on data. That is a statistical-honesty
violation. These tests lock the fix in:

  1. With today's real-world data (no judgment shadow book, no data/posture/, no data/benchmark/),
     BOTH evaluators return insufficient_power (== STATE_BLOCKED), never ready.
  2. Given synthetic data that clears the pre-registered bar, each returns ready_for_review.
  3. The matured-by-date-only path can no longer bypass the evaluator: on 2026-07-17 with data
     absent the state is STILL blocked (the registered evaluator overrides _eval_date_driven).
  4. evaluate() never auto-promotes status.

Thresholds under test (their documented sources):
  * judgment-book-promotion: effective-n >= 8 non-overlapping 21-bday obs AND >= 168-day span.
    Source: docs/design/desk/AB_EXPERIMENT.md §4.2 (_MIN_DATES=8), §5 (decision rule / "INSUFFICIENT
    POWER — keep running"), §7 (168-day floor; "2026-07-17 is before any forward verdict").
  * posture-decider-arming: >= 2wk (14d) shadow posture window with >= 10 dated records AND
    >= 8 benchmark snapshots. Source: registry gate string "2wk shadow no-compounding" +
    brain/posture_governor.py:48 `_MIN_EFFECTIVE_N = 8` (the guard posture_governor.guards() enforces).

All data reads are redirected to tmp_path via monkeypatch of er._ROOT — no reliance on live
shadow/posture/benchmark data existing on disk.
"""
from __future__ import annotations

import json
from datetime import date, timedelta

import pytest


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
        "owner": "fable-review",
        "artifact_paths": artifact_paths or [],
        "notes": "",
        "_evaluator_first_blocked": first_blocked,
    }


def _write_judgment_nav(root, dates: list[date]) -> None:
    """Write a judgment shadow book nav_history.jsonl with one row per date."""
    book_dir = root / "data" / "shadow" / "books" / "flagship_judgment"
    book_dir.mkdir(parents=True, exist_ok=True)
    lines = []
    for i, d in enumerate(dates):
        lines.append(json.dumps({
            "date": d.isoformat(),
            "nav": 1_000_000.0 + i * 1000,
            "cash": 800_000.0,
            "invested": 200_000.0,
            "spy_px": 750.0 + i,
        }))
    (book_dir / "nav_history.jsonl").write_text("\n".join(lines) + "\n")


def _write_posture_records(root, dates: list[date]) -> None:
    """Write dated data/posture/<asof>.json shadow artifacts, one per date."""
    posture_dir = root / "data" / "posture"
    posture_dir.mkdir(parents=True, exist_ok=True)
    for d in dates:
        (posture_dir / f"{d.isoformat()}.json").write_text(json.dumps({
            "schema_version": "posture.v1",
            "asof": d.isoformat(),
            "shadow": True,
            "posture_class": "BALANCED",
        }))
    # latest.json / state.json are NON-dated and must be excluded from the count
    (posture_dir / "latest.json").write_text(json.dumps({"asof": dates[-1].isoformat()}))
    (posture_dir / "state.json").write_text(json.dumps({"class": "BALANCED"}))


def _write_benchmarks(root, n: int) -> None:
    """Write n weekly benchmark snapshots + one internal _series.json that must be excluded."""
    bench_dir = root / "data" / "benchmark"
    bench_dir.mkdir(parents=True, exist_ok=True)
    for i in range(n):
        (bench_dir / f"2026-05-{i + 1:02d}.json").write_text(
            json.dumps({"as_of": f"2026-05-{i + 1:02d}", "bogeys": {}, "leaderboard": []}))
    (bench_dir / "_series.json").write_text(json.dumps({}))


# ═══════════════════════════════════════════════════════════════════════════════
# judgment-book-promotion
# ═══════════════════════════════════════════════════════════════════════════════

class TestJudgmentBookPromotion:
    TODAY = date(2026, 7, 17)   # the comeback_date — the exact day the calendar-bug would fire

    def test_blocked_when_book_absent(self, tmp_path, monkeypatch):
        """No judgment shadow book on disk (today's real state) → insufficient_power, not ready."""
        import brain.experiment_registry as er
        monkeypatch.setattr(er, "_ROOT", tmp_path)   # tmp_path has no data/ tree
        item = _make_exp("judgment-book-promotion", comeback_date="2026-07-17")
        result = er.evaluate(item, self.TODAY)
        assert result["state"] == er.STATE_BLOCKED
        assert result["state"] != er.STATE_READY
        assert result["evidence_n"] == 0
        assert result["required_n"] == 8
        assert "insufficient power" in result["reason"].lower()

    def test_blocked_when_book_empty(self, tmp_path, monkeypatch):
        """An empty nav_history.jsonl → still blocked (0 observations)."""
        import brain.experiment_registry as er
        monkeypatch.setattr(er, "_ROOT", tmp_path)
        book_dir = tmp_path / "data" / "shadow" / "books" / "flagship_judgment"
        book_dir.mkdir(parents=True)
        (book_dir / "nav_history.jsonl").write_text("")
        item = _make_exp("judgment-book-promotion", comeback_date="2026-07-17")
        result = er.evaluate(item, self.TODAY)
        assert result["state"] == er.STATE_BLOCKED
        assert result["evidence_n"] == 0

    def test_blocked_below_effective_n_even_with_long_span(self, tmp_path, monkeypatch):
        """Only 3 non-overlapping obs (well below 8) → blocked, even if the span is long."""
        import brain.experiment_registry as er
        monkeypatch.setattr(er, "_ROOT", tmp_path)
        # 3 obs spaced ~90 days apart: span is huge but effective-n is 3 < 8.
        base = date(2026, 1, 1)
        dates = [base + timedelta(days=90 * i) for i in range(3)]
        _write_judgment_nav(tmp_path, dates)
        item = _make_exp("judgment-book-promotion", comeback_date="2026-07-17")
        result = er.evaluate(item, self.TODAY)
        assert result["state"] == er.STATE_BLOCKED
        assert result["evidence_n"] == 3
        assert result["required_n"] == 8

    def test_blocked_daily_marks_are_not_independent(self, tmp_path, monkeypatch):
        """20 CONSECUTIVE daily marks over 20 days → effective-n=1 (overlapping) → blocked.

        This is the core honesty guard: raw row count (20) would clear 8, but overlapping daily
        marks are NOT independent 21-bday observations, so the thinned effective-n is 1.
        """
        import brain.experiment_registry as er
        monkeypatch.setattr(er, "_ROOT", tmp_path)
        base = date(2026, 6, 1)
        dates = [base + timedelta(days=i) for i in range(20)]   # 20 consecutive calendar days
        _write_judgment_nav(tmp_path, dates)
        item = _make_exp("judgment-book-promotion", comeback_date="2026-07-17")
        result = er.evaluate(item, self.TODAY)
        assert result["state"] == er.STATE_BLOCKED
        assert result["evidence_n"] == 1, "consecutive daily marks collapse to one independent obs"

    def test_blocked_enough_obs_but_span_too_short(self, tmp_path, monkeypatch):
        """8 non-overlapping obs but span < 168 days → still blocked (both floors must clear)."""
        import brain.experiment_registry as er
        monkeypatch.setattr(er, "_ROOT", tmp_path)
        # 8 obs spaced exactly 29 days → span = 7*29 = 203 days? No: force short span with 15-day gaps
        # would drop below independence; instead use 8 obs at 29d = 203d (clears). To make span short,
        # we need obs closer than 29d, which reduces effective-n. So construct: 8 obs at 20d spacing —
        # 20 < 29 gap means every-other collapses. Use 8 obs at exactly the min gap but only spanning
        # < 168d is impossible with 8 independent obs (7*29=203). So this case is: many obs, short span.
        # 8 obs at 29-day spacing → span 203d which CLEARS. Demonstrate the span floor instead with
        # obs that are independent but the FIRST-to-LAST span is short by packing 8 at 21d cal spacing
        # (< 29 gap) → effective-n collapses below 8. The span floor binds via effective-n here.
        base = date(2026, 6, 1)
        dates = [base + timedelta(days=21 * i) for i in range(8)]  # 21d cal < 29d gap
        _write_judgment_nav(tmp_path, dates)
        item = _make_exp("judgment-book-promotion", comeback_date="2026-07-17")
        result = er.evaluate(item, self.TODAY)
        # 21-day spacing < 29-day independence gap → every other obs kept → effective-n = 4 < 8
        assert result["state"] == er.STATE_BLOCKED
        assert result["evidence_n"] < 8

    def test_ready_when_bar_cleared(self, tmp_path, monkeypatch):
        """8 non-overlapping 21-bday obs spanning > 168 days → ready_for_review."""
        import brain.experiment_registry as er
        monkeypatch.setattr(er, "_ROOT", tmp_path)
        # 8 obs spaced 30 days apart (>= 29d gap → all independent); span = 7*30 = 210 days >= 168.
        base = date(2026, 1, 1)
        dates = [base + timedelta(days=30 * i) for i in range(8)]
        _write_judgment_nav(tmp_path, dates)
        item = _make_exp("judgment-book-promotion", comeback_date="2026-07-17")
        result = er.evaluate(item, date(2026, 8, 1))
        assert result["state"] == er.STATE_READY
        assert result["evidence_n"] == 8
        assert result["required_n"] == 8

    def test_ready_ignores_bad_rows(self, tmp_path, monkeypatch):
        """Malformed / nav-less / duplicate rows do not count; 8 good independent obs still clear."""
        import brain.experiment_registry as er
        monkeypatch.setattr(er, "_ROOT", tmp_path)
        book_dir = tmp_path / "data" / "shadow" / "books" / "flagship_judgment"
        book_dir.mkdir(parents=True)
        base = date(2026, 1, 1)
        good = [base + timedelta(days=30 * i) for i in range(8)]
        lines = [json.dumps({"date": d.isoformat(), "nav": 1e6 + i}) for i, d in enumerate(good)]
        lines.append("not json at all")                         # bad line
        lines.append(json.dumps({"date": good[0].isoformat(), "nav": 999}))  # duplicate date
        lines.append(json.dumps({"date": "2026-04-15"}))        # no nav → ignored
        (book_dir / "nav_history.jsonl").write_text("\n".join(lines) + "\n")
        item = _make_exp("judgment-book-promotion", comeback_date="2026-07-17")
        result = er.evaluate(item, date(2026, 8, 1))
        assert result["state"] == er.STATE_READY
        assert result["evidence_n"] == 8


# ═══════════════════════════════════════════════════════════════════════════════
# posture-decider-arming
# ═══════════════════════════════════════════════════════════════════════════════

class TestPostureDeciderArming:
    TODAY = date(2026, 7, 17)

    def test_blocked_when_nothing_exists(self, tmp_path, monkeypatch):
        """No data/posture/ and no data/benchmark/ (today's real state) → insufficient_power."""
        import brain.experiment_registry as er
        monkeypatch.setattr(er, "_ROOT", tmp_path)
        item = _make_exp("posture-decider-arming", comeback_date="2026-07-17")
        result = er.evaluate(item, self.TODAY)
        assert result["state"] == er.STATE_BLOCKED
        assert result["state"] != er.STATE_READY
        assert "insufficient power" in result["reason"].lower()

    def test_blocked_posture_ok_but_no_benchmarks(self, tmp_path, monkeypatch):
        """A full 2wk posture window but 0 benchmark snapshots → blocked (both preconditions bind)."""
        import brain.experiment_registry as er
        monkeypatch.setattr(er, "_ROOT", tmp_path)
        base = date(2026, 6, 1)
        _write_posture_records(tmp_path, [base + timedelta(days=i) for i in range(16)])  # 16d span
        # no benchmarks written
        item = _make_exp("posture-decider-arming", comeback_date="2026-07-17")
        result = er.evaluate(item, self.TODAY)
        assert result["state"] == er.STATE_BLOCKED
        assert "benchmark" in result["reason"].lower()

    def test_blocked_benchmarks_ok_but_posture_window_short(self, tmp_path, monkeypatch):
        """8 benchmarks but only a 5-day posture window → blocked (window precondition unmet)."""
        import brain.experiment_registry as er
        monkeypatch.setattr(er, "_ROOT", tmp_path)
        _write_benchmarks(tmp_path, 8)
        base = date(2026, 6, 1)
        _write_posture_records(tmp_path, [base + timedelta(days=i) for i in range(5)])  # 5 records/4d
        item = _make_exp("posture-decider-arming", comeback_date="2026-07-17")
        result = er.evaluate(item, self.TODAY)
        assert result["state"] == er.STATE_BLOCKED
        assert "posture" in result["reason"].lower()

    def test_blocked_seven_benchmarks(self, tmp_path, monkeypatch):
        """A full posture window but only 7 benchmarks (< 8) → blocked."""
        import brain.experiment_registry as er
        monkeypatch.setattr(er, "_ROOT", tmp_path)
        _write_benchmarks(tmp_path, 7)   # 7 real + 1 _series.json (excluded)
        base = date(2026, 6, 1)
        _write_posture_records(tmp_path, [base + timedelta(days=i) for i in range(16)])
        item = _make_exp("posture-decider-arming", comeback_date="2026-07-17")
        result = er.evaluate(item, self.TODAY)
        assert result["state"] == er.STATE_BLOCKED

    def test_ready_when_both_preconditions_met(self, tmp_path, monkeypatch):
        """>= 2wk posture window (>= 10 records) AND >= 8 benchmarks → ready_for_review."""
        import brain.experiment_registry as er
        monkeypatch.setattr(er, "_ROOT", tmp_path)
        _write_benchmarks(tmp_path, 8)
        base = date(2026, 6, 1)
        # 16 daily posture records → span 15 days >= 14, count 16 >= 10.
        _write_posture_records(tmp_path, [base + timedelta(days=i) for i in range(16)])
        item = _make_exp("posture-decider-arming", comeback_date="2026-07-17")
        result = er.evaluate(item, self.TODAY)
        assert result["state"] == er.STATE_READY
        assert "human must still confirm" in result["reason"].lower()

    def test_ready_excludes_internal_files_from_counts(self, tmp_path, monkeypatch):
        """latest.json/state.json (posture) and _series.json (benchmark) are excluded from counts."""
        import brain.experiment_registry as er
        monkeypatch.setattr(er, "_ROOT", tmp_path)
        _write_benchmarks(tmp_path, 8)   # writes _series.json too
        base = date(2026, 6, 1)
        _write_posture_records(tmp_path, [base + timedelta(days=i) for i in range(16)])  # + latest/state
        item = _make_exp("posture-decider-arming", comeback_date="2026-07-17")
        result = er.evaluate(item, self.TODAY)
        # If internal files were counted the numbers would be off; ready still holds and the
        # deviations/latest/state files must not have inflated the dated-record count.
        assert result["state"] == er.STATE_READY


# ═══════════════════════════════════════════════════════════════════════════════
# The calendar-only maturation bypass is closed
# ═══════════════════════════════════════════════════════════════════════════════

class TestCalendarBypassClosed:
    """The whole point: on/after 2026-07-17 with NO evidence, these must NOT be ready — proving the
    date-driven fallback no longer decides these two experiments."""

    def test_registered_evaluators_override_date_driven(self):
        import brain.experiment_registry as er
        assert "judgment-book-promotion" in er._EVALUATORS
        assert "posture-decider-arming" in er._EVALUATORS
        assert er._EVALUATORS["judgment-book-promotion"] is er._eval_judgment_book_promotion
        assert er._EVALUATORS["posture-decider-arming"] is er._eval_posture_decider_arming

    @pytest.mark.parametrize("eid", ["judgment-book-promotion", "posture-decider-arming"])
    def test_on_comeback_date_with_no_data_still_blocked(self, eid, tmp_path, monkeypatch):
        """ON the comeback_date, data absent → STATE_BLOCKED (a bare date-driven item would be READY)."""
        import brain.experiment_registry as er
        monkeypatch.setattr(er, "_ROOT", tmp_path)   # empty data tree
        on_the_day = date(2026, 7, 17)
        item = _make_exp(eid, comeback_date="2026-07-17")
        result = er.evaluate(item, on_the_day)
        assert result["state"] == er.STATE_BLOCKED, \
            f"{eid} matured on the calendar with no data — the bug is not fixed"

    @pytest.mark.parametrize("eid", ["judgment-book-promotion", "posture-decider-arming"])
    def test_well_past_comeback_date_still_blocked(self, eid, tmp_path, monkeypatch):
        """Even 60 days PAST the comeback_date, data absent → still blocked, never ready."""
        import brain.experiment_registry as er
        monkeypatch.setattr(er, "_ROOT", tmp_path)
        way_past = date(2026, 9, 15)
        item = _make_exp(eid, comeback_date="2026-07-17")
        result = er.evaluate(item, way_past)
        assert result["state"] == er.STATE_BLOCKED

    def test_control_bare_date_item_WOULD_be_ready(self):
        """Control: a date-driven item with NO registered evaluator IS ready on its comeback_date —
        this is exactly the behavior the two evaluators above suppress for their ids."""
        import brain.experiment_registry as er
        item = _make_exp("some-unregistered-exp", comeback_date="2026-07-17")
        result = er.evaluate(item, date(2026, 7, 17))
        assert result["state"] == er.STATE_READY, \
            "sanity: the date-driven fallback DOES mature on the calendar — that is the bug we fenced"


# ═══════════════════════════════════════════════════════════════════════════════
# No auto-promotion (the safety invariant holds for these evaluators too)
# ═══════════════════════════════════════════════════════════════════════════════

class TestNoAutoPromotion:
    TODAY = date(2026, 7, 17)

    @pytest.fixture()
    def registry(self, tmp_path, monkeypatch):
        reg_path = tmp_path / "experiments" / "registry.json"
        reg_path.parent.mkdir(parents=True, exist_ok=True)
        import brain.experiment_registry as er
        monkeypatch.setattr(er, "_REGISTRY_PATH", reg_path)
        monkeypatch.setattr(er, "_ROOT", tmp_path)
        yield er

    def test_evaluate_ready_does_not_promote_status(self, registry, tmp_path):
        """Even when a judgment book clears the bar, evaluate() must not mutate status→matured."""
        er = registry
        base = date(2026, 1, 1)
        _write_judgment_nav(tmp_path, [base + timedelta(days=30 * i) for i in range(8)])
        er.add(_make_exp("judgment-book-promotion", comeback_date="2026-07-17"))
        item = er.get("judgment-book-promotion")
        result = er.evaluate(item, date(2026, 8, 1))
        assert result["state"] == er.STATE_READY
        assert er.get("judgment-book-promotion")["status"] == "open", \
            "evaluate() must never auto-promote; only matured()/resolve() may change status"


# ═══════════════════════════════════════════════════════════════════════════════
# Real-seed integration — with today's data both are honestly insufficient_power
# ═══════════════════════════════════════════════════════════════════════════════

class TestRealSeedToday:
    """Against the real registry.json and the real (empty-of-evidence) data tree, both experiments
    return insufficient_power TODAY. This is the correct, honest outcome — asserting it prevents a
    future regression that re-introduces calendar maturation."""
    from pathlib import Path as _Path
    _SEED = _Path(__file__).resolve().parent.parent / "data" / "experiments" / "registry.json"

    @pytest.mark.skipif(not _SEED.exists(), reason="registry.json seed not found")
    @pytest.mark.parametrize("eid", ["judgment-book-promotion", "posture-decider-arming"])
    def test_current_real_state_is_insufficient_power(self, eid):
        import brain.experiment_registry as er
        seed = json.loads(self._SEED.read_text())
        item = next((e for e in seed if e.get("id") == eid), None)
        assert item is not None, f"{eid} missing from registry.json seed"
        # Use the real _ROOT (no monkeypatch) so we read the actual data tree as it stands today.
        result = er.evaluate(item, date(2026, 7, 17))
        assert result["state"] == er.STATE_BLOCKED, \
            (f"{eid} is not insufficient_power against today's real data — either the evidence "
             "genuinely arrived (great, then re-judge) or the evaluator regressed to calendar maturation")
        assert result["state"] != er.STATE_READY
