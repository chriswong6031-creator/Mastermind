"""Conviction sleeve — only matrix-confirmed, veto-clear names take paper size."""
import bot  # noqa: F401

from portfolio import conviction, lenses


def test_only_confirmed_names_are_sized():
    sized, _rejected = conviction.build(0.30, name_cap=0.08)
    for p in sized:
        assert p["weight"] > 0 and p["weight"] <= 0.08 + 1e-9
        syn = lenses.full(p["ticker"], "name")["synthesis"]
        assert syn["size_authority"] == "up" and not syn["vetoes"]   # all sides confirm, no veto


def test_vetoed_and_distribution_names_excluded():
    sized, _rejected = conviction.build(0.30)
    sized = {p["ticker"] for p in sized}
    # names our matrix flags (distribution / distress / cycle-blocked) must NOT be sized
    for t in ["NVDA", "AMD", "MU"]:
        syn = lenses.full(t, "name")["synthesis"]
        if syn["size_authority"] != "up" or syn["vetoes"]:
            assert t not in sized


def test_candidate_pool_includes_open_proposals(monkeypatch):
    monkeypatch.setattr(conviction, "_SHORTLIST", [])
    # an open ledger thesis (Claude's proposal) becomes a conviction candidate
    monkeypatch.setattr("brain.ledger.all_theses",
                        lambda: [{"subject": "AVGO", "status": "open"}])
    assert "AVGO" in conviction.candidates()
