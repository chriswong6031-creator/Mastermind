"""Append-only thesis ledger (data/brain/theses.jsonl) — the accountability spine.

Interval-gated against double-counting (one open thesis per subject), mirroring the
ai_desk ledger discipline. Resolved outcomes are graded by brain/scorer.py.
"""
from __future__ import annotations

import json
from pathlib import Path

_LEDGER = Path(__file__).resolve().parent.parent / "data" / "brain" / "theses.jsonl"


def _read() -> list[dict]:
    if not _LEDGER.exists():
        return []
    return [json.loads(l) for l in _LEDGER.read_text().splitlines() if l.strip()]


def open_subjects() -> set[str]:
    return {t["subject"] for t in _read() if t.get("status", "open") == "open"}


def append(doc: dict) -> bool:
    """Append a decision doc unless an open thesis on the same subject exists. Returns appended?"""
    if doc["subject"] in open_subjects():
        return False
    doc = {**doc, "status": "open"}
    _LEDGER.parent.mkdir(parents=True, exist_ok=True)
    with _LEDGER.open("a") as fh:
        fh.write(json.dumps(doc, default=str) + "\n")
    return True


def close(subject: str, resolution: str = "closed", *, outcome: int | None = None,
          realized: float | None = None) -> int:
    """Mark every OPEN thesis on `subject` closed (rewriting the JSONL). Returns the count closed.

    Without this the append-only ledger keeps a name's first thesis 'open' forever: append() refuses
    a new thesis while one is open (the dedup lock), so a name that left and re-entered the book
    could never get a refreshed thesis, and the open-thesis set (which feeds the conviction candidate
    pool) accreted stale names indefinitely."""
    rows = _read()
    n = 0
    for t in rows:
        if t.get("subject") == subject and t.get("status", "open") == "open":
            t["status"] = resolution
            if outcome is not None:
                t["outcome"] = outcome
            if realized is not None:
                t["realized"] = realized
            n += 1
    if n:
        _LEDGER.parent.mkdir(parents=True, exist_ok=True)
        _LEDGER.write_text("".join(json.dumps(t, default=str) + "\n" for t in rows))
    return n


def all_theses() -> list[dict]:
    return _read()
