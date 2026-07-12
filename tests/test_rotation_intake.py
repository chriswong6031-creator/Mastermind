"""tests/test_rotation_intake.py — the sole-reader contract + fail-soft behaviour of rotation_intake.

Every case points `_ARTIFACT_PATH` (and the fallback) at a tmp file and calls `_reset_cache()` around
it, so no vendor data is touched and the process cache never leaks between cases. The golden fixture's
`as_of` is a far-future placeholder that `_write_artifact` REWRITES relative to today (fresh) or into
the past (stale) — so freshness is exercised without the fixture rotting.
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import pytest

from brain import rotation_intake as ri

FIXTURE = Path(__file__).parent / "fixtures" / "rotation_calls_v1.json"


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def _load_fixture() -> dict:
    return json.loads(FIXTURE.read_text())


def _point_at(monkeypatch, tmp_path: Path, artifact: dict | None) -> Path:
    """Write `artifact` (or nothing) to a tmp file and point BOTH artifact paths at it.

    Returns the primary path. When `artifact` is None the file is NOT created — the ABSENT case.
    Always resets the process cache so the read is fresh.
    """
    primary = tmp_path / "rotation_calls.json"
    # fallback points at a definitely-absent path so only the primary matters in tests
    fallback = tmp_path / "does_not_exist" / "rotation_calls.json"
    if artifact is not None:
        primary.write_text(json.dumps(artifact))
    monkeypatch.setattr(ri, "_ARTIFACT_PATH", primary)
    monkeypatch.setattr(ri, "_ARTIFACT_PATH_FALLBACK", fallback)
    # never let a real tensor leak into fallback synthesis during these tests
    monkeypatch.setattr(ri, "_TENSOR_PATH", tmp_path / "no_tensor.json")
    ri._reset_cache()
    return primary


def _fresh_fixture(days_old: int = 0) -> dict:
    """The golden fixture with as_of rewritten to `days_old` calendar days before today."""
    art = _load_fixture()
    art["as_of"] = (date.today() - timedelta(days=days_old)).isoformat()
    return art


@pytest.fixture(autouse=True)
def _reset_around_each():
    ri._reset_cache()
    yield
    ri._reset_cache()


# --------------------------------------------------------------------------- #
# calls() — valid / absent / stale / malformed
# --------------------------------------------------------------------------- #

def test_valid_fixture_parses(monkeypatch, tmp_path):
    _point_at(monkeypatch, tmp_path, _fresh_fixture(days_old=0))
    out = ri.calls()
    assert len(out) == 3
    ids = {c["call_id"] for c in out}
    assert ids == {"rc-2099-financials-001", "rc-2099-semis-002", "rc-2099-aapl-003"}
    # target_kinds + states covered
    kinds = {c["target_kind"] for c in out}
    states = {c["state"] for c in out}
    assert "sector" in kinds and "ticker" in kinds
    assert {"EARLY", "TURNING", "CONFIRMED"} <= states


def test_absent_file_returns_empty(monkeypatch, tmp_path):
    _point_at(monkeypatch, tmp_path, None)  # no file written
    assert ri.calls() == []


def test_stale_asof_returns_empty_absence_handshake(monkeypatch, tmp_path):
    # 5 calendar days old > 2-session budget → treated as absent ("no calls today", NOT all-clear)
    _point_at(monkeypatch, tmp_path, _fresh_fixture(days_old=5))
    assert ri.calls() == []


def test_freshness_boundary(monkeypatch, tmp_path):
    # exactly 2 days old is still fresh; 3 is stale
    _point_at(monkeypatch, tmp_path, _fresh_fixture(days_old=2))
    assert len(ri.calls()) == 3
    _point_at(monkeypatch, tmp_path, _fresh_fixture(days_old=3))
    assert ri.calls() == []


def test_malformed_schema_returns_empty(monkeypatch, tmp_path):
    art = _fresh_fixture()
    art["schema"] = "rotation_calls.v0_WRONG"
    _point_at(monkeypatch, tmp_path, art)
    assert ri.calls() == []


def test_not_a_dict_returns_empty(monkeypatch, tmp_path):
    primary = tmp_path / "rotation_calls.json"
    primary.write_text(json.dumps(["not", "an", "envelope"]))
    monkeypatch.setattr(ri, "_ARTIFACT_PATH", primary)
    monkeypatch.setattr(ri, "_ARTIFACT_PATH_FALLBACK", tmp_path / "nope.json")
    ri._reset_cache()
    assert ri.calls() == []


def test_unparseable_json_returns_empty(monkeypatch, tmp_path):
    primary = tmp_path / "rotation_calls.json"
    primary.write_text("{ this is not valid json ")
    monkeypatch.setattr(ri, "_ARTIFACT_PATH", primary)
    monkeypatch.setattr(ri, "_ARTIFACT_PATH_FALLBACK", tmp_path / "nope.json")
    ri._reset_cache()
    assert ri.calls() == []


def test_one_malformed_call_skipped_not_fatal(monkeypatch, tmp_path):
    art = _fresh_fixture()
    # inject a malformed call (missing call_id) + a call with an invalid state
    art["calls"].append({"target_kind": "sector", "target": "junk", "state": "EARLY"})  # no call_id
    art["calls"].append({"call_id": "rc-bad-state", "target_kind": "sector",
                         "target": "junk2", "state": "NOT_A_STATE"})
    art["calls"].append({"call_id": "rc-bad-kind", "target_kind": "planet",
                         "target": "junk3", "state": "EARLY"})
    _point_at(monkeypatch, tmp_path, art)
    out = ri.calls()
    # the 3 good calls survive; the 3 bad ones are skipped
    assert len(out) == 3
    assert all(c["call_id"].startswith("rc-2099-") for c in out)


def test_calls_missing_calls_list(monkeypatch, tmp_path):
    art = _fresh_fixture()
    art["calls"] = "not a list"
    _point_at(monkeypatch, tmp_path, art)
    assert ri.calls() == []


def test_fallback_artifact_path_used_when_primary_absent(monkeypatch, tmp_path):
    # primary absent, fallback present + fresh → the fallback path is read
    primary = tmp_path / "primary.json"          # not written
    fb = tmp_path / "rotationdata"
    fb.mkdir()
    fb_file = fb / "rotation_calls.json"
    fb_file.write_text(json.dumps(_fresh_fixture()))
    monkeypatch.setattr(ri, "_ARTIFACT_PATH", primary)
    monkeypatch.setattr(ri, "_ARTIFACT_PATH_FALLBACK", fb_file)
    monkeypatch.setattr(ri, "_TENSOR_PATH", tmp_path / "no_tensor.json")
    ri._reset_cache()
    assert len(ri.calls()) == 3


def test_cache_reset_forces_reread(monkeypatch, tmp_path):
    _point_at(monkeypatch, tmp_path, _fresh_fixture())
    assert len(ri.calls()) == 3
    # overwrite with an empty (but still valid) artifact; cache should still show 3 until reset
    (tmp_path / "rotation_calls.json").write_text(json.dumps({**_fresh_fixture(), "calls": []}))
    assert len(ri.calls()) == 3  # cached
    ri._reset_cache()
    assert ri.calls() == []       # re-read


# --------------------------------------------------------------------------- #
# synthesize_fallback()
# --------------------------------------------------------------------------- #

def test_synthesize_fallback_caps_confidence_and_tags_provenance(monkeypatch, tmp_path):
    # drive cycles() deterministically: two entry-favored sectors, one not.
    fake_cycles = {
        "XLF": {"phase": "Recovery", "osc_slope": 0.4, "pos": 40.0, "entry_favored": True},
        "XLV": {"phase": "Expansion", "osc_slope": None, "pos": 55.0, "entry_favored": True},
        "XLE": {"phase": "Peak", "osc_slope": -0.3, "pos": 80.0, "entry_favored": False},
        "XLK": {"phase": "Recovery", "osc_slope": -0.1, "pos": 30.0, "entry_favored": True},  # osc<=0 → dropped
    }
    import brain.regime_frame as rf
    monkeypatch.setattr(rf, "cycles", lambda: fake_cycles)
    # no tensor
    monkeypatch.setattr(ri, "_TENSOR_PATH", tmp_path / "no_tensor.json")

    out = ri.synthesize_fallback(asof="2026-07-11")
    targets = {c["target"] for c in out}
    # XLF (osc>0) and XLV (osc absent, allowed) IN; XLE (not entry_favored) and XLK (osc<=0) OUT
    assert targets == {"XLF", "XLV"}
    for c in out:
        assert c["provenance"] == "fallback_synth"
        assert c["confidence"] <= 0.5
        assert c["call_id"].startswith("synth:")
        assert c["state"] == "EARLY"
        assert c["as_of"] == "2026-07-11"


def test_synthesize_fallback_uses_tensor_top_pairs(monkeypatch, tmp_path):
    import brain.regime_frame as rf
    monkeypatch.setattr(rf, "cycles", lambda: {})  # no cycle source
    tensor_file = tmp_path / "rotation_tensor.json"
    tensor_file.write_text(json.dumps({
        "as_of": date.today().isoformat(),
        "rs_velocity": {"top_pairs": [
            {"lead": "XLV", "lag": "SMH", "R_bps_day": 117.0, "dR_bps_day": 5.0, "accelerating": True},
            {"lead": "XLU", "lag": "XLK", "R_bps_day": 40.0, "dR_bps_day": 2.0, "accelerating": True},
        ]},
    }))
    monkeypatch.setattr(ri, "_TENSOR_PATH", tensor_file)

    out = ri.synthesize_fallback()
    targets = {c["target"] for c in out}
    assert targets == {"XLV", "XLU"}
    for c in out:
        assert c["provenance"] == "fallback_synth"
        assert c["confidence"] <= 0.5


def test_synthesize_fallback_stale_tensor_ignored(monkeypatch, tmp_path):
    import brain.regime_frame as rf
    monkeypatch.setattr(rf, "cycles", lambda: {})
    tensor_file = tmp_path / "rotation_tensor.json"
    tensor_file.write_text(json.dumps({
        "as_of": (date.today() - timedelta(days=10)).isoformat(),  # stale
        "rs_velocity": {"top_pairs": [{"lead": "XLV", "lag": "SMH", "R_bps_day": 1.0,
                                       "dR_bps_day": 1.0, "accelerating": True}]},
    }))
    monkeypatch.setattr(ri, "_TENSOR_PATH", tensor_file)
    assert ri.synthesize_fallback() == []


def test_synthesize_fallback_failsoft_on_broken_cycles(monkeypatch, tmp_path):
    import brain.regime_frame as rf
    def _boom():
        raise RuntimeError("cycles blew up")
    monkeypatch.setattr(rf, "cycles", _boom)
    monkeypatch.setattr(ri, "_TENSOR_PATH", tmp_path / "no_tensor.json")
    assert ri.synthesize_fallback() == []  # never raises


# --------------------------------------------------------------------------- #
# active_calls() — real preferred, else fallback
# --------------------------------------------------------------------------- #

def test_active_calls_prefers_real(monkeypatch, tmp_path):
    _point_at(monkeypatch, tmp_path, _fresh_fixture())
    out = ri.active_calls()
    assert len(out) == 3
    assert all(c["intake_path"] == "real" for c in out)


def test_active_calls_falls_back_when_empty(monkeypatch, tmp_path):
    _point_at(monkeypatch, tmp_path, None)  # no real artifact → calls() == []
    import brain.regime_frame as rf
    monkeypatch.setattr(rf, "cycles", lambda: {
        "XLF": {"phase": "Recovery", "osc_slope": 0.4, "pos": 40.0, "entry_favored": True},
    })
    out = ri.active_calls(asof="2026-07-11")
    assert len(out) == 1
    assert out[0]["intake_path"] == "fallback_synth"
    assert out[0]["provenance"] == "fallback_synth"
    assert out[0]["target"] == "XLF"


# --------------------------------------------------------------------------- #
# expand()
# --------------------------------------------------------------------------- #

def test_expand_ticker_call_returns_that_ticker(monkeypatch, tmp_path):
    call = {"call_id": "x", "target_kind": "ticker", "target": "AAPL", "state": "CONFIRMED"}
    out = ri.expand(call)
    assert out == [{"ticker": "AAPL", "score": None}]


def test_expand_explicit_members_ranked_against_basket(monkeypatch, tmp_path):
    # explicit members on the call; baskets provide ret_20d + rel_20d so ranking is reachable.
    fake_baskets = {"baskets": [{
        "id": "semiconductors",
        "perf": {"20d": {"rel": 0.05}},
        "members": [
            {"symbol": "NVDA", "ret_20d": 0.20},
            {"symbol": "AVGO", "ret_20d": 0.10},
            {"symbol": "AMD", "ret_20d": 0.08},
        ],
    }]}
    monkeypatch.setattr(ri, "_load_baskets", lambda: fake_baskets)
    call = {"call_id": "rc-semis", "target_kind": "sector", "target": "semiconductors",
            "members": ["NVDA", "AVGO", "AMD"], "state": "TURNING"}
    out = ri.expand(call)
    tickers = [r["ticker"] for r in out]
    assert tickers == ["NVDA", "AVGO", "AMD"]  # ranked by (ret_20d - rel_20d) descending
    # NVDA score = 0.20 - 0.05 = 0.15
    assert abs(out[0]["score"] - 0.15) < 1e-9


def test_expand_sector_call_matches_basket_by_id(monkeypatch, tmp_path):
    fake_baskets = {"baskets": [{
        "id": "financials",
        "perf": {"20d": {"rel": 0.02}},
        "members": [
            {"symbol": "JPM", "ret_20d": 0.09},
            {"ticker": "GS", "ret_20d": 0.15},   # note: 'ticker' key variant
        ],
    }]}
    monkeypatch.setattr(ri, "_load_baskets", lambda: fake_baskets)
    call = {"call_id": "rc-fin", "target_kind": "sector", "target": "financials",
            "members": None, "state": "EARLY"}
    out = ri.expand(call)
    tickers = [r["ticker"] for r in out]
    assert tickers == ["GS", "JPM"]  # GS (0.15-0.02=0.13) > JPM (0.09-0.02=0.07)


def test_expand_unranked_when_baskets_absent(monkeypatch, tmp_path):
    monkeypatch.setattr(ri, "_load_baskets", lambda: None)  # no basket data
    call = {"call_id": "rc-x", "target_kind": "theme", "target": "whatever",
            "members": ["ABC", "DEF"], "state": "EARLY"}
    out = ri.expand(call)
    assert out == [{"ticker": "ABC", "score": None}, {"ticker": "DEF", "score": None}]


def test_expand_no_matching_basket_returns_empty(monkeypatch, tmp_path):
    monkeypatch.setattr(ri, "_load_baskets", lambda: {"baskets": []})
    call = {"call_id": "rc-x", "target_kind": "sector", "target": "unknown_sector",
            "members": None, "state": "EARLY"}
    assert ri.expand(call) == []


def test_expand_failsoft_on_garbage(monkeypatch, tmp_path):
    assert ri.expand(None) == []
    assert ri.expand("not a dict") == []


# --------------------------------------------------------------------------- #
# active_call_for()
# --------------------------------------------------------------------------- #

def test_active_call_for_ticker_target(monkeypatch, tmp_path):
    _point_at(monkeypatch, tmp_path, _fresh_fixture())
    monkeypatch.setattr(ri, "_load_baskets", lambda: None)  # keep expand cheap/deterministic
    got = ri.active_call_for("AAPL")
    assert got is not None
    assert got["call_id"] == "rc-2099-aapl-003"


def test_active_call_for_explicit_member(monkeypatch, tmp_path):
    _point_at(monkeypatch, tmp_path, _fresh_fixture())
    monkeypatch.setattr(ri, "_load_baskets", lambda: None)
    got = ri.active_call_for("NVDA")  # in the semis call's explicit members
    assert got is not None
    assert got["call_id"] == "rc-2099-semis-002"


def test_active_call_for_none_when_absent(monkeypatch, tmp_path):
    _point_at(monkeypatch, tmp_path, _fresh_fixture())
    monkeypatch.setattr(ri, "_load_baskets", lambda: None)
    assert ri.active_call_for("ZZZZ_NOT_A_TICKER") is None


def test_active_call_for_skips_terminal_states(monkeypatch, tmp_path):
    art = _fresh_fixture()
    # flip the AAPL ticker call to FAILED — it must NOT be returned as active
    for c in art["calls"]:
        if c["call_id"] == "rc-2099-aapl-003":
            c["state"] = "FAILED"
    _point_at(monkeypatch, tmp_path, art)
    monkeypatch.setattr(ri, "_load_baskets", lambda: None)
    assert ri.active_call_for("AAPL") is None


# --------------------------------------------------------------------------- #
# audit_row() — status transitions
# --------------------------------------------------------------------------- #

def test_audit_row_present(monkeypatch, tmp_path):
    _point_at(monkeypatch, tmp_path, _fresh_fixture())
    row = ri.audit_row()
    assert row["status"] == "present"
    assert row["n_calls"] == 3
    assert row["age_days"] == 0
    assert row["provenance_mix"] == {"real": 3}


def test_audit_row_stale(monkeypatch, tmp_path):
    _point_at(monkeypatch, tmp_path, _fresh_fixture(days_old=9))
    # kill the fallback so audit reports 'stale', not 'fallback'
    import brain.regime_frame as rf
    monkeypatch.setattr(rf, "cycles", lambda: {})
    row = ri.audit_row()
    assert row["status"] == "stale"
    assert row["n_calls"] == 0
    assert row["age_days"] == 9


def test_audit_row_absent(monkeypatch, tmp_path):
    _point_at(monkeypatch, tmp_path, None)
    import brain.regime_frame as rf
    monkeypatch.setattr(rf, "cycles", lambda: {})  # no fallback either
    row = ri.audit_row()
    assert row["status"] == "absent"
    assert row["n_calls"] == 0
    assert row["provenance_mix"] == {}


def test_audit_row_fallback(monkeypatch, tmp_path):
    _point_at(monkeypatch, tmp_path, None)  # no real artifact
    import brain.regime_frame as rf
    monkeypatch.setattr(rf, "cycles", lambda: {
        "XLF": {"phase": "Recovery", "osc_slope": 0.4, "pos": 40.0, "entry_favored": True},
        "XLV": {"phase": "Trough", "osc_slope": 0.2, "pos": 20.0, "entry_favored": True},
    })
    row = ri.audit_row()
    assert row["status"] == "fallback"
    assert row["n_calls"] == 2
    assert row["provenance_mix"] == {"fallback_synth": 2}
