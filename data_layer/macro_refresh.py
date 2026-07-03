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

--- R2 DATA PLANE (2026-07-02) ---

On 2026-07-01 the macro repo untracked its heavy per-ticker stores from git (macro commit
2e379484a3, "Inc 4" of its Cloudflare-R2 migration: ~700 MB of per-ticker JSON was pushing the
GitHub-Pages deploy toward the 1 GB limit). `site/stockdata/` et al. now ship to a public R2
bucket via the macro repo's scripts/publish_r2.py, and the live site fetches them through a
client-side shim. Consequence here: the sparse git pull alone no longer materializes
site/stockdata/ — the W0 audit read that post-untrack state as a "never-published gap", but it
was a deliberate migration, and re-tracking it in git is not an option (Pages size limit).

So refresh() now has a second leg: after the git pull, `_sync_r2()` mirrors the per-ticker
stores the bot reads (site/stockdata/) from the R2 public data plane into the same vendored
path. The name list comes from `{dir}/_manifest.json` (published by the macro repo after each
upload batch; the public r2.dev host has no LIST endpoint), falling back to index.json tickers
+ known extras until the first manifest lands. An ETag fast-path skips the ~1,700-file download
when nothing changed; failures keep the last-good local mirror and never raise.

--- ANCHOR CONTRACT (2026-07-01 hardening, R2 leg added 2026-07-02) ---

Anchors, each representing a critical data domain the bot relies on:

  1. site/factordata/us_standouts.json  — the standout board the conviction sleeve reads every build
                                          date field: "as_of" (git leg)
  2. data/regime/latest.json            — the macro regime the run-gate keys on
                                          date field: "date" (git leg; requires data/regime in the
                                          sparse-checkout set — `site` alone never materializes it)
  3. site/sectordata/sector_cycles.json — the cycle-phase data (consumed by zero bot code today, but
                                          the audit flags it as a critical missing input; including it
                                          here guarantees the runlog surfaces its freshness)
                                          date field: meta["asOf"] (git leg)
  4. site/stockdata/SPY.json            — the per-name store behind the 6-dim conviction gate
                                          date field: "asof" (R2 leg — trips the wire if the R2
                                          publish silently stops)

The `asof()` function returns the MINIMUM (oldest) date across all resolvable anchors, because
the engine is only as fresh as its stalest critical input.  `anchors_report()` exposes per-anchor
resolution for debugging.

`site/stockdata/` is always checked and reported in `data_gaps` — so the runlog surfaces an
R2-sync failure loudly on every build, even when everything else is fresh.
"""
from __future__ import annotations

import json
import os
import subprocess
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from pathlib import Path
from typing import NamedTuple

_ROOT = Path(__file__).resolve().parent.parent
_SRC = _ROOT / "vendor" / "macro_src"            # the dedicated checkout (gitignored)
_REMOTE = "https://github.com/chriswong6031-creator/macro.git"
# cone-mode sparse set: `site` alone misses data/regime/latest.json (anchor 2).
# engine/ + lib/ are LOAD-BEARING: the bot imports the macro analyzer as a library
# (CLAUDE.md contract; loop/harness.py does `from engine import active_alloc, validation`,
# lib/store.py is the parquet reader). Narrowing the set to site+regime alone (the R2
# migration) silently broke every engine import in loop/ and the test_smoke canary —
# keep the code trees in the checkout; they are small text. data/yahoo (~250 curated
# parquets) is the engine price store — loop/harness backtests and test_smoke need it.
_SPARSE_PATHS = ("site", "data/regime", "engine", "lib", "data/yahoo", "data/risk_radar",
                  "data/china_regime")  # W-I Task 5(b): CN/HK books' regime read — was missing

_MAX_AGE_DAYS = 2

# --- R2 data plane (see the module docstring) --------------------------------
# The macro repo's public bucket; mirrors the site paths (stockdata/SPY.json).
_R2_BASE = os.environ.get("MACRO_R2_BASE",
                          "https://pub-f7ffb4441c5f4ad983ca56ec7c651c61.r2.dev")
_R2_DIRS = ("stockdata",)                        # per-ticker stores bot code reads today
_R2_EXTRAS = ("index.json", "fund_flows.json")   # non-ticker files (manifest-fallback mode)
_R2_META = ".r2_sync.json"                       # per-dir sync state (manifest ETag)
_R2_TIMEOUT = 30                                 # seconds, per HTTP request
_R2_WORKERS = 16

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


def _read_stockdata_date(d: dict) -> str | None:
    # site/stockdata/SPY.json → "asof" (no underscore; per-name store schema)
    return d.get("asof") or d.get("as_of") or d.get("generated_at")


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
    _AnchorDef(
        rel="site/stockdata/SPY.json",
        reader=_read_stockdata_date,
        label="stockdata_spy",
    ),
)

# Convenience tuple kept for callers that only need the paths (e.g. quick existence checks).
_ANCHORS: tuple[str, ...] = tuple(a.rel for a in _ANCHOR_DEFS)

# site/stockdata/ is untracked in the macro repo (R2 migration, 2026-07-01) so the git leg can
# never produce it — only the R2 leg does.  We always check for it and report it in data_gaps so
# an R2-sync failure surfaces loudly on every build.
_STOCKDATA_GAP_REL = "site/stockdata"


def _run(args: list[str], cwd: Path | None = None, timeout: int = 240):
    return subprocess.run(args, cwd=str(cwd) if cwd else None,
                          capture_output=True, text=True, timeout=timeout)


def ensure_clone() -> bool:
    """Create the sparse checkout (site/ + data/regime) if it is missing. Returns whether site/
    is present."""
    if (_SRC / "site").is_dir():
        return True
    try:
        _SRC.parent.mkdir(parents=True, exist_ok=True)
        r = _run(["git", "clone", "--depth", "1", "--filter=blob:none", "--sparse", _REMOTE, str(_SRC)],
                 timeout=900)
        if r.returncode == 0:
            _run(["git", "sparse-checkout", "set", *_SPARSE_PATHS], cwd=_SRC)
    except Exception:
        pass
    return (_SRC / "site").is_dir()


# --- R2 leg -------------------------------------------------------------------

def _fetch(url: str) -> tuple[bytes, str] | None:
    """GET url -> (body, etag) or None on any error. The single network seam (tests patch it).
    Cloudflare's r2.dev WAF 403s the default Python-urllib User-Agent — send our own."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "mastermind-feed/1.0"})
        with urllib.request.urlopen(req, timeout=_R2_TIMEOUT) as r:
            return r.read(), (r.headers.get("ETag") or "").strip('"')
    except Exception:
        return None


def _r2_names(d: str) -> tuple[list[str], str] | None:
    """The authoritative file list for R2 dir `d`, plus a change-detection tag.

    Prefers {d}/_manifest.json (written by the macro repo's publish_r2.py after every upload
    batch). Falls back to index.json tickers + _R2_EXTRAS until the first manifest lands.
    Returns None when neither is fetchable — the caller keeps the last-good mirror."""
    got = _fetch(f"{_R2_BASE}/{d}/_manifest.json")
    if got:
        try:
            names = [str(n) for n in json.loads(got[0])["files"]]
            if names:
                return names, got[1]
        except Exception:
            pass
    got = _fetch(f"{_R2_BASE}/{d}/index.json")
    if not got:
        return None
    try:
        tickers = [f"{row['t']}.json" for row in json.loads(got[0]) if row.get("t")]
    except Exception:
        return None
    return tickers + list(_R2_EXTRAS), got[1]


def _sync_r2_dir(d: str) -> int | None:
    """Mirror R2 dir `d` into the vendored checkout. Returns files synced (0 = fresh, skipped),
    or None when the sync could not run — the last-good local mirror is left intact."""
    names = _r2_names(d)
    if names is None:
        return None
    names, tag = names
    dest = _SRC / "site" / d
    meta = dest / _R2_META
    try:  # ETag fast-path: same manifest + populated dir -> nothing to do
        if tag and json.loads(meta.read_text()).get("etag") == tag \
                and sum(1 for _ in dest.glob("*.json")) >= len(names):
            return 0
    except Exception:
        pass
    dest.mkdir(parents=True, exist_ok=True)

    def _pull(name: str) -> bool:
        got = _fetch(f"{_R2_BASE}/{d}/{name}")
        if not got:
            return False                      # keep whatever last-good copy exists
        out = dest / name
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_name(f".{out.name}.tmp")
        tmp.write_bytes(got[0])
        tmp.replace(out)
        return True

    with ThreadPoolExecutor(max_workers=_R2_WORKERS) as ex:
        ok = sum(ex.map(_pull, names))
    keep = set(names) | {_R2_META}
    for p in dest.glob("*.json"):             # prune delistings — mirror the manifest exactly
        if p.name not in keep:
            p.unlink(missing_ok=True)
    if ok == len(names) and tag:              # only stamp complete syncs as done
        meta.write_text(json.dumps({"etag": tag, "count": ok}))
    return ok


def _sync_r2(log=print) -> None:
    """Mirror every R2-served store the bot reads. Never raises; per-dir failures keep the
    last-good local mirror (staleness then surfaces via the stockdata anchor + data_gaps)."""
    for d in _R2_DIRS:
        try:
            n = _sync_r2_dir(d)
        except Exception:
            n = None
        if n is None:
            log(f"[macro_refresh] R2 sync failed for {d}/ — keeping last-good mirror")


def refresh() -> str | None:
    """Pull the latest data from origin/main (== the live site) + the R2 data plane. Returns the
    data `asof` on success, or None on failure — leaving the last-good cached checkout intact.
    Never raises."""
    try:
        if not ensure_clone():
            return None
        f = _run(["git", "fetch", "--depth", "1", "origin", "main"], cwd=_SRC)
        if f.returncode != 0:
            return None                                  # network down -> keep last-good data
        _run(["git", "reset", "--hard", "origin/main"], cwd=_SRC)
        # `set` (not `reapply`) so pre-existing clones self-migrate to the current sparse set
        _run(["git", "sparse-checkout", "set", *_SPARSE_PATHS], cwd=_SRC)
        _sync_r2()                                       # the stores git no longer carries
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

    Always includes a check for `site/stockdata/` (R2-leg-only; absent = the R2 sync has never
    succeeded) so the runlog surfaces it loudly on every run.  Any anchor file that is absent
    also lands here.

    Returns a list of relative paths that are expected but absent.
    """
    gaps: list[str] = []
    # Always check stockdata first — this is the highest-severity missing contract (it causes
    # the 6-dim confluence gate to fail open; see audit finding C1 /
    # `missing-stockdata-degenerate-confluence`). Since the macro repo's R2 migration it can
    # only materialize via the R2 leg, never the git pull.
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
                   f"site/stockdata/ missing => lenses.full() fails open (all names confluence=1.0); "
                   f"it comes from the R2 leg (_sync_r2), so check R2 reachability + the macro "
                   f"repo's publish_r2 runs. data/regime needs the sparse set, not `site` alone.")
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
