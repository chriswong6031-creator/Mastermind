"""brain/posture_compliance.py — POSTURE COMPLIANCE LEDGER — W-E.2 task E2.3.

WHAT THIS MODULE DOES
---------------------
After each build cycle, grades REALIZED gross/defensive-weight per book against the
posture decider's targets (offense_budget / defense_floor) and writes deviations to:

    data/posture/<asof>/deviations.json

Deviations feed the three-questions/journal machinery as drafted entries:
  'you ran offense 0.71 against a ROTATE-DEFENSIVE 0.45 posture — journal why'
via the EXISTING journal.py lazy-import seam.

ADVISORY-ONLY: grading, NEVER enforcement.  The compliance module observes and logs;
enforcement is the armed posture decider's job (E3.3).

FLAG-INDEPENDENT: E2.3 is flag-independent (read-only; it reads the artifact that the
decider publishes in shadow every build regardless of flag, and enriches existing journal
workflows).  Missing artifact → degrade silently (no block, no grade).

BOOKS TRACKED
-------------
The same eight seats the journal covers (journal.SEATS) plus "flagship":
  autonomous, heavyweight, china, hk, etf  (the five LLM/free-form books)
  flagship   (the gated systematic book)
  strategist, pm, gate, risk  (from journal.SEATS; no live book → no gross grading)

For each book that has a readable latest.json:
  realized_offense_gross  — sum of non-defensive position weights
  realized_defense_gross  — sum of defensive/ballast position weights
  offense_budget_target   — from posture artifact
  defense_floor_target    — from posture artifact
  offense_deviation       — realized_offense - offense_budget
  defense_deviation       — defense_floor - realized_defense  (how short of the floor)
  verdict                 — 'on_target' | 'offense_hot' | 'defense_short' | 'both' | 'unavailable'

NEVER RAISES: every public function degrades gracefully.

PUBLIC API
----------
* ``grade(asof) -> dict``             — grade all books for `asof`, write deviations.json,
                                        return the deviations dict.
* ``load_deviations(asof) -> dict``   — read the on-disk deviations for `asof`.
* ``emit_journal_drafts(asof) -> int`` — emit journal draft entries for BAD-deviation books.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Optional

_ROOT: Path = Path(__file__).resolve().parent.parent
_POSTURE_DIR: Path = _ROOT / "data" / "posture"

# Deviation threshold: if the book is this far from the posture target, it is "hot/short".
# (unverified-prior) — sized as the smallest graded resolution that produces a detectable effect.
_OFFENSE_HOT_MARGIN: float = 0.05   # offense > budget + this margin → 'offense_hot'
_DEFENSE_SHORT_MARGIN: float = 0.03  # defense < floor - this margin → 'defense_short'

# The five LLM books that carry a latest.json and are graded for realized gross.
_LLM_BOOKS: tuple[str, ...] = ("autonomous", "heavyweight", "china", "hk", "etf")
_ALL_GRADED_BOOKS: tuple[str, ...] = ("flagship",) + _LLM_BOOKS

# Defensive theme_ids / sleeve tags that identify a defensive position.
# A position tagged with any of these prefixes is counted as defensive weight.
_DEFENSIVE_THEME_PREFIXES: tuple[str, ...] = (
    "DEFENSIVE_",      # the DEF_SLEEVE theme_id pattern from portfolio/rotation.py
    "defensive",
    "duration",
    "ballast",
)
_BALLAST_ALLOWLIST: frozenset[str] = frozenset({"SGOV", "BIL", "SHY", "USFR"})


# ─────────────────────────────────────────────────────────────────────────────
# artifact readers
# ─────────────────────────────────────────────────────────────────────────────

def _latest_posture() -> Optional[dict]:
    """Read the latest posture artifact. None on any miss/error. Never raises."""
    try:
        p = _POSTURE_DIR / "latest.json"
        if p.exists():
            d = json.loads(p.read_text())
            return d if isinstance(d, dict) else None
    except Exception:  # noqa: BLE001
        pass
    return None


def _book_latest(portfolio_id: str) -> Optional[dict]:
    """Read a book's latest.json. None on any miss/error. Never raises."""
    try:
        from portfolio import registry
        p = registry.data_dir(portfolio_id) / "latest.json"
        if p.exists():
            d = json.loads(p.read_text())
            return d if isinstance(d, dict) else None
    except Exception:  # noqa: BLE001
        pass
    return None


def _safe_float(v: Any) -> Optional[float]:
    try:
        f = float(v)
        return f if f == f else None
    except (TypeError, ValueError):
        return None


# ─────────────────────────────────────────────────────────────────────────────
# gross weight analysis
# ─────────────────────────────────────────────────────────────────────────────

def _is_defensive(pos: dict) -> bool:
    """True if a position counts as defensive weight (tagged or ballast-allowlisted)."""
    ticker = str(pos.get("ticker") or "").upper()
    if ticker in _BALLAST_ALLOWLIST:
        return True
    theme_id = str(pos.get("theme_id") or "").lower()
    sleeve = str(pos.get("sleeve") or "").lower()
    verdict = str(pos.get("verdict") or "").lower()
    for prefix in _DEFENSIVE_THEME_PREFIXES:
        if theme_id.startswith(prefix.lower()) or sleeve.startswith(prefix.lower()):
            return True
    if verdict in ("defensive", "defense", "def"):
        return True
    return False


def _book_gross(book: dict) -> tuple[float, float]:
    """Return (realized_offense_gross, realized_defense_gross) for a book's latest.json.

    Uses positions[].weight, tagged by _is_defensive.  Cash positions (weight 0 / no ticker)
    are excluded.  Returns (0.0, 0.0) on any parsing failure.
    """
    offense = 0.0
    defense = 0.0
    try:
        positions = book.get("positions") or []
        for pos in positions:
            w = _safe_float(pos.get("weight"))
            if w is None or w <= 0:
                continue
            if _is_defensive(pos):
                defense += w
            else:
                offense += w
    except Exception:  # noqa: BLE001
        pass
    return round(offense, 4), round(defense, 4)


# ─────────────────────────────────────────────────────────────────────────────
# grade one book
# ─────────────────────────────────────────────────────────────────────────────

def _grade_book(portfolio_id: str, posture: dict) -> dict:
    """Grade one book against the posture targets. Returns a deviation record."""
    offense_target = _safe_float(posture.get("offense_budget"))
    defense_target = _safe_float(posture.get("defense_floor"))
    posture_class = posture.get("posture_class") or "BALANCED"

    stub = {
        "portfolio_id": portfolio_id,
        "posture_class": posture_class,
        "offense_budget_target": offense_target,
        "defense_floor_target": defense_target,
        "realized_offense_gross": None,
        "realized_defense_gross": None,
        "offense_deviation": None,
        "defense_deviation": None,
        "verdict": "unavailable",
        "note": "",
    }

    book = _book_latest(portfolio_id)
    if book is None:
        stub["note"] = "no latest.json"
        return stub

    off_gross, def_gross = _book_gross(book)
    stub["realized_offense_gross"] = off_gross
    stub["realized_defense_gross"] = def_gross

    if offense_target is None or defense_target is None:
        stub["note"] = "posture targets absent"
        stub["verdict"] = "unavailable"
        return stub

    off_dev = round(off_gross - offense_target, 4)   # positive = offense hotter than target
    def_dev = round(defense_target - def_gross, 4)   # positive = defense short of floor

    stub["offense_deviation"] = off_dev
    stub["defense_deviation"] = def_dev

    offense_hot = off_dev > _OFFENSE_HOT_MARGIN
    defense_short = def_dev > _DEFENSE_SHORT_MARGIN

    if offense_hot and defense_short:
        verdict = "both"
    elif offense_hot:
        verdict = "offense_hot"
    elif defense_short:
        verdict = "defense_short"
    else:
        verdict = "on_target"

    stub["verdict"] = verdict
    return stub


# ─────────────────────────────────────────────────────────────────────────────
# grade() — the main entry point
# ─────────────────────────────────────────────────────────────────────────────

def grade(asof: Optional[str] = None) -> dict:
    """Grade all graded books for `asof`, write deviations.json, return the deviations dict.

    Degrades silently when the posture artifact is absent (returns empty grades dict).
    Never raises.
    """
    asof = asof or date.today().isoformat()
    result: dict = {
        "asof": asof,
        "graded_at": datetime.now(timezone.utc).isoformat(),
        "posture_artifact": None,
        "grades": {},
    }
    try:
        posture = _latest_posture()
        if posture is None:
            result["note"] = "posture artifact absent — degrade silently"
            return result

        result["posture_artifact"] = {
            "posture_class": posture.get("posture_class"),
            "offense_budget": posture.get("offense_budget"),
            "defense_floor": posture.get("defense_floor"),
            "shadow": posture.get("shadow", True),
        }

        grades: dict[str, dict] = {}
        for pid in _ALL_GRADED_BOOKS:
            try:
                grades[pid] = _grade_book(pid, posture)
            except Exception:  # noqa: BLE001
                grades[pid] = {
                    "portfolio_id": pid,
                    "verdict": "unavailable",
                    "note": "grading error",
                }
        result["grades"] = grades

        # write the deviations artifact
        _write_deviations(asof, result)

    except Exception:  # noqa: BLE001 — compliance is advisory, never blocks a build
        pass

    return result


def _write_deviations(asof: str, payload: dict) -> None:
    """Atomic write of data/posture/<asof>/deviations.json. Never raises."""
    try:
        out_dir = _POSTURE_DIR / str(asof)[:10]
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / "deviations.json"
        tmp = out_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2, default=str))
        tmp.replace(out_path)
    except Exception:  # noqa: BLE001
        pass


# ─────────────────────────────────────────────────────────────────────────────
# load_deviations()
# ─────────────────────────────────────────────────────────────────────────────

def load_deviations(asof: Optional[str] = None) -> Optional[dict]:
    """Read on-disk deviations for `asof`. None on any miss/error. Never raises."""
    try:
        d = str(asof or date.today().isoformat())[:10]
        p = _POSTURE_DIR / d / "deviations.json"
        if p.exists():
            payload = json.loads(p.read_text())
            return payload if isinstance(payload, dict) else None
    except Exception:  # noqa: BLE001
        pass
    return None


# ─────────────────────────────────────────────────────────────────────────────
# emit_journal_drafts() — feed the three-questions/journal machinery (E2.3 Brier duty)
# ─────────────────────────────────────────────────────────────────────────────

def emit_journal_drafts(asof: Optional[str] = None) -> int:
    """Emit journal draft entries for books with BAD-deviation verdicts.

    Uses the EXISTING journal draft seam (lazy import of brain.journal) so the journal
    apparatus handles idempotency, ring-buffers, and the conscious duty.  The draft text
    is the spec's example: 'you ran offense 0.71 against a ROTATE-DEFENSIVE 0.45 posture
    — journal why'.

    Returns the number of new drafts emitted. Best-effort; 0 on any failure. Advisory-only.
    """
    asof = asof or date.today().isoformat()
    count = 0
    try:
        devs = load_deviations(asof)
        if not isinstance(devs, dict):
            return 0
        grades = devs.get("grades") or {}
        posture_art = devs.get("posture_artifact") or {}
        posture_class = posture_art.get("posture_class") or "BALANCED"
        offense_target = posture_art.get("offense_budget")
        defense_target = posture_art.get("defense_floor")

        for portfolio_id, rec in grades.items():
            verdict = rec.get("verdict") or "unavailable"
            if verdict in ("unavailable", "on_target"):
                continue
            # Build the draft message
            off_real = rec.get("realized_offense_gross")
            def_real = rec.get("realized_defense_gross")
            parts: list[str] = []
            if verdict in ("offense_hot", "both") and off_real is not None and offense_target is not None:
                parts.append(
                    f"you ran offense {off_real:.2f} against a {posture_class} "
                    f"{offense_target:.2f} posture — journal why"
                )
            if verdict in ("defense_short", "both") and def_real is not None and defense_target is not None:
                parts.append(
                    f"you ran defense {def_real:.2f} short of the {posture_class} "
                    f"floor {defense_target:.2f} — journal why"
                )
            if not parts:
                continue

            draft_text = "; ".join(parts)
            # Emit via the journal seam (lazy import; best-effort)
            try:
                _emit_one_draft(portfolio_id, asof, draft_text, verdict, posture_class)
                count += 1
            except Exception:  # noqa: BLE001
                pass
    except Exception:  # noqa: BLE001
        pass
    return count


def _emit_one_draft(portfolio_id: str, asof: str, draft_text: str,
                    verdict: str, posture_class: str) -> None:
    """Emit a single posture-deviation journal draft via the journal seam.

    The journal module's ``draft_resolutions`` expects resolved graded rows from
    self_mirror — posture deviation drafts are a different kind, so we write them
    directly as 'unresolved' entries into the drafts file via the journal's writer.

    NOTE: We use a SEPARATE posture-deviation draft id pattern (pd:<seat>:<asof>:<verdict>)
    so these never collide with the self_mirror graded rows.

    Best-effort / lazy import: if the journal module is unavailable (e.g. in tests), silently
    return.  Idempotent by draft_id (the journal module deduplicates).
    """
    try:
        from brain import journal as J
        # Map portfolio_id to journal seat
        seat = _portfolio_to_seat(portfolio_id)
        if seat is None:
            return

        draft_id = f"pd:{seat}:{asof}:{verdict}"
        existing = J.load_drafts(seat)
        if any(d.get("id") == draft_id for d in existing):
            return  # idempotent

        draft = {
            "id": draft_id,
            "seat": seat,
            "date": asof,
            "resolved_on": asof,
            "call": f"posture deviation ({verdict})",
            "thesis_at_entry": draft_text,
            "planes_at_entry": None,
            "outcome": 0,       # treated as a bad grade — requires a lesson
            "grade": -0.01,     # nominal bad grade to trigger the conscious duty
            "benchmark": "posture_target",
            "regime_context": f"posture={posture_class}",
            "close_reason": "posture_compliance_check",
            "taxonomy_hint": "bad-sizing",
            "lesson_id": None,
            "status": "unresolved",
            "kind": "posture_deviation",
        }
        existing.append(draft)
        J._write_json(J._drafts_path(seat), existing)
    except Exception:  # noqa: BLE001 — never block; advisory seam
        pass


def _portfolio_to_seat(portfolio_id: str) -> Optional[str]:
    """Map portfolio_id → journal seat. None = not a graded seat."""
    _MAP = {
        "autonomous": "autonomous",
        "heavyweight": "heavyweight",
        "china": "china",
        "hk": "hk",
        "etf": "autonomous",     # etf maps to autonomous seat (closest analogue)
        "flagship": "pm",        # flagship maps to pm seat
    }
    return _MAP.get(portfolio_id)
