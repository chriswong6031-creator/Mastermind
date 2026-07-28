"""A failed re-run must never erase a good book (bot/decision_rows).

Regression cover for 2026-07-24: HK produced a real book at 09:00 UTC, then the
overnight-review job re-ran the brain, hit a Claude session limit, and its error stub
replaced that day's row — the successful book vanished from decisions.jsonl and the
book read as "hasn't fired since the 22nd".
"""
import pytest

from bot import decision_rows as dr


BOOK = {"asof": "2026-07-24", "summary": "Rotating into HK financials",
        "holdings": [{"ticker": "0005.HK", "weight": 0.1}], "error": None}
STUB = {"asof": "2026-07-24", "summary": None, "holdings": [],
        "error": "You've hit your session limit"}


# --------------------------------------------------------------- is_substantive

@pytest.mark.parametrize("row, expected", [
    (BOOK, True),
    ({"asof": "d", "summary": "a summary, no holdings yet"}, True),
    ({"asof": "d", "holdings": [{"ticker": "0700.HK"}]}, True),
    (STUB, False),
    ({"asof": "d", "summary": None, "holdings": []}, False),   # no-op run
    ({"asof": "d"}, False),
    # an error row is barren EVEN carrying holdings — stubs copy fields forward
    ({"asof": "d", "holdings": [{"ticker": "0005.HK"}], "error": "boom"}, False),
    (None, False),
    ("not a dict", False),
])
def test_is_substantive(row, expected):
    assert dr.is_substantive(row) is expected


# ------------------------------------------------------------ replace_for_asof

def test_failed_rerun_does_not_erase_good_book():
    """THE regression: barren entry + existing good row for that date → book survives."""
    rows = dr.replace_for_asof([BOOK], STUB, "2026-07-24")
    assert rows == [BOOK], "a session-limit stub must not supersede a real book"


def test_good_rerun_still_supersedes():
    """A better book replaces an earlier one — normal idempotent-per-date behaviour."""
    better = {**BOOK, "summary": "revised after the close"}
    assert dr.replace_for_asof([BOOK], better, "2026-07-24") == [better]


def test_barren_entry_recorded_when_nothing_good_exists():
    """First run of the day fails → the stub IS the row (nothing to protect)."""
    assert dr.replace_for_asof([], STUB, "2026-07-24") == [STUB]


def test_barren_replaces_barren():
    """A stub may replace an earlier stub — no information is lost."""
    older = {"asof": "2026-07-24", "holdings": [], "error": "auth revoked"}
    assert dr.replace_for_asof([older], STUB, "2026-07-24") == [STUB]


def test_other_dates_preserved_in_order():
    d22 = {"asof": "2026-07-22", "summary": "the last good book", "holdings": [{"t": 1}]}
    d23 = {"asof": "2026-07-23", "holdings": [], "error": "auth"}
    rows = dr.replace_for_asof([d22, d23, BOOK], STUB, "2026-07-24")
    assert rows == [d22, d23, BOOK]
    # and the 07-22 book is never touched by a 07-24 write
    assert rows[0]["summary"] == "the last good book"


def test_new_date_appends():
    d22 = {"asof": "2026-07-22", "summary": "s", "holdings": [{"t": 1}]}
    new = {"asof": "2026-07-27", "summary": "fresh", "holdings": [{"t": 2}]}
    assert dr.replace_for_asof([d22], new, "2026-07-27") == [d22, new]


def test_duplicate_good_rows_for_a_date_all_survive_a_stub():
    """Defensive: a file that already has 2 rows for one date keeps both rather than
    letting a stub collapse them to nothing."""
    a = {**BOOK, "summary": "first"}
    b = {**BOOK, "summary": "second"}
    assert dr.replace_for_asof([a, b], STUB, "2026-07-24") == [a, b]
