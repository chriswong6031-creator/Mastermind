"""Tests for the VPS state-sync scheduler job — the bot.mastermind-x.com refresh path.

Root cause it guards against: the box (serve-only mirror) only refreshes when data/ is pushed from
the Mac; the old launchd pusher was disabled and the box silently froze for ~5 days. This job pushes
from the always-on Brain process instead. These tests never trigger a real push (they only exercise
the surfacing, the never-raises contract, and the box-safety guard).
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path


def _read_events(root: Path) -> list[dict]:
    p = root / "data" / "governance" / "run_events.jsonl"
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text().splitlines() if line.strip()]


def test_vps_state_sync_is_a_known_job():
    """Must be surfaced in /api/scheduler so a stalled sync is queryable, not silent — the whole
    point of moving it into the ledgered scheduler."""
    from app.scheduler import scheduler_health
    ids = {r["id"] for r in scheduler_health()}
    assert "vps_state_sync" in ids


def test_missing_script_records_step_failed_and_never_raises(tmp_path, monkeypatch):
    """A missing sync script must record a step_failed event and NOT raise (best-effort contract).
    Pointing __file__ at an empty tmp tree makes the resolved script path not exist — so the job
    takes the missing-script branch and never attempts a real rsync."""
    import app.scheduler as sched_mod
    import control_plane.run_events as re_mod

    orig_lp = re_mod._ledger_path
    monkeypatch.setattr(re_mod, "_ledger_path", lambda root=None: orig_lp(tmp_path))
    monkeypatch.setattr(sched_mod, "__file__", str(tmp_path / "app" / "scheduler.py"))

    sched_mod._vps_state_sync_job()  # must not raise

    failed = [e for e in _read_events(tmp_path)
              if e.get("kind") == "step_failed" and e.get("job") == "vps_state_sync"]
    assert failed, "missing script must record a step_failed event"


def test_sync_script_noops_under_serve_only():
    """On the box (MASTERMIND_SERVE_ONLY=1) the committed script must exit BEFORE the rsync — never
    push box->box. This is the safety guard that lets the job be a no-op even if ever run there."""
    script = Path(__file__).resolve().parent.parent / "scripts" / "sync_state_to_vps.sh"
    assert script.exists(), "the sync script must be committed in the repo (was previously untracked)"
    r = subprocess.run(
        ["/bin/bash", str(script)],
        capture_output=True, text=True, timeout=20,
        env={"MASTERMIND_SERVE_ONLY": "1", "PATH": "/usr/bin:/bin"},
    )
    assert r.returncode == 0
    assert "sync ok" not in r.stdout, "serve-only guard must exit before the rsync/echo"
