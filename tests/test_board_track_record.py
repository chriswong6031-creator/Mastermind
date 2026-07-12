"""tests/test_board_track_record.py — the sole-reader contract + fail-soft behaviour of board_track_record.

Every case points `_ARTIFACT_PATH` (and the fallback) at a tmp file and calls `_reset_cache()` around
it, so NO vendor data is touched and the process cache never leaks between cases. The golden fixture's
`as_of` is a far-future placeholder (2099-01-01) that `_fresh_fixture` REWRITES relative to today
(fresh) or into the past (stale) — so freshness is exercised without the fixture rotting. The row
`surfaced` dates are the historical 2026 board-entry facts and are NOT rewritten (they are the
point-in-time query surface).

No vendor / macro-engine dependency: the reader never imports the macro engine and this test never
reads the vendored site tree.
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import pytest

from brain import board_track_record as btr

FIXTURE = Path(__file__).parent / "fixtures" / "us_board_track_record.json"


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def _load_fixture() -> dict:
    return json.loads(FIXTURE.read_text())


def _fresh_fixture(days_old: int = 0) -> dict:
    """The golden fixture with as_of rewritten to `days_old` calendar days before today.

    Only the ENVELOPE as_of is rewritten; the per-row `surfaced` dates are left as the historical
    2026-07-01 / 2026-06-20 facts (they are the point-in-time board-entry surface).
    """
    art = _load_fixture()
    art["as_of"] = (date.today() - timedelta(days=days_old)).isoformat()
    return art


def _point_at(monkeypatch, tmp_path: Path, artifact: dict | None) -> Path:
    """Write `artifact` (or nothing) to a tmp file and point BOTH artifact paths at it.

    Returns the primary path. When `artifact` is None the file is NOT created — the ABSENT case.
    Always resets the process cache so the read is fresh.
    """
    primary = tmp_path / "us_board_track_record.json"
    fallback = tmp_path / "does_not_exist" / "track_record.json"
    if artifact is not None:
        primary.write_text(json.dumps(artifact))
    monkeypatch.setattr(btr, "_ARTIFACT_PATH", primary)
    monkeypatch.setattr(btr, "_ARTIFACT_PATH_FALLBACK", fallback)
    btr._reset_cache()
    return primary


@pytest.fixture(autouse=True)
def _reset_around_each():
    btr._reset_cache()
    yield
    btr._reset_cache()


# --------------------------------------------------------------------------- #
# records() — valid / absent / malformed / stale
# --------------------------------------------------------------------------- #

def test_valid_fixture_parses(monkeypatch, tmp_path):
    _point_at(monkeypatch, tmp_path, _fresh_fixture(days_old=0))
    out = btr.records()
    assert len(out) == 3
    tickers = {r["ticker"] for r in out}
    assert tickers == {"AAPL", "SNOW", "WDAY"}
    statuses = {r["status"] for r in out}
    assert statuses == {"running", "stopped", "flat"}


def test_absent_file_returns_empty(monkeypatch, tmp_path):
    _point_at(monkeypatch, tmp_path, None)  # no file written
    assert btr.records() == []
    assert btr.record("AAPL") is None
    assert btr.surfaced_on("2026-07-01") == set()
    assert btr.on_board() == set()
    assert btr.forward_grade("AAPL") is None
    assert btr.board_stats() == {
        "n": 0, "running": 0, "stopped": 0, "flat": 0, "win_rate": None, "avg_return": None}


def test_wrong_schema_returns_empty(monkeypatch, tmp_path):
    art = _fresh_fixture()
    art["schema"] = "us_board_track_record.v0_WRONG"
    _point_at(monkeypatch, tmp_path, art)
    assert btr.records() == []


def test_not_a_dict_returns_empty(monkeypatch, tmp_path):
    primary = tmp_path / "us_board_track_record.json"
    primary.write_text(json.dumps(["not", "an", "envelope"]))
    monkeypatch.setattr(btr, "_ARTIFACT_PATH", primary)
    monkeypatch.setattr(btr, "_ARTIFACT_PATH_FALLBACK", tmp_path / "nope.json")
    btr._reset_cache()
    assert btr.records() == []


def test_unparseable_json_returns_empty(monkeypatch, tmp_path):
    primary = tmp_path / "us_board_track_record.json"
    primary.write_text("{ this is not valid json ")
    monkeypatch.setattr(btr, "_ARTIFACT_PATH", primary)
    monkeypatch.setattr(btr, "_ARTIFACT_PATH_FALLBACK", tmp_path / "nope.json")
    btr._reset_cache()
    assert btr.records() == []


def test_stale_asof_returns_empty(monkeypatch, tmp_path):
    # 6 calendar days old > 5-day budget → treated as absent-stale.
    _point_at(monkeypatch, tmp_path, _fresh_fixture(days_old=6))
    assert btr.records() == []
    assert btr.surfaced_on("2026-07-01") == set()


def test_freshness_boundary(monkeypatch, tmp_path):
    # exactly 5 days old is still fresh; 6 is stale.
    _point_at(monkeypatch, tmp_path, _fresh_fixture(days_old=5))
    assert len(btr.records()) == 3
    _point_at(monkeypatch, tmp_path, _fresh_fixture(days_old=6))
    assert btr.records() == []


def test_missing_asof_returns_empty(monkeypatch, tmp_path):
    art = _fresh_fixture()
    art["as_of"] = ""
    _point_at(monkeypatch, tmp_path, art)
    assert btr.records() == []


def test_one_malformed_row_skipped_not_fatal(monkeypatch, tmp_path):
    art = _fresh_fixture()
    art["rows"].append({"sector": "X", "surfaced": "2026-07-01", "status": "running"})  # no ticker
    art["rows"].append({"ticker": "BADSTAT", "surfaced": "2026-07-01", "status": "??"})  # bad status
    art["rows"].append({"ticker": "BADDATE", "surfaced": "not-a-date", "status": "running"})  # bad date
    _point_at(monkeypatch, tmp_path, art)
    out = btr.records()
    assert len(out) == 3  # the 3 good rows survive; the 3 bad ones are skipped
    assert {r["ticker"] for r in out} == {"AAPL", "SNOW", "WDAY"}


def test_rows_not_a_list_returns_empty(monkeypatch, tmp_path):
    art = _fresh_fixture()
    art["rows"] = "not a list"
    _point_at(monkeypatch, tmp_path, art)
    assert btr.records() == []


def test_keep_first_per_date_ticker(monkeypatch, tmp_path):
    art = _fresh_fixture()
    # a duplicate (surfaced, ticker) for AAPL with a DIFFERENT return — the first must win.
    art["rows"].append({"ticker": "AAPL", "sector": "Technology", "surfaced": "2026-07-01",
                        "return_pct": 99.9, "status": "stopped", "on_board": False})
    _point_at(monkeypatch, tmp_path, art)
    out = btr.records()
    aapls = [r for r in out if r["ticker"] == "AAPL"]
    assert len(aapls) == 1
    assert aapls[0]["return_pct"] == 7.4          # first-seen wins
    assert aapls[0]["status"] == "running"


def test_cache_reset_forces_reread(monkeypatch, tmp_path):
    _point_at(monkeypatch, tmp_path, _fresh_fixture())
    assert len(btr.records()) == 3
    (tmp_path / "us_board_track_record.json").write_text(json.dumps({**_fresh_fixture(), "rows": []}))
    assert len(btr.records()) == 3  # cached
    btr._reset_cache()
    assert btr.records() == []       # re-read


def test_fallback_path_used_when_primary_absent(monkeypatch, tmp_path):
    primary = tmp_path / "primary.json"          # not written
    fb_dir = tmp_path / "us_board_ledger"
    fb_dir.mkdir()
    fb_file = fb_dir / "track_record.json"
    fb_file.write_text(json.dumps(_fresh_fixture()))
    monkeypatch.setattr(btr, "_ARTIFACT_PATH", primary)
    monkeypatch.setattr(btr, "_ARTIFACT_PATH_FALLBACK", fb_file)
    btr._reset_cache()
    assert len(btr.records()) == 3


# --------------------------------------------------------------------------- #
# surfaced_on() — the point-in-time board-ENTRY set
# --------------------------------------------------------------------------- #

def test_surfaced_on_point_in_time_set(monkeypatch, tmp_path):
    _point_at(monkeypatch, tmp_path, _fresh_fixture())
    # AAPL + WDAY were both surfaced 2026-07-01; SNOW was surfaced 2026-06-20.
    assert btr.surfaced_on("2026-07-01") == {"AAPL", "WDAY"}
    assert btr.surfaced_on("2026-06-20") == {"SNOW"}
    assert btr.surfaced_on("2026-01-01") == set()   # a day nobody was surfaced


def test_surfaced_on_empty_asof(monkeypatch, tmp_path):
    _point_at(monkeypatch, tmp_path, _fresh_fixture())
    assert btr.surfaced_on("") == set()
    assert btr.surfaced_on(None) == set()  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# record() / on_board() / forward_grade()
# --------------------------------------------------------------------------- #

def test_record_returns_aapl_running_row(monkeypatch, tmp_path):
    _point_at(monkeypatch, tmp_path, _fresh_fixture())
    r = btr.record("AAPL")
    assert r is not None
    assert r["ticker"] == "AAPL"
    assert r["sector"] == "Technology"
    assert r["surfaced"] == "2026-07-01"
    assert r["return_pct"] == 7.4
    assert r["status"] == "running"
    assert r["on_board"] is True
    # case-insensitive lookup
    assert btr.record("aapl")["ticker"] == "AAPL"
    assert btr.record("NOPE") is None


def test_record_keep_first_across_dates(monkeypatch, tmp_path):
    art = _fresh_fixture()
    # a LATER re-surfacing of AAPL — record() must return the FIRST (earliest) surfacing.
    art["rows"].append({"ticker": "AAPL", "sector": "Technology", "surfaced": "2026-07-08",
                        "return_pct": 2.0, "status": "running", "on_board": True})
    _point_at(monkeypatch, tmp_path, art)
    r = btr.record("AAPL")
    assert r["surfaced"] == "2026-07-01"   # earliest wins


def test_on_board_set(monkeypatch, tmp_path):
    _point_at(monkeypatch, tmp_path, _fresh_fixture())
    assert btr.on_board() == {"AAPL"}   # only AAPL has on_board=true


def test_forward_grade(monkeypatch, tmp_path):
    _point_at(monkeypatch, tmp_path, _fresh_fixture())
    fg = btr.forward_grade("AAPL")
    assert fg == {"return_pct": 7.4, "status": "running", "fwd_mfe_pct": 9.1}
    assert btr.forward_grade("NOPE") is None


# --------------------------------------------------------------------------- #
# board_stats() — counts + win_rate + guards /0
# --------------------------------------------------------------------------- #

def test_board_stats_counts_and_win_rate(monkeypatch, tmp_path):
    _point_at(monkeypatch, tmp_path, _fresh_fixture())
    stats = btr.board_stats()
    assert stats["n"] == 3
    assert stats["running"] == 1
    assert stats["stopped"] == 1
    assert stats["flat"] == 1
    # win_rate = running / (running + stopped) = 1 / 2 = 0.5  (flat excluded from the denominator)
    assert stats["win_rate"] == pytest.approx(0.5)
    # avg_return = mean(7.4, -6.2, 0.4) = 0.5333...
    assert stats["avg_return"] == pytest.approx((7.4 - 6.2 + 0.4) / 3, abs=1e-4)


def test_board_stats_win_rate_guards_div_zero(monkeypatch, tmp_path):
    art = _fresh_fixture()
    # keep ONLY flat rows → running + stopped == 0 → win_rate must be None (no 0/0 crash).
    art["rows"] = [{"ticker": "FLT", "sector": "X", "surfaced": "2026-07-01",
                    "return_pct": 0.1, "status": "flat", "on_board": False}]
    _point_at(monkeypatch, tmp_path, art)
    stats = btr.board_stats()
    assert stats["n"] == 1
    assert stats["flat"] == 1
    assert stats["running"] == 0 and stats["stopped"] == 0
    assert stats["win_rate"] is None       # guarded /0
    assert stats["avg_return"] == pytest.approx(0.1)


def test_board_stats_empty_when_absent(monkeypatch, tmp_path):
    _point_at(monkeypatch, tmp_path, None)
    assert btr.board_stats() == {
        "n": 0, "running": 0, "stopped": 0, "flat": 0, "win_rate": None, "avg_return": None}


# --------------------------------------------------------------------------- #
# audit_row() — status transitions
# --------------------------------------------------------------------------- #

def test_audit_row_present(monkeypatch, tmp_path):
    _point_at(monkeypatch, tmp_path, _fresh_fixture())
    row = btr.audit_row()
    assert row["status"] == "present"
    assert row["n_rows"] == 3
    assert row["n_running"] == 1
    assert row["n_stopped"] == 1
    assert row["n_flat"] == 1


def test_audit_row_stale(monkeypatch, tmp_path):
    _point_at(monkeypatch, tmp_path, _fresh_fixture(days_old=9))
    row = btr.audit_row()
    assert row["status"] == "stale"
    assert row["n_rows"] == 0


def test_audit_row_absent(monkeypatch, tmp_path):
    _point_at(monkeypatch, tmp_path, None)
    row = btr.audit_row()
    assert row["status"] == "absent"
    assert row["n_rows"] == 0


def test_audit_row_wrong_schema_absent(monkeypatch, tmp_path):
    art = _fresh_fixture()
    art["schema"] = "nope"
    _point_at(monkeypatch, tmp_path, art)
    row = btr.audit_row()
    assert row["status"] == "absent"
