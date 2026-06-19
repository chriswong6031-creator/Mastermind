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


def all_theses() -> list[dict]:
    return _read()
