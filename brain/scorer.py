"""Deterministic thesis scorer -> Brier/calibration track record (no LLM).

Grades theses whose check_by has passed against realized rel-return, idempotent on id.
Until any thesis resolves, the track record is honestly "building (n=0)". This is the
gate that keeps the bot paper-only until a rolling Brier proves skill.
"""
from __future__ import annotations

from datetime import date

from brain.ledger import all_theses


def _brier(rows: list[dict]) -> float | None:
    graded = [r for r in rows if "outcome" in r and "prob_correct" in r]
    if not graded:
        return None
    return round(sum((r["prob_correct"] - r["outcome"]) ** 2 for r in graded) / len(graded), 4)


def track_record(asof: date | None = None, realized: dict[str, float] | None = None) -> dict:
    """Build the rolling track record. `realized` maps thesis id -> realized rel-return;
    absent ids stay open. Returns the portfolio.json track_record block."""
    asof = asof or date.today()
    realized = realized or {}
    rows = []
    for t in all_theses():
        due = t.get("check_by") and date.fromisoformat(t["check_by"]) <= asof
        if due and t["id"] in realized:
            chk = (t.get("falsifier") or {}).get("check") or {}
            # only DIRECTIONAL theses (rel_return) are gradable; a non-directional 'watch' carries
            # kind='none' with no op/threshold — skip it (grading it would KeyError, and a watch is
            # not a bet to be scored right/wrong).
            if chk.get("kind") != "rel_return":
                continue
            r = realized[t["id"]]
            # falsified if op(realized, threshold) holds
            miss = (r < chk["threshold"]) if chk["op"] == "<" else (r > chk["threshold"])
            rows.append({**t, "outcome": 0 if miss else 1, "realized": r})
    hits = sum(r["outcome"] for r in rows)
    n = len(rows)
    return {
        "n": n, "hits": hits,
        "hit_rate": round(hits / n, 3) if n else None,
        "brier": _brier(rows),
        "skill": None,                # vs climatology base; populated once n is meaningful
        "status": "building" if n == 0 else "scoring",
    }
