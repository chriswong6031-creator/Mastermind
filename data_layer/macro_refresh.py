"""Keep the vendored macro-analyzer data FRESH (and refuse to trade on stale reads).

The Mastermind engine reads the macro analyzer's per-stock + board JSON from `vendor/macro/site/`.
Historically `vendor/macro` symlinked a hand-maintained checkout that silently drifted STALE (a
detached, 200+-commits-behind tree), so the engine made buy decisions on days-old reads — e.g. it
bought NVDA off a "Constructive / 50% / building a base" snapshot days after the live analyzer had
flipped NVDA to "avoid / 0% / wait for a base". See docs/case_studies/2026-06-22-avgo-nvda-override.

This module pins `vendor/macro` to a dedicated, build-managed sparse checkout of just `site/` at
origin/main — which IS the data the live GitHub-Pages site serves — refreshed once per build, with a
staleness TRIPWIRE so a build can refuse (opt-in) to trade on stale macro reads rather than do so
silently. The git pull is one bulk operation (vs hundreds of per-file HTTP reads) and keeps the hot
read path local.

--- ANCHOR CONTRACT (2026-07-01 hardening) ---

The original tripwire anchored on `site/stockdata/SPY.json` and `site/stockdata/NVDA.json`, but
`site/stockdata/` does NOT exist on origin/main — it is a 1,684-file publish gap that has never been
committed to the macro repo's remote (see audit finding `intake-regime-stale-anchor-gap`).  With 2 of
3 anchors perpetually absent the staleness check silently degraded to a single-file read of
`us_standouts.json`, hiding the gap entirely.

Replacement anchors — verified to exist on origin/main as of 2026-07-01, each representing a
different critical data domain the bot relies on:

  1. site/factordata/us_standouts.json  — the standout board the conviction sleeve reads every build
                                          date field: "as_of"
  2. data/regime/latest.json            — the macro regime the run-gate keys on
                                          date field: "date"
  3. site/sectordata/sector_cycles.json — the cycle-phase data (consumed by zero bot code today, but
                                          the audit flags it as a critical missing input; including it
                                          here guarantees the runlog surfaces its freshness)
                                          date field: meta["asOf"]

The `asof()` function now returns the MINIMUM (oldest) date across all resolvable anchors, because
the engine is only as fresh as its stalest critical input.  `anchors_report()` exposes per-anchor
resolution for debugging.

`site/stockdata/` (the known publish gap) is always checked and reported in `data_gaps` — so the
runlog surfaces it loudly on every build, even when everything else is fresh.
"""
from __future__ import annotations

import json
import os
import subprocess
from datetime import date, datetime
from pathlib import Path
from typing import NamedTuple

_ROOT = Path(__file__).resolve().parent.parent
_SRC = _ROOT / "vendor" / "macro_src"            # the dedicated checkout (gitignored)
_REMOTE = "https://github.com/chriswong6031-creator/macro.git"

_MAX_AGE_DAYS = 2

# ---------------------------------------------------------------------------
# Anchor contracts
#
# Each entry describes one critical file on origin/main and how to extract its
# freshness date.  "reader" is a callable (dict) -> str | None that pulls the
# date string from the parsed JSON.  Keeping the reader inline (not a lambda
# in a loop) avoids the classic closure-capture bug.
# ---------------------------------------------------------------------------

class _AnchorDef(NamedTuple):
    rel: str                          # path relative to _SRC
    reader: object                    # Callable[[dict], str | None]
    label: str                        # human-readable name for logs / reports


def _read_standouts_date(d: dict) -> str | None:
    # site/factordata/us_standouts.json → "as_of"
    return d.get("as_of") or d.get("asof") or d.get("generated_at")


def _read_regime_date(d: dict) -> str | None:
    # data/regime/latest.json → "date" (not "as_of"; different schema)
    return d.get("date") or d.get("as_of") or d.get("asof") or d.get("generated_at")


def _read_sector_cycles_date(d: dict) -> str | None:
    # site/sectordata/sector_cycles.json → nested at meta["asOf"]  (camelCase)
    meta = d.get("meta", {}) if isinstance(d, dict) else {}
    return (meta.get("asOf") or meta.get("as_of") or meta.get("date")
            or d.get("as_of") or d.get("asof") or d.get("generated_at"))


_ANCHOR_DEFS: tuple[_AnchorDef, ...] = (
    _AnchorDef(
        rel="site/factordata/us_standouts.json",
        reader=_read_standouts_date,
        label="us_standouts",
    ),
    _AnchorDef(
        rel="data/regime/latest.json",
        reader=_read_regime_date,
        label="regime_latest",
    ),
    _AnchorDef(
        rel="site/sectordata/sector_cycles.json",
        reader=_read_sector_cycles_date,
        label="sector_cycles",
    ),
)

# Convenience tuple kept for callers that only need the paths (e.g. quick existence checks).
_ANCHORS: tuple[str, ...] = tuple(a.rel for a in _ANCHOR_DEFS)

# The known publish gap: site/stockdata/ is never committed to origin/main (1,684 per-name JSON
# files exist only on the macro repo's local main).  We always check for it and report it in
# data_gaps so the runlog surfaces the gap loudly on every build.
_STOCKDATA_GAP_REL = "site/stockdata"


def _run(args: list[str], cwd: Path | None = None, timeout: int = 240):
    return subprocess.run(args, cwd=str(cwd) if cwd else None,
                          capture_output=True, text=True, timeout=timeout)


def ensure_clone() -> bool:
    """Create the sparse `site/` checkout if it is missing. Returns whether site/ is present."""
    if (_SRC / "site").is_dir():
        return True
    try:
        _SRC.parent.mkdir(parents=True, exist_ok=True)
        r = _run(["git", "clone", "--depth", "1", "--filter=blob:none", "--sparse", _REMOTE, str(_SRC)],
                 timeout=900)
        if r.returncode == 0:
            _run(["git", "sparse-checkout", "set", "site"], cwd=_SRC)
    except Exception:
        pass
    return (_SRC / "site").is_dir()


def refresh() -> str | None:
    """Pull the latest site/ data from origin/main (== the live site). Returns the data `asof` on
    success, or None on failure — leaving the last-good cached checkout intact. Never raises."""
    try:
        if not ensure_clone():
            return None
        f = _run(["git", "fetch", "--depth", "1", "origin", "main"], cwd=_SRC)
        if f.returncode != 0:
            return None                                  # network down -> keep last-good data
        _run(["git", "reset", "--hard", "origin/main"], cwd=_SRC)
        _run(["git", "sparse-checkout", "reapply"], cwd=_SRC)
        return asof()
    except Exception:
        return None


def anchors_report() -> dict[str, str | None]:
    """Per-anchor freshness dates for debugging and runlog surfacing.

    Returns a dict keyed by anchor label mapping to the resolved date string (YYYY-MM-DD)
    or None when the file is absent/unreadable.  This lets callers log which specific input
    is stale or missing, rather than seeing only the minimum.
    """
    report: dict[str, str | None] = {}
    for anchor in _ANCHOR_DEFS:
        p = _SRC / anchor.rel
        resolved: str | None = None
        try:
            if p.exists():
                d = json.loads(p.read_text())
                raw = anchor.reader(d)
                if raw:
                    resolved = str(raw)[:10]
        except Exception:
            pass
        report[anchor.label] = resolved
    return report


def asof() -> str | None:
    """The vendored macro data freshness date (YYYY-MM-DD), or None.

    Returns the MINIMUM (oldest) date across all resolvable anchors — the engine is only as
    fresh as its stalest critical input.  If no anchor resolves, returns None.

    WHY minimum, not first-hit: the original code returned the first anchor that had a date.
    With 2 of 3 anchors perpetually absent, that silently narrowed to us_standouts alone,
    hiding staleness in regime/latest or sector_cycles.  Minimum forces the caller to confront
    the oldest ingredient in the data pipeline.
    """
    dates: list[str] = []
    for anchor in _ANCHOR_DEFS:
        p = _SRC / anchor.rel
        try:
            if p.exists():
                d = json.loads(p.read_text())
                raw = anchor.reader(d)
                if raw:
                    dates.append(str(raw)[:10])
        except Exception:
            continue
    if not dates:
        return None
    # Lexicographic minimum is correct for ISO-8601 YYYY-MM-DD strings
    return min(dates)


def is_stale(max_age_days: int = _MAX_AGE_DAYS, today: date | None = None) -> bool | None:
    """True if the vendored macro data is older than `max_age_days`. None if the date is unreadable
    (don't block on an unknown — staleness is only asserted when we can prove it)."""
    a = asof()
    if not a:
        return None
    try:
        d = datetime.strptime(a, "%Y-%m-%d").date()
    except Exception:
        return None
    return ((today or date.today()) - d).days > max_age_days


def _collect_data_gaps() -> list[str]:
    """Identify expected-but-missing contracts on every build.

    Always includes a check for `site/stockdata/` (the known publish gap) so the runlog
    surfaces it loudly on every run.  Any anchor file that is absent also lands here.

    Returns a list of relative paths that are expected but absent.
    """
    gaps: list[str] = []
    # Always check the known stockdata publish gap first — this is the highest-severity missing
    # contract (it causes the 6-dim confluence gate to fail open; see audit finding C1 /
    # `missing-stockdata-degenerate-confluence`).
    if not (_SRC / _STOCKDATA_GAP_REL).is_dir():
        gaps.append(_STOCKDATA_GAP_REL)
    # Also surface any anchor files that are absent — these degrade the staleness check itself
    for anchor in _ANCHOR_DEFS:
        if not (_SRC / anchor.rel).exists():
            gaps.append(anchor.rel)
    return gaps


def check_and_warn(*, block: bool = False, log=print) -> dict:
    """Staleness tripwire. Logs a loud warning when the vendored macro data is stale; if `block`,
    raises RuntimeError so a build refuses to trade on stale reads (the NVDA-Constructive-vs-avoid
    class of bug). Returns an info dict either way.

    The returned dict always includes:
      asof          — minimum date across resolvable anchors (YYYY-MM-DD or None)
      stale         — bool (False when unknown, matching original behaviour)
      max_age_days  — the threshold applied
      data_gaps     — list of expected-but-missing paths; non-empty = loud surface needed
    """
    a = asof()
    stale = is_stale()
    gaps = _collect_data_gaps()
    report = anchors_report()

    info: dict = {
        "asof": a,
        "stale": bool(stale),
        "max_age_days": _MAX_AGE_DAYS,
        "data_gaps": gaps,
        "anchors": report,
    }

    if stale:
        msg = (f"[macro_refresh] STALE vendored macro data: asof={a} (> {_MAX_AGE_DAYS}d old). "
               f"Engine reads may be wrong — set MACRO_STALE_BLOCK=1 to refuse trading on stale data. "
               f"Per-anchor dates: {report}")
        log(msg)
        if block:
            raise RuntimeError(msg)

    if gaps:
        # Emit a distinct loud warning for every missing contract so it surfaces in the runlog
        # independently of the staleness warning.  site/stockdata/ in particular collapses the
        # 6-dim confluence gate to altdata-only (all names score confluence=1.0, price=None).
        gap_msg = (f"[macro_refresh] DATA GAPS — expected contracts absent from vendor/macro_src: "
                   f"{gaps}. "
                   f"site/stockdata/ missing => lenses.full() fails open (all names confluence=1.0). "
                   f"These gaps will NOT self-heal until the macro engine publishes them to origin/main.")
        log(gap_msg)

    return info


def refresh_and_check(log=print) -> dict:
    """Build entry point: pull fresh macro data, then run the staleness tripwire. `block` on stale
    is opt-in via MACRO_STALE_BLOCK=1. Never raises unless blocking is enabled AND data is stale."""
    new_asof = refresh()
    if new_asof:
        log(f"[macro_refresh] vendored macro data refreshed to asof={new_asof}")
    else:
        log("[macro_refresh] refresh skipped/failed — reading last-good cached macro data")
    info = check_and_warn(block=(os.environ.get("MACRO_STALE_BLOCK", "0") == "1"), log=log)
    info["refreshed_to"] = new_asof
    return info
