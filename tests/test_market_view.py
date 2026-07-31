"""brain/market_view.py — THE one market view (task E0.3) + replay battery (task E0.4).

WHAT IS ASSERTED (charter P1/P2/P3/P7; wave contract = zero behaviour change):
  * SCHEMA / GOLDEN KEY ORDER — the artifact feeds LLM prompts; top-level + per-plane + brief
    key orders are frozen (key-order drift silently changes prompt output).
  * ADAPTERS — each Tier-A embedded-key plane normalizes to a PlaneRecord with the right
    discrete direction; a missing block degrades to an ABSENT record (never a fabricated read).
  * FRESHNESS — per-plane freshness is computed from THAT block's OWN asof; a stale plane is
    stale:true and EXCLUDED from the validated tilt (fail-closed).
  * DISAGREEMENT LAYER — net_posture_tilt is signed ONLY by VALIDATED, FRESH planes; advisory /
    absent / stale planes weight 0 and can NEVER lower disagreement or sign the tilt (P2).
  * label_vs_planes — the incident's label-vs-validated-consensus split as a first-class field.
  * INCIDENT REPLAY (§4.3) — on the frozen fixtures label_vs_planes.conflict=True EVERY session
    06-26..07-01; the 07-01 assert: label risk_on @ conf 0.327 vs a risk_off validated consensus,
    posture_floor_defense=True, brief.posture_implication mentions defense.
  * CALM-TAPE CONTROL (§4.6) — a high-confidence agreeing tape → conflict=False, validated
    consensus risk_on, posture_floor_defense=False.
  * MISSING-FILE NO-OP + FRESHNESS FAIL-CLOSED — an absent regime file yields an all-absent view
    that never raises; a stale plane never signs the tilt.

The shared vendor/macro store is NEVER live-read: every regime / sector-cycle payload is a
hand-built FROZEN fixture (tests/fixtures/market_view/build_fixtures.py) injected by monkeypatching
regime_frame._REGION_PATHS / _CYCLES_PATH; the per-plane freshness clock is frozen via
_trading_days_since so intent (not a pinned live state) is asserted.  No account / network / LLM.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_MACRO_SRC = _ROOT / "vendor" / "macro_src"
if _MACRO_SRC.exists() and str(_MACRO_SRC) not in sys.path:
    sys.path.insert(0, str(_MACRO_SRC))

from brain import market_view as MV       # noqa: E402
from brain import regime_frame as RF      # noqa: E402

sys.path.insert(0, str(_ROOT / "tests" / "fixtures" / "market_view"))
import build_fixtures as FIX               # noqa: E402


# ---------------------------------------------------------------------------
# helpers — write a frozen regime + cycles payload into tmp and patch the readers
# ---------------------------------------------------------------------------

def _patch_regime(monkeypatch, tmp_path, regime: dict, cycles: dict | None = None,
                  *, age: int = 0) -> None:
    """Point regime_frame at a frozen regime JSON + (optional) sector_cycles JSON in tmp_path.

    ``age`` freezes the per-plane freshness clock: every plane's _trading_days_since returns
    ``age`` so a plane whose asof == its session reads as ``age`` sessions old (0 = fresh today).
    """
    rp = tmp_path / "regime_latest.json"
    rp.write_text(json.dumps(regime))
    monkeypatch.setitem(RF._REGION_PATHS, "us", rp)
    if cycles is not None:
        cp = tmp_path / "sector_cycles.json"
        cp.write_text(json.dumps(cycles))
        monkeypatch.setattr(RF, "_CYCLES_PATH", cp, raising=False)
    # Published Macro context files are separately tested below.  Default every frozen-view
    # test to explicit absence so the live vendored tree cannot leak into deterministic fixtures.
    monkeypatch.setattr(MV, "_RRG_PATH", tmp_path / "missing_rrg.json", raising=False)
    monkeypatch.setattr(
        MV, "_GROUP_FLOW_PATH", tmp_path / "missing_group_flow.json", raising=False
    )
    monkeypatch.setattr(RF, "_trading_days_since", lambda asof: age, raising=False)


# ---------------------------------------------------------------------------
# SCHEMA / golden key order
# ---------------------------------------------------------------------------

class TestSchemaKeyOrder:
    def test_top_level_key_order(self, monkeypatch, tmp_path):
        _patch_regime(monkeypatch, tmp_path,
                      FIX.incident_regime("2026-07-01"),
                      FIX.incident_sector_cycles("2026-07-01"))
        v = MV.view("us")
        assert list(v.keys()) == list(MV.TOP_LEVEL_ORDER)
        assert v["schema_version"] == "market_view.v1"

    def test_plane_key_order(self, monkeypatch, tmp_path):
        _patch_regime(monkeypatch, tmp_path,
                      FIX.incident_regime("2026-07-01"),
                      FIX.incident_sector_cycles("2026-07-01"))
        v = MV.view("us")
        assert list(v["planes"].keys()) == list(MV.PLANE_ORDER)

    def test_plane_record_key_order(self, monkeypatch, tmp_path):
        _patch_regime(monkeypatch, tmp_path,
                      FIX.incident_regime("2026-07-01"),
                      FIX.incident_sector_cycles("2026-07-01"))
        v = MV.view("us")
        rec = v["planes"]["risk_radar"]
        assert list(rec.keys()) == list(MV._PLANE_RECORD_ORDER)

    def test_brief_key_order(self, monkeypatch, tmp_path):
        _patch_regime(monkeypatch, tmp_path,
                      FIX.incident_regime("2026-07-01"),
                      FIX.incident_sector_cycles("2026-07-01"))
        v = MV.view("us")
        assert list(v["brief"].keys()) == list(MV._BRIEF_ORDER)


# ---------------------------------------------------------------------------
# adapters — direction + degrade-to-absent
# ---------------------------------------------------------------------------

class TestAdapters:
    def test_risk_radar_caution_is_risk_off_and_validated(self, monkeypatch, tmp_path):
        _patch_regime(monkeypatch, tmp_path,
                      FIX.incident_regime("2026-07-01"),
                      FIX.incident_sector_cycles("2026-07-01"))
        rec = MV.view("us")["planes"]["risk_radar"]
        assert rec["direction"] == "risk_off"
        assert rec["status"] == "validated"
        assert rec["magnitude"] == 0.19   # drawdown_prob.h21

    def test_missing_block_is_absent_never_fabricated(self, monkeypatch, tmp_path):
        reg = FIX.incident_regime("2026-07-01")
        del reg["risk_radar"]
        _patch_regime(monkeypatch, tmp_path, reg,
                      FIX.incident_sector_cycles("2026-07-01"))
        rec = MV.view("us")["planes"]["risk_radar"]
        assert rec["reading"] is None
        assert rec["direction"] is None
        assert rec["raw"]["present"] is False
        assert rec["freshness"]["stale"] is True

    def test_gross_factor_below_one_is_risk_off_advisory(self, monkeypatch, tmp_path):
        _patch_regime(monkeypatch, tmp_path,
                      FIX.incident_regime("2026-07-01"),
                      FIX.incident_sector_cycles("2026-07-01"))
        rec = MV.view("us")["planes"]["gross_factor"]
        assert rec["direction"] == "risk_off"
        assert rec["status"] == "advisory"          # advisory even when directional
        assert rec["magnitude"] == 0.9

    def test_cross_asset_concentrated_is_risk_off(self, monkeypatch, tmp_path):
        _patch_regime(monkeypatch, tmp_path,
                      FIX.incident_regime("2026-07-01"),
                      FIX.incident_sector_cycles("2026-07-01"))
        rec = MV.view("us")["planes"]["cross_asset"]
        assert rec["direction"] == "risk_off"

    def test_null_stubs_present_from_day_one(self, monkeypatch, tmp_path):
        _patch_regime(monkeypatch, tmp_path,
                      FIX.incident_regime("2026-07-01"),
                      FIX.incident_sector_cycles("2026-07-01"))
        planes = MV.view("us")["planes"]
        for stub in ("rrg", "group_flow", "event_calendar", "intl_spillover"):
            assert stub in planes
            assert planes[stub]["raw"]["present"] is False
            assert planes[stub]["status"] == "advisory"

    def test_wi_planes_injected_not_live_read(self, monkeypatch, tmp_path):
        _patch_regime(monkeypatch, tmp_path,
                      FIX.incident_regime("2026-07-01"),
                      FIX.incident_sector_cycles("2026-07-01"))
        # inject a hot distribution + stress liquidity + doubt nowcast
        v = MV.view(
            "us",
            distribution_tells_out={"hot": True, "def_rs_cross": True,
                                    "distributing_weight_frac": 0.30, "asof": "2026-07-01",
                                    "reason": "distribution: SMH crowd99+3D-MACD-bear"},
            liquidity_quality_out={"label": "stress-expansion", "asof": "2026-07-01"},
            regime_nowcast_out={"applies": True, "stance": "doubt",
                                "legs": {"n_doubt": 2, "asof": "2026-07-01"},
                                "reason": "doubt: 2/3 legs"},
        )
        assert v["planes"]["distribution_tells"]["direction"] == "risk_off"
        assert v["planes"]["liquidity_quality"]["direction"] == "risk_off"
        assert v["planes"]["regime_nowcast"]["direction"] == "risk_off"
        # regime_nowcast is ADVISORY-ONLY forever (gate failed) — never validated
        assert v["planes"]["regime_nowcast"]["status"] == "advisory"

    def test_wi_planes_absent_when_not_supplied(self, monkeypatch, tmp_path):
        _patch_regime(monkeypatch, tmp_path,
                      FIX.incident_regime("2026-07-01"),
                      FIX.incident_sector_cycles("2026-07-01"))
        v = MV.view("us")   # no W-I injections
        for name in ("distribution_tells", "liquidity_quality", "regime_nowcast"):
            assert v["planes"][name]["raw"]["present"] is False

    def test_distribution_cross_reading_matches_risk_off_direction(
        self, monkeypatch, tmp_path
    ):
        _patch_regime(
            monkeypatch,
            tmp_path,
            FIX.incident_regime("2026-07-01"),
            FIX.incident_sector_cycles("2026-07-01"),
        )
        v = MV.view(
            "us",
            distribution_tells_out={
                "hot": False,
                "def_rs_cross": True,
                "distributing_weight_frac": 0.0,
                "asof": "2026-07-01",
            },
        )
        plane = v["planes"]["distribution_tells"]
        assert plane["direction"] == "risk_off"
        assert plane["reading"] == "defensive relative-strength cross"

    def test_rrg_contract_is_present_precise_and_non_directional(
        self, monkeypatch, tmp_path
    ):
        _patch_regime(
            monkeypatch,
            tmp_path,
            FIX.incident_regime("2026-07-01"),
            FIX.incident_sector_cycles("2026-07-01"),
        )
        path = tmp_path / "rrg.json"
        path.write_text(
            json.dumps(
                {
                    "asof": "2026-07-01",
                    "sectors": [
                        {"key": "XLK", "quadrant": "lagging"},
                        {"key": "XLP", "quadrant": "leading"},
                        {"key": "XLC", "quadrant": "improving"},
                    ],
                    "track_record": {
                        "verdict": "validated",
                        "proven": {"10": True, "21": True},
                        "horizons": {
                            "10": {"score_ic": 0.25, "score_ic_t_hac": 4.1},
                            "21": {"score_ic": 0.26, "score_ic_t_hac": 3.6},
                        },
                    },
                }
            )
        )
        monkeypatch.setattr(MV, "_RRG_PATH", path)
        plane = MV.view("us")["planes"]["rrg"]
        assert plane["raw"]["artifact_present"] is True
        assert plane["direction"] == "neutral"
        assert plane["status"] == "advisory"
        assert "leading=XLP" in plane["reading"]
        assert plane["raw"]["proven_horizons"] == ["10", "21"]

    def test_group_flow_preserves_display_only_uncalibrated_contract(
        self, monkeypatch, tmp_path
    ):
        _patch_regime(
            monkeypatch,
            tmp_path,
            FIX.incident_regime("2026-07-01"),
            FIX.incident_sector_cycles("2026-07-01"),
        )
        path = tmp_path / "group_flow.json"
        path.write_text(
            json.dumps(
                {
                    "as_of": "2026-07-01",
                    "verdict": "display_only",
                    "calibrated": False,
                    "cluster": {"regime": "mixed", "absorption": 0.45},
                    "emerging": {
                        "sectors": [{"name": "Energy"}],
                        "baskets": [{"name": "Non-AI Software"}],
                    },
                    "cooling": {
                        "sectors": [{"name": "Consumer Staples"}],
                        "baskets": [],
                    },
                }
            )
        )
        monkeypatch.setattr(MV, "_GROUP_FLOW_PATH", path)
        plane = MV.view("us")["planes"]["group_flow"]
        assert plane["raw"]["artifact_present"] is True
        assert plane["raw"]["calibrated"] is False
        assert plane["raw"]["directional"] is False
        assert plane["direction"] == "neutral"
        assert plane["status"] == "advisory"
        assert "emerging=Energy,Non-AI Software" in plane["reading"]


# ---------------------------------------------------------------------------
# freshness — per-plane, fail-closed
# ---------------------------------------------------------------------------

class TestFreshness:
    def test_stale_validated_plane_downgraded_and_excluded(self, monkeypatch, tmp_path):
        # age=99 sessions → every plane stale → NO validated plane may sign the tilt.
        _patch_regime(monkeypatch, tmp_path,
                      FIX.incident_regime("2026-07-01"),
                      FIX.incident_sector_cycles("2026-07-01"), age=99)
        v = MV.view("us")
        assert v["planes"]["risk_radar"]["freshness"]["stale"] is True
        assert v["planes"]["risk_radar"]["status"] == "advisory"   # stale → downgraded
        assert v["net_posture_tilt"]["n_validated"] == 0
        assert v["assembly"]["decision_coverage"] == 0.0
        assert v["assembly"]["degraded"] is True
        assert "validated_decision_plane_incomplete" in v["assembly"]["degrade_reasons"]
        # a fully-stale view can never manufacture a conflict
        assert v["label_vs_planes"]["conflict"] is False

    def test_unknown_age_is_stale_fail_closed(self, monkeypatch, tmp_path):
        reg = FIX.incident_regime("2026-07-01")
        reg["risk_radar"].pop("asof", None)   # no own asof → unknown age
        _patch_regime(monkeypatch, tmp_path, reg,
                      FIX.incident_sector_cycles("2026-07-01"))
        # restore the real _trading_days_since so a None asof returns None (unknown)
        monkeypatch.setattr(RF, "_trading_days_since",
                            lambda asof: None if asof in (None, "None") else 0, raising=False)
        rec = MV.view("us")["planes"]["risk_radar"]
        assert rec["freshness"]["stale"] is True
        assert rec["status"] == "advisory"

    def test_future_dated_plane_is_stale_fail_closed(self, monkeypatch, tmp_path):
        _patch_regime(monkeypatch, tmp_path,
                      FIX.incident_regime("2026-07-01"),
                      FIX.incident_sector_cycles("2026-07-01"))
        monkeypatch.setattr(RF, "_trading_days_since", lambda asof: -1, raising=False)
        rec = MV.view("us")["planes"]["risk_radar"]
        assert rec["freshness"]["age_sessions"] == -1
        assert rec["freshness"]["stale"] is True
        assert rec["status"] == "advisory"


# ---------------------------------------------------------------------------
# disagreement layer — validated-only tilt; absent/advisory can't sign or lower it
# ---------------------------------------------------------------------------

class TestDisagreementLayer:
    def test_tilt_only_from_validated_fresh_planes(self, monkeypatch, tmp_path):
        _patch_regime(monkeypatch, tmp_path,
                      FIX.incident_regime("2026-07-01"),
                      FIX.incident_sector_cycles("2026-07-01"))
        tilt = MV.view("us")["net_posture_tilt"]
        # only the three validated planes may contribute
        assert set(tilt["contributors"]) <= {"risk_radar", "mtf_signals", "cycles"}
        assert tilt["n_validated"] == len(tilt["contributors"])

    def test_advisory_planes_never_sign_tilt(self, monkeypatch, tmp_path):
        # a calm regime whose ADVISORY planes are forced risk_off must not flip the risk_on
        # validated tilt — advisory can annotate/shrink, never sign.
        reg = FIX.calm_regime("2026-07-01")
        reg["froth_fragility"]["alert"] = True
        reg["cross_asset"]["verdict"] = "concentrated"
        reg["gross_factor_hack"] = None
        reg["risk_state"]["gross_factor"] = 0.8
        _patch_regime(monkeypatch, tmp_path, reg,
                      FIX.calm_sector_cycles("2026-07-01"))
        v = MV.view("us")
        assert v["net_posture_tilt"]["direction"] == "risk_on"   # validated stay risk_on
        # advisory risk_off planes DO create disagreements (they annotate)…
        assert any(d["a"] == "froth_fragility" or d["b"] == "froth_fragility"
                   for d in v["disagreements"])
        # …but they never sign the tilt and never (here) flip label_vs_planes to conflict
        assert v["label_vs_planes"]["conflict"] is False

    def test_absent_plane_cannot_lower_disagreement(self, monkeypatch, tmp_path):
        _patch_regime(monkeypatch, tmp_path,
                      FIX.incident_regime("2026-07-01"),
                      FIX.incident_sector_cycles("2026-07-01"))
        v_full = MV.view("us")
        # drop an advisory risk_off plane → disagreements must not INCREASE coherence-artificially;
        # the absent plane simply stops participating (never manufactures agreement).
        reg2 = FIX.incident_regime("2026-07-01")
        del reg2["froth_fragility"]
        _patch_regime(monkeypatch, tmp_path, reg2,
                      FIX.incident_sector_cycles("2026-07-01"))
        v_drop = MV.view("us")
        # the validated conflict is unchanged by dropping an advisory plane
        assert v_drop["label_vs_planes"]["conflict"] == v_full["label_vs_planes"]["conflict"]

    def test_neutral_consensus_is_unconfirmed_never_agreement(self):
        lvp = MV._label_vs_planes(
            "risk_on",
            {"direction": "neutral", "tilt": 0.0, "contributors": []},
            {},
        )
        assert lvp["conflict"] is False
        assert lvp["confirmed"] is False
        assert lvp["relationship"] == "unconfirmed"

    def test_matching_directional_consensus_is_confirmed(self):
        lvp = MV._label_vs_planes(
            "risk_on",
            {"direction": "risk_on", "tilt": 1.0, "contributors": []},
            {},
        )
        assert lvp["confirmed"] is True
        assert lvp["relationship"] == "confirmed"

    def test_stale_directional_plane_is_excluded_from_coherence(self, monkeypatch):
        monkeypatch.setattr(
            RF,
            "_trading_days_since",
            lambda asof: None if asof in (None, "None") else 0,
            raising=False,
        )
        planes = {
            name: MV._absent_record("fixture")
            for name in MV.PLANE_ORDER
        }
        planes["risk_radar"] = MV._plane_record(
            reading="fresh caution",
            direction="risk_off",
            magnitude=0.2,
            asof="2026-07-29",
            confidence=0.7,
            validated=True,
            source_contract="fixture",
            raw={},
        )
        planes["froth_fragility"] = MV._plane_record(
            reading="stale contrary",
            direction="risk_on",
            magnitude=1.0,
            asof=None,
            confidence=None,
            validated=False,
            source_contract="fixture",
            raw={},
        )
        assert MV._coherence(planes) == 1.0

    def test_turning_point_inactive_is_present_not_missing(self, monkeypatch, tmp_path):
        regime = FIX.incident_regime("2026-07-01")
        regime["turning_point"] = {
            "asof": "2026-07-01",
            "present": False,
            "active": False,
            "raw_fire": False,
            "state": "normal",
        }
        _patch_regime(
            monkeypatch,
            tmp_path,
            regime,
            FIX.incident_sector_cycles("2026-07-01"),
        )
        v = MV.view("us")
        rec = v["planes"]["turning_point"]
        assert rec["raw"]["artifact_present"] is True
        assert rec["raw"]["signal_active"] is False
        assert rec["raw"]["present"] is True

    def test_cycles_uses_source_asof_not_wall_clock(self, monkeypatch, tmp_path):
        _patch_regime(
            monkeypatch,
            tmp_path,
            FIX.incident_regime("2026-07-01"),
            FIX.incident_sector_cycles("2026-07-01"),
        )
        rec = MV.view("us")["planes"]["cycles"]
        assert rec["freshness"]["asof"] == "2026-07-01"
        assert rec["raw"]["source_asof"] == "2026-07-01"


# ---------------------------------------------------------------------------
# E0.1 / E0.2 organ adapters — read the contract when present, degrade when absent
# ---------------------------------------------------------------------------

class TestOrganAdapters:
    def test_rotation_tensor_absent_when_unbuilt(self, monkeypatch, tmp_path):
        _patch_regime(monkeypatch, tmp_path,
                      FIX.incident_regime("2026-07-01"),
                      FIX.incident_sector_cycles("2026-07-01"))
        monkeypatch.setattr(MV, "_ROTATION_TENSOR_PATH",
                            tmp_path / "no_tensor.json", raising=False)
        rec = MV.view("us")["planes"]["rotation_tensor"]
        assert rec["raw"]["present"] is False

    def test_rotation_tensor_defensive_episode_is_risk_off(self, monkeypatch, tmp_path):
        _patch_regime(monkeypatch, tmp_path,
                      FIX.incident_regime("2026-07-01"),
                      FIX.incident_sector_cycles("2026-07-01"))
        tp = tmp_path / "rotation_tensor.json"
        tp.write_text(json.dumps({
            "schema_version": 1, "as_of": "2026-07-01", "confidence": 0.71,
            "advisory": True,
            "headline_episode": {"axis": "DEF_over_OFF", "start": "2026-06-24",
                                 "n_sessions": 6, "magnitude_bps": 10.3,
                                 "percentile": 0.83, "direction": "defensive"},
        }))
        monkeypatch.setattr(MV, "_ROTATION_TENSOR_PATH", tp, raising=False)
        rec = MV.view("us")["planes"]["rotation_tensor"]
        assert rec["direction"] == "risk_off"
        assert rec["status"] == "advisory"      # organ is advisory until its §4 gate
        assert rec["magnitude"] == 10.3

    def test_anticipation_absent_when_unbuilt(self, monkeypatch, tmp_path):
        _patch_regime(monkeypatch, tmp_path,
                      FIX.incident_regime("2026-07-01"),
                      FIX.incident_sector_cycles("2026-07-01"))
        monkeypatch.setattr(MV, "_ANTICIPATION_DIR", tmp_path / "no_antic", raising=False)
        rec = MV.view("us")["planes"]["anticipation"]
        assert rec["raw"]["present"] is False

    def test_anticipation_crash_elevated_is_risk_off(self, monkeypatch, tmp_path):
        _patch_regime(monkeypatch, tmp_path,
                      FIX.incident_regime("2026-07-01"),
                      FIX.incident_sector_cycles("2026-07-01"))
        adir = tmp_path / "anticipation"
        adir.mkdir()
        (adir / "latest.json").write_text(json.dumps({
            "schema_version": 1, "asof": "2026-07-01", "top_level": "elevated",
            "sector_top": [{"kind": "sector_top", "scope": "XLK", "level": "critical",
                            "status": "advisory"}],
            "bubble_formation": [],
            "crash_risk": {"kind": "crash_risk", "scope": "market", "level": "elevated",
                           "status": "advisory"},
        }))
        monkeypatch.setattr(MV, "_ANTICIPATION_DIR", adir, raising=False)
        rec = MV.view("us")["planes"]["anticipation"]
        assert rec["direction"] == "risk_off"
        assert rec["raw"]["crash_risk_level"] == "elevated"
        assert rec["raw"]["sector_top_fired"] == 1


# ---------------------------------------------------------------------------
# INCIDENT REPLAY (§4.3) — the permanent CI battery
# ---------------------------------------------------------------------------

class TestIncidentReplay:
    @pytest.mark.parametrize("session", FIX.HARD_CONFLICT_SESSIONS)
    def test_conflict_true_every_hard_session(self, monkeypatch, tmp_path, session):
        _patch_regime(monkeypatch, tmp_path,
                      FIX.incident_regime(session),
                      FIX.incident_sector_cycles(session))
        v = MV.view("us")
        assert v["label_vs_planes"]["conflict"] is True, (
            f"{session}: label lies risk_on while validated planes dissent risk_off")
        assert v["label_vs_planes"]["label_direction"] == "risk_on"
        assert v["label_vs_planes"]["plane_consensus_direction"] == "risk_off"
        assert v["posture_floor_defense"] is True

    def test_0701_full_incident_assert(self, monkeypatch, tmp_path):
        _patch_regime(monkeypatch, tmp_path,
                      FIX.incident_regime("2026-07-01"),
                      FIX.incident_sector_cycles("2026-07-01"))
        v = MV.view("us")
        # label risk_on @ conf 0.327 (from the frozen regime)
        assert v["label_vs_planes"]["label_direction"] == "risk_on"
        assert v["planes"]["risk_radar"]["status"] == "validated"
        # validated consensus dissents risk_off
        assert v["net_posture_tilt"]["direction"] == "risk_off"
        # radar + cycles are among the dissenters (the incident's named split)
        dissent = set(v["label_vs_planes"]["dissenting_planes"])
        assert "risk_radar" in dissent
        assert "cycles" in dissent
        assert v["posture_floor_defense"] is True
        # the deterministic brief mentions defense (posture_implication)
        assert "defens" in v["brief"]["posture_implication"].lower()

    def test_0624_soft_boundary_allowed_either(self, monkeypatch, tmp_path):
        # 06-24 is the soft boundary — mtf is NOT yet risk_off; the assert is only that the
        # view assembles and does not RAISE (conflict may be either way).
        _patch_regime(monkeypatch, tmp_path,
                      FIX.incident_regime("2026-06-24"),
                      FIX.incident_sector_cycles("2026-06-24"))
        v = MV.view("us")
        assert v["schema_version"] == "market_view.v1"
        assert isinstance(v["label_vs_planes"]["conflict"], bool)


# ---------------------------------------------------------------------------
# CALM-TAPE CONTROL (§4.6) — zero drift, no conflict
# ---------------------------------------------------------------------------

class TestCalmTape:
    def test_calm_no_conflict_validated_risk_on(self, monkeypatch, tmp_path):
        _patch_regime(monkeypatch, tmp_path,
                      FIX.calm_regime("2026-07-01"),
                      FIX.calm_sector_cycles("2026-07-01"))
        v = MV.view("us")
        assert v["label_vs_planes"]["conflict"] is False
        assert v["net_posture_tilt"]["direction"] == "risk_on"
        assert v["posture_floor_defense"] is False
        assert "no defensive" in v["brief"]["posture_implication"].lower() \
            or "agree" in v["brief"]["posture_implication"].lower()

    def test_calm_radar_is_risk_on_validated(self, monkeypatch, tmp_path):
        _patch_regime(monkeypatch, tmp_path,
                      FIX.calm_regime("2026-07-01"),
                      FIX.calm_sector_cycles("2026-07-01"))
        rec = MV.view("us")["planes"]["risk_radar"]
        assert rec["direction"] == "risk_on"
        assert rec["status"] == "validated"


# ---------------------------------------------------------------------------
# MISSING-FILE NO-OP — an absent regime file never raises
# ---------------------------------------------------------------------------

class TestMissingFileNoOp:
    def test_absent_regime_file_all_absent_no_raise(self, monkeypatch, tmp_path):
        absent = tmp_path / "nonexistent.json"
        monkeypatch.setitem(RF._REGION_PATHS, "us", absent)
        monkeypatch.setattr(RF, "_CYCLES_PATH", tmp_path / "no_cycles.json", raising=False)
        # W8: complete the isolation — the NW reader is flag-independent, and the anticipation /
        # rotation-tensor organs read persisted repo-state a prior (green) replay run may have
        # seeded. This test's subject is EVERY-plane-absent no-raise, so stub them all absent.
        import brain.neural_web_context as _NWC
        monkeypatch.setattr(_NWC, "context", lambda: {})
        monkeypatch.setattr(_NWC, "market_plane", lambda: {"stale": True, "asof": None})
        monkeypatch.setattr(MV, "_ANTICIPATION_DIR", tmp_path / "no_anticipation", raising=False)
        monkeypatch.setattr(MV, "_ROTATION_TENSOR_PATH", tmp_path / "no_rt.json", raising=False)
        monkeypatch.setattr(MV, "_RRG_PATH", tmp_path / "no_rrg.json", raising=False)
        monkeypatch.setattr(
            MV, "_GROUP_FLOW_PATH", tmp_path / "no_group_flow.json", raising=False
        )
        v = MV.view("us")   # must not raise
        assert v["schema_version"] == "market_view.v1"
        assert v["asof"] is None
        assert v["net_posture_tilt"]["n_validated"] == 0
        assert v["label_vs_planes"]["conflict"] is False
        assert v["assembly"]["missing"] == v["assembly"]["total"]

    def test_corrupt_regime_file_no_raise(self, monkeypatch, tmp_path):
        p = tmp_path / "latest.json"
        p.write_text("{not valid json!!!")
        monkeypatch.setitem(RF._REGION_PATHS, "us", p)
        monkeypatch.setattr(RF, "_CYCLES_PATH", tmp_path / "no_cycles.json", raising=False)
        v = MV.view("us")
        assert v["label_vs_planes"]["conflict"] is False


# ---------------------------------------------------------------------------
# build() — atomic write + dated copy + what_changed diff
# ---------------------------------------------------------------------------

class TestBuild:
    def test_build_writes_latest_and_dated_atomic(self, monkeypatch, tmp_path):
        _patch_regime(monkeypatch, tmp_path,
                      FIX.incident_regime("2026-07-01"),
                      FIX.incident_sector_cycles("2026-07-01"))
        art = tmp_path / "market_view"
        monkeypatch.setattr(MV, "_ARTIFACT_DIR", art, raising=False)
        monkeypatch.setattr(MV, "_LATEST_PATH", art / "latest.json", raising=False)
        v = MV.build("us", write=True)
        assert (art / "latest.json").exists()
        assert (art / "2026-07-01.json").exists()
        on_disk = json.loads((art / "latest.json").read_text())
        assert on_disk["label_vs_planes"]["conflict"] is True
        assert on_disk["seq"] == 1
        assert list(on_disk.keys()) == list(MV.TOP_LEVEL_ORDER)
        # no .tmp litter left behind
        assert not list(art.glob("*.tmp"))

    def test_what_changed_diff_from_prev(self, monkeypatch, tmp_path):
        art = tmp_path / "market_view"
        monkeypatch.setattr(MV, "_ARTIFACT_DIR", art, raising=False)
        monkeypatch.setattr(MV, "_LATEST_PATH", art / "latest.json", raising=False)
        # day 1: calm (no conflict)
        _patch_regime(monkeypatch, tmp_path,
                      FIX.calm_regime("2026-06-25"),
                      FIX.calm_sector_cycles("2026-06-25"))
        MV.build("us", write=True)
        # day 2: incident (conflict opens) — what_changed must say OPENED
        _patch_regime(monkeypatch, tmp_path,
                      FIX.incident_regime("2026-06-26"),
                      FIX.incident_sector_cycles("2026-06-26"))
        v = MV.build("us", write=True)
        assert "OPENED" in v["brief"]["what_changed"]
        assert v["seq"] == 2

    def test_build_no_op_on_write_failure(self, monkeypatch, tmp_path):
        _patch_regime(monkeypatch, tmp_path,
                      FIX.incident_regime("2026-07-01"),
                      FIX.incident_sector_cycles("2026-07-01"))
        # point the artifact dir at an un-creatable path → write fails silently, view still returned
        monkeypatch.setattr(MV, "_ARTIFACT_DIR", Path("/proc/nonexistent/market_view"),
                            raising=False)
        monkeypatch.setattr(MV, "_LATEST_PATH",
                            Path("/proc/nonexistent/market_view/latest.json"), raising=False)
        v = MV.build("us", write=True)   # must not raise
        assert v["schema_version"] == "market_view.v1"
