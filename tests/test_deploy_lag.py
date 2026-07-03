"""Tests for the deploy-lag tripwire (W-I Task 4b).

The tripwire lives in scripts/check_deploy_lag.py and is called from bot/daily.py step-0a.
It compares the running checkout's HEAD against the repo's master/main ref using only local
git metadata (pure local, no network).  When master is ahead by >24h, it emits a LOUD log
line and writes data/deploy_lag.json.

TEST STRATEGY: we build fixture git repos in tmp_path using ``git init`` + ``git commit``
and drive the check() function against them directly, bypassing any live repo state.

INVARIANTS UNDER TEST:
  (1) Current (HEAD == master) → ok=True, behind=0, no warn, no raise.
  (2) Behind <24h → ok=True, behind>0, warn=False, behind_by_commits correct.
  (3) Behind >24h → ok=False, warn=True, loud message, artifact written.
  (4) Git unavailable (non-git dir) → degrades silently, ok=True, git_unavailable=True.
  (5) Only master ref visible → no crash when main absent (and vice versa).
  (6) daily.py step-0a calls check() and surfaces deploy_lag in its output (wiring test).
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import pytest

import bot  # noqa: F401  -> vendor/macro onto sys.path


# ── helpers to build fixture git repos ──────────────────────────────────────────────────────────

def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def _make_commit(cwd: Path, message: str, author_date: str | None = None) -> str:
    """Create an empty commit with an optional --date override (ISO-8601 or unix-ts)."""
    env = dict(os.environ)
    if author_date:
        env["GIT_AUTHOR_DATE"] = author_date
        env["GIT_COMMITTER_DATE"] = author_date
    subprocess.run(
        ["git", "-C", str(cwd), "commit", "--allow-empty", "-m", message],
        env=env, capture_output=True, text=True, check=True,
    )
    return _git(cwd, "rev-parse", "HEAD")


def _init_repo(tmp_path: Path, branch_name: str = "master") -> Path:
    """Initialise a bare git repo with a single initial commit on branch_name."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", branch_name)
    _git(repo, "config", "user.email", "test@mastermind.local")
    _git(repo, "config", "user.name", "Test Bot")
    _make_commit(repo, "initial commit")
    return repo


# ── import the module under test ────────────────────────────────────────────────────────────────

from scripts.check_deploy_lag import check, _ARTIFACT


# ════════════════════════════════════════════════════════════════════════════════════════════════
# (1) HEAD == master → current, no warn
# ════════════════════════════════════════════════════════════════════════════════════════════════

def test_current_checkout_no_lag(tmp_path):
    """When HEAD == master there is no lag and no warning."""
    repo = _init_repo(tmp_path, branch_name="master")
    result = check(root=repo)

    assert result["ok"] is True
    assert result["behind_by_commits"] == 0
    assert result["lag_hours"] == 0.0
    assert result["warn"] is False
    assert result["git_unavailable"] is False
    assert result["master_ref"] == "master"


def test_current_checkout_main_branch(tmp_path):
    """Works with 'main' as the default branch."""
    repo = _init_repo(tmp_path, branch_name="main")
    result = check(root=repo)

    assert result["ok"] is True
    assert result["behind_by_commits"] == 0
    assert result["master_ref"] == "main"


# ════════════════════════════════════════════════════════════════════════════════════════════════
# (2) Behind <24h → ok=True, warn=False, behind_by_commits correct
# ════════════════════════════════════════════════════════════════════════════════════════════════

def test_behind_within_threshold_no_warn(tmp_path):
    """N commits behind master but within the 24h threshold → ok, no warn."""
    repo = _init_repo(tmp_path, branch_name="master")

    # Create a detached-HEAD-style: make two extra commits on master while leaving HEAD
    # pointing at the initial commit (simulate production running an older sha).
    head_sha = _git(repo, "rev-parse", "HEAD")

    # Detach HEAD to the initial commit
    subprocess.run(["git", "-C", str(repo), "checkout", "--detach", head_sha],
                   capture_output=True, text=True)

    # Add 2 commits to master (dated ~1h ago — within 24h)
    one_hour_ago = str(int(time.time()) - 3600)
    _git(repo, "checkout", "master")
    _make_commit(repo, "fix-1", author_date=one_hour_ago)
    _make_commit(repo, "fix-2", author_date=one_hour_ago)

    # Check out the old sha (production simulation)
    subprocess.run(["git", "-C", str(repo), "checkout", "--detach", head_sha],
                   capture_output=True, text=True)

    result = check(root=repo, warn_hours=24.0)

    assert result["behind_by_commits"] == 2
    assert result["warn"] is False      # within 24h threshold
    assert result["lag_hours"] < 24.0
    assert result["ok"] is True


# ════════════════════════════════════════════════════════════════════════════════════════════════
# (3) Behind >24h → warn=True, loud message, artifact written
# ════════════════════════════════════════════════════════════════════════════════════════════════

def test_behind_over_threshold_warns(tmp_path, monkeypatch):
    """N commits behind master and the oldest is >24h old → warn=True, artifact written."""
    repo = _init_repo(tmp_path, branch_name="master")
    head_sha = _git(repo, "rev-parse", "HEAD")

    # Detach HEAD
    subprocess.run(["git", "-C", str(repo), "checkout", "--detach", head_sha],
                   capture_output=True, text=True)

    # Add 3 commits to master dated 48h ago (well beyond threshold)
    two_days_ago = str(int(time.time()) - 48 * 3600)
    _git(repo, "checkout", "master")
    _make_commit(repo, "incident-fix-w0", author_date=two_days_ago)
    _make_commit(repo, "incident-fix-w1", author_date=two_days_ago)
    _make_commit(repo, "incident-fix-w2", author_date=two_days_ago)

    # Point HEAD to the old sha
    subprocess.run(["git", "-C", str(repo), "checkout", "--detach", head_sha],
                   capture_output=True, text=True)

    # Redirect the artifact to tmp_path so we don't pollute the real repo's data/
    artifact_path = tmp_path / "deploy_lag.json"
    from scripts import check_deploy_lag as cdl
    monkeypatch.setattr(cdl, "_ARTIFACT", artifact_path, raising=False)

    result = check(root=repo, warn_hours=24.0)

    assert result["warn"] is True
    assert result["ok"] is False
    assert result["behind_by_commits"] == 3
    assert result["lag_hours"] > 24.0
    assert "DEPLOY-LAG" in result["message"]
    assert result["master_ref"] == "master"

    # Artifact must have been written
    assert artifact_path.exists(), "deploy_lag.json artifact must be written when warn=True"
    written = json.loads(artifact_path.read_text())
    assert written["warn"] is True
    assert written["behind_by_commits"] == 3
    assert written["oldest_unshipped_commit_age_h"] > 24.0


# ════════════════════════════════════════════════════════════════════════════════════════════════
# (4) Git unavailable → silent degrade
# ════════════════════════════════════════════════════════════════════════════════════════════════

def test_non_git_dir_degrades_silently(tmp_path):
    """Pointing at a non-git directory must return ok=True and git_unavailable=True, no raise."""
    plain_dir = tmp_path / "notarepo"
    plain_dir.mkdir()

    result = check(root=plain_dir)

    assert result["ok"] is True
    assert result["git_unavailable"] is True
    assert result["behind_by_commits"] == 0
    assert result["warn"] is False


# ════════════════════════════════════════════════════════════════════════════════════════════════
# (5) Only 'main' visible (no 'master') and vice versa
# ════════════════════════════════════════════════════════════════════════════════════════════════

def test_main_branch_discovered_when_master_absent(tmp_path):
    """check() must discover 'main' when 'master' doesn't exist."""
    repo = _init_repo(tmp_path, branch_name="main")
    result = check(root=repo)

    assert result["master_ref"] == "main"
    assert result["ok"] is True


def test_no_known_branch_returns_ok(tmp_path):
    """When neither 'master' nor 'main' exists (orphan checkout), return ok=True gracefully."""
    repo = _init_repo(tmp_path, branch_name="develop")
    result = check(root=repo)

    # Neither master nor main resolved → no clamp, no raise
    assert result["ok"] is True
    assert result["master_ref"] is None
    assert result["behind_by_commits"] == 0


# ════════════════════════════════════════════════════════════════════════════════════════════════
# (6) daily.py wiring test — step-0a surfaces deploy_lag in the output
# ════════════════════════════════════════════════════════════════════════════════════════════════

def test_daily_exposes_deploy_lag_key(monkeypatch):
    """run_daily() must include 'deploy_lag' in its output dict (wiring test, no live git needed).

    We monkeypatch check() to return a known result; the test just verifies the output key
    is present and carries the returned value — not that git actually ran.
    """
    from scripts import check_deploy_lag as cdl
    sentinel = {"ok": True, "behind_by_commits": 0, "lag_hours": 0.0, "warn": False,
                "message": "test stub", "git_unavailable": False}
    monkeypatch.setattr(cdl, "check", lambda **kw: sentinel, raising=False)

    # Stub out the heavy steps so the daily loop finishes fast
    monkeypatch.setenv("MASTERMIND_RESEARCH_LLM", "0")

    from data_layer import macro_refresh as mr
    monkeypatch.setattr(mr, "refresh_and_check", lambda: {"ok": True, "stub": True}, raising=False)

    from bot import phase2
    monkeypatch.setattr(phase2, "run", lambda **kw: {"ran": False, "reason": "stub"}, raising=False)

    from bot.daily import run_daily
    out = run_daily(asof="2026-07-02", force=False, armed=False)

    assert "deploy_lag" in out, "run_daily() output must contain 'deploy_lag' key"
    assert out["deploy_lag"].get("message") == "test stub"


# ════════════════════════════════════════════════════════════════════════════════════════════════
# Return value contract: all required keys always present
# ════════════════════════════════════════════════════════════════════════════════════════════════

_REQUIRED_KEYS = {
    "ok", "behind_by_commits", "oldest_unshipped_commit_age_h", "lag_hours",
    "head_sha", "master_sha", "master_ref", "git_unavailable", "warn", "message",
}

@pytest.mark.parametrize("branch", ["master", "main"])
def test_return_keys_always_present(tmp_path, branch):
    """check() must always return the complete contract dict regardless of branch name."""
    repo = _init_repo(tmp_path, branch_name=branch)
    result = check(root=repo)
    missing = _REQUIRED_KEYS - set(result.keys())
    assert not missing, f"check() result missing keys: {missing}"


def test_return_keys_present_on_git_unavailable(tmp_path):
    """Even when git is unavailable, all contract keys must be present."""
    plain_dir = tmp_path / "notarepo"
    plain_dir.mkdir()
    result = check(root=plain_dir)
    missing = _REQUIRED_KEYS - set(result.keys())
    assert not missing, f"git-unavailable result missing keys: {missing}"
