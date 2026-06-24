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
"""
from __future__ import annotations

import json
import os
import subprocess
from datetime import date, datetime
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_SRC = _ROOT / "vendor" / "macro_src"            # the dedicated checkout (gitignored)
_REMOTE = "https://github.com/chriswong6031-creator/macro.git"
# anchor files that always exist and carry a build date; first hit wins
_ANCHORS = ("site/factordata/us_standouts.json", "site/stockdata/SPY.json", "site/stockdata/NVDA.json")
_MAX_AGE_DAYS = 2


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


def asof() -> str | None:
    """The `asof` build date stamped on the vendored macro data (YYYY-MM-DD), or None."""
    for rel in _ANCHORS:
        p = _SRC / rel
        try:
            if p.exists():
                d = json.loads(p.read_text())
                a = d.get("asof") or d.get("as_of") or d.get("generated_at")
                if a:
                    return str(a)[:10]
        except Exception:
            continue
    return None


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


def check_and_warn(*, block: bool = False, log=print) -> dict:
    """Staleness tripwire. Logs a loud warning when the vendored macro data is stale; if `block`,
    raises RuntimeError so a build refuses to trade on stale reads (the NVDA-Constructive-vs-avoid
    class of bug). Returns an info dict either way."""
    a, stale = asof(), is_stale()
    info = {"asof": a, "stale": bool(stale), "max_age_days": _MAX_AGE_DAYS}
    if stale:
        msg = (f"[macro_refresh] STALE vendored macro data: asof={a} (> {_MAX_AGE_DAYS}d old). "
               f"Engine reads may be wrong — set MACRO_STALE_BLOCK=1 to refuse trading on stale data.")
        log(msg)
        if block:
            raise RuntimeError(msg)
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
