"""tests/test_validate_perception.py — smoke tests for the perception validation harness
(scripts/validate_perception.py, W-E.1 task E1.4).

These are SMOKE tests (the harness itself is a deliberate on-demand tool, not a daily gate). They
assert the harness's CONTRACT, never a live market state (intent-only, per the wave rules):

  * fixture-injection: the rotation_tensor job reads the FROZEN tests/fixtures/rotation_tensor
    parquets (never the live/vendored data) — we point ``_yahoo_dir`` at the fixture dir so the test
    is hermetic and the shared live stores are untouched;
  * the rotation_tensor job returns a well-formed, computable verdict with a real AUC and a
    fire-fraction, and the gate logic (AUC>0.55 AND fires<10%) resolves PASS/FAIL correctly;
  * the CRASH-RISK and label_vs_planes jobs return the HONEST uncomputable-with-vendored-history
    verdict (cold_start=True, computable=False, verdict='FAIL', ships advisory — the nowcast
    precedent, P3);
  * markdown verdicts are emitted, one per object, with the pre-registered gate stated;
  * the harness WIRES NOTHING (it only reads price data + writes markdown).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from scripts import validate_perception as VP

_FIX = Path(__file__).resolve().parent / "fixtures" / "rotation_tensor"


@pytest.fixture()
def _fixture_prices(monkeypatch):
    """Point the harness's read-only price loader at the FROZEN fixture parquets (2015-2026), so no
    test touches the live/vendored yahoo store."""
    monkeypatch.setattr(VP, "_yahoo_dir", lambda: _FIX, raising=True)
    # also point the tensor's own default reader at the fixtures (it uses its own _YAHOO_DIR)
    from brain import rotation_tensor as rt
    monkeypatch.setattr(rt, "_YAHOO_DIR", _FIX, raising=False)
    return _FIX


# ---------------------------------------------------------------------------
# rotation_tensor — the one computable job
# ---------------------------------------------------------------------------

class TestRotationTensorJob:
    def test_returns_computable_wellformed_verdict(self, _fixture_prices):
        # a short window, large stride → a handful of evaluable sessions (fast, hermetic)
        res = VP.run_rotation_tensor("2020-01-01", "2021-12-31", stride=10)
        assert res["status"] == "ok"
        assert res["object"] == "rotation_tensor"
        assert res["computable"] is True
        assert res["cold_start"] is False
        # a real walk-forward happened
        assert res["n_eval"] > 0
        # AUC is a float in [0,1] or None (one-class), fire_frac in [0,1]
        assert res["auc"] is None or (0.0 <= res["auc"] <= 1.0)
        assert 0.0 <= res["fire_frac"] <= 1.0
        # verdict is a pre-registered PASS/FAIL, never fabricated
        assert res["verdict"] in ("PASS", "FAIL")

    def test_gate_logic_resolves_from_metrics(self, _fixture_prices):
        res = VP.run_rotation_tensor("2020-01-01", "2021-12-31", stride=10)
        g = res["gate"]
        # gate is the AND of the two pre-registered conditions
        expect_pass = bool(g["auc_pass"] and g["fires_pass"])
        assert (res["verdict"] == "PASS") == expect_pass
        # the arming string reflects the verdict (shrink-only plane vs display-only)
        if res["verdict"] == "PASS":
            assert "may enter D" in res["arms"]
        else:
            assert "DISPLAY-ONLY" in res["arms"]

    def test_no_data_is_indeterminate_not_crash(self, monkeypatch, tmp_path):
        # empty dir → benchmark parquet missing → honest no_data, never raises
        monkeypatch.setattr(VP, "_yahoo_dir", lambda: tmp_path, raising=True)
        res = VP.run_rotation_tensor("2020-01-01", "2020-06-30", stride=10)
        assert res["status"] == "no_data"


# ---------------------------------------------------------------------------
# CRASH-RISK / label_vs_planes — the honest uncomputable jobs
# ---------------------------------------------------------------------------

class TestUncomputableJobs:
    def test_crash_risk_is_honest_cold_start_fail(self):
        res = VP.run_crash_risk()
        assert res["status"] == "uncomputable"
        assert res["object"] == "crash_risk"
        assert res["computable"] is False
        assert res["cold_start"] is True
        assert res["verdict"] == "FAIL"
        # names what it needs + cites the nowcast precedent (P3 honesty)
        assert "drawdown_prob" in res["needs"]
        assert "advisory" in res["arms"].lower()

    def test_label_vs_planes_is_honest_cold_start_fail(self):
        res = VP.run_label_vs_planes()
        assert res["computable"] is False
        assert res["cold_start"] is True
        assert res["verdict"] == "FAIL"
        assert "market_view" in res["needs"]


# ---------------------------------------------------------------------------
# markdown emit + end-to-end run
# ---------------------------------------------------------------------------

class TestEmitAndRun:
    def test_run_emits_one_md_per_object(self, _fixture_prices, monkeypatch, tmp_path):
        monkeypatch.setattr(VP, "_RUNS_DIR", tmp_path, raising=True)
        results = VP.run("all", "2020-01-01", "2020-12-31", stride=20, write=True)
        assert set(results) == {"rotation_tensor", "crash_risk", "label_vs_planes"}
        for obj, res in results.items():
            md = Path(res["_md"])
            assert md.exists()
            text = md.read_text()
            # the pre-registered gate / cold-start honesty is stated in the file
            assert obj in text
            assert ("Pre-registered gate" in text) or ("UNCOMPUTABLE" in text)

    def test_uncomputable_md_states_the_reason(self, monkeypatch, tmp_path):
        monkeypatch.setattr(VP, "_RUNS_DIR", tmp_path, raising=True)
        results = VP.run("crash_risk", "2020-01-01", None, stride=20, write=True)
        text = Path(results["crash_risk"]["_md"]).read_text()
        assert "cold-start" in text.lower()
        assert "degrade-never-fabricate" in text or "does NOT" in text

    def test_no_write_skips_files(self, _fixture_prices):
        results = VP.run("rotation_tensor", "2020-01-01", "2020-06-30", stride=20, write=False)
        assert "_md" not in results["rotation_tensor"]
