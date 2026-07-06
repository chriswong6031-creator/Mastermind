"""Publish the Mastermind dashboard snapshot to the public Macro Dashboard.

Builds ``site/mastermind/mastermind_snapshot.json`` (via ``bridge.macro_snapshot``) inside
the macro repo working tree — reached through the ``vendor/macro`` symlink — then commits +
pushes it to the macro ``origin/main`` so GitHub Pages serves the fresh snapshot.

Mastermind itself has NO git remote; the macro repo does. The snapshot lives in its own
``site/mastermind/`` path so it rebases cleanly against the daily engine's ``site/`` commits
(the same isolation the macro ``factor_series`` job relies on). The push uses the macro
daily.yml rebase-retry loop (5 attempts, ``pull --rebase -X theirs``).

The scheduler calls ``run()`` twice a day (see ``app/scheduler.py``). Manual use:

    python -m scripts.export_macro_snapshot              # build + commit + push
    python -m scripts.export_macro_snapshot --no-push    # build only (dry run / tests)
    python -m scripts.export_macro_snapshot --dest /path/to/site   # write elsewhere, no push

SAFETY: it only commits/pushes when the macro checkout is on ``main`` — never a feature
branch. Resilient: it logs and returns rather than raising into the scheduler.
"""
from __future__ import annotations

import argparse
import subprocess
import time
from pathlib import Path

import bot  # noqa: F401  -> vendor/macro bootstrap

from bridge import macro_snapshot
from bridge import nw_feedback as _nw_feedback

_ROOT = Path(__file__).resolve().parent.parent
_REL_PATH = "site/mastermind/mastermind_snapshot.json"
_NW_REL_PATH = "site/mastermind/nw_feedback.json"

# Bot identity for the snapshot commit (matches the macro daily.yml convention).
_GIT_NAME = "mastermind-bot"
_GIT_EMAIL = "actions@users.noreply.github.com"


def _macro_root() -> Path | None:
    """Resolve the macro repo working tree behind vendor/macro (follows the symlink)."""
    vendor = _ROOT / "vendor" / "macro"
    try:
        root = vendor.resolve()
    except Exception:
        return None
    return root if (root / ".git").exists() else None


def _git(root: Path, *args: str, check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(root), *args],
                          capture_output=True, text=True, check=check)


def _current_branch(root: Path) -> str:
    return _git(root, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()


def _commit_and_push(root: Path) -> bool:
    """Commit the snapshot and push to origin/main with a rebase-retry loop.

    Returns True on a successful push (or a clean no-op), False otherwise. Never raises.
    """
    branch = _current_branch(root)
    if branch != "main":
        print(f"[export_macro_snapshot] macro checkout on '{branch}', not 'main' — "
              f"wrote snapshot but SKIPPING commit/push (won't touch a feature branch).")
        return False

    # Stage only the snapshot file (-f: site/ subtrees are partly gitignored in the macro repo).
    _git(root, "add", "-f", _REL_PATH)
    # Stage nw_feedback.json alongside — best-effort: absent/error must never abort the snapshot push.
    try:
        nw_path = root / _NW_REL_PATH
        if nw_path.exists():
            _git(root, "add", "-f", _NW_REL_PATH)
    except Exception as _exc:
        print(f"[export_macro_snapshot] nw_feedback stage skipped (non-fatal): {_exc}")
    staged = _git(root, "diff", "--cached", "--quiet")
    if staged.returncode == 0:
        print("[export_macro_snapshot] no snapshot changes to commit.")
        return True

    stamp = macro_snapshot.build().get("generated_at", "")
    commit = _git(root, "-c", f"user.name={_GIT_NAME}", "-c", f"user.email={_GIT_EMAIL}",
                  "commit", "-m", f"mastermind: dashboard snapshot {stamp}")
    if commit.returncode != 0:
        print(f"[export_macro_snapshot] commit failed: {commit.stderr.strip()}")
        return False

    for i in range(1, 6):
        pull = _git(root, "pull", "--rebase", "-X", "theirs", "origin", "main")
        if pull.returncode == 0:
            push = _git(root, "push")
            if push.returncode == 0:
                print(f"[export_macro_snapshot] pushed snapshot on attempt {i}.")
                return True
        _git(root, "rebase", "--abort")  # best-effort; harmless if no rebase in progress
        print(f"[export_macro_snapshot] push attempt {i} lost a race; re-syncing.")
        time.sleep(i * 7)
    print("[export_macro_snapshot] could not push after 5 attempts (non-fatal — "
          "next run re-syncs; the site keeps serving the last pushed snapshot).")
    return False


def run(no_push: bool = False, dest: str | Path | None = None) -> Path | None:
    """Build the snapshot and (unless no_push/custom dest) push it to the macro repo.

    Returns the written path, or None on a write failure. Never raises (scheduler-safe).
    """
    root = _macro_root()
    # Auto-push only the default write into the macro working tree; a custom --dest or
    # --no-push is build-only.
    push = (not no_push) and (dest is None)
    try:
        if push and root is not None:
            dest = root / "site"   # write straight into the macro tree so the commit picks it up
        out = macro_snapshot.write(dest)
        print(f"[export_macro_snapshot] wrote {out}")
    except Exception as exc:
        print(f"[export_macro_snapshot] snapshot build/write failed (non-fatal): {exc}")
        return None

    if not push:
        if no_push:
            print("[export_macro_snapshot] --no-push: skipped commit/push.")
        return out
    if root is None:
        print("[export_macro_snapshot] no macro git checkout behind vendor/macro — "
              "wrote snapshot but skipped push.")
        return out

    try:
        _commit_and_push(root)
    except Exception as exc:
        print(f"[export_macro_snapshot] push failed (non-fatal): {exc}")
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Publish the Mastermind snapshot to the Macro Dashboard.")
    ap.add_argument("--no-push", action="store_true", help="build + write only; never commit/push")
    ap.add_argument("--dest", default=None,
                    help="write the snapshot under this site/ dir instead of the macro repo (implies no push)")
    args = ap.parse_args(argv)
    run(no_push=args.no_push, dest=args.dest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
