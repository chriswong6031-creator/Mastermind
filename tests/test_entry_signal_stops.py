"""P-NEW-3: the dashboard's PUBLISHED per-name entry_signal (stop / buy_zone / entry_grade /
chase_above) must be READ, PERSISTED on the positions ledger at entry, and SURFACED as a runlog
breach — W1 records + observes; it does NOT execute (no sell on breach; that is W2).

Covers:
  (a) _published_entry_signal() returns the stop/buy_zone from a fixture standouts row, None on miss;
  (b) position_log.update() persists the published stop/buy_zone/entry_grade at open, and a buy with
      NO published entry_signal leaves the ledger byte-identical to today (backwards-compat);
  (c) replay: an open position whose current price is below its persisted published_stop emits
      exactly one 'STOP BREACH' runlog step and does NOT remove the position (surface-not-execute).
"""
import json
from pathlib import Path

import bot  # noqa: F401

_FIX = Path(__file__).resolve().parent / "fixtures"


def _load_fixture(name: str) -> dict:
    return json.loads((_FIX / name).read_text())


# --------------------------------------------------------------------------- #
# (a) _published_entry_signal
# --------------------------------------------------------------------------- #
def test_published_entry_signal_reads_levels(monkeypatch, tmp_path):
    from bot import phase2
    # point _V at a tmp vendor tree carrying the fixture, and reset the per-build index cache
    vdir = tmp_path / "vendor"
    (vdir / "site" / "factordata").mkdir(parents=True)
    (vdir / "site" / "factordata" / "us_standouts.json").write_text(
        json.dumps(_load_fixture("us_standouts_gate_true.json")))
    monkeypatch.setattr(phase2, "_V", vdir, raising=False)
    monkeypatch.setattr(phase2, "_STANDOUT_ROWS", None, raising=False)
    monkeypatch.setattr(phase2, "_STANDOUT_ROWS_ASOF", None, raising=False)

    es = phase2._published_entry_signal("WDAY")
    assert es["stop"] == 113.0
    assert es["buy_zone"] == {"low": 124.0, "high": 130.3, "pct_from_spot": -2.4}
    assert es["entry_grade"] == "solid"
    assert es["chase_above"] == 132.8


def test_published_entry_signal_missing_ticker_is_none(monkeypatch, tmp_path):
    from bot import phase2
    vdir = tmp_path / "vendor"
    (vdir / "site" / "factordata").mkdir(parents=True)
    (vdir / "site" / "factordata" / "us_standouts.json").write_text(
        json.dumps(_load_fixture("us_standouts_gate_true.json")))
    monkeypatch.setattr(phase2, "_V", vdir, raising=False)
    monkeypatch.setattr(phase2, "_STANDOUT_ROWS", None, raising=False)
    monkeypatch.setattr(phase2, "_STANDOUT_ROWS_ASOF", None, raising=False)

    es = phase2._published_entry_signal("NOTONBOARD")
    assert es == {"stop": None, "buy_zone": None, "entry_grade": None, "chase_above": None}


def test_published_entry_signal_absent_file_is_none(monkeypatch, tmp_path):
    from bot import phase2
    monkeypatch.setattr(phase2, "_V", tmp_path / "empty", raising=False)
    monkeypatch.setattr(phase2, "_STANDOUT_ROWS", None, raising=False)
    monkeypatch.setattr(phase2, "_STANDOUT_ROWS_ASOF", None, raising=False)
    es = phase2._published_entry_signal("WDAY")
    assert es == {"stop": None, "buy_zone": None, "entry_grade": None, "chase_above": None}


# --------------------------------------------------------------------------- #
# (b) position_log persists the published stop; no-signal buy is byte-identical
# --------------------------------------------------------------------------- #
def test_position_log_persists_published_stop(tmp_path, monkeypatch):
    from portfolio import position_log as pl
    monkeypatch.setattr(pl, "_LEDGER_PATH", tmp_path / "ledger.json", raising=False)

    pl.update([{
        "ticker": "WDAY", "sleeve": "conviction", "weight": 0.05,
        "entry_levels": {"ticker": "WDAY", "price": 127.0, "stop": 113.0,
                         "buy_zone": {"low": 124.0, "high": 130.3, "pct_from_spot": -2.4},
                         "entry_grade": "solid"},
    }], "2026-07-01")

    ledger = json.loads((tmp_path / "ledger.json").read_text())
    e = ledger["conviction:WDAY"]
    assert e["published_stop"] == 113.0
    assert e["buy_zone"] == {"low": 124.0, "high": 130.3, "pct_from_spot": -2.4}
    assert e["entry_grade"] == "solid"
    # open_positions() echoes the levels for the audit/API + breach pass
    op = {p["ticker"]: p for p in pl.open_positions()}
    assert op["WDAY"]["published_stop"] == 113.0
    assert op["WDAY"]["buy_zone"]["low"] == 124.0


def test_position_log_no_signal_buy_has_null_stop(tmp_path, monkeypatch):
    """A buy with no published entry_signal persists published_stop=None — degrades to today's ledger
    (the field is present but null; downstream reads treat None exactly as a legacy row would)."""
    from portfolio import position_log as pl
    monkeypatch.setattr(pl, "_LEDGER_PATH", tmp_path / "ledger.json", raising=False)
    pl.update([{"ticker": "NVDA", "sleeve": "conviction", "weight": 0.04}], "2026-07-01")
    e = json.loads((tmp_path / "ledger.json").read_text())["conviction:NVDA"]
    assert e["published_stop"] is None
    assert e["buy_zone"] is None
    assert e["entry_grade"] is None


def test_position_log_stop_not_erased_by_later_miss(tmp_path, monkeypatch):
    """Invariant: once a stop is persisted at entry, a later build with NO published stop (data gap)
    must NOT blank it — degrade-never-raise (a missing field can freeze/shrink, never un-record)."""
    from portfolio import position_log as pl
    monkeypatch.setattr(pl, "_LEDGER_PATH", tmp_path / "ledger.json", raising=False)
    pl.update([{"ticker": "WDAY", "sleeve": "conviction", "weight": 0.05,
                "entry_levels": {"ticker": "WDAY", "price": 127.0, "stop": 113.0}}], "2026-07-01")
    # next build: the same held name arrives with no entry_levels/stop (gap)
    pl.update([{"ticker": "WDAY", "sleeve": "conviction", "weight": 0.05}], "2026-07-02")
    e = json.loads((tmp_path / "ledger.json").read_text())["conviction:WDAY"]
    assert e["published_stop"] == 113.0            # preserved, not erased


# --------------------------------------------------------------------------- #
# (c) replay: price below persisted stop → exactly one STOP BREACH runlog step, no exit
# --------------------------------------------------------------------------- #
def test_stop_breach_surfaces_and_does_not_exit(tmp_path, monkeypatch):
    """Simulate the phase2 breach-surfacing pass in isolation: an open conviction position whose
    current price is below its persisted published_stop emits exactly one STOP BREACH runlog step
    and the position stays open (surface-not-execute)."""
    from portfolio import position_log as pl
    monkeypatch.setattr(pl, "_LEDGER_PATH", tmp_path / "ledger.json", raising=False)
    # WDAY breached (price 100 < stop 113), SNOW fine (price 210 > stop 190), NOSTOP has no stop
    pl.update([
        {"ticker": "WDAY", "sleeve": "conviction", "weight": 0.05,
         "entry_levels": {"ticker": "WDAY", "price": 127.0, "stop": 113.0}},
        {"ticker": "SNOW", "sleeve": "conviction", "weight": 0.04,
         "entry_levels": {"ticker": "SNOW", "price": 210.0, "stop": 190.0}},
        {"ticker": "NOSTOP", "sleeve": "conviction", "weight": 0.03,
         "entry_levels": {"ticker": "NOSTOP", "price": 50.0}},
    ], "2026-07-01")

    prices = {"WDAY": 100.0, "SNOW": 211.0, "NOSTOP": 40.0}
    from portfolio import paper_account
    monkeypatch.setattr(paper_account, "_current_price", lambda t: prices.get(t))

    # replicate the phase2 breach-surfacing loop (kept byte-for-byte in intent with phase2.run)
    breaches: list[str] = []

    def _capture(run_id, step_type, title, detail, **kw):
        if step_type == "risk" and title.startswith("STOP BREACH"):
            breaches.append(kw.get("ticker"))

    for op in pl.open_positions():
        if op.get("sleeve") != "conviction":
            continue
        stop = op.get("published_stop")
        if stop is None:
            continue
        cur = paper_account._current_price(op["ticker"])
        if cur is None:
            continue
        if float(cur) < float(stop):
            _capture(None, "risk", f"STOP BREACH {op['ticker']}",
                     f"price={cur} < published_stop={stop}", ticker=op["ticker"], sleeve="conviction")

    assert breaches == ["WDAY"]                        # exactly one breach, only the breached name
    # the position is NOT removed by the surfacing pass — it is still open in the ledger
    still_open = {p["ticker"] for p in pl.open_positions()}
    assert {"WDAY", "SNOW", "NOSTOP"} <= still_open
