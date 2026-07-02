"""P-NEW-2: the bot must RESPECT the us_standouts board's own `gate_go` Phase-0 verdict.

When gate_go is explicitly False the board is "a confluence read, NOT a standalone alpha" — so the
two standout consumers (portfolio/conviction._us_standouts + brain/intake._from_standouts) DROP the
board's names. Invariant-safe: a MISSING gate_go (legacy artifacts) or a truthy gate_go degrades to
today's ingest behaviour — only an explicit False skips, and skipping only ever REMOVES names.

Fixture-injected (DI style): conviction._load and intake._read are monkeypatched to return the
trimmed us_standouts fixtures rather than reading the live vendor path.
"""
import json
from pathlib import Path

import bot  # noqa: F401

from brain import intake
from portfolio import conviction

_FIX = Path(__file__).resolve().parent / "fixtures"


def _load_fixture(name: str) -> dict:
    return json.loads((_FIX / name).read_text())


# --------------------------------------------------------------------------- #
# conviction._us_standouts()
# --------------------------------------------------------------------------- #
def test_conviction_gate_false_drops_standouts(monkeypatch, caplog):
    """gate_go=False (present, falsy) → _us_standouts() returns [] and logs a loud warning."""
    fix = _load_fixture("us_standouts_gate_false.json")
    monkeypatch.setattr(conviction, "_load", lambda rel: fix if "us_standouts" in rel else None)
    import logging
    with caplog.at_level(logging.WARNING, logger="portfolio.conviction"):
        assert conviction._us_standouts() == []
    assert any("gate_go=False" in r.getMessage() for r in caplog.records)


def test_conviction_gate_true_ingests_standouts(monkeypatch):
    """gate_go=True → names ingested exactly as today."""
    fix = _load_fixture("us_standouts_gate_true.json")
    monkeypatch.setattr(conviction, "_load", lambda rel: fix if "us_standouts" in rel else None)
    assert conviction._us_standouts() == ["WDAY", "SNOW"]


def test_conviction_gate_absent_ingests_standouts(monkeypatch):
    """gate_go field ABSENT (legacy artifact) → today's behaviour, ingest (never skip on missing)."""
    fix = _load_fixture("us_standouts_no_gate.json")
    assert "gate_go" not in fix
    monkeypatch.setattr(conviction, "_load", lambda rel: fix if "us_standouts" in rel else None)
    assert conviction._us_standouts() == ["WDAY", "SNOW"]


def test_conviction_gate_false_but_toggle_off_ingests(monkeypatch):
    """The doctrine toggle can be flipped off — then even gate_go=False ingests (reversible fix)."""
    fix = _load_fixture("us_standouts_gate_false.json")
    monkeypatch.setattr(conviction, "_load", lambda rel: fix if "us_standouts" in rel else None)
    monkeypatch.setattr(conviction, "_respect_standout_gate", lambda: False)
    assert conviction._us_standouts() == ["WDAY", "SNOW"]


def test_conviction_candidates_excludes_standout_only_names_when_gated(monkeypatch):
    """Integration: with gate_go=False and no other source, standout-only names never reach the
    candidate universe. Names present only on the (now-skipped) board are absent from candidates()."""
    fix = _load_fixture("us_standouts_gate_false.json")
    monkeypatch.setattr(conviction, "_load", lambda rel: fix if "us_standouts" in rel else None)
    # neutralise the other candidate sources so only the standout board could contribute WDAY/SNOW
    monkeypatch.setattr(conviction, "_SHORTLIST", [])
    monkeypatch.setattr(conviction, "_basket_top_picks", lambda n=100: [])
    monkeypatch.setattr("brain.ledger.all_theses", lambda: [])
    monkeypatch.setattr("brain.intake.tickers", lambda limit=60, min_score=0.4: [])
    cands = conviction.candidates()
    assert "WDAY" not in cands and "SNOW" not in cands


# --------------------------------------------------------------------------- #
# intake._from_standouts()
# --------------------------------------------------------------------------- #
def test_intake_gate_false_skips_source(monkeypatch, caplog):
    """gate_go=False → _from_standouts() returns {} (no positive corroboration) and logs a warning."""
    fix = _load_fixture("us_standouts_gate_false.json")
    monkeypatch.setattr(intake, "_read", lambda rel: fix if "us_standouts" in rel else None)
    import logging
    with caplog.at_level(logging.WARNING, logger="brain.intake"):
        assert intake._from_standouts() == {}
    assert any("gate_go=False" in r.getMessage() for r in caplog.records)


def test_intake_gate_true_ingests_source(monkeypatch):
    """gate_go=True → names ingested; provenance carries the published stop/buy_zone/entry_grade."""
    fix = _load_fixture("us_standouts_gate_true.json")
    monkeypatch.setattr(intake, "_read", lambda rel: fix if "us_standouts" in rel else None)
    out = intake._from_standouts()
    assert set(out) == {"WDAY", "SNOW"}
    assert out["WDAY"]["lean"] == 1
    assert out["WDAY"]["stop"] == 113.0
    assert out["WDAY"]["buy_zone"] == {"low": 124.0, "high": 130.3, "pct_from_spot": -2.4}
    assert out["WDAY"]["entry_grade"] == "solid"


def test_intake_gate_absent_ingests_source(monkeypatch):
    """gate_go ABSENT → today's behaviour, ingest."""
    fix = _load_fixture("us_standouts_no_gate.json")
    monkeypatch.setattr(intake, "_read", lambda rel: fix if "us_standouts" in rel else None)
    assert set(intake._from_standouts()) == {"WDAY", "SNOW"}


def test_intake_gate_false_but_toggle_off_ingests(monkeypatch):
    fix = _load_fixture("us_standouts_gate_false.json")
    monkeypatch.setattr(intake, "_read", lambda rel: fix if "us_standouts" in rel else None)
    monkeypatch.setattr(intake, "_respect_standout_gate", lambda: False)
    assert set(intake._from_standouts()) == {"WDAY", "SNOW"}
