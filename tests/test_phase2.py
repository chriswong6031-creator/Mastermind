"""Phase 2 acceptance — gated multi-name 3-sleeve book on real data."""
from pathlib import Path

import bot  # noqa: F401

from bot import phase2
from data_layer import store

_DB = Path(__file__).resolve().parent.parent / "data" / "bot.db"


def _fresh():
    if _DB.exists():
        _DB.unlink()


def test_phase2_multiname_book_and_gate():
    _fresh()
    out = phase2.run()                       # first run -> fires
    assert out["ran"] and "first_run" in out["triggers"]

    book = out["book"]
    lead = [p for p in book if p["sleeve"] == "leadership"]
    conv = [p for p in book if p["sleeve"] == "conviction"]
    assert lead and conv                                    # both sleeves populated

    # doctrine: PRESENT in the leader mechanically (leadership sleeve sized)...
    assert out["sleeves"]["leadership"] > 0.3
    # ...without CHASING the single name (conviction gated to a watch at 0 while dims unverified)
    nvda = next(p for p in conv if p["ticker"] == "NVDA")
    assert nvda["verdict"] == "watch" and nvda["weight"] == 0.0

    # firebreaks: no single name over the cap; cash preserves rotation capacity
    assert all(p["weight"] <= 0.08 + 1e-9 for p in conv)
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
    assert con.execute("SELECT count(*) FROM positions").fetchone()[0] >= 5
    assert con.execute("SELECT count(*) FROM theses").fetchone()[0] >= 1
    _fresh()
