#!/opt/homebrew/Caskroom/miniconda/base/bin/python3
"""Stage Mastermind runtime state outside ``~/Documents`` for the VPS rsync.

The canonical bot runs under launchd.  Its Python executable can read the repo,
but child command-line tools can lose that macOS Documents permission and see a
stale or empty source tree while still exiting successfully.  Copying through
this trusted Python process into ``/private/tmp`` gives rsync a TCC-safe source.

The stage is additive, matching the VPS mirror contract: deleted local files are
not removed from the stage or the public mirror.  Unchanged files are not copied
again, so the recurring 15-minute job only pays for changed state.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import sys
import time
from pathlib import Path


def _excluded(name: str) -> bool:
    return (
        name == ".DS_Store"
        or name == "scheduler.sqlite"
        or name.startswith("scheduler.sqlite-")
        or name.endswith(".lock")
    )


def _same_file(source: Path, staged: Path) -> bool:
    try:
        src = source.stat()
        dst = staged.stat()
    except FileNotFoundError:
        return False
    return src.st_size == dst.st_size and src.st_mtime_ns == dst.st_mtime_ns


def stage(source: Path, destination: Path) -> str:
    """Incrementally copy ``source`` into ``destination`` and return a probe token."""
    source = source.resolve()
    destination = destination.resolve()
    if source == destination or source in destination.parents:
        raise ValueError("VPS stage destination must be outside the canonical data tree")

    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    destination.chmod(0o700)
    for root, dirs, files in os.walk(source, followlinks=False):
        dirs[:] = [name for name in dirs if not _excluded(name)]
        src_root = Path(root)
        rel = src_root.relative_to(source)
        dst_root = destination / rel
        dst_root.mkdir(parents=True, exist_ok=True)
        dst_root.chmod(0o700)

        for name in files:
            if _excluded(name):
                continue
            src = src_root / name
            dst = dst_root / name
            try:
                if src.is_symlink():
                    target = os.readlink(src)
                    if dst.is_symlink() and os.readlink(dst) == target:
                        continue
                    if dst.exists() or dst.is_symlink():
                        dst.unlink()
                    dst.symlink_to(target)
                elif not _same_file(src, dst):
                    shutil.copy2(src, dst)
            except FileNotFoundError:
                # Runtime writers may atomically replace a temporary file while
                # the stage walk is in progress. The next 15-minute tick catches it.
                continue

    token = f"{time.time_ns()}-{os.getpid()}"
    (destination / ".vps_sync_token").write_text(token + "\n")

    # The token proves that this exact rsync reached the expected host. The
    # manifest additionally proves that every user-visible book snapshot made
    # it intact, rather than trusting rsync's exit code alone.
    snapshots = [destination / "portfolio" / "latest.json"]
    snapshots.extend(sorted((destination / "portfolios").glob("*/latest.json")))
    manifest: list[str] = []
    for snapshot in snapshots:
        if not snapshot.is_file():
            continue
        digest = hashlib.sha256(snapshot.read_bytes()).hexdigest()
        manifest.append(f"{digest}  {snapshot.relative_to(destination).as_posix()}")
    if not manifest:
        raise RuntimeError("VPS stage contains no portfolio latest.json snapshots")
    (destination / ".vps_sync_manifest.sha256").write_text("\n".join(manifest) + "\n")
    return token


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 2:
        print("usage: stage_state_for_vps.py SOURCE DESTINATION", file=sys.stderr)
        return 2
    print(stage(Path(args[0]), Path(args[1])))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
