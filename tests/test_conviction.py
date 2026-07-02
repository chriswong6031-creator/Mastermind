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
    # W2.3: _SHORTLIST is gone; the seed is now the derived regime_seed(). Neutralise the other
    # sources so the assertion isolates the open-proposal path.
    monkeypatch.setattr(conviction, "regime_seed", lambda: [])
    monkeypatch.setattr(conviction, "universe", lambda: [])
    # an open ledger thesis (Claude's proposal) becomes a conviction candidate
    monkeypatch.setattr("brain.ledger.all_theses",
                        lambda: [{"subject": "PLTR", "status": "open"}])
    assert "PLTR" in conviction.candidates()


def test_manual_exclude_keeps_reversed_names_out(monkeypatch):
    # operational hold-out: names manually reversed out of the book (the AVGO/NVDA override
    # post-mortem) must NOT be auto-re-added as candidates — even via an open proposal OR the
    # DERIVED regime seed — so the daily rebalance can't silently re-buy them.
    monkeypatch.setattr(conviction, "regime_seed", lambda: ["NVDA", "AVGO"])
    monkeypatch.setattr(conviction, "universe", lambda: [])
    monkeypatch.setattr("brain.ledger.all_theses",
                        lambda: [{"subject": "AVGO", "status": "open"},
                                 {"subject": "NVDA", "status": "open"}])
    cands = conviction.candidates()
    assert "NVDA" not in cands and "AVGO" not in cands
    assert conviction._MANUAL_EXCLUDE == {"NVDA", "AVGO"}


def test_no_hardcoded_shortlist_ticker_literals_remain():
    # W2.3: the hardcoded 20-name AI/MAG7 _SHORTLIST is DELETED — the module must carry NO leadership
    # ticker literals (the frozen-cohort crowding failure). _MANUAL_EXCLUDE (an operational hold-out,
    # not a leadership bet) is the ONLY ticker set that legitimately survives.
    import inspect
    assert not hasattr(conviction, "_SHORTLIST")
    src = inspect.getsource(conviction)
    # the old shortlist-only names must not appear as bare string literals anywhere in the module
    # source. (NVDA/AVGO are DELIBERATELY excluded from this list — they legitimately survive in
    # _MANUAL_EXCLUDE, which is an operational do-not-auto-re-add hold-out, not a leadership bet.)
    for tok in ("MRVL", "LRCX", "KLAC", "ANET", "BWXT", "\"MSFT\"", "\"GOOGL\"", "\"META\"", "\"AMAT\""):
        assert tok not in src, f"stray leadership ticker literal {tok!r} still in conviction.py"


# ================= W2.3 REGIME SEED (replaces the dead _SHORTLIST) =========================
# Fully offline: `_load` (baskets) and `regime_frame.cycles` are monkeypatched so the seed's
# response to cycle phase is deterministic and does not depend on live vendor data.

# Two synthetic baskets in DIFFERENT sectors, so the cycle filter can include one and exclude the
# other. TECHB → XLK (we drive it to Peak = not entry-favored); POWERB → XLU (Trough = favored).
_SEED_BASKETS = {
    "baskets": [
        {"id": "techb", "reference": {"label": "SMH"},            # SMH → XLK via the coarse map
         "perf": {"20d": {"rel": 0.50}},
         "members": [{"symbol": "TCHA", "ret_20d": 0.9, "last": 100.0},
                     {"symbol": "TCHB", "ret_20d": 0.5, "last": 100.0}]},
        {"id": "powerb", "reference": {"label": "XLU"},           # a bare sector ETF → itself
         "perf": {"20d": {"rel": 0.10}},
         "members": [{"symbol": "PWRA", "ret_20d": 0.8, "last": 100.0},
                     {"symbol": "PWRB", "ret_20d": 0.4, "last": 100.0}]},
    ]
}


def _patch_seed(monkeypatch, *, cyc):
    monkeypatch.setattr(conviction, "_load",
                        lambda rel: _SEED_BASKETS if "baskets.json" in rel else None)
    monkeypatch.setattr("brain.regime_frame.cycles", lambda: cyc)


def test_regime_seed_excludes_peak_sector_but_keeps_favored(monkeypatch):
    # XLK Peak (not entry-favored) → its basket's names are ABSENT; XLU Trough (favored) → present.
    _patch_seed(monkeypatch, cyc={
        "XLK": {"phase": "Peak", "entry_favored": False},
        "XLU": {"phase": "Trough", "entry_favored": True},
    })
    seed = conviction.regime_seed()
    assert "PWRA" in seed and "PWRB" in seed          # favored sector → included
    assert "TCHA" not in seed and "TCHB" not in seed  # Peak sector → excluded (ENTRY tilt only)


def test_regime_seed_responds_to_a_phase_flip(monkeypatch):
    # flip XLK to Recovery (favored): the same tech names now APPEAR — the seed tracks the cycle.
    _patch_seed(monkeypatch, cyc={
        "XLK": {"phase": "Recovery", "entry_favored": True},
        "XLU": {"phase": "Trough", "entry_favored": True},
    })
    seed = conviction.regime_seed()
    assert "TCHA" in seed and "PWRA" in seed


def test_regime_seed_stale_cycles_degrades_to_unfiltered(monkeypatch):
    # cycles() == {} (the stale/absent degrade) → the filter is a NO-OP → ALL basket leaders seed,
    # including the (would-be-excluded) Peak-sector names. Staleness may only remove a filter.
    _patch_seed(monkeypatch, cyc={})
    seed = conviction.regime_seed()
    assert {"TCHA", "TCHB", "PWRA", "PWRB"}.issubset(set(seed))


def test_regime_seed_unmapped_sector_is_allowed(monkeypatch):
    # a basket whose reference maps to NO cycle row (IBIT/crypto here) is UNMAPPED → allowed, never
    # blocked on missing data — even while a real Peak sector IS filtered out.
    baskets = {"baskets": [
        {"id": "cryptob", "reference": {"label": "IBIT"}, "perf": {"20d": {"rel": 0.9}},
         "members": [{"symbol": "COINX", "ret_20d": 0.9, "last": 50.0}]},
        {"id": "techb", "reference": {"label": "SMH"}, "perf": {"20d": {"rel": 0.5}},
         "members": [{"symbol": "TCHA", "ret_20d": 0.9, "last": 100.0}]},
    ]}
    monkeypatch.setattr(conviction, "_load",
                        lambda rel: baskets if "baskets.json" in rel else None)
    monkeypatch.setattr("brain.regime_frame.cycles",
                        lambda: {"XLK": {"phase": "Peak", "entry_favored": False}})
    seed = conviction.regime_seed()
    assert "COINX" in seed        # unmapped (IBIT) → allowed
    assert "TCHA" not in seed     # XLK Peak → excluded


def test_regime_seed_respects_max_names_cap(monkeypatch):
    # a wide basket in a favored sector is truncated to regime_seed.max_names.
    members = [{"symbol": f"N{i:02d}", "ret_20d": 1.0 - i * 0.01, "last": 100.0} for i in range(60)]
    baskets = {"baskets": [
        {"id": "wide", "reference": {"label": "XLU"}, "perf": {"20d": {"rel": 0.5}},
         "members": members},
    ]}
    monkeypatch.setattr(conviction, "_load",
                        lambda rel: baskets if "baskets.json" in rel else None)
    monkeypatch.setattr("brain.regime_frame.cycles",
                        lambda: {"XLU": {"phase": "Trough", "entry_favored": True}})
    monkeypatch.setattr(conviction, "_seed_cfg",
                        lambda: {"max_names": 5, "leader_top_n_per_basket": 50, "liquidity_min_last": 0.0})
    seed = conviction.regime_seed()
    assert len(seed) == 5


def test_regime_seed_never_touches_held_peak_name(monkeypatch):
    # THE REFUTED-VETO ANCHOR (masterplan §0): the cycle read is an ENTRY tilt ONLY. A HELD name in a
    # Peak sector must be UNAFFECTED — not exited, not trimmed, not filtered from the book. The seed
    # only feeds NEW candidates; it has no exit authority. We prove build() holds a Peak-sector name
    # even though the seed would never re-source it.
    _patch_seed(monkeypatch, cyc={"XLK": {"phase": "Peak", "entry_favored": False}})
    # TCHA (Peak-sector, seed-excluded) is HELD; feed it directly to the gate as a candidate and give
    # it a clean 'up' read → it must be RETAINED (held), independent of the cycle phase of its sector.
    monkeypatch.setattr(conviction, "candidates", lambda: ["TCHA"])
    monkeypatch.setattr(lenses, "full", lambda t, kind="name": _fake_full(
        size_authority="up", confluence=0.5))
    sized, _rej = conviction.build(0.30, name_cap=0.08, held={"TCHA"})
    assert "TCHA" in {p["ticker"] for p in sized}     # held Peak-sector name is NOT exited/vetoed

    # and the seed itself would never re-add it (entry-tilt exclusion) — the two facts coexist.
    assert "TCHA" not in conviction.regime_seed()


def test_candidates_seed_respects_manual_exclude(monkeypatch):
    # a seed name on the operational hold-out list is still filtered out of candidates().
    monkeypatch.setattr(conviction, "regime_seed", lambda: ["NVDA", "GOODX"])
    monkeypatch.setattr(conviction, "universe", lambda: [])
    monkeypatch.setattr("brain.ledger.all_theses", lambda: [])
    cands = conviction.candidates()
    assert "NVDA" not in cands and "GOODX" in cands
