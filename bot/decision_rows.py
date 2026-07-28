"""Shared decision-log row semantics for the Brain books (hk / china / etf / autonomous /
heavyweight).

Every Brain book keeps ``decisions.jsonl`` idempotent per ``asof``: a re-run for a date drops
the existing row(s) for that date and appends the new one. That rule is right for a *better*
re-run and catastrophic for a *failed* one.

2026-07-24, HK: the 09:00 UTC scheduled run produced a real book (10 holdings, packet
``ed5dd3359d9dbe53``). At 14:20 the ``watch_asia_overnight`` job re-ran the same brain in
OVERNIGHT REVIEW mode; the LLM call died on ``"You've hit your session limit"``, and the error
stub it wrote for the same ``asof`` **erased that day's book** — the successful run left no
trace in ``decisions.jsonl``. The book looked "not fired since the 22nd" when it had in fact
fired and been overwritten.

The rule here: **a barren row may never supersede a substantive one for the same date.** A
failed re-run is a non-event for the ledger — the failure is already recorded in the run log
(``brain.runlog`` / ``run_events.jsonl``), which is where errors belong. A substantive re-run
still supersedes freely, so a genuinely better book replaces an earlier one as before.
"""
from __future__ import annotations

from typing import Any


def is_substantive(row: Any) -> bool:
    """True when a decision row actually carries a book, rather than an error/no-op stub.

    Barren = the brain errored, or it produced neither holdings nor a summary. Note an
    ``error`` row is barren EVEN IF it carries holdings: the error stubs copy forward
    surrounding fields, and a stub must never outrank a clean book.
    """
    if not isinstance(row, dict):
        return False
    if row.get("error"):
        return False
    return bool(row.get("holdings")) or bool(row.get("summary"))


def replace_for_asof(existing: list[dict], entry: dict, asof: str) -> list[dict]:
    """The rows to persist after ``entry`` is recorded for ``asof`` (idempotent per date).

    Rows for other dates are preserved in order. For ``asof`` itself: normally ``entry``
    supersedes whatever was there; but when ``entry`` is barren and something substantive
    already exists for that date, the existing row(s) are KEPT and ``entry`` is dropped —
    a failed re-run cannot destroy a good book.
    """
    kept = [r for r in existing if r.get("asof") != asof]
    same_day = [r for r in existing if r.get("asof") == asof]
    if same_day and not is_substantive(entry) and any(is_substantive(r) for r in same_day):
        return kept + same_day
    return kept + [entry]
