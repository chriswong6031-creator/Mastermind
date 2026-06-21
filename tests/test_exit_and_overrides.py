"""Commit-2 wiring: the hard-exit sweep, the single-position close, the ledger close/unlock, and
the self-deriving risk-officer clamp. All offline (tmp paths + monkeypatched lens reads)."""
from __future__ import annotations

import json
from unittest import mock

import bot  # noqa: F401


# ---------------------------------------------------------------------------
# _is_hard_exit (the gate-closed sweep predicate)
# ---------------------------------------------------------------------------

def test_is_hard_exit_predicate():
    from bot.phase2 import _is_hard_exit
    assert _is_hard_exit({"vetoes": ["parabolic"]}) is True
    assert _is_hard_exit({"price_downtrend": True}) is True
    assert _is_hard_exit({"size_authority": "blocked"}) is True
    # a softened-but-not-broken name is NOT a hard exit (hysteresis carries it)
    assert _is_hard_exit({"vetoes": [], "price_downtrend": False, "size_authority": "hold"}) is False
    assert _is_hard_exit({"vetoes": [], "price_downtrend": False, "size_authority": "up"}) is False
    # a fresh falling-knife alone is not a hard exit (entry veto, not exit)
    assert _is_hard_exit({"vetoes": [], "price_downtrend": False, "price_falling_fast": True,
                          "size_authority": "hold"}) is False


# ---------------------------------------------------------------------------
# position_log.close_position — closes ONE position, leaves the rest open
# ---------------------------------------------------------------------------

def test_close_position_closes_only_target(tmp_path, monkeypatch):
    from portfolio import position_log as pl
    monkeypatch.setattr(pl, "_LEDGER_PATH", tmp_path / "positions.json", raising=False)
    pl.update([{"ticker": "NVDA", "sleeve": "conviction", "weight": 0.05},
               {"ticker": "SMH", "sleeve": "leadership", "weight": 0.10}], "2026-06-20")
    assert {p["ticker"] for p in pl.open_positions()} == {"NVDA", "SMH"}

    assert pl.close_position("conviction", "NVDA", "2026-06-21", reason="hard_exit_sweep") is True
    assert {p["ticker"] for p in pl.open_positions()} == {"SMH"}, "only NVDA should close"
    assert "NVDA" in {p["ticker"] for p in pl.closed_positions()}
    # idempotent: closing an already-closed position is a no-op
    assert pl.close_position("conviction", "NVDA", "2026-06-22") is False


# ---------------------------------------------------------------------------
# ledger.close — removes the dedup lock so a name can be re-proposed
# ---------------------------------------------------------------------------

def test_ledger_close_unlocks_resubmit(tmp_path, monkeypatch):
    from brain import ledger
    monkeypatch.setattr(ledger, "_LEDGER", tmp_path / "theses.jsonl", raising=False)
    assert ledger.append({"id": "d1", "subject": "NVDA", "lean": "add"}) is True
    assert ledger.append({"id": "d2", "subject": "NVDA", "lean": "add"}) is False  # dedup lock
    assert "NVDA" in ledger.open_subjects()

    assert ledger.close("NVDA", "exited") == 1
    assert "NVDA" not in ledger.open_subjects()
    # now a fresh thesis on the same subject is accepted again
    assert ledger.append({"id": "d3", "subject": "NVDA", "lean": "add"}) is True
    assert ledger.close("MISSING") == 0


# ---------------------------------------------------------------------------
# risk-officer clamp — self-derives engine-blocked status (daily loop passes no set)
# ---------------------------------------------------------------------------

def test_engine_blocked_true_on_veto(monkeypatch):
    from brain import research_desk as rd
    from portfolio import lenses
    monkeypatch.setattr(lenses, "full",
                        lambda s, k="name": {"synthesis": {"vetoes": ["parabolic"], "size_authority": "blocked"}})
    assert rd._engine_blocked("NVDA") is True


def test_engine_blocked_false_when_clean(monkeypatch):
    from brain import research_desk as rd
    from portfolio import lenses
    monkeypatch.setattr(lenses, "full",
                        lambda s, k="name": {"synthesis": {"vetoes": [], "size_authority": "up"}})
    assert rd._engine_blocked("AVGO") is False


def test_ingest_clamps_bullish_lean_on_engine_blocked(tmp_path, monkeypatch):
    """A bullish Claude proposal on an engine-blocked name must be de-escalated to 'watch' even when
    the caller passes no blocked set (the daily-loop path)."""
    from brain import research_desk as rd
    from brain import ledger
    monkeypatch.setattr(rd, "_PROPOSALS", tmp_path / "proposals.jsonl", raising=False)
    monkeypatch.setattr(ledger, "_LEDGER", tmp_path / "theses.jsonl", raising=False)
    monkeypatch.setattr(rd, "_engine_blocked", lambda s: s.upper() == "NVDA")

    (tmp_path / "proposals.jsonl").write_text(
        json.dumps({"subject": "NVDA", "lean": "add", "conviction": "high", "prob_correct": 0.7,
                    "horizon_d": 21, "thesis": "t", "evidence": [], "status": "proposed"}) + "\n"
        + json.dumps({"subject": "AVGO", "lean": "add", "conviction": "high", "prob_correct": 0.7,
                      "horizon_d": 21, "thesis": "t", "evidence": [], "status": "proposed"}) + "\n")

    out = rd.ingest_proposals(asof="2026-06-20")
    assert out["ingested"] == 2
    by_subj = {t["subject"]: t for t in out["theses"]}
    assert by_subj["NVDA"]["lean"] == "watch" and by_subj["NVDA"]["clamped"] is True
    assert by_subj["AVGO"]["lean"] == "add" and by_subj["AVGO"]["clamped"] is False


def test_ingest_clamps_when_caller_passes_blocked(tmp_path, monkeypatch):
    """The explicit-blocked path (review_ticker) still works."""
    from brain import research_desk as rd
    from brain import ledger
    monkeypatch.setattr(rd, "_PROPOSALS", tmp_path / "proposals.jsonl", raising=False)
    monkeypatch.setattr(ledger, "_LEDGER", tmp_path / "theses.jsonl", raising=False)
    monkeypatch.setattr(rd, "_engine_blocked", lambda s: False)
    (tmp_path / "proposals.jsonl").write_text(
        json.dumps({"subject": "TSLA", "lean": "overweight", "conviction": "high",
                    "prob_correct": 0.7, "horizon_d": 21, "thesis": "t", "evidence": [],
                    "status": "proposed"}) + "\n")
    out = rd.ingest_proposals(asof="2026-06-20", blocked={"TSLA"})
    assert out["theses"][0]["lean"] == "watch" and out["theses"][0]["clamped"] is True
