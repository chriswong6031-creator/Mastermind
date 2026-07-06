"""THE benchmark ledger (brain.benchmark_ledger) — W-L / L1.

Guards the renorm math, the regime-conditional bogey switch, and — THE ACCEPTANCE the whole
program started from — that on the incident-window fixtures the defensive basket beats the Brain.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import bot  # noqa: F401 — bootstraps vendor/macro
from brain import benchmark_ledger as B

_FIXT = Path(__file__).resolve().parent / "incident_replays" / "fixtures" / "2026-07-02-semis-breakdown"


@pytest.fixture()
def sandbox(tmp_path, monkeypatch):
    monkeypatch.setattr(B, "_BENCH_DIR", tmp_path)
    return tmp_path


def _incident_series() -> dict:
    """{TICKER: {date: px}} from the real trimmed incident closes (SMH/XLV/XLK/XLU/SPY)."""
    return json.loads((_FIXT / "etf_closes.json").read_text())


# ── renorm math ───────────────────────────────────────────────────────────────
def test_single_name_renorm_is_growth_of_dollar():
    series = {"SPY": {"2026-01-02": 500.0, "2026-01-03": 550.0}}
    curve = B.renorm_basket(series, ["SPY"])
    assert curve["2026-01-02"] == 1.0
    assert curve["2026-01-03"] == 1.1                      # +10%


def test_equal_weight_basket_renorm():
    series = {"A": {"d1": 100.0, "d2": 110.0}, "B": {"d1": 50.0, "d2": 45.0}}
    curve = B.renorm_basket(series, ["A", "B"])
    assert curve["d1"] == 1.0
    # mean(1.10, 0.90) = 1.0
    assert curve["d2"] == 1.0


def test_common_inception_skips_pre_alignment_dates():
    # B only starts on d2, so the basket inception is d2 (not d1)
    series = {"A": {"d1": 100.0, "d2": 110.0, "d3": 120.0},
              "B": {"d2": 50.0, "d3": 55.0}}
    curve = B.renorm_basket(series, ["A", "B"])
    assert "d1" not in curve
    assert curve["d2"] == 1.0
    assert min(curve) == "d2"


# ── regime-conditional bogey ────────────────────────────────────────────────────
def test_regime_max_uses_defensive_when_not_risk_on():
    spy = {"d1": 1.0, "d2": 1.01}
    dfn = {"d1": 1.0, "d2": 1.06}
    out = B.regime_max_curve(spy, dfn, {"state": "risk_off"})
    assert out["d2"] == 1.06                                # took the defensive max


def test_regime_max_uses_defensive_when_weakening_even_if_risk_on():
    spy = {"d1": 1.0, "d2": 1.01}
    dfn = {"d1": 1.0, "d2": 1.06}
    out = B.regime_max_curve(spy, dfn, {"state": "risk_on", "transition_state": "WEAKENING"})
    assert out["d2"] == 1.06


def test_regime_max_is_plain_spy_in_risk_on():
    spy = {"d1": 1.0, "d2": 1.01}
    dfn = {"d1": 1.0, "d2": 1.06}
    out = B.regime_max_curve(spy, dfn, {"state": "risk_on"})
    assert out["d2"] == 1.01                                # cash-hoarding still loses to the market


def test_regime_missing_degrades_to_plain_spy():
    spy = {"d1": 1.0, "d2": 1.01}
    dfn = {"d1": 1.0, "d2": 1.06}
    out = B.regime_max_curve(spy, dfn, None)
    assert out["d2"] == 1.01                                # no defensive alibi without evidence


# ── THE ACCEPTANCE — the fact the whole program started from ──────────────────────
def test_defensive_basket_beats_every_brain_book_on_incident_window(sandbox):
    """On the semis-unwind window (SMH −7.2%, XLV +6.3%), the equal-weight defensive basket must
    out-return SPY *and* every Brain book. The peer books were piled into SMH/tech, so their carry
    curve tracks the offensive names down while the defensive sleeve rides XLU/XLV/XLF/XLP.

    The defensive basket in this fixture is XLU/XLV/XLF/XLP; the fixture ships XLU/XLV (the two that
    dominate the divergence), so we build the defensive curve from those and the offensive Brain
    curve from SMH/XLK (the pile-up the audit named)."""
    series = _incident_series()
    # the Brain books' carry curve = the SMH/tech pile-up (equal-weight of the offensive names the
    # peer_books held) — this stands in for the do-nothing carry of a tech-heavy book.
    brain_curve = B.renorm_basket(series, ["SMH", "XLK"])
    # defensive sleeve available in the fixture (XLU + XLV — utilities + healthcare)
    def_curve = B.renorm_basket(series, ["XLU", "XLV"])

    ledger = B.build(series, asof="2026-07-01", regime={"state": "risk_off"},
                     book_curves={"autonomous": brain_curve, "heavyweight": brain_curve})

    lb = ledger["leaderboard"]
    by_id = {r["id"]: r for r in lb}
    # the defensive bogey out-returns SPY and every book row
    def_ret = by_id["defensive"]["return_pct"]
    spy_ret = by_id["spy"]["return_pct"]
    book_rets = [r["return_pct"] for r in lb if r["kind"] == "book"]
    assert def_ret is not None and def_ret > spy_ret, (def_ret, spy_ret)
    assert all(def_ret > br for br in book_rets), (def_ret, book_rets)
    # and the leaderboard is actually ranked (defensive above the offensive books)
    ids_in_order = [r["id"] for r in lb]
    assert ids_in_order.index("defensive") < ids_in_order.index("autonomous")


def test_build_persists_and_loads(sandbox):
    series = _incident_series()
    B.build(series, asof="2026-07-01", regime={"state": "risk_off"})
    loaded = B.load("2026-07-01")
    assert loaded["as_of"] == "2026-07-01"
    assert "defensive" in loaded["bogeys"]
    assert B.latest()["as_of"] == "2026-07-01"


def test_build_degrades_on_empty_series(sandbox):
    ledger = B.build({}, asof="2026-07-01", regime=None)
    # no curves → empty leaderboard, no crash
    assert ledger["leaderboard"] == []
    assert ledger["bogeys"]["spy"]["curve"] == {}


# ── W6 T4 — per-book regional bogeys ─────────────────────────────────────────
class TestRegionalBogeys:
    """Guards for the CN/HK per-book bogey overrides (W6 T4)."""

    def _fxi_series(self) -> dict:
        """Minimal FXI price series (growth curve)."""
        return {"FXI": {"2026-01-02": 30.0, "2026-01-03": 31.5, "2026-01-04": 29.0}}

    def test_book_bogey_overrides_are_declared(self):
        assert "china" in B.BOOK_BOGEY_OVERRIDES
        assert "hk" in B.BOOK_BOGEY_OVERRIDES
        # US books are NOT in the override map (they use the standard suite)
        assert "flagship" not in B.BOOK_BOGEY_OVERRIDES
        assert "autonomous" not in B.BOOK_BOGEY_OVERRIDES

    def test_cn_bogey_constituent_is_priceable_proxy(self):
        """CN bogey uses FXI (the only China-region ETF in the parquet store).  The test
        asserts the constituent is named, not that 2800.HK or 000300.SS is used (those are
        unpriceable today)."""
        assert B.CN_BOGEY == ["FXI"]
        assert B.HK_BOGEY == ["FXI"]   # same proxy until 2800.HK lands in the store

    def test_build_regional_china_produces_regional_bogey(self, sandbox):
        series = self._fxi_series()
        result = B.build_regional(series, "china", asof="2026-01-04")
        assert "regional" in result["bogeys"]
        curve = result["bogeys"]["regional"]["curve"]
        assert curve, "regional curve must be non-empty when FXI is priced"
        assert curve[min(curve)] == pytest.approx(1.0, abs=1e-5)  # inception = 1.0
        assert result["book_id"] == "china"

    def test_build_regional_hk_produces_regional_bogey(self, sandbox):
        series = self._fxi_series()
        result = B.build_regional(series, "hk", asof="2026-01-04")
        assert "regional" in result["bogeys"]
        curve = result["bogeys"]["regional"]["curve"]
        assert curve
        assert result["book_id"] == "hk"

    def test_build_regional_degrades_on_missing_fxi(self, sandbox):
        """When FXI has no price data the regional bogey degrades to an empty curve (P2)."""
        result = B.build_regional({}, "china", asof="2026-01-04")
        assert result["bogeys"]["regional"]["curve"] == {}
        assert result["leaderboard"] == []   # nothing to rank

    def test_build_regional_leaderboard_contains_regional(self, sandbox):
        series = self._fxi_series()
        result = B.build_regional(series, "china", asof="2026-01-04")
        ids = {r["id"] for r in result["leaderboard"]}
        assert "regional" in ids

    def test_build_regional_unknown_book_falls_back_to_full_build(self, sandbox):
        """A book_id not in BOOK_BOGEY_OVERRIDES gets the standard US ledger (four bogeys)."""
        series = _incident_series()
        result = B.build_regional(series, "flagship", asof="2026-07-01")
        # standard build returns spy, defensive, regime_max, do_nothing
        assert "spy" in result["bogeys"]
        assert "defensive" in result["bogeys"]

    def test_build_regional_persists_under_book_subdir(self, sandbox, monkeypatch):
        monkeypatch.setattr(B, "_BENCH_DIR", sandbox)
        series = self._fxi_series()
        B.build_regional(series, "china", asof="2026-01-04")
        # file should be at sandbox/china/2026-01-04.json (NOT at sandbox/2026-01-04.json)
        assert (sandbox / "china" / "2026-01-04.json").exists()
        assert not (sandbox / "2026-01-04.json").exists()

    def test_defensive_basket_not_imposed_on_cn_book(self, sandbox):
        """The CN/HK bogeys must NOT include the US defensive basket (XLU/XLV/XLF/XLP).
        Imposing a US sector-ETF bogey on a CNY-denominated book is category error."""
        series = self._fxi_series()
        result = B.build_regional(series, "china", asof="2026-01-04")
        for bogey in result["bogeys"].values():
            for c in bogey.get("constituents") or []:
                assert c not in B.DEFENSIVE_BASKET, (
                    f"CN regional bogey must not include US defensive constituent {c}")

    def test_proxy_meta_stamped_on_disk_artifact(self, sandbox):
        """proxy_meta passed to build_regional must appear in the PERSISTED JSON, not just in the
        in-memory return value.  This guards the Fix-1 regression: the old code stamped flags post-
        hoc onto the in-memory dict AFTER persist() had already written to disk, so the on-disk
        artifact lacked bogey_is_proxy / proxy_reason."""
        import json as _json
        series = self._fxi_series()
        proxy_meta = {"bogey_is_proxy": True, "proxy_reason": "FXI proxy test"}
        B.build_regional(series, "china", asof="2026-01-04", proxy_meta=proxy_meta)
        artifact = _json.loads((sandbox / "china" / "2026-01-04.json").read_text())
        regional_bogey = artifact["bogeys"]["regional"]
        assert regional_bogey.get("bogey_is_proxy") is True, (
            "on-disk artifact must carry bogey_is_proxy=True; was the flag stamped before persist()?")
        assert regional_bogey.get("proxy_reason") == "FXI proxy test", (
            "on-disk artifact must carry proxy_reason; post-hoc in-memory mutation is not enough")


# ── W6 T4 — registry experiment well-formed ──────────────────────────────────
def test_cn_funnel_registry_experiment_is_well_formed():
    """The 'cn-funnel-edge-led' experiment must be in the registry with the correct schema."""
    from brain import experiment_registry as R
    exp = R.get("cn-funnel-edge-led")
    assert exp is not None, "cn-funnel-edge-led experiment must be registered"
    assert exp["status"] == "open"
    assert exp["owner"] == "fable-review"
    assert exp["comeback_date"] == "2026-07-31"
    # gate must reference the bogey comparison
    assert "bogey" in exp["gate"].lower() or "fxi" in exp["gate"].lower()
    # artifact paths must include china_intake.py and the regional benchmark dir
    paths = exp.get("artifact_paths") or []
    assert any("china_intake" in p for p in paths)
    assert any("benchmark" in p for p in paths)
