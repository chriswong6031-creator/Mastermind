"""Flagship WATCHLIST — the parked-name backstop for the subtract-only entry-timing gate.

A prototype of the first-class WATCH state from the buy-pipeline design
(``docs/design/desk/03-buy-pipeline-and-watchlist.md`` §3.6): a name the engine would otherwise
BUY but whose entry technicals are poor is not bought — it is PARKED here for daily re-review
instead of being force-bought at a bad entry. This module is deliberately minimal: an append-only,
idempotent-per-(ticker, date) JSONL log plus a tiny read API. It owns NO sizing and NEVER touches
prod trading state; the gate in ``bot.phase2`` only ever WITHHOLDS (subtract-only) and records here.

The timing predicate (``timing_withhold``) is the EXACT mirror of the shadow lever
``portfolio.desk_ab.apply_timing_gated`` / ``_timing_ok`` (AB_EXPERIMENT.md §2.4) so the live gate
and the forward A/B arm gate identically: withhold iff
  * extension grade ∈ {stretched, parabolic, extended}  OR the parabolic flag is set, OR
  * pct_vs_200dma >= 30, OR
  * rs < 50, OR
  * urgency == 'avoid', OR
  * eq_grade == 'weak'.
Every field is nullable; a missing field never fires (fail-open) so an absent snapshot can never
silently withhold a name — matching ``_timing_ok``'s fail-open contract.
"""
from __future__ import annotations

import json
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_WATCHLIST = _ROOT / "data" / "portfolios" / "flagship" / "watchlist.jsonl"

# Thresholds — identical to portfolio.desk_ab (the shadow lever) so the two gates never diverge.
_EXT_PCT_VS_200DMA = 30.0           # >= 30% above the 200dma → extended
_WEAK_RS_PCTILE = 50.0              # RS vs SPY below the median → weak relative strength
_BAD_EXT_GRADES = {"stretched", "parabolic", "extended"}
_BAD_URGENCY = {"avoid"}            # entry urgency that says "do not chase"
_BAD_EQ_GRADES = {"weak"}          # entry-quality grade


def timing_withhold(tech: dict | None) -> str | None:
    """Return a human-readable reason iff the name's entry technicals are poor enough to WITHHOLD it,
    else None (keep / buy). `tech` is an ``_entry_tech_fields(ticker)`` dict — every field nullable.
    FAILS OPEN: a None field never fires, so a missing snapshot withholds nothing (mirrors
    ``desk_ab._timing_ok``). Pure; never raises."""
    if not tech:
        return None
    try:
        grade = str(tech.get("eq_grade") or "").lower()
        if grade in _BAD_EXT_GRADES or bool(tech.get("parabolic")):
            return f"extended (grade={grade or 'parabolic'})"
        pv200 = tech.get("pct_vs_200dma")
        if isinstance(pv200, (int, float)) and pv200 >= _EXT_PCT_VS_200DMA:
            return f"extended (pct_vs_200dma={pv200:.0f}>={_EXT_PCT_VS_200DMA:.0f})"
        if str(tech.get("urgency") or "").lower() in _BAD_URGENCY:
            return "entry urgency=avoid"
        rs = tech.get("rs")
        if isinstance(rs, (int, float)) and rs < _WEAK_RS_PCTILE:
            return f"weak RS ({rs:.0f}<{_WEAK_RS_PCTILE:.0f})"
        if grade in _BAD_EQ_GRADES:
            return f"weak entry quality (eq_grade={grade})"
    except Exception:  # noqa: BLE001 — a malformed snapshot must never block the gate
        return None
    return None


def _path() -> Path:
    return _WATCHLIST


def _read_rows() -> list[dict]:
    try:
        return [json.loads(l) for l in _path().read_text().splitlines() if l.strip()]
    except Exception:  # noqa: BLE001
        return []


def append(ticker: str, asof: str, reason: str, tech: dict | None = None,
           combined: float | None = None) -> bool:
    """Append a withheld-name record, IDEMPOTENT per (ticker, asof): a re-run on the same day for the
    same name replaces (does not duplicate) the row. Returns True if a row was written. Best-effort;
    never raises (the gate must never break the build on a logging failure)."""
    t = (ticker or "").upper().strip()
    if not t or not asof:
        return False
    asof = str(asof)[:10]
    rec = {"ticker": t, "asof": asof, "reason": reason,
           "tech": dict(tech) if isinstance(tech, dict) else None, "combined": combined}
    try:
        rows = [r for r in _read_rows()
                if not ((r.get("ticker") or "").upper() == t and str(r.get("asof"))[:10] == asof)]
        rows.append(rec)
        _path().parent.mkdir(parents=True, exist_ok=True)
        _path().write_text("".join(json.dumps(r, default=str) + "\n" for r in rows))
        return True
    except Exception:  # noqa: BLE001
        return False


def for_date(asof: str) -> list[dict]:
    """Every name parked on `asof` — the daily re-review queue."""
    asof = str(asof)[:10]
    return [r for r in _read_rows() if str(r.get("asof"))[:10] == asof]


def latest() -> list[dict]:
    """The most recent parked record per ticker (the current watchlist for re-review)."""
    by_ticker: dict[str, dict] = {}
    for r in sorted(_read_rows(), key=lambda x: str(x.get("asof"))):
        t = (r.get("ticker") or "").upper()
        if t:
            by_ticker[t] = r
    return sorted(by_ticker.values(), key=lambda x: (x.get("ticker") or ""))


def all_rows() -> list[dict]:
    """The full append-only log (audit / grading source)."""
    return _read_rows()
