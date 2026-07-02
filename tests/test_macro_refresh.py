"""The vendored-macro freshness guard: staleness math + the tripwire (no network).

Tests are grouped into two sections:
  A. Original tests — preserved verbatim (is_stale thresholds, check_and_warn smoke).
  B. New anchor-hardening tests — tmp_path fake checkouts covering the W0 rewrite:
       all-anchors-present, one-missing, all-missing, stale-oldest-wins,
       stockdata-gap-reported, and anchors_report().

All tests use tmp_path or monkeypatch to stay network-free.
"""
import datetime
import json
from pathlib import Path

import pytest

from data_layer import macro_refresh as mr


# ---------------------------------------------------------------------------
# Section A — original tests (unchanged)
# ---------------------------------------------------------------------------

def test_is_stale_thresholds(monkeypatch):
    monkeypatch.setattr(mr, "asof", lambda: "2026-06-22")
    # boundary: > max_age_days is stale, == is not
    assert mr.is_stale(max_age_days=2, today=datetime.date(2026, 6, 23)) is False   # 1d old
    assert mr.is_stale(max_age_days=2, today=datetime.date(2026, 6, 24)) is False   # 2d == threshold
    assert mr.is_stale(max_age_days=2, today=datetime.date(2026, 6, 25)) is True    # 3d old -> stale
    # unknown date -> None (never assert stale on an unreadable date)
    monkeypatch.setattr(mr, "asof", lambda: None)
    assert mr.is_stale() is None


def test_check_and_warn_warns_and_blocks(monkeypatch):
    monkeypatch.setattr(mr, "asof", lambda: "2026-01-01")
    monkeypatch.setattr(mr, "is_stale", lambda *a, **k: True)
    monkeypatch.setattr(mr, "_collect_data_gaps", lambda: [])
    monkeypatch.setattr(mr, "anchors_report", lambda: {})
    msgs: list[str] = []
    info = mr.check_and_warn(block=False, log=msgs.append)
    assert info["stale"] is True and msgs and "STALE" in msgs[0]          # warns, does not raise
    with pytest.raises(RuntimeError):                                      # block -> refuse
        mr.check_and_warn(block=True, log=lambda *_: None)
    # fresh data never blocks even with block=True
    monkeypatch.setattr(mr, "is_stale", lambda *a, **k: False)
    assert mr.check_and_warn(block=True, log=lambda *_: None)["stale"] is False


# ---------------------------------------------------------------------------
# Section B — anchor-hardening tests (new for W0 / TASK 3)
# ---------------------------------------------------------------------------

def _make_checkout(tmp_path: Path, anchors: dict[str, dict | None], *,
                   stockdata: bool = True) -> Path:
    """Build a minimal fake macro_src checkout under tmp_path.

    anchors: maps anchor rel-path to the JSON dict to write, or None to skip (absent).
    stockdata: if True, create the site/stockdata/ directory.
    """
    src = tmp_path / "macro_src"
    for rel, payload in anchors.items():
        if payload is None:
            continue
        p = src / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(payload))
    if stockdata:
        (src / "site" / "stockdata").mkdir(parents=True, exist_ok=True)
    return src


def _patch_src(monkeypatch, src: Path):
    """Redirect mr._SRC to the fake checkout."""
    monkeypatch.setattr(mr, "_SRC", src)


# --- B1: all anchors present, minimum is returned ---------------------------

def test_all_anchors_present_returns_minimum(monkeypatch, tmp_path):
    """When all three anchors are present with different dates, asof() returns the oldest."""
    src = _make_checkout(tmp_path, {
        "site/factordata/us_standouts.json": {"as_of": "2026-06-30"},
        "data/regime/latest.json":           {"date": "2026-06-28"},   # oldest
        "site/sectordata/sector_cycles.json": {"meta": {"asOf": "2026-07-01"}},
    })
    _patch_src(monkeypatch, src)
    assert mr.asof() == "2026-06-28"   # regime is the stalest; that governs


def test_all_anchors_present_same_date(monkeypatch, tmp_path):
    """All anchors share the same date — asof() still returns that date."""
    src = _make_checkout(tmp_path, {
        "site/factordata/us_standouts.json": {"as_of": "2026-07-01"},
        "data/regime/latest.json":           {"date": "2026-07-01"},
        "site/sectordata/sector_cycles.json": {"meta": {"asOf": "2026-07-01"}},
    })
    _patch_src(monkeypatch, src)
    assert mr.asof() == "2026-07-01"


# --- B2: one anchor missing --------------------------------------------------

def test_one_anchor_missing_minimum_from_remainder(monkeypatch, tmp_path):
    """When sector_cycles is absent, the minimum is drawn from the other two."""
    src = _make_checkout(tmp_path, {
        "site/factordata/us_standouts.json": {"as_of": "2026-06-29"},
        "data/regime/latest.json":           {"date": "2026-06-27"},   # still oldest of two
        # sector_cycles absent
        "site/sectordata/sector_cycles.json": None,
    })
    _patch_src(monkeypatch, src)
    assert mr.asof() == "2026-06-27"


def test_one_anchor_missing_appears_in_data_gaps(monkeypatch, tmp_path):
    """A missing anchor file is surfaced in data_gaps."""
    src = _make_checkout(tmp_path, {
        "site/factordata/us_standouts.json": {"as_of": "2026-06-30"},
        "data/regime/latest.json":           {"date": "2026-06-30"},
        # sector_cycles absent
        "site/sectordata/sector_cycles.json": None,
    }, stockdata=False)   # also omit stockdata so we can distinguish the two gaps
    _patch_src(monkeypatch, src)
    gaps = mr._collect_data_gaps()
    assert "site/sectordata/sector_cycles.json" in gaps
    assert "site/stockdata" in gaps


# --- B3: all anchors missing -------------------------------------------------

def test_all_anchors_missing_asof_none(monkeypatch, tmp_path):
    """When no anchor file exists, asof() returns None (do not assert stale)."""
    src = _make_checkout(tmp_path, {
        "site/factordata/us_standouts.json": None,
        "data/regime/latest.json":           None,
        "site/sectordata/sector_cycles.json": None,
    }, stockdata=False)
    _patch_src(monkeypatch, src)
    assert mr.asof() is None


def test_all_anchors_missing_is_stale_none(monkeypatch, tmp_path):
    """When asof() is None, is_stale() returns None (unknown is not stale)."""
    src = _make_checkout(tmp_path, {
        "site/factordata/us_standouts.json": None,
        "data/regime/latest.json":           None,
        "site/sectordata/sector_cycles.json": None,
    }, stockdata=False)
    _patch_src(monkeypatch, src)
    assert mr.is_stale() is None


def test_all_anchors_missing_all_appear_in_data_gaps(monkeypatch, tmp_path):
    """When no anchor file exists, all three appear in data_gaps."""
    src = _make_checkout(tmp_path, {
        "site/factordata/us_standouts.json": None,
        "data/regime/latest.json":           None,
        "site/sectordata/sector_cycles.json": None,
    }, stockdata=False)
    _patch_src(monkeypatch, src)
    gaps = mr._collect_data_gaps()
    assert "site/factordata/us_standouts.json" in gaps
    assert "data/regime/latest.json" in gaps
    assert "site/sectordata/sector_cycles.json" in gaps
    assert "site/stockdata" in gaps


# --- B4: stale-oldest-wins ---------------------------------------------------

def test_stale_oldest_wins_triggers_staleness(monkeypatch, tmp_path):
    """Even if two anchors are fresh, the stalest one governs is_stale()."""
    # regime is 5 days old; everything else is fresh (today)
    src = _make_checkout(tmp_path, {
        "site/factordata/us_standouts.json": {"as_of": "2026-07-01"},
        "data/regime/latest.json":           {"date": "2026-06-25"},   # 6d old
        "site/sectordata/sector_cycles.json": {"meta": {"asOf": "2026-07-01"}},
    })
    _patch_src(monkeypatch, src)
    # With today=2026-07-01, regime is 6 days old > max_age_days=2
    assert mr.is_stale(max_age_days=2, today=datetime.date(2026, 7, 1)) is True
    # Confirm asof() returned the minimum
    assert mr.asof() == "2026-06-25"


def test_fresh_anchors_not_stale(monkeypatch, tmp_path):
    """When all anchors are within max_age_days, is_stale() is False."""
    src = _make_checkout(tmp_path, {
        "site/factordata/us_standouts.json": {"as_of": "2026-07-01"},
        "data/regime/latest.json":           {"date": "2026-07-01"},
        "site/sectordata/sector_cycles.json": {"meta": {"asOf": "2026-07-01"}},
    })
    _patch_src(monkeypatch, src)
    assert mr.is_stale(max_age_days=2, today=datetime.date(2026, 7, 1)) is False


# --- B5: stockdata gap always reported --------------------------------------

def test_stockdata_gap_reported_when_absent(monkeypatch, tmp_path):
    """site/stockdata/ absence is always in data_gaps, regardless of anchor freshness."""
    src = _make_checkout(tmp_path, {
        "site/factordata/us_standouts.json": {"as_of": "2026-07-01"},
        "data/regime/latest.json":           {"date": "2026-07-01"},
        "site/sectordata/sector_cycles.json": {"meta": {"asOf": "2026-07-01"}},
    }, stockdata=False)   # <-- the key: no site/stockdata/
    _patch_src(monkeypatch, src)
    gaps = mr._collect_data_gaps()
    assert "site/stockdata" in gaps
    # Confirm the stockdata gap doesn't suppress anchor dates
    assert mr.asof() == "2026-07-01"


def test_stockdata_gap_not_reported_when_present(monkeypatch, tmp_path):
    """site/stockdata/ present means it does NOT appear in data_gaps."""
    src = _make_checkout(tmp_path, {
        "site/factordata/us_standouts.json": {"as_of": "2026-07-01"},
        "data/regime/latest.json":           {"date": "2026-07-01"},
        "site/sectordata/sector_cycles.json": {"meta": {"asOf": "2026-07-01"}},
    }, stockdata=True)   # <-- stockdata exists
    _patch_src(monkeypatch, src)
    gaps = mr._collect_data_gaps()
    assert "site/stockdata" not in gaps


def test_stockdata_gap_logged_in_check_and_warn(monkeypatch, tmp_path):
    """The DATA GAPS warning is logged when site/stockdata/ is absent."""
    src = _make_checkout(tmp_path, {
        "site/factordata/us_standouts.json": {"as_of": "2026-07-01"},
        "data/regime/latest.json":           {"date": "2026-07-01"},
        "site/sectordata/sector_cycles.json": {"meta": {"asOf": "2026-07-01"}},
    }, stockdata=False)
    _patch_src(monkeypatch, src)
    msgs: list[str] = []
    info = mr.check_and_warn(block=False, log=msgs.append)
    assert "site/stockdata" in info["data_gaps"]
    gap_msgs = [m for m in msgs if "DATA GAPS" in m]
    assert gap_msgs, "expected a DATA GAPS warning in the log"
    assert "site/stockdata" in gap_msgs[0]


def test_no_gap_warning_when_stockdata_present(monkeypatch, tmp_path):
    """No DATA GAPS warning when site/stockdata/ is present and anchors are fresh."""
    src = _make_checkout(tmp_path, {
        "site/factordata/us_standouts.json": {"as_of": "2026-07-01"},
        "data/regime/latest.json":           {"date": "2026-07-01"},
        "site/sectordata/sector_cycles.json": {"meta": {"asOf": "2026-07-01"}},
    }, stockdata=True)
    _patch_src(monkeypatch, src)
    msgs: list[str] = []
    mr.check_and_warn(block=False, log=msgs.append)
    gap_msgs = [m for m in msgs if "DATA GAPS" in m]
    assert not gap_msgs


# --- B6: anchors_report() ---------------------------------------------------

def test_anchors_report_all_present(monkeypatch, tmp_path):
    """anchors_report() returns per-label dates when all anchors resolve."""
    src = _make_checkout(tmp_path, {
        "site/factordata/us_standouts.json": {"as_of": "2026-07-01"},
        "data/regime/latest.json":           {"date": "2026-06-29"},
        "site/sectordata/sector_cycles.json": {"meta": {"asOf": "2026-06-30"}},
    })
    _patch_src(monkeypatch, src)
    report = mr.anchors_report()
    assert report["us_standouts"] == "2026-07-01"
    assert report["regime_latest"] == "2026-06-29"
    assert report["sector_cycles"] == "2026-06-30"


def test_anchors_report_missing_is_none(monkeypatch, tmp_path):
    """anchors_report() returns None for absent anchors without raising."""
    src = _make_checkout(tmp_path, {
        "site/factordata/us_standouts.json": {"as_of": "2026-07-01"},
        "data/regime/latest.json":           None,   # absent
        "site/sectordata/sector_cycles.json": None,  # absent
    })
    _patch_src(monkeypatch, src)
    report = mr.anchors_report()
    assert report["us_standouts"] == "2026-07-01"
    assert report["regime_latest"] is None
    assert report["sector_cycles"] is None


def test_anchors_report_included_in_check_and_warn(monkeypatch, tmp_path):
    """check_and_warn() result includes 'anchors' key with per-anchor resolution."""
    src = _make_checkout(tmp_path, {
        "site/factordata/us_standouts.json": {"as_of": "2026-07-01"},
        "data/regime/latest.json":           {"date": "2026-07-01"},
        "site/sectordata/sector_cycles.json": {"meta": {"asOf": "2026-07-01"}},
    })
    _patch_src(monkeypatch, src)
    info = mr.check_and_warn(block=False, log=lambda *_: None)
    assert "anchors" in info
    assert isinstance(info["anchors"], dict)
    assert "us_standouts" in info["anchors"]
    assert "regime_latest" in info["anchors"]
    assert "sector_cycles" in info["anchors"]


# --- B7: backwards-compatibility — all original return keys preserved --------

def test_refresh_and_check_return_keys_present(monkeypatch):
    """refresh_and_check() always returns the original keys (add, never remove)."""
    monkeypatch.setattr(mr, "refresh", lambda: "2026-07-01")
    monkeypatch.setattr(mr, "check_and_warn",
                        lambda **kw: {"asof": "2026-07-01", "stale": False,
                                      "max_age_days": 2, "data_gaps": [],
                                      "anchors": {}})
    info = mr.refresh_and_check(log=lambda *_: None)
    # original keys must all survive
    for key in ("asof", "stale", "max_age_days", "refreshed_to"):
        assert key in info, f"missing key: {key}"
    # new keys must be present too
    assert "data_gaps" in info
    assert "anchors" in info


def test_check_and_warn_original_keys_present(monkeypatch):
    """check_and_warn() must include all original return keys (regression guard)."""
    monkeypatch.setattr(mr, "asof", lambda: "2026-07-01")
    monkeypatch.setattr(mr, "is_stale", lambda *a, **k: False)
    monkeypatch.setattr(mr, "_collect_data_gaps", lambda: [])
    monkeypatch.setattr(mr, "anchors_report", lambda: {})
    info = mr.check_and_warn(block=False, log=lambda *_: None)
    assert "asof" in info
    assert "stale" in info
    assert "max_age_days" in info


# --- B8: date-field fall-through coverage -----------------------------------

def test_regime_date_field_primary(monkeypatch, tmp_path):
    """regime/latest.json uses 'date' not 'as_of' — verify it is read correctly."""
    src = _make_checkout(tmp_path, {
        "site/factordata/us_standouts.json": {"as_of": "2026-07-01"},
        "data/regime/latest.json":           {"date": "2026-06-30", "quad": 1},
        "site/sectordata/sector_cycles.json": {"meta": {"asOf": "2026-07-01"}},
    })
    _patch_src(monkeypatch, src)
    report = mr.anchors_report()
    assert report["regime_latest"] == "2026-06-30"
    # And the minimum propagates through asof()
    assert mr.asof() == "2026-06-30"


def test_sector_cycles_camelcase_asOf(monkeypatch, tmp_path):
    """sector_cycles uses meta.asOf (camelCase) — verify it is read correctly."""
    src = _make_checkout(tmp_path, {
        "site/factordata/us_standouts.json": {"as_of": "2026-07-01"},
        "data/regime/latest.json":           {"date": "2026-07-01"},
        "site/sectordata/sector_cycles.json": {"meta": {"asOf": "2026-06-28"}},
    })
    _patch_src(monkeypatch, src)
    report = mr.anchors_report()
    assert report["sector_cycles"] == "2026-06-28"
    assert mr.asof() == "2026-06-28"   # oldest → governs
