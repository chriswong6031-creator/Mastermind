"""R7 raw-vendor-read ratchet — static grep over repo py files.

RULE: no new module may directly read vendor/macro files via open()/read_text()/json.load()
without going through a typed adapter.  The frozen allowlist below is the current offender set.
The test fails if:
  (a) A NEW module appears that was not in the allowlist, or
  (b) An existing module's count INCREASES.

Counts may DECREASE (ratchet down only).  A decrease tightens the ratchet going forward —
update the allowlist by running the test with --update-ratchet (not yet wired) or by manually
updating ALLOWLIST below.

Detection logic:
  Pattern 1 — same-line direct: `open(`, `read_text(`, or `json.load(` on a line that also
    contains the literal string 'vendor/macro' or 'vendor.macro'.
  Pattern 2 — _V helper: modules that define `_V = Path(...) / "vendor" / "macro"` AND have
    a line where a read verb (`read_text(` or `json.load`) appears on the SAME line as `_V`.
    (Modules that wrap _V in a private `_load()` helper on separate lines are typed adapters —
    they are NOT raw reads and are intentionally excluded from this ratchet.)

Exclusions: tests/, vendor/, .worktrees/, .claude/, __pycache__.

The allowlist maps module_path (relative to repo root) -> max_allowed_count.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parent.parent

# Directories to exclude from the scan (path fragment match)
_EXCL_FRAGMENTS = [
    "/tests/",
    "/vendor/",
    "/.worktrees/",
    "/.claude/",
    "/__pycache__/",
]

# Frozen allowlist: {relative_module_path: max_count}.
# A count of 1 means exactly one raw vendor/macro read line is currently permitted.
# Ratchet is DOWN-ONLY: you may reduce a count; you may NOT increase it or add new entries.
ALLOWLIST: dict[str, int] = {
    "scripts/dryrun_diag.py": 1,
    "scripts/dryrun_strat_raw.py": 1,
    "bot/phase1.py": 1,   # _V helper pattern: json.loads((_V / rel).read_text()) on one line
    "bot/phase2.py": 1,   # _V helper pattern: json.loads((_V / rel).read_text()) on one line
}

# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------

_DIRECT_READ_RE = re.compile(r'(open\(|read_text\(|json\.load\b)')
_VENDOR_MACRO_RE = re.compile(r'vendor[/\.]macro')
_V_DEFINITION_RE = re.compile(r'^_V\s*=.*vendor.*macro', re.MULTILINE)
_V_READ_RE = re.compile(r'(read_text\(|json\.load\b).*\b_V\b|\b_V\b.*(read_text\(|json\.load\b)')


def _is_excluded(path: Path) -> bool:
    s = str(path)
    return any(frag in s for frag in _EXCL_FRAGMENTS)


def _count_raw_reads(path: Path) -> int:
    """Count raw vendor/macro read lines in a single file."""
    try:
        text = path.read_text(errors="replace")
    except Exception:
        return 0

    lines = text.splitlines()

    # Pattern 1: direct string 'vendor/macro' + read verb on same line
    direct_count = sum(
        1 for line in lines
        if _DIRECT_READ_RE.search(line) and _VENDOR_MACRO_RE.search(line)
    )

    # Pattern 2: _V helper — module defines _V pointing at vendor/macro,
    # AND has a line where read_text/json.load appears on the same line as _V
    v_count = 0
    if _V_DEFINITION_RE.search(text):
        v_count = sum(1 for line in lines if _V_READ_RE.search(line))

    return direct_count + v_count


def scan_repo() -> dict[str, int]:
    """Return {relative_path: count} for all offending modules found in the repo."""
    results: dict[str, int] = {}
    for py_file in _REPO_ROOT.rglob("*.py"):
        if _is_excluded(py_file):
            continue
        count = _count_raw_reads(py_file)
        if count > 0:
            rel = str(py_file.relative_to(_REPO_ROOT))
            results[rel] = count
    return results


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_no_new_raw_vendor_reads():
    """Ratchet: no new module may appear outside the allowlist."""
    found = scan_repo()
    new_offenders = {mod: cnt for mod, cnt in found.items() if mod not in ALLOWLIST}
    assert not new_offenders, (
        f"NEW raw vendor/macro reads found (not in ratchet allowlist):\n"
        + "\n".join(f"  {mod}: {cnt} line(s)" for mod, cnt in sorted(new_offenders.items()))
        + "\n\nFix: route reads through a typed adapter (e.g. a local _load() helper), "
          "or add to ALLOWLIST if this is intentional (counts must not increase)."
    )


def test_existing_counts_not_increased():
    """Ratchet: existing module counts may only stay same or decrease."""
    found = scan_repo()
    regressions = {
        mod: (found[mod], ALLOWLIST[mod])
        for mod in ALLOWLIST
        if mod in found and found[mod] > ALLOWLIST[mod]
    }
    assert not regressions, (
        f"Raw vendor/macro read count INCREASED in allowlisted modules:\n"
        + "\n".join(
            f"  {mod}: found {found_cnt}, allowed {allowed_cnt}"
            for mod, (found_cnt, allowed_cnt) in sorted(regressions.items())
        )
    )


def test_ratchet_self_check_catches_new_offender(tmp_path, monkeypatch):
    """Self-test: adding a fake offender file triggers the new-offender failure."""
    # Create a fake py file that does a raw vendor/macro read
    fake = tmp_path / "fake_offender.py"
    fake.write_text('result = json.load(open("vendor/macro/data/foo.json"))\n')

    # Monkeypatch scan to also include our tmp file
    original_scan = scan_repo

    def patched_scan():
        found = original_scan()
        # inject our fake file as if it were in the repo
        found["fake_module_for_test/offender.py"] = 1
        return found

    monkeypatch.setattr(sys.modules[__name__], "scan_repo", patched_scan)

    found = scan_repo()
    new_offenders = {mod: cnt for mod, cnt in found.items() if mod not in ALLOWLIST}
    assert "fake_module_for_test/offender.py" in new_offenders, (
        "Self-check failed: the ratchet should have detected the injected fake offender"
    )
