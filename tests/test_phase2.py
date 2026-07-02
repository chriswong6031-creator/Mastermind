"""Phase 2 acceptance — gated multi-name 3-sleeve book on real data."""
from pathlib import Path

import pytest

import bot  # noqa: F401

from bot import phase2
from data_layer import store

# NOTE: the store DB is isolated to a tmp file per test by the autouse `_isolate_bot_db`
# conftest fixture, which monkeypatches `store._DB`. `_fresh()` therefore reads `store._DB`
# at call time (not a frozen module constant) so its wipe hits the SAME isolated DB that
# `store.connect()` / `phase2.run()` use — and never the live data/bot.db.

# Live-data guard: parts of this suite assert against a REAL vendored render (per-name
# site/stockdata/*.json). On a checkout where the stockdata contract is absent (the known
# origin/main publish gap), the fail-closed coverage gate correctly returns
# size_authority='insufficient_data' for every name — asserting 'up' there would re-encode
# the fail-open bug this repo just fixed. Skip the live-data assertions instead.
_STOCKDATA = Path(__file__).resolve().parent.parent / "vendor" / "macro" / "site" / "stockdata"
_HAS_STOCKDATA = (_STOCKDATA / "NVDA.json").exists()
_needs_stockdata = pytest.mark.skipif(
    not _HAS_STOCKDATA, reason="vendored site/stockdata absent — fail-closed gate blocks all entries by design"
)


def _fresh():
    db = store._DB              # the tmp-isolated path (see _isolate_bot_db); never the live DB
    if db.exists():
        db.unlink()


@_needs_stockdata
def test_phase2_multiname_book_and_gate():
    _fresh()
    out = phase2.run()                       # first run -> fires
    assert out["ran"] and "first_run" in out["triggers"]

    book = out["book"]
    lead = [p for p in book if p["sleeve"] == "leadership"]
    conv = [p for p in book if p["sleeve"] == "conviction"]
    assert lead                                             # leadership sleeve always present

    # doctrine: PRESENT in the leaders mechanically...
    assert out["sleeves"]["leadership"] > 0.3
    # ...and conviction names are ONLY there because the multi-sided matrix confirmed all sides
    from portfolio import lenses
    for p in conv:
        syn = lenses.full(p["ticker"], "name")["synthesis"]
        assert syn["size_authority"] == "up" and not syn["vetoes"]   # confirmed + veto-clear
        assert p["weight"] <= 0.08 + 1e-9                            # name cap
    # NVDA is a cheap-for-growth leader (PEG ~0.25): after the valuation/13F alignment fix it
    # CLEARS the gate (no longer the raw-value-factor 'distribution' false-reject). It is eligible
    # for the conviction sleeve — the old 'NVDA must be excluded' assertion encoded the bug.
    assert lenses.full("NVDA", "name")["synthesis"]["size_authority"] == "up"
    # genuine hard vetoes (parabolic / distress / cycle-blocked) still exclude a name from the book
    for p in conv:
        assert not lenses.full(p["ticker"], "name")["synthesis"]["vetoes"]
    assert out["sleeves"]["cash"] >= 0.05

    # the gate carries forward when nothing material changed
    out2 = phase2.run()
    assert out2["ran"] is False

    # a forced event interrupt re-runs
    assert phase2.run(force=True)["ran"] is True
    _fresh()


def test_store_roundtrip():
    _fresh()
    phase2.run()
    con = store.connect()
    # With stockdata present, conviction entries join the 4 leadership legs (>=5 rows).
    # Without it, the fail-closed gate freezes new conviction adds — leadership only (>=4).
    _min_rows = 5 if _HAS_STOCKDATA else 4
    assert con.execute("SELECT count(*) FROM positions").fetchone()[0] >= _min_rows
    if _HAS_STOCKDATA:  # theses are authored for conviction entries; none exist under the freeze
        assert con.execute("SELECT count(*) FROM theses").fetchone()[0] >= 1
    _fresh()


def test_runs_table_written_and_dedup():
    """record_run must write to the runs table so same-day re-runs carry forward.

    The empty-runs-table bug: when bot.db is wiped (e.g. by tests), store.last_run()
    returns None -> gate.should_run() always fires 'first_run' -> same-day dedup is
    broken and every intraday call triggers a full rebuild.  This test proves that a
    successful run writes exactly one row, and that a same-day second call reads that
    row and carries forward instead of rebuilding.
    """
    _fresh()
    # First run — must fire (first_run) AND write a row to the runs table.
    out = phase2.run()
    assert out["ran"] is True
    con = store.connect()
    lr = store.last_run(con)
    assert lr is not None, "record_run was not called: runs table is empty after a successful build"
    assert lr["ran"] == 1, "record_run wrote ran=False instead of True for a successful build"

    # Same-day second call — must carry forward (dedup) using the stored row, NOT rebuild.
    out2 = phase2.run()
    assert out2["ran"] is False, (
        "same-day dedup failed: gate.should_run fired a second rebuild even though "
        "today's run is already in the runs table"
    )
    _fresh()
