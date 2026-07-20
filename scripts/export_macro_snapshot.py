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
# Cost summary — lives in data/ (NOT site/) so it is NOT served publicly.
_COST_REL_PATH = "data/mastermind/cost_summary.json"
# Key events — filtered copy of the key ledger for the macro AI Cost admin tab.
_KEY_EVENTS_REL_PATH = "data/mastermind/key_events.jsonl"
# Key pool status — the bot's CURRENT pool view (mastermind.key_pool_status.v1) that the
# macro/admin side reads to render live cooling/dead state alongside key_events.
_KEY_POOL_STATUS_REL_PATH = "data/mastermind/key_pool_status.json"
# Ledger source path relative to Mastermind root
_KEY_LEDGER_REL = "data/metabolism/key_ledger.jsonl"
# Rolling window for key events export (days)
_KEY_EVENTS_WINDOW_DAYS = 14
# Max rows to export (most-recent)
_KEY_EVENTS_MAX_ROWS = 20_000

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


def _write_key_events(macro_root: Path) -> None:
    """Write a filtered copy of the key ledger to <macro_root>/data/mastermind/key_events.jsonl.

    Filters to rows with parseable ts within the last 14 days, capped at 20 000 most-recent
    rows.  Skips silently (logs one line) when the ledger is missing or empty.  Never raises.
    """
    import json as _json
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz

    try:
        ledger_path = _ROOT / _KEY_LEDGER_REL
        if not ledger_path.exists():
            print("[export_macro_snapshot] key ledger absent — key_events export skipped.")
            return
        raw = ledger_path.read_text(encoding="utf-8").splitlines()
        if not raw:
            print("[export_macro_snapshot] key ledger empty — key_events export skipped.")
            return

        cutoff = _dt.now(_tz.utc) - _td(days=_KEY_EVENTS_WINDOW_DAYS)
        kept: list[str] = []
        for line in raw:
            line = line.strip()
            if not line:
                continue
            try:
                row = _json.loads(line)
            except Exception:
                continue
            ts_raw = row.get("ts", "")
            try:
                if ts_raw.endswith("Z"):
                    ts_raw = ts_raw[:-1] + "+00:00"
                ts = _dt.fromisoformat(ts_raw).astimezone(_tz.utc)
            except Exception:
                continue  # unparseable ts → drop
            if ts >= cutoff:
                kept.append(line)

        if not kept:
            print("[export_macro_snapshot] key ledger has no rows within the last "
                  f"{_KEY_EVENTS_WINDOW_DAYS} days — key_events export skipped.")
            return

        # Cap at most-recent _KEY_EVENTS_MAX_ROWS rows
        if len(kept) > _KEY_EVENTS_MAX_ROWS:
            kept = kept[-_KEY_EVENTS_MAX_ROWS:]

        dest = macro_root / _KEY_EVENTS_REL_PATH
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text("\n".join(kept) + "\n", encoding="utf-8")
        print(f"[export_macro_snapshot] wrote key_events ({len(kept)} rows) to {dest}")
    except Exception as exc:
        print(f"[export_macro_snapshot] key_events write skipped (non-fatal): {exc}")


def _write_pool_status(macro_root: Path) -> None:
    """Write the bot's current key-pool view to <macro_root>/data/mastermind/key_pool_status.json.

    Cheap rebuild from the bot's own ledger tail via key_rotor.pool_status_view.  The macro/admin
    side reads this (mastermind.key_pool_status.v1) to show live cooling/dead pool state.  Never
    raises (a publish miss must never abort the snapshot push).
    """
    try:
        from brain import key_rotor
        dest = macro_root / _KEY_POOL_STATUS_REL_PATH
        # root=_ROOT so the view reads the bot's live ledger + env; dest is inside the macro tree.
        written = key_rotor.write_pool_status(dest=dest, root=_ROOT)
        if written:
            print(f"[export_macro_snapshot] wrote key_pool_status to {written}")
        else:
            print("[export_macro_snapshot] key_pool_status write returned None (non-fatal).")
    except Exception as exc:
        print(f"[export_macro_snapshot] key_pool_status write skipped (non-fatal): {exc}")


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
    # Stage cost_summary.json — best-effort: absent/error must never abort the snapshot push.
    # data/mastermind/ paths sit OUTSIDE the vendored clone's cone-mode sparse
    # checkout (cone = data/{china_regime,regime,risk_radar,yahoo} + engine/lib/site),
    # so a plain `git add -f` silently refuses to stage them and _git() does not
    # raise on rc!=0 — cost_summary.json never reached origin/main until the
    # `--sparse` flag was added here.  Keep the rc check loud.
    try:
        cost_path = root / _COST_REL_PATH
        if cost_path.exists():
            _r = _git(root, "add", "--sparse", "-f", _COST_REL_PATH)
            if _r.returncode != 0:
                print(f"[export_macro_snapshot] cost_summary stage FAILED rc={_r.returncode}: "
                      f"{(_r.stderr or '').strip()[:200]}")
    except Exception as _exc:
        print(f"[export_macro_snapshot] cost_summary stage skipped (non-fatal): {_exc}")
    # Stage key_events.jsonl — best-effort: absent/error must never abort the snapshot push.
    try:
        ke_path = root / _KEY_EVENTS_REL_PATH
        if ke_path.exists():
            _r = _git(root, "add", "--sparse", "-f", _KEY_EVENTS_REL_PATH)
            if _r.returncode != 0:
                print(f"[export_macro_snapshot] key_events stage FAILED rc={_r.returncode}: "
                      f"{(_r.stderr or '').strip()[:200]}")
    except Exception as _exc:
        print(f"[export_macro_snapshot] key_events stage skipped (non-fatal): {_exc}")
    # Stage key_pool_status.json — same cone-mode caveat as key_events (data/mastermind/ is
    # outside the sparse cone, so --sparse is required or `git add` silently refuses it).
    try:
        kps_path = root / _KEY_POOL_STATUS_REL_PATH
        if kps_path.exists():
            _r = _git(root, "add", "--sparse", "-f", _KEY_POOL_STATUS_REL_PATH)
            if _r.returncode != 0:
                print(f"[export_macro_snapshot] key_pool_status stage FAILED rc={_r.returncode}: "
                      f"{(_r.stderr or '').strip()[:200]}")
    except Exception as _exc:
        print(f"[export_macro_snapshot] key_pool_status stage skipped (non-fatal): {_exc}")
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

    # Write cost summary into data/mastermind/ (NOT site/ — never publicly served).
    try:
        from brain import cost_guard as _cg
        cost_dest = (root / _COST_REL_PATH) if (push and root is not None) else None
        if cost_dest is None:
            # dry-run / custom dest: write alongside the snapshot for debugging but skip commit
            cost_dest = (Path(dest).parent.parent / _COST_REL_PATH) if dest else None
        if cost_dest is not None:
            written = _cg.write_cost_summary(cost_dest)
            if written:
                print(f"[export_macro_snapshot] wrote cost summary {written}")
    except Exception as _ce:
        print(f"[export_macro_snapshot] cost summary write skipped (non-fatal): {_ce}")

    # Write key events (filtered ledger copy) + the current pool-status view into macro
    # data/mastermind/.
    if push and root is not None:
        _write_key_events(root)
        _write_pool_status(root)

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
