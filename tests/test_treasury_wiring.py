"""W-TSY.1 wiring tests — the treasury_context layer's injection into the Neural Web
decision surfaces (pm_conviction prompt, strategist payload, market_plane liquidity,
phase2 audit sidecar). Mirrors tests/test_neural_web_context.py fixture/monkeypatch style.

Core invariant under test: with MASTERMIND_TREASURY_CONTEXT OFF (the default) every seat is
byte-identical to its pre-change behavior — the ONLY always-on addition is the phase2 audit
row (mirroring the NW audit precedent), which is exercised directly here.
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import brain.treasury_context as TC  # noqa: E402


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #
def _fresh_artifact(tmp_path: Path, as_of: str = "2026-07-09") -> Path:
    art = {
        "schema": "treasury_watch.v1",
        "as_of": as_of,
        "is_context_only": True,
        "tga": {
            "level_bn": 744.637, "asof": as_of,
            "chg_1d_bn": -4.607, "chg_5d_bn": -38.5, "chg_20d_bn": -56.447,
            "quarter_end": {"date": "2026-06-30", "level_bn": 919.145, "chg_since_bn": -174.508},
            "spark_60d": [["2026-07-08", 749.2], ["2026-07-09", 744.6]],
            "spark_points": "0.0,10.0 560.0,54.0",
        },
        "net_liquidity": {"netliq_bn": 5990.4, "chg_20d_bn": 65.0, "chg_65d_bn": -6.2,
                          "pctile_expanding": 0.84, "overlay": "expanding",
                          "walcl_bn": 6735.6, "rrp_bn": 0.5},
        "plumbing": {"headline_state": "stress_liquidity_expansion",
                     "headline_summary_en": "poor-quality expansion",
                     "headline_summary_zh": None,
                     "rrp_buffer_state": "exhausted",
                     "reserve_scarcity_state": "normalizing"},
        "tga_impulse": {"active": True, "direction": "drawdown", "magnitude_bn": -174.508,
                        "days": 7, "since": "2026-06-30", "quarter_end_adjacent": True,
                        "summary_en": "TGA drawdown of $174.5bn since Jun 30 — cash into reserves.",
                        "summary_zh": "自Jun 30以来，TGA下降——现金释放至银行准备金。"},
        "market_context": {"regime_quad": "Q1", "quad_name": "Goldilocks",
                           "liquidity_overlay": "expanding", "regime_asof": "2026-07-10"},
        # a live event whose (hypothetical) prose fields must NEVER reach a prompt
        "events": [{
            "id": "tw-2026-06-30-tga-release", "published": "2026-07-09T20:00:00+00:00",
            "kind": "tga_release", "banner_title": "Treasury spends down $175bn quarter-end cash",
            "banner_title_zh": "财政部释放约1750亿季末现金", "tone": "tailwind",
            "importance": 78, "live": True,
            "analysis": "TW_PROSE_SENTINEL_XYZ analysis prose",
            "summary": "TW_PROSE_SENTINEL_XYZ summary prose",
        }],
        "gaps": [],
    }
    p = tmp_path / "treasury_watch.json"
    p.write_text(json.dumps(art))
    return p


@pytest.fixture(autouse=True)
def _reset_tc_cache():
    TC._reset_context_cache()
    yield
    TC._reset_context_cache()


def _point_tc(monkeypatch, path: Path):
    monkeypatch.setattr(TC, "_ARTIFACT_PATH", path)
    TC._reset_context_cache()


# --------------------------------------------------------------------------- #
# pm_conviction prompt block (flag-gated)
# --------------------------------------------------------------------------- #
def _minimal_pm_payload() -> dict:
    # mirrors tests/test_neural_web_context.py::TestPmConvictionNwBlock minimal payload
    return {
        "asof": "2026-07-09",
        "regime": {"quad_name": "Goldilocks"},
        "sized": [], "rejected": [],
        "defensive_benchmark": ["SPY"],
        "engine_proposed_weights_ADVISORY": {},
    }


def test_pm_prompt_flag_off_has_no_treasury_block(monkeypatch, tmp_path):
    from brain import pm_conviction
    _point_tc(monkeypatch, _fresh_artifact(tmp_path))       # artifact present…
    monkeypatch.delenv("MASTERMIND_TREASURY_CONTEXT", raising=False)  # …but flag OFF
    out = pm_conviction._build_prompt(_minimal_pm_payload())
    assert "TREASURY LIQUIDITY" not in out


def test_pm_prompt_flag_on_injects_bounded_block(monkeypatch, tmp_path):
    from brain import pm_conviction
    _point_tc(monkeypatch, _fresh_artifact(tmp_path))
    monkeypatch.setenv("MASTERMIND_TREASURY_CONTEXT", "1")
    out = pm_conviction._build_prompt(_minimal_pm_payload())
    assert "TREASURY LIQUIDITY (context-only, not validated for sizing)" in out
    assert "TGA $744.6bn" in out
    # structural prose exclusion holds through the whole prompt
    assert "TW_PROSE_SENTINEL_XYZ" not in out


def test_pm_prompt_flag_on_absent_artifact_no_block(monkeypatch, tmp_path):
    from brain import pm_conviction
    _point_tc(monkeypatch, tmp_path / "does_not_exist.json")
    monkeypatch.setenv("MASTERMIND_TREASURY_CONTEXT", "1")
    out = pm_conviction._build_prompt(_minimal_pm_payload())
    assert "TREASURY LIQUIDITY" not in out   # empty seat_prompt_block → no header


# --------------------------------------------------------------------------- #
# strategist payload key (flag-gated)
# --------------------------------------------------------------------------- #
def test_strategist_key_present_iff_flag_on(monkeypatch, tmp_path):
    from brain import strategist
    _point_tc(monkeypatch, _fresh_artifact(tmp_path))

    monkeypatch.delenv("MASTERMIND_TREASURY_CONTEXT", raising=False)
    off = strategist._strategist_input({"quad_name": "Goldilocks"}, "2026-07-09")
    assert "treasury_context" not in off

    monkeypatch.setenv("MASTERMIND_TREASURY_CONTEXT", "1")
    TC._reset_context_cache()
    on = strategist._strategist_input({"quad_name": "Goldilocks"}, "2026-07-09")
    assert "treasury_context" in on
    assert on["treasury_context"]["tga_bn"] == 744.637
    assert on["treasury_context"]["stale"] is False


def test_strategist_flag_on_stale_artifact_key_absent(monkeypatch, tmp_path):
    from brain import strategist
    _point_tc(monkeypatch, _fresh_artifact(tmp_path, as_of="2020-01-01"))  # very stale
    monkeypatch.setenv("MASTERMIND_TREASURY_CONTEXT", "1")
    out = strategist._strategist_input({"quad_name": "Goldilocks"}, "2026-07-09")
    assert "treasury_context" not in out


# --------------------------------------------------------------------------- #
# neural_web_context.market_plane() liquidity distillation (additive, stable shape)
# --------------------------------------------------------------------------- #
def test_market_plane_liquidity_shape_with_block(monkeypatch):
    import brain.neural_web_context as NWC
    NWC._reset_context_cache()
    monkeypatch.setattr(NWC, "_CACHE", {
        "schema": NWC._EXPECTED_SCHEMA, "is_context_only": True,
        "as_of": __import__("datetime").date.today().isoformat(),
        "lobes": {"market": {
            "liquidity_plumbing": {
                "headline": {"state": "stress_liquidity_expansion"},
                "quantity": {"netliq_bn": 5990.4},
                "treasury": {"tga_bn": 744.6,
                             "tga_impulse": {"direction": "drawdown",
                                             "magnitude_bn": -174.5,
                                             "quarter_end_adjacent": True}},
            },
        }},
    })
    monkeypatch.setattr(NWC, "_CACHE_LOADED", True)
    liq = NWC.market_plane().get("liquidity")
    assert liq is not None
    assert liq["state"] == "stress_liquidity_expansion"
    assert liq["netliq_bn"] == 5990.4
    assert liq["tga_bn"] == 744.6
    assert liq["tga_impulse"]["quarter_end_adjacent"] is True
    NWC._reset_context_cache()


def test_market_plane_liquidity_shape_without_block(monkeypatch):
    import brain.neural_web_context as NWC
    NWC._reset_context_cache()
    monkeypatch.setattr(NWC, "_CACHE", {
        "schema": NWC._EXPECTED_SCHEMA, "is_context_only": True,
        "as_of": __import__("datetime").date.today().isoformat(),
        "lobes": {"market": {}},   # no liquidity_plumbing block
    })
    monkeypatch.setattr(NWC, "_CACHE_LOADED", True)
    liq = NWC.market_plane().get("liquidity")
    assert liq == {"state": None, "netliq_bn": None, "tga_bn": None, "tga_impulse": None}
    NWC._reset_context_cache()


# --------------------------------------------------------------------------- #
# phase2 always-on audit sidecar (flag-independent observability)
# --------------------------------------------------------------------------- #
def test_phase2_treasury_audit_helper_writes_jsonl(tmp_path):
    from bot import phase2
    row = {"status": "present", "asof": "2026-07-09", "age_days": 1,
           "n_events": 1, "headline_state": "stress_liquidity_expansion"}
    phase2._append_treasury_context_audit(tmp_path, "run-xyz", row)
    p = tmp_path / "data" / "brain" / "treasury_context_audit.jsonl"
    assert p.exists()
    rec = json.loads(p.read_text().splitlines()[-1])
    assert rec["run_id"] == "run-xyz"
    assert rec["status"] == "present"
    assert rec["n_events"] == 1
    assert rec["headline_state"] == "stress_liquidity_expansion"
    assert rec.get("ts")   # timestamp injected


def test_phase2_treasury_audit_helper_never_raises_on_bad_root(tmp_path):
    from bot import phase2
    # a file where a directory is expected must be swallowed (best-effort append)
    bad = tmp_path / "afile"
    bad.write_text("x")
    phase2._append_treasury_context_audit(bad, "run", {"status": "absent"})  # must not raise
