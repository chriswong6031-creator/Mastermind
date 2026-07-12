"""brain/board_track_record.py — the SOLE bot-side reader of the buy-board forward track-record ledger.

WHAT THIS READS (and the macro side it mirrors)
-----------------------------------------------
The macro dashboard surfaces a daily "buy board" (``us_standouts.json``) that is VOLATILE —
overwritten every build, no history, currently ``gate_go=False``. Separately, the macro side keeps a
PERSISTENT, append-only track-record LEDGER (keep-FIRST per ``(date, ticker)``) that records every
time a name was surfaced on that board plus its FORWARD grade, and renders it in
``vendor/macro/site/us_stocks.html``. The canonical macro-side store is
``data/us_board_ledger/retro_grades.parquet`` (macro repo, NOT vendored here). A confirmed real row:

    AAPL · Technology · Surfaced 2026-07-01 · Return +7.4% · status "running".

The bot reads NONE of this today. THIS MODULE is the sole bot-side consumer once the macro side
publishes a bot-consumable projection.

PROPOSED — NOT-YET-PUBLISHED CONTRACT (read this)
-------------------------------------------------
The macro side has not yet published a bot-consumable export. Until it does, this reader targets a
PROPOSED JSON projection of ``retro_grades.parquet`` at a documented path (``_ARTIFACT_PATH``,
falling back to ``_ARTIFACT_PATH_FALLBACK``). Absent the file, EVERY public accessor fail-softs to a
stable empty read — the module is provably inert, never raising, until macro ships the export. See
``docs/design/rotation/BOARD_TRACK_RECORD_CONTRACT.md`` for the full contract + the two open operator
rulings (publication path; candidacy-when-gate_go=False).

    {
      "schema": "us_board_track_record.v1",
      "as_of": "YYYY-MM-DD",
      "window_sessions": 21,
      "rows": [
        {
          "ticker": "AAPL",
          "sector": "Technology",
          "surfaced": "YYYY-MM-DD",     // the board-ENTRY date (keep-first per (date,ticker))
          "return_pct": 7.4,             // forward return since surfaced, in PERCENT
          "status": "running"|"stopped"|"flat",
          "board_pos": 1 | null,         // rank on the board when surfaced (advisory)
          "fwd_mfe_pct": 9.1 | null,     // forward max-favorable-excursion, percent
          "on_board": true               // is the name CURRENTLY on the (volatile) board
        }
      ]
    }

STATUS SEMANTICS (verbatim from the macro grader)
  * running — advancing, no stop breach.
  * stopped — broke its stop.
  * flat    — neither (chop / no decisive move).

THE INVARIANT (mirrors brain/neural_web_context.py + brain/rotation_intake.py)
------------------------------------------------------------------------------
Fail-soft EVERYWHERE. Absent / malformed / stale / wrong-schema → a stable EMPTY read, never a raise.
A single reader (nothing else in the firm opens this file). A process-level cache with an explicit
``_reset_cache()`` for tests. Never imports the Macro engine.

STALENESS
---------
``as_of`` age > _STALE_DAYS (5) CALENDAR days → treated as absent-stale (records()/record()/…
degrade to empty; audit_row() reports 'stale'). Calendar days are a deliberately over-tight proxy —
we would rather drop a marginally-old ledger than trust a stale forward-grade. An absent/unparseable
``as_of`` is treated as stale (fail-closed), never as fresh. The staleness budget here is LOOSER than
the rotation seam's (2d) because the track-record ledger is a slow-moving, mostly-append artifact —
a forward grade does not go materially wrong in 3–5 days — but it is still bounded so a dead export
can never masquerade as a live one.

PUBLIC API (all fail-soft)
--------------------------
* records()            -> list[dict]  — validated rows; [] on absent/malformed/stale.
* record(ticker)       -> dict|None   — the ticker's ledger row (keep-first surfacing); None if absent.
* surfaced_on(asof)    -> set[str]    — tickers whose surfaced == asof (point-in-time board-ENTRY set).
* on_board()           -> set[str]    — tickers currently on_board=True.
* forward_grade(ticker)-> dict|None   — {return_pct, status, fwd_mfe_pct}; None if absent.
* board_stats()        -> dict        — {n, running, stopped, flat, win_rate, avg_return}.
* audit_row()          -> dict        — {status, as_of, n_rows, n_running, n_stopped, n_flat}.
* _reset_cache()       -> None        — explicit cache reset for tests.
"""
from __future__ import annotations

import json
import logging
from datetime import date
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)

_ROOT = Path(__file__).resolve().parent.parent

# ── ARTIFACT LOCATION — PROPOSED, pending the macro-side publication ruling ────────────────────────
# PRIMARY: the proposed macro site-JSON projection, alongside the other site/factordata contracts and
#          eligible for the same R2 data plane. FALLBACK: a bot-side data-plane copy, tried only if
#          the primary is absent. When the home is ruled this is a one-line change (see the contract).
_ARTIFACT_PATH: Path = _ROOT / "vendor" / "macro" / "site" / "factordata" / "us_board_track_record.json"
_ARTIFACT_PATH_FALLBACK: Path = _ROOT / "data" / "us_board_ledger" / "track_record.json"

_EXPECTED_SCHEMA = "us_board_track_record.v1"
_STALE_DAYS = 5  # calendar days; see the module docstring (deliberately over-tight proxy).

# valid forward-grade statuses (a row with any other status is skipped, not fatal).
_VALID_STATUS = frozenset({"running", "stopped", "flat"})

# --------------------------------------------------------------------------- #
# process-level cache — reset via _reset_cache() for tests (mirrors rotation_intake)
# --------------------------------------------------------------------------- #
_CACHE: Optional[list[dict]] = None   # None = not yet loaded; [] = empty/absent/stale/invalid
_CACHE_LOADED: bool = False


def _reset_cache() -> None:
    """Invalidate the per-process cache. Tests MUST call this around fixtures."""
    global _CACHE, _CACHE_LOADED
    _CACHE = None
    _CACHE_LOADED = False


# --------------------------------------------------------------------------- #
# internal helpers
# --------------------------------------------------------------------------- #

def _u(t: Any) -> str:
    """Upper-cased, stripped ticker string. '' for None / non-stringable."""
    return (str(t) if t is not None else "").upper().strip()


def _f(x: Any) -> Optional[float]:
    """Coerce to float, or None. Rejects bool / dict / list (never a fabricated number)."""
    if x is None or isinstance(x, (dict, list, bool)):
        return None
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _age_days(asof_str: Any) -> Optional[int]:
    """Calendar days since asof_str (YYYY-MM-DD). None if absent/unparseable.

    None ("we do not know how old this is") is NEVER treated as fresh — the callers fail-closed on it.
    """
    if not asof_str:
        return None
    try:
        asof_date = date.fromisoformat(str(asof_str)[:10])
        return (date.today() - asof_date).days
    except Exception:  # noqa: BLE001
        return None


def _resolve_path() -> Optional[Path]:
    """PRIMARY if it exists, else FALLBACK if IT exists, else None."""
    try:
        if _ARTIFACT_PATH.exists():
            return _ARTIFACT_PATH
        if _ARTIFACT_PATH_FALLBACK.exists():
            return _ARTIFACT_PATH_FALLBACK
    except Exception:  # noqa: BLE001
        return None
    return None


def _load_raw() -> Optional[dict[str, Any]]:
    """Read + JSON-parse the artifact from the resolved path. None on any IO/parse error / absence."""
    path = _resolve_path()
    if path is None:
        return None
    try:
        return json.loads(path.read_text())
    except Exception as e:  # noqa: BLE001
        log.debug("board_track_record: read failed (%s)", e)
        return None


def _validate_envelope(raw: Any) -> tuple[bool, str]:
    """Validate the artifact ENVELOPE (schema + as_of presence + freshness). (valid, reason).

    Individual rows are validated separately (a bad row is skipped, not fatal) — this gate is only
    about whether the artifact as a whole is trustworthy at all.
    """
    if not isinstance(raw, dict):
        return False, "not a dict"
    if raw.get("schema") != _EXPECTED_SCHEMA:
        return False, f"wrong schema {raw.get('schema')!r}"
    asof = raw.get("as_of")
    if not asof:
        return False, "as_of absent"
    age = _age_days(asof)
    if age is None:
        return False, f"as_of unparseable: {asof!r}"
    if age > _STALE_DAYS:
        return False, f"stale: as_of={asof} age={age}d > {_STALE_DAYS}d"
    return True, "ok"


def _valid_row(row: Any) -> bool:
    """A row is VALID iff it is a dict with a non-empty ticker, a parseable surfaced date, and a
    status in the enum. Everything else (board_pos, fwd_mfe_pct, on_board, return_pct) is passed
    through opaque / coerced downstream — a malformed optional never drops an otherwise-valid row.
    A row failing this is SKIPPED (logged), never fatal to the whole read.
    """
    if not isinstance(row, dict):
        return False
    if not _u(row.get("ticker")):
        return False
    surfaced = row.get("surfaced")
    if not surfaced:
        return False
    try:
        date.fromisoformat(str(surfaced)[:10])
    except Exception:  # noqa: BLE001
        return False
    if row.get("status") not in _VALID_STATUS:
        return False
    return True


def _normalize_row(row: dict) -> dict:
    """Return a normalized copy of a validated row: canonical ticker/sector/status + coerced numerics."""
    return {
        "ticker": _u(row.get("ticker")),
        "sector": (str(row.get("sector")) if row.get("sector") is not None else None),
        "surfaced": str(row.get("surfaced"))[:10],
        "return_pct": _f(row.get("return_pct")),
        "status": row.get("status"),
        "board_pos": (int(row["board_pos"]) if isinstance(row.get("board_pos"), (int, float))
                      and not isinstance(row.get("board_pos"), bool) else None),
        "fwd_mfe_pct": _f(row.get("fwd_mfe_pct")),
        "on_board": bool(row.get("on_board")),
    }


# --------------------------------------------------------------------------- #
# records() — the sole reader
# --------------------------------------------------------------------------- #

def records() -> list[dict]:
    """Return the validated, fresh track-record rows, or [] in every degraded state.

    [] means "no track record available" — absent file, stale as_of (> 5 calendar days), a
    wrong/absent schema, or an unparseable artifact all collapse to []. Individually-malformed rows
    (bad ticker / unparseable surfaced date / invalid status) are SKIPPED, not fatal — a single bad
    row never sinks a good artifact.

    KEEP-FIRST per (surfaced_date, ticker): the ledger is append-only and the macro grader keeps the
    FIRST surfacing of a name on a given date; if the export ever carries a duplicate (date, ticker)
    the reader also keeps the first-seen occurrence, mirroring the ledger's own semantics.

    Result is process-cached (like rotation_intake.calls()); call _reset_cache() to force a fresh read.
    """
    global _CACHE, _CACHE_LOADED
    if _CACHE_LOADED:
        return list(_CACHE or [])
    _CACHE_LOADED = True
    try:
        raw = _load_raw()
        if raw is None:
            _CACHE = []
            return []
        valid, reason = _validate_envelope(raw)
        if not valid:
            log.debug("board_track_record: envelope rejected (%s) → empty read", reason)
            _CACHE = []
            return []
        raw_rows = raw.get("rows")
        if not isinstance(raw_rows, list):
            _CACHE = []
            return []
        out: list[dict] = []
        seen: set[tuple[str, str]] = set()  # (surfaced, ticker) keep-first
        for r in raw_rows:
            if not _valid_row(r):
                log.debug("board_track_record: skipping malformed row %r", r)
                continue
            norm = _normalize_row(r)
            key = (norm["surfaced"], norm["ticker"])
            if key in seen:
                continue  # keep-first per (date, ticker)
            seen.add(key)
            out.append(norm)
        _CACHE = out
        return list(out)
    except Exception as e:  # noqa: BLE001 — fail-soft: never raise into a build
        log.warning("board_track_record: unexpected error loading records (%s)", e)
        _CACHE = []
        return []


# --------------------------------------------------------------------------- #
# per-ticker accessors
# --------------------------------------------------------------------------- #

def record(ticker: str) -> Optional[dict]:
    """Return the ledger row for `ticker` (keep-first surfacing), or None if absent.

    When the ledger carries multiple surfacings of a name (distinct dates), the FIRST-surfaced row is
    returned — the ledger's keep-first identity for the name. Fail-soft → None.
    """
    tk = _u(ticker)
    if not tk:
        return None
    try:
        first: Optional[dict] = None
        for r in records():
            if r.get("ticker") == tk:
                if first is None or str(r.get("surfaced")) < str(first.get("surfaced")):
                    first = r
        return first
    except Exception:  # noqa: BLE001
        return None


def surfaced_on(asof: str) -> set[str]:
    """Return the set of tickers whose surfaced date == `asof` (YYYY-MM-DD).

    This is the POINT-IN-TIME board-ENTRY set — the historically-correct "was this name surfaced on
    the buy board that day" query the divergence replay needs. It is immutable (a past surfacing is a
    historical fact) and is NOT gated by the volatile board's gate_go. Fail-soft → empty set.
    """
    try:
        target = str(asof)[:10] if asof else None
        if not target:
            return set()
        return {r["ticker"] for r in records() if r.get("surfaced") == target}
    except Exception:  # noqa: BLE001
        return set()


def on_board() -> set[str]:
    """Return the set of tickers currently on the (volatile) board (on_board=True). Fail-soft → set()."""
    try:
        return {r["ticker"] for r in records() if r.get("on_board")}
    except Exception:  # noqa: BLE001
        return set()


def forward_grade(ticker: str) -> Optional[dict]:
    """Return {return_pct, status, fwd_mfe_pct} for `ticker` (keep-first row), or None if absent."""
    try:
        r = record(ticker)
        if not r:
            return None
        return {
            "return_pct": r.get("return_pct"),
            "status": r.get("status"),
            "fwd_mfe_pct": r.get("fwd_mfe_pct"),
        }
    except Exception:  # noqa: BLE001
        return None


# --------------------------------------------------------------------------- #
# board_stats() — aggregate track-record read
# --------------------------------------------------------------------------- #

def board_stats() -> dict[str, Any]:
    """Return aggregate track-record stats over all validated rows.

    Shape::

        {
          "n": <int>,                     # total rows
          "running": <int>,               # status == running
          "stopped": <int>,               # status == stopped
          "flat": <int>,                  # status == flat
          "win_rate": <float>|None,       # running / (running + stopped), GUARDING /0 → None
          "avg_return": <float>|None,     # mean return_pct over rows with a numeric return; None if none
        }

    win_rate deliberately excludes 'flat' from the denominator — a flat outcome is neither a win nor a
    loss (chop). When running+stopped == 0 the rate is None (never a fabricated 0/0). Fail-soft: on any
    error returns the all-zero/None stat block (never raises).
    """
    empty = {"n": 0, "running": 0, "stopped": 0, "flat": 0, "win_rate": None, "avg_return": None}
    try:
        rows = records()
        if not rows:
            return empty
        n = len(rows)
        running = sum(1 for r in rows if r.get("status") == "running")
        stopped = sum(1 for r in rows if r.get("status") == "stopped")
        flat = sum(1 for r in rows if r.get("status") == "flat")

        decided = running + stopped
        win_rate = round(running / decided, 4) if decided > 0 else None

        rets = [r["return_pct"] for r in rows if isinstance(r.get("return_pct"), (int, float))]
        avg_return = round(sum(rets) / len(rets), 4) if rets else None

        return {
            "n": n,
            "running": running,
            "stopped": stopped,
            "flat": flat,
            "win_rate": win_rate,
            "avg_return": avg_return,
        }
    except Exception:  # noqa: BLE001
        return empty


# --------------------------------------------------------------------------- #
# audit_row() — perception runlog
# --------------------------------------------------------------------------- #

def audit_row() -> dict[str, Any]:
    """Return {status, as_of, n_rows, n_running, n_stopped, n_flat} for the perception runlog.

    status ∈ {present, absent, stale}:
      * present — a fresh, valid artifact (its rows are counted; may be 0).
      * stale   — an artifact exists with the right schema + an as_of, but the as_of is older than the
                  5-day budget (or unparseable).
      * absent  — no artifact, wrong/absent schema, or a missing as_of.

    Flag-independent — always safe to run. Fail-soft → the absent block.
    """
    try:
        raw = _load_raw()
        if raw is None or not isinstance(raw, dict):
            return {"status": "absent", "as_of": None, "n_rows": 0,
                    "n_running": 0, "n_stopped": 0, "n_flat": 0}
        asof = raw.get("as_of")
        age = _age_days(asof)

        # wrong schema / missing as_of → absent.
        if raw.get("schema") != _EXPECTED_SCHEMA or not asof:
            return {"status": "absent", "as_of": asof, "n_rows": 0,
                    "n_running": 0, "n_stopped": 0, "n_flat": 0}

        # right schema + present as_of, but old/unparseable → stale.
        if age is None or age > _STALE_DAYS:
            return {"status": "stale", "as_of": asof, "n_rows": 0,
                    "n_running": 0, "n_stopped": 0, "n_flat": 0}

        stats = board_stats()
        return {
            "status": "present",
            "as_of": asof,
            "n_rows": stats["n"],
            "n_running": stats["running"],
            "n_stopped": stats["stopped"],
            "n_flat": stats["flat"],
        }
    except Exception:  # noqa: BLE001
        return {"status": "absent", "as_of": None, "n_rows": 0,
                "n_running": 0, "n_stopped": 0, "n_flat": 0}
