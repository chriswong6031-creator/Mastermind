"""Incident-replay battery isolation — the READ side.

The replay battery is the regression law (MAINTENANCE.md §1 step 4): it must be
byte-identical green in a fresh worktree, in CI, and in the PRODUCTION checkout —
where gitignored live artifacts (vendor/macro data hydrated by macro_refresh every
3h, data/posture/ published every build) exist and would otherwise leak into a
replay.  The parent tests/conftest.py isolates the WRITE side (runlog / ledgers /
bot.db / macro_risk state / gate root); this conftest isolates the replay-relevant
READ side: every live macro artifact a replay could silently consume is pointed at
an empty tmp dir, so a replay sees the fresh-worktree filesystem in every checkout.
A test that NEEDS one of these inputs injects its own frozen fixture over the
isolation (an in-test monkeypatch wins — the established pattern in this battery).

2026-07-25 forensics: test_composed_stack_flag_on_disagreeing_tape went red ONLY in
production — posture_decider.decide() reads regime_frame.frame("us") ->
vendor/macro/data/regime/latest.json, and the live calm frame (conf 0.408 /
TRANSITIONING / margin 0.15) diluted defense_pressure to 0.273 -> BALANCED instead
of the tape's intended 0.5433 -> ROTATE_DEFENSIVE.
"""
import pytest


@pytest.fixture(autouse=True)
def _isolate_regime_frame_reads(tmp_path, monkeypatch):
    """Point every regime_frame live-read path (regime latest.json per region, the
    sector-cycles JSON, the radar forward-log) at an empty tmp dir.  Absent files
    degrade frame()/cycles()/the radar source to their None/{} paths — the documented
    invariant (missing data coarsens, never flips) and the exact condition the battery
    was authored and proven green against."""
    try:
        from brain import regime_frame as RF
        iso = tmp_path / "_regime_frame_iso"
        monkeypatch.setattr(
            RF, "_REGION_PATHS",
            {region: iso / f"{region}_regime_latest.json" for region in RF._REGION_PATHS},
            raising=False,
        )
        monkeypatch.setattr(RF, "_CYCLES_PATH", iso / "sector_cycles.json", raising=False)
        monkeypatch.setattr(RF, "_RADAR_FORWARD_LOG", iso / "forward_log.jsonl",
                            raising=False)
    except Exception:  # noqa: BLE001 — module absent on an older base: nothing to isolate
        pass


@pytest.fixture(autouse=True)
def _isolate_posture_artifacts(tmp_path, monkeypatch):
    """Isolate the posture decider's published artifact + hysteresis state so a replay
    can neither READ a live data/posture/latest.json (published every build in
    production, absent in worktrees) nor write one."""
    try:
        from brain import posture_decider as PD
        pdir = tmp_path / "_posture_iso"
        monkeypatch.setattr(PD, "_ARTIFACT_DIR", pdir, raising=False)
        monkeypatch.setattr(PD, "_LATEST_PATH", pdir / "latest.json", raising=False)
        monkeypatch.setattr(PD, "_STATE_PATH", pdir / "state.json", raising=False)
    except Exception:  # noqa: BLE001 — module absent on an older base: nothing to isolate
        pass
