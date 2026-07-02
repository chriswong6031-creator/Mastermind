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


# ---------------- FAIL-CLOSED build semantics (the 2026-07-01 fail-open incident) ----------------
def _fake_full(size_authority="up", *, degraded=False, stockdata_present=True,
               confluence=0.5, vetoes=None, price_downtrend=False, rows=None):
    """A minimal lenses.full() stand-in so the build gate can be exercised deterministically without
    real vendor data — mirrors the synthesis schema build() reads."""
    return {
        "rows": rows if rows is not None else [
            {"lens": "trend", "direction": "bull"}, {"lens": "sector_rs", "direction": "bull"}],
        "synthesis": {
            "size_authority": size_authority, "confluence": confluence,
            "vetoes": vetoes or [], "bull": 3, "bear": 0,
            "data_degraded": degraded, "stockdata_present": stockdata_present,
            "price_downtrend": price_downtrend, "divergences": [],
        },
    }


def test_absent_stockdata_cannot_enter_the_book(monkeypatch):
    """(Test 1 + 5) A candidate with NO stockdata is data-degraded (size_authority='insufficient_data')
    and can NEVER be opened — the alt-data-only single-lens path is refused entry BY DESIGN. This is
    the direct fix for the 07-01 book that opened 24 names on one political-flow signal, price=None."""
    monkeypatch.setattr(conviction, "candidates", lambda: ["EFXX"])
    monkeypatch.setattr(lenses, "full", lambda t, kind="name": _fake_full(
        size_authority="insufficient_data", degraded=True, stockdata_present=False, confluence=1.0))
    sized, rejected = conviction.build(0.30, held=set())
    assert not sized                                   # zero opens on missing data — the fix
    assert any(r["ticker"] == "EFXX" for r in rejected)


def test_held_degraded_name_freezes_not_exited(monkeypatch):
    """(Test 2) CRITICAL FREEZE SEMANTICS: a name we ALREADY hold whose data went dark is RETAINED as
    a hold (retained_reason='data_degraded_freeze'), NOT hard-exited or dropped by the confluence
    floor. Missing data must never liquidate the book (the inverse of the fail-open disaster)."""
    monkeypatch.setattr(conviction, "candidates", lambda: ["HELDX"])
    # degraded + a low/untrusted confluence that would fail the 0.25 exit floor if this were churnable
    monkeypatch.setattr(lenses, "full", lambda t, kind="name": _fake_full(
        size_authority="insufficient_data", degraded=True, stockdata_present=False, confluence=0.0))
    sized, _rej = conviction.build(0.30, held={"HELDX"})
    held = {p["ticker"]: p for p in sized}
    assert "HELDX" in held                             # frozen, not liquidated
    assert held["HELDX"]["weight"] > 0                 # survives the weight>0 filter (freeze floor)
    assert held["HELDX"]["verdict"] == "hold"
    assert held["HELDX"]["retained_reason"] == "data_degraded_freeze"


def test_held_degraded_name_with_real_veto_still_exits(monkeypatch):
    """Freeze protects against DATA loss, not against a genuine hard veto. A held+degraded name that
    also trips a real veto (parabolic/Altman) is still a hard exit — freeze doesn't rescue it."""
    monkeypatch.setattr(conviction, "candidates", lambda: ["BADX"])
    monkeypatch.setattr(lenses, "full", lambda t, kind="name": _fake_full(
        size_authority="blocked", degraded=True, stockdata_present=False,
        confluence=0.0, vetoes=["parabolic"]))
    sized, _rej = conviction.build(0.30, held={"BADX"})
    assert "BADX" not in {p["ticker"] for p in sized}


def test_build_wide_breaker_freezes_all_new_adds(monkeypatch):
    """(Test 3) >80% of evaluated candidates degraded => refuse ALL new adds, keep holds, and emit a
    loud data_health record. Universe: 4 degraded + 1 healthy new add = 80%+... use 9 degraded of 10
    (90%). The one healthy NEW name must NOT open; a held name is kept."""
    universe = [f"DEG{i}" for i in range(9)] + ["GOODNEW", "HELDX"]

    def _full(t, kind="name"):
        if t == "GOODNEW":
            return _fake_full(size_authority="up", confluence=0.6)     # healthy fresh add
        if t == "HELDX":
            return _fake_full(size_authority="up", confluence=0.6)     # healthy held name
        return _fake_full(size_authority="insufficient_data", degraded=True,
                          stockdata_present=False, confluence=1.0)
    monkeypatch.setattr(conviction, "candidates", lambda: universe)
    monkeypatch.setattr(lenses, "full", _full)
    sized, _rej = conviction.build(0.30, held={"HELDX"})
    names = {p["ticker"] for p in sized}
    assert "GOODNEW" not in names                      # breaker refused the new add
    assert "HELDX" in names                            # held name kept (freeze, don't churn)
    assert getattr(sized, "data_health", None) is not None
    assert sized.data_health["degraded"] is True
    assert sized.data_health["degraded_fraction"] > 0.80
    assert "FROZEN" in sized.data_health["action"]


def test_normal_full_coverage_build_unchanged(monkeypatch):
    """(Test 4) Regression: with full healthy coverage the breaker does NOT trip, data_health.degraded
    is False, and normal names size and open exactly as before."""
    monkeypatch.setattr(conviction, "candidates", lambda: ["AAAX", "BBBX"])
    monkeypatch.setattr(lenses, "full", lambda t, kind="name": _fake_full(
        size_authority="up", confluence=0.5))
    sized, _rej = conviction.build(0.30, held=set())
    assert {p["ticker"] for p in sized} == {"AAAX", "BBBX"}
    assert all(p["verdict"] == "add" and p["weight"] > 0 for p in sized)
    assert sized.data_health["degraded"] is False
    assert sized.data_health["degraded_fraction"] == 0.0


def test_candidate_pool_includes_open_proposals(monkeypatch):
    monkeypatch.setattr(conviction, "_SHORTLIST", [])
    # an open ledger thesis (Claude's proposal) becomes a conviction candidate
    monkeypatch.setattr("brain.ledger.all_theses",
                        lambda: [{"subject": "PLTR", "status": "open"}])
    assert "PLTR" in conviction.candidates()


def test_manual_exclude_keeps_reversed_names_out(monkeypatch):
    # operational hold-out: names manually reversed out of the book (the AVGO/NVDA override
    # post-mortem) must NOT be auto-re-added as candidates — even via an open proposal OR the
    # leadership shortlist — so the daily rebalance can't silently re-buy them.
    monkeypatch.setattr(conviction, "_SHORTLIST", ["NVDA", "AVGO"])
    monkeypatch.setattr("brain.ledger.all_theses",
                        lambda: [{"subject": "AVGO", "status": "open"},
                                 {"subject": "NVDA", "status": "open"}])
    cands = conviction.candidates()
    assert "NVDA" not in cands and "AVGO" not in cands
    assert conviction._MANUAL_EXCLUDE == {"NVDA", "AVGO"}
