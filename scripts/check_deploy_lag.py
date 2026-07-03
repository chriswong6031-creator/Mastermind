"""Deploy-lag tripwire — W-I Task 4 (b).

Compares the running checkout's HEAD commit against the repo's master ref to detect
when production is trailing master by more than 24h (the incident class: 4 fixed waves
sat merged on master while production bled on a pre-W0 branch).

Pure, local, no network.  Uses ``git -C <root> rev-parse HEAD`` vs ``git -C <root>
rev-parse master`` (or ``main`` as a fallback), then compares the commit-author dates.
If master is ahead AND the oldest unshipped commit is >24h old, it emits a LOUD runlog
line and writes ``data/deploy_lag.json``.

INVARIANT: never raises; degrades silently if git is unavailable.  A missing git binary,
non-git directory, or any subprocess error → returns ``{"ok": True, "git_unavailable": True}``.

Called from ``bot/daily.py`` step-0 (before the book build) so a stale deployment is
surfaced at the START of every daily run, not buried in the output.

Usage
-----
    from scripts import check_deploy_lag
    result = check_deploy_lag.check()     # idempotent; safe to call repeatedly
    # result["behind_by_commits"] is 0 when production is current
    # result["lag_hours"] is 0.0 when not behind

Standalone:
    python -m scripts.check_deploy_lag
"""
from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# The repo root: scripts/ lives one level below root.
_ROOT: Path = Path(__file__).resolve().parent.parent
_ARTIFACT: Path = _ROOT / "data" / "deploy_lag.json"

# Threshold: if the oldest unshipped commit is older than this, emit the loud warning.
_LAG_WARN_HOURS: float = 24.0

# Branch names to probe for master, in priority order.
_MASTER_CANDIDATES: tuple[str, ...] = ("master", "main")


def _git(*args: str, cwd: Path | None = None) -> str | None:
    """Run a git command, return stdout stripped, or None on any error."""
    try:
        result = subprocess.run(
            ["git", "-C", str(cwd or _ROOT), *args],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            return None
        return result.stdout.strip() or None
    except Exception:  # noqa: BLE001 — no git, wrong dir, subprocess failure → None
        return None


def _commit_timestamp(ref: str, cwd: Path | None = None) -> float | None:
    """Unix timestamp of a commit's author date, or None when unavailable."""
    ts_str = _git("log", "-1", "--format=%at", ref, cwd=cwd)
    if ts_str is None:
        return None
    try:
        return float(ts_str)
    except (TypeError, ValueError):
        return None


def _commits_between(older_ref: str, newer_ref: str, cwd: Path | None = None) -> list[str]:
    """SHAs reachable from newer_ref but not older_ref (commits not yet in the checkout)."""
    raw = _git("rev-list", f"{older_ref}..{newer_ref}", cwd=cwd)
    if raw is None:
        return []
    return [s for s in raw.splitlines() if s.strip()]


def check(root: Path | None = None, *, warn_hours: float = _LAG_WARN_HOURS) -> dict[str, Any]:
    """Compare HEAD against master/main and report any deploy lag.

    Parameters
    ----------
    root : override the repo root (used by tests with a fixture git dir).
    warn_hours : minimum lag (in hours) before emitting the loud warning (default 24).

    Returns
    -------
    {
      "ok": bool,                          # True = no lag or git unavailable
      "behind_by_commits": int,            # 0 when current
      "oldest_unshipped_commit_age_h": float, # hours since oldest unshipped commit; 0.0 if current
      "lag_hours": float,                  # age of the OLDEST unshipped commit (alias)
      "head_sha": str | None,
      "master_sha": str | None,
      "master_ref": str | None,            # "master" or "main" (whichever resolved)
      "git_unavailable": bool,
      "warn": bool,                        # True when lag_hours > warn_hours
      "message": str,
    }
    Never raises.
    """
    cwd = root or _ROOT

    head_sha = _git("rev-parse", "HEAD", cwd=cwd)
    if head_sha is None:
        return _no_git_result()

    # Resolve master / main — try each candidate.
    master_ref: str | None = None
    master_sha: str | None = None
    for candidate in _MASTER_CANDIDATES:
        sha = _git("rev-parse", candidate, cwd=cwd)
        if sha is not None:
            master_ref = candidate
            master_sha = sha
            break

    if master_sha is None:
        # Git works but no master/main branch visible (detached head only checkout, etc.)
        return {
            "ok": True,
            "behind_by_commits": 0,
            "oldest_unshipped_commit_age_h": 0.0,
            "lag_hours": 0.0,
            "head_sha": head_sha,
            "master_sha": None,
            "master_ref": None,
            "git_unavailable": False,
            "warn": False,
            "message": "deploy-lag: master/main ref not found (worktree or shallow clone); skip",
        }

    if head_sha == master_sha:
        result = {
            "ok": True,
            "behind_by_commits": 0,
            "oldest_unshipped_commit_age_h": 0.0,
            "lag_hours": 0.0,
            "head_sha": head_sha,
            "master_sha": master_sha,
            "master_ref": master_ref,
            "git_unavailable": False,
            "warn": False,
            "message": "deploy-lag: production is current (HEAD == master)",
        }
        _write_artifact(result)
        return result

    # Find commits on master not in HEAD.
    unshipped = _commits_between(head_sha, master_sha, cwd=cwd)
    if not unshipped:
        # HEAD is not master but master has no commits ahead of HEAD (diverged, or HEAD ahead)
        result = {
            "ok": True,
            "behind_by_commits": 0,
            "oldest_unshipped_commit_age_h": 0.0,
            "lag_hours": 0.0,
            "head_sha": head_sha,
            "master_sha": master_sha,
            "master_ref": master_ref,
            "git_unavailable": False,
            "warn": False,
            "message": "deploy-lag: HEAD is not behind master (possibly diverged or ahead)",
        }
        _write_artifact(result)
        return result

    n_behind = len(unshipped)

    # Age of the OLDEST unshipped commit (the last in the rev-list = the root of the gap).
    oldest_sha = unshipped[-1]
    oldest_ts = _commit_timestamp(oldest_sha, cwd=cwd)
    now_ts = datetime.now(timezone.utc).timestamp()
    lag_hours = (now_ts - oldest_ts) / 3600.0 if oldest_ts is not None else 0.0

    warn = lag_hours > warn_hours
    msg_prefix = (
        "!!!  DEPLOY-LAG  !!!" if warn
        else "deploy-lag (within threshold)"
    )
    message = (
        f"{msg_prefix}: production HEAD is {n_behind} commit(s) behind {master_ref} "
        f"(oldest unshipped commit is {lag_hours:.1f}h old, "
        f"threshold={warn_hours:.0f}h)"
    )

    result = {
        "ok": not warn,
        "behind_by_commits": n_behind,
        "oldest_unshipped_commit_age_h": round(lag_hours, 2),
        "lag_hours": round(lag_hours, 2),
        "head_sha": head_sha,
        "master_sha": master_sha,
        "master_ref": master_ref,
        "git_unavailable": False,
        "warn": warn,
        "message": message,
    }
    _write_artifact(result)
    return result


def _no_git_result() -> dict[str, Any]:
    """Returned when git is unavailable — never raises, never blocks the build."""
    return {
        "ok": True,
        "behind_by_commits": 0,
        "oldest_unshipped_commit_age_h": 0.0,
        "lag_hours": 0.0,
        "head_sha": None,
        "master_sha": None,
        "master_ref": None,
        "git_unavailable": True,
        "warn": False,
        "message": "deploy-lag: git unavailable — skipping lag check",
    }


def _write_artifact(result: dict[str, Any]) -> None:
    """Write data/deploy_lag.json; never raises."""
    try:
        _ARTIFACT.parent.mkdir(parents=True, exist_ok=True)
        _ARTIFACT.write_text(json.dumps(result, indent=2, default=str))
    except Exception:  # noqa: BLE001
        pass


if __name__ == "__main__":
    r = check()
    if r.get("warn"):
        print(r["message"], file=sys.stderr)
    else:
        print(r["message"])
    sys.exit(1 if r.get("warn") else 0)
