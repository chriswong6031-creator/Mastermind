"""tests/test_liquidity_quality.py — guards for the liquidity-QUALITY classifier (W-I task 2).

Pure / offline / intent-only.  Every input is either a trimmed JSON fixture of the actual
June–July 2026 FRED/treasury window (tests/fixtures/liquidity_quality/) or a synthetic
series_fn built inline.  NO live market state is pinned and the shared vendor store is never
live-read — the classifier takes a series_fn precisely so tests can inject.

Coverage:
  * The pure rule table (_label) — every branch, including the degrade-toward-stress logic.
  * Component reads: RoC / composition / RRP-buffer / credit — each degrading to 'unknown'
    (never benign) when its series is missing.
  * REPLAY: the actual incident window classifies STRESS-or-hollow (NOT benign-expansion),
    and a synthetic genuine-QE window classifies benign-expansion.
  * The INVARIANT: any missing series shrinks the label toward unknown/stress — never benign.
"""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pytest

from brain import liquidity_quality as LQ

_FIXDIR = Path(__file__).parent / "fixtures" / "liquidity_quality"


# ════════════════════════════════════════════════════════════════════════════════════════
# synthetic series_fn helpers — build date->value dicts on a business-day grid
# ════════════════════════════════════════════════════════════════════════════════════════
def _bdays(start: date, n: int) -> list[date]:
    out, cur = [], start
    while len(out) < n:
        if cur.weekday() < 5:
            out.append(cur)
        cur += timedelta(days=1)
    return out


def _ramp(start: date, n: int, first: float, per_step: float) -> dict[str, float]:
    """A business-day series ramping linearly by *per_step* per grid point."""
    days = _bdays(start, n)
    return {d.isoformat(): first + i * per_step for i, d in enumerate(days)}


def _flat(start: date, n: int, level: float) -> dict[str, float]:
    return {d.isoformat(): level for d in _bdays(start, n)}


def _fn_from(mapping: dict[str, dict | None]):
    """Wrap a {name: series-or-None} mapping into a series_fn callback."""
    return lambda name: mapping.get(name)


# ════════════════════════════════════════════════════════════════════════════════════════
# _label — the pure rule table (no I/O)
# ════════════════════════════════════════════════════════════════════════════════════════
class TestLabelRuleTable:
    def test_unknown_quantity_is_unknown(self):
        # cannot even measure liquidity → unknown regardless of other legs
        assert LQ._label("unknown", "benign", "ample", "calm") == "unknown"
        assert LQ._label("unknown", "mechanical", "exhausted", "confirming") == "unknown"

    def test_contracting_is_contracting(self):
        assert LQ._label("contracting", "benign", "ample", "calm") == "contracting"
        assert LQ._label("contracting", "mechanical", "exhausted", "confirming") == "contracting"

    def test_expanding_benign_only_with_positive_reads(self):
        # the ONLY path to benign: benign composition AND calm credit AND ample buffer
        assert LQ._label("expanding", "benign", "ample", "calm") == "benign-expansion"

    def test_expanding_mechanical_is_stress(self):
        assert LQ._label("expanding", "mechanical", "ample", "calm") == "stress-expansion"

    def test_expanding_exhausted_buffer_is_stress(self):
        assert LQ._label("expanding", "benign", "exhausted", "calm") == "stress-expansion"

    def test_expanding_credit_confirming_is_stress(self):
        assert LQ._label("expanding", "benign", "ample", "confirming") == "stress-expansion"

    def test_expanding_unknown_composition_degrades_to_stress(self):
        # unknown composition can NOT earn benign — conservative direction
        assert LQ._label("expanding", "unknown", "ample", "calm") == "stress-expansion"

    def test_expanding_unknown_credit_degrades_to_stress(self):
        assert LQ._label("expanding", "benign", "ample", "unknown") == "stress-expansion"

    def test_flat_exhausted_is_neutral_hollow(self):
        assert LQ._label("flat", "benign", "exhausted", "calm") == "neutral-hollow"

    def test_flat_ample_is_neutral_hollow_not_benign(self):
        # a flat RoC is NEVER benign-expansion (there is no expansion to bless)
        assert LQ._label("flat", "benign", "ample", "calm") == "neutral-hollow"


# ════════════════════════════════════════════════════════════════════════════════════════
# component reads — RoC / composition / buffer / credit + their unknown degrades
# ════════════════════════════════════════════════════════════════════════════════════════
class TestNetLiquidityComponents:
    def test_rrp_buffer_exhausted_and_ample(self):
        cfg = LQ._cfg()
        start = date(2026, 3, 2)
        # exhausted
        m = {"walcl_bn": _flat(start, 40, 6700.0),
             "rrp_bn": _flat(start, 40, 6.4),
             "tga_bn": _flat(start, 40, 870.0)}
        out = LQ._net_liquidity_components(_fn_from(m), cfg)
        assert out["buffer"] == "exhausted" and out["rrp_level_bn"] == pytest.approx(6.4)
        # ample
        m["rrp_bn"] = _flat(start, 40, 450.0)
        out = LQ._net_liquidity_components(_fn_from(m), cfg)
        assert out["buffer"] == "ample"

    def test_missing_rrp_buffer_unknown(self):
        cfg = LQ._cfg()
        start = date(2026, 3, 2)
        m = {"walcl_bn": _flat(start, 40, 6700.0), "rrp_bn": None,
             "tga_bn": _flat(start, 40, 870.0)}
        out = LQ._net_liquidity_components(_fn_from(m), cfg)
        assert out["buffer"] == "unknown"
        # quantity/composition also unknown (a missing leg kills the net read)
        assert out["quantity"] == "unknown" and out["composition"] == "unknown"

    def test_walcl_driven_expansion_is_benign_composition(self):
        cfg = LQ._cfg()
        start = date(2026, 3, 2)
        # WALCL ramps +2/step (dominant), RRP/TGA flat → benign composition, expanding RoC
        m = {"walcl_bn": _ramp(start, 40, 6700.0, 2.0),
             "rrp_bn": _flat(start, 40, 450.0),
             "tga_bn": _flat(start, 40, 700.0)}
        out = LQ._net_liquidity_components(_fn_from(m), cfg)
        assert out["quantity"] == "expanding"
        assert out["composition"] == "benign"
        assert out["walcl_share"] == pytest.approx(1.0)

    def test_tga_drain_expansion_is_mechanical(self):
        cfg = LQ._cfg()
        start = date(2026, 3, 2)
        # WALCL flat, TGA drains −3/step (dominant) → net expands MECHANICALLY
        m = {"walcl_bn": _flat(start, 40, 6700.0),
             "rrp_bn": _flat(start, 40, 6.4),
             "tga_bn": _ramp(start, 40, 900.0, -3.0)}
        out = LQ._net_liquidity_components(_fn_from(m), cfg)
        assert out["quantity"] == "expanding"
        assert out["composition"] == "mechanical"
        assert out["walcl_share"] == pytest.approx(0.0)

    def test_contracting_quantity(self):
        cfg = LQ._cfg()
        start = date(2026, 3, 2)
        m = {"walcl_bn": _flat(start, 40, 6700.0),
             "rrp_bn": _ramp(start, 40, 10.0, 3.0),   # RRP building → net drains
             "tga_bn": _flat(start, 40, 700.0)}
        out = LQ._net_liquidity_components(_fn_from(m), cfg)
        assert out["quantity"] == "contracting"


class TestCreditStress:
    def test_oas_widening_confirms_stress(self):
        cfg = LQ._cfg()
        start = date(2026, 3, 2)
        m = {"hy_oas": _ramp(start, 60, 3.0, 0.02),   # +0.02/step → +0.4 over 20d, » 0.10pp
             "nfci": _flat(start, 60, -0.5)}
        out = LQ._credit_stress(_fn_from(m), cfg)
        assert out["credit"] == "confirming"
        assert out["oas_chg_pp"] > cfg["oas_widen_pp"]

    def test_calm_credit(self):
        cfg = LQ._cfg()
        start = date(2026, 3, 2)
        m = {"hy_oas": _flat(start, 60, 2.8), "nfci": _ramp(start, 60, -0.4, -0.001)}
        out = LQ._credit_stress(_fn_from(m), cfg)
        assert out["credit"] == "calm"

    def test_nfci_tightening_confirms_stress(self):
        cfg = LQ._cfg()
        start = date(2026, 3, 2)
        m = {"hy_oas": _flat(start, 60, 2.8),
             "nfci": _ramp(start, 60, -0.6, 0.01)}   # NFCI rising = tightening
        out = LQ._credit_stress(_fn_from(m), cfg)
        assert out["credit"] == "confirming" and out["nfci_dir"] == "tightening"

    def test_no_stress_series_is_unknown_not_calm(self):
        # INVARIANT: absent stress series must NOT read 'calm' (which would help earn benign)
        out = LQ._credit_stress(_fn_from({"hy_oas": None, "nfci": None}), LQ._cfg())
        assert out["credit"] == "unknown"


# ════════════════════════════════════════════════════════════════════════════════════════
# INVARIANT — a missing series never yields benign; it shrinks toward unknown/stress
# ════════════════════════════════════════════════════════════════════════════════════════
class TestInvariantNeverBenignOnMissing:
    def _benign_base(self):
        """A genuinely benign synthetic window (used as the 'without-corruption' control)."""
        start = date(2026, 3, 2)
        return {
            "walcl_bn": _ramp(start, 60, 6700.0, 2.0),   # WALCL-driven expansion
            "rrp_bn": _flat(start, 60, 450.0),           # fat RRP buffer
            "tga_bn": _flat(start, 60, 700.0),
            "hy_oas": _flat(start, 60, 2.8),             # calm credit
            "nfci": _flat(start, 60, -0.5),
        }

    def test_control_is_benign(self):
        assert LQ.classify(_fn_from(self._benign_base()))["label"] == "benign-expansion"

    def test_missing_net_leg_becomes_unknown(self):
        m = self._benign_base()
        m["walcl_bn"] = None
        assert LQ.classify(_fn_from(m))["label"] == "unknown"

    def test_missing_credit_downgrades_benign_to_stress(self):
        m = self._benign_base()
        m["hy_oas"] = None
        m["nfci"] = None
        # was benign; losing the credit read must degrade toward stress, NEVER stay benign
        assert LQ.classify(_fn_from(m))["label"] == "stress-expansion"

    def test_throwing_series_fn_treated_as_missing(self):
        def _boom(name):
            raise RuntimeError("data store on fire")
        assert LQ.classify(_boom)["label"] == "unknown"

    def test_empty_series_fn_is_unknown(self):
        assert LQ.classify(lambda name: None)["label"] == "unknown"


# ════════════════════════════════════════════════════════════════════════════════════════
# REPLAY — the actual incident window + a synthetic genuine-QE window
# ════════════════════════════════════════════════════════════════════════════════════════
class TestReplayIncidentWindow:
    """The load-bearing test: the label the bot BOUGHT on must be corrected."""

    def _incident_fn(self):
        assert _FIXDIR.exists(), f"missing fixture dir {_FIXDIR}"
        return LQ.series_from_json_fixtures(_FIXDIR)

    def test_incident_is_not_benign(self):
        # THE incident assertion: 07-01 must NOT classify benign-expansion (what it bought on)
        out = LQ.classify(self._incident_fn())
        assert out["label"] != "benign-expansion", (
            f"regression: incident window re-classified {out['label']} — the bot's false "
            "benign-expansion read must never return"
        )

    def test_incident_is_stress_or_hollow(self):
        out = LQ.classify(self._incident_fn())
        assert out["label"] in ("stress-expansion", "neutral-hollow"), out["label"]

    def test_incident_components_match_the_autopsy(self):
        # anchor the reconstructed numbers to the F3 autopsy (RoC +68.9, dWALCL +31.3,
        # d(-TGA) +32.4, RRP $6.4bn, OAS +0.06/20d) so a fixture/alignment drift is caught.
        c = LQ.classify(self._incident_fn())["components"]
        assert c["quantity"] == "expanding"
        assert c["roc_bn"] == pytest.approx(68.9, abs=1.0)
        assert c["dWALCL_bn"] == pytest.approx(31.3, abs=1.0)
        assert c["dnegTGA_bn"] == pytest.approx(32.4, abs=1.0)
        assert c["composition"] == "mechanical"        # WALCL share 0.454 < 0.50
        assert c["walcl_share"] == pytest.approx(0.454, abs=0.02)
        assert c["buffer"] == "exhausted"              # RRP $6.4bn « $100bn
        assert c["rrp_level_bn"] == pytest.approx(6.4, abs=0.1)
        # credit is quiescent at the LEVEL (the radar's credit scare leads the OAS print)
        assert c["credit"] == "calm"

    def test_genuine_qe_window_is_benign(self):
        # A synthetic REAL-QE month: WALCL-driven RoC, FAT RRP, calm OAS → benign-expansion.
        start = date(2026, 3, 2)
        m = {
            "walcl_bn": _ramp(start, 60, 6700.0, 3.0),   # Fed BUYING → +3/step, dominant
            "rrp_bn": _flat(start, 60, 500.0),           # fat cushion ($500bn » $100bn)
            "tga_bn": _flat(start, 60, 700.0),           # TGA steady (no mechanical drain)
            "hy_oas": _flat(start, 60, 3.0),             # credit calm & flat
            "nfci": _ramp(start, 60, -0.4, -0.002),      # NFCI easing (falling)
        }
        out = LQ.classify(_fn_from(m))
        assert out["label"] == "benign-expansion", out
        c = out["components"]
        assert c["composition"] == "benign" and c["buffer"] == "ample" and c["credit"] == "calm"


# ════════════════════════════════════════════════════════════════════════════════════════
# config plumbing
# ════════════════════════════════════════════════════════════════════════════════════════
class TestConfig:
    def test_cfg_has_all_fallback_keys(self):
        cfg = LQ._cfg()
        for k in LQ._FALLBACK:
            assert k in cfg, f"missing config key {k}"

    def test_cfg_reads_doctrine_block(self):
        # doctrine.yml carries a liquidity_quality block; expand_bn should mirror the engine ±25
        cfg = LQ._cfg()
        assert cfg["expand_bn"] == pytest.approx(25.0)
        assert cfg["rrp_buffer_bn"] == pytest.approx(100.0)
