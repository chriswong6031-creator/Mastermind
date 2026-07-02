"""tests/test_regime_frame.py — golden-output + contract tests for brain/regime_frame.

WHY THESE TESTS MATTER
-----------------------
lens_row() replaces five byte-identical _regime_dict() copies that fed LLM prompts.
If the output drifts (different keys, different key order, missing None on absent
field) the prompts silently change.  This file is the load-bearing guard.

GOLDEN TEST STRATEGY
--------------------
The golden reference IS the old _regime_dict() body, copied verbatim inline — the
same three-liner that lived in autonomous.py:449, etf.py:650, heavyweight.py:525,
hk.py:549, china.py:538.  Asserting lens_row() == _golden_regime_dict(raw) proves
byte-identity without importing any of the old modules (which carry heavy deps).

Three regime states are tested:
  * FULL  — a complete regime file with all known optional fields
  * MINIMAL — only the three required lens fields
  * MISSING_KEYS — some keys absent, some None (exercises the None-default path)

Additionally:
  * frame() returns all nine keys with None for any absent field
  * freshness() returns correct calendar-day delta and None on missing/bad dates
  * Unknown region degrades to all-None frame (never raises)
"""
from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

import bot  # noqa: F401  -> vendor/macro onto sys.path
from brain import regime_frame as RF


# ---------------------------------------------------------------------------
# Golden reference — the EXACT three-liner from the five _regime_dict() copies.
# This is intentionally NOT a call to any bot module; it is the ground truth.
# ---------------------------------------------------------------------------

def _golden_regime_dict(raw: dict) -> dict:
    """The verbatim _regime_dict() body from autonomous.py:449 / etf.py:650 /
    heavyweight.py:525 / hk.py:549 / china.py:538.

    ``raw`` is what _read_regime() / _read_china_regime() returned.
    The comparison with lens_row() proves byte-identity.
    """
    return {
        "quad": raw.get("quad"),
        "quad_name": raw.get("quad_name"),
        "liquidity_overlay": raw.get("liquidity_overlay"),
    }


# ---------------------------------------------------------------------------
# Fixture helpers — write regime JSON files into tmp_path, then monkeypatch
# _REGION_PATHS so lens_row() / frame() read from the tmp dir.
# ---------------------------------------------------------------------------

_FULL_REGIME: dict = {
    "date": "2026-07-01",
    "quad": "Q1",
    "quad_name": "Goldilocks",
    "label": "Q1",
    "growth_score": 0.333,
    "inflation_score": -0.52,
    "growth_confidence": 0.238,
    "inflation_confidence": 0.416,
    "confidence": 0.327,
    "liquidity_overlay": "expanding",
    "cycle_tag": "mid",
    "transition_state": "STABLE",
    "transition_flags": {
        "flag_breadth_price": False,
        "flag_credit_equity": False,
        "flag_ratio_inflection": False,
        "flag_inflation_basket": False,
        "flag_confidence_decay": False,
        "flag_gex": False,
    },
    "confirming": ["growth_copper_gold", "growth_iwm_spy"],
    "contradicting": ["growth_cyclical_defensive", "growth_wei_trend"],
    "flip_condition": {
        "axis": "growth",
        "component": "copper_gold",
        "z": 0.5,
        "threshold": 0.45,
        "margin": 0.05,
        "note": "closest supporter to its cutoff",
    },
}

_MINIMAL_REGIME: dict = {
    "quad": "Q2",
    "quad_name": "Inflation",
    "liquidity_overlay": "neutral",
}

_MISSING_KEYS_REGIME: dict = {
    "quad": "Q4",
    # quad_name deliberately absent
    # liquidity_overlay deliberately absent
    "confidence": 0.55,
    "transition_state": "WEAKENING",
}


def _make_regime_file(tmp_path: Path, payload: dict, filename: str = "latest.json") -> Path:
    p = tmp_path / filename
    p.write_text(json.dumps(payload))
    return p


@pytest.fixture()
def patch_region(monkeypatch, tmp_path):
    """Return a helper that writes a payload and monkeypatches _REGION_PATHS['us']."""
    def _apply(payload: dict) -> Path:
        p = _make_regime_file(tmp_path, payload)
        monkeypatch.setitem(RF._REGION_PATHS, "us", p)
        return p
    return _apply


# ---------------------------------------------------------------------------
# GOLDEN TEST — the load-bearing assertion
# ---------------------------------------------------------------------------

class TestLensRowGolden:
    """lens_row() must be byte-identical to the old _regime_dict() for every regime shape."""

    def test_full_regime(self, patch_region):
        patch_region(_FULL_REGIME)
        result = RF.lens_row("us")
        golden = _golden_regime_dict(_FULL_REGIME)
        assert result == golden, (
            f"lens_row() diverged from golden _regime_dict().\n"
            f"  got:    {result}\n"
            f"  expect: {golden}"
        )

    def test_minimal_regime(self, patch_region):
        patch_region(_MINIMAL_REGIME)
        result = RF.lens_row("us")
        golden = _golden_regime_dict(_MINIMAL_REGIME)
        assert result == golden

    def test_missing_keys_regime(self, patch_region):
        """Absent quad_name and liquidity_overlay must become None — same as raw.get()."""
        patch_region(_MISSING_KEYS_REGIME)
        result = RF.lens_row("us")
        golden = _golden_regime_dict(_MISSING_KEYS_REGIME)
        assert result == golden
        # Belt-and-suspenders: explicitly verify None defaults
        assert result["quad_name"] is None
        assert result["liquidity_overlay"] is None

    def test_key_order_is_preserved(self, patch_region):
        """Key insertion order must match exactly — LLM prompts embed the dict repr."""
        patch_region(_FULL_REGIME)
        result = RF.lens_row("us")
        assert list(result.keys()) == ["quad", "quad_name", "liquidity_overlay"]

    def test_empty_file_all_none(self, monkeypatch, tmp_path):
        """A completely empty JSON object degrades to all-None — invariant: never raises."""
        p = _make_regime_file(tmp_path, {})
        monkeypatch.setitem(RF._REGION_PATHS, "us", p)
        result = RF.lens_row("us")
        assert result == {"quad": None, "quad_name": None, "liquidity_overlay": None}

    def test_missing_file_all_none(self, monkeypatch, tmp_path):
        """If the file is absent, lens_row must not raise — degrade to all-None."""
        absent = tmp_path / "nonexistent.json"
        monkeypatch.setitem(RF._REGION_PATHS, "us", absent)
        result = RF.lens_row("us")
        assert result == {"quad": None, "quad_name": None, "liquidity_overlay": None}

    def test_corrupt_file_all_none(self, monkeypatch, tmp_path):
        """Corrupt JSON must not raise — degrade to all-None."""
        p = tmp_path / "latest.json"
        p.write_text("{not valid json!!!")
        monkeypatch.setitem(RF._REGION_PATHS, "us", p)
        result = RF.lens_row("us")
        assert result == {"quad": None, "quad_name": None, "liquidity_overlay": None}


# ---------------------------------------------------------------------------
# frame() contract tests
# ---------------------------------------------------------------------------

class TestFrame:
    """frame() exposes the enriched dict; all nine keys must be present."""

    _EXPECTED_KEYS = {
        "quad", "quad_name", "liquidity_overlay",
        "confidence", "transition_state", "contradicting",
        "flip_condition", "flip_margin", "flag_confidence_decay", "date",
    }

    def test_full_regime_all_keys_present(self, patch_region):
        patch_region(_FULL_REGIME)
        result = RF.frame("us")
        assert self._EXPECTED_KEYS.issubset(result.keys()), (
            f"Missing keys: {self._EXPECTED_KEYS - set(result.keys())}"
        )

    def test_full_regime_values(self, patch_region):
        patch_region(_FULL_REGIME)
        result = RF.frame("us")
        assert result["quad"] == "Q1"
        assert result["quad_name"] == "Goldilocks"
        assert result["liquidity_overlay"] == "expanding"
        assert result["confidence"] == pytest.approx(0.327)
        assert result["transition_state"] == "STABLE"
        assert isinstance(result["contradicting"], list)
        assert "growth_cyclical_defensive" in result["contradicting"]

    def test_flip_margin_extracted_from_nested_flip_condition(self, patch_region):
        """flip_margin is synthesised from flip_condition.margin — not a raw top-level key."""
        patch_region(_FULL_REGIME)
        result = RF.frame("us")
        # _FULL_REGIME has flip_condition.margin = 0.05 and no top-level flip_margin key
        assert result["flip_margin"] == pytest.approx(0.05)

    def test_flip_margin_top_level_takes_priority(self, monkeypatch, tmp_path):
        payload = {**_FULL_REGIME, "flip_margin": 0.12}
        p = _make_regime_file(tmp_path, payload)
        monkeypatch.setitem(RF._REGION_PATHS, "us", p)
        result = RF.frame("us")
        assert result["flip_margin"] == pytest.approx(0.12)

    def test_flag_confidence_decay_from_transition_flags(self, patch_region):
        patch_region(_FULL_REGIME)
        result = RF.frame("us")
        # _FULL_REGIME has transition_flags.flag_confidence_decay = False
        assert result["flag_confidence_decay"] is False

    def test_flag_confidence_decay_top_level(self, monkeypatch, tmp_path):
        payload = {"quad": "Q3", "flag_confidence_decay": True}
        p = _make_regime_file(tmp_path, payload)
        monkeypatch.setitem(RF._REGION_PATHS, "us", p)
        result = RF.frame("us")
        assert result["flag_confidence_decay"] is True

    def test_missing_keys_yield_none_not_keyerror(self, patch_region):
        patch_region(_MISSING_KEYS_REGIME)
        result = RF.frame("us")
        # Fields not in _MISSING_KEYS_REGIME must be None — never KeyError
        assert result["quad_name"] is None
        assert result["liquidity_overlay"] is None
        assert result["flip_condition"] is None
        assert result["flip_margin"] is None
        assert result["flag_confidence_decay"] is None
        assert result["date"] is None

    def test_unknown_region_degrades_to_all_none(self):
        """Unknown region must degrade cleanly — never raise, never un-cap."""
        result = RF.frame("unknown_region_xyz")
        for key in self._EXPECTED_KEYS:
            assert result.get(key) is None, f"Expected None for {key!r}"

    def test_frame_does_not_raise_on_missing_file(self, monkeypatch, tmp_path):
        absent = tmp_path / "nope.json"
        monkeypatch.setitem(RF._REGION_PATHS, "us", absent)
        result = RF.frame("us")  # must not raise
        assert result["quad"] is None

    def test_frame_and_lens_row_agree_on_three_fields(self, patch_region):
        """frame() and lens_row() must return the same values for the three shared fields."""
        patch_region(_FULL_REGIME)
        f = RF.frame("us")
        lr = RF.lens_row("us")
        assert f["quad"] == lr["quad"]
        assert f["quad_name"] == lr["quad_name"]
        assert f["liquidity_overlay"] == lr["liquidity_overlay"]


# ---------------------------------------------------------------------------
# region routing tests
# ---------------------------------------------------------------------------

class TestRegionRouting:
    """China and HK route to china_regime/latest.json; US routes to regime/latest.json."""

    def _patch_region(self, monkeypatch, tmp_path, region: str, payload: dict) -> None:
        p = _make_regime_file(tmp_path, payload)
        monkeypatch.setitem(RF._REGION_PATHS, region, p)

    def test_us_and_china_read_different_files(self, monkeypatch, tmp_path):
        us_payload = {**_MINIMAL_REGIME, "quad": "Q1"}
        cn_payload = {**_MINIMAL_REGIME, "quad": "Q3", "quad_name": "Stagflation",
                      "liquidity_overlay": "neutral"}
        us_path = tmp_path / "us.json"
        cn_path = tmp_path / "cn.json"
        us_path.write_text(json.dumps(us_payload))
        cn_path.write_text(json.dumps(cn_payload))
        monkeypatch.setitem(RF._REGION_PATHS, "us", us_path)
        monkeypatch.setitem(RF._REGION_PATHS, "china", cn_path)
        assert RF.lens_row("us")["quad"] == "Q1"
        assert RF.lens_row("china")["quad"] == "Q3"

    def test_hk_uses_china_file(self, monkeypatch, tmp_path):
        """HK and China must point to the same underlying path (china_regime/latest.json)."""
        cn_path = tmp_path / "china.json"
        cn_path.write_text(json.dumps({"quad": "Q3", "quad_name": "Stagflation",
                                        "liquidity_overlay": "neutral"}))
        monkeypatch.setitem(RF._REGION_PATHS, "china", cn_path)
        monkeypatch.setitem(RF._REGION_PATHS, "hk", cn_path)
        assert RF.lens_row("hk")["quad"] == "Q3"
        assert RF.lens_row("hk") == RF.lens_row("china")


# ---------------------------------------------------------------------------
# freshness() tests
# ---------------------------------------------------------------------------

class TestFreshness:
    """freshness() returns calendar days since the regime file's date field."""

    def test_dated_file(self, monkeypatch, tmp_path):
        # Use the real UTC today so the test does not encode a hardcoded date.
        today = datetime.now(tz=timezone.utc).date()
        payload = {"date": today.isoformat(), "quad": "Q1"}
        p = _make_regime_file(tmp_path, payload)
        monkeypatch.setitem(RF._REGION_PATHS, "us", p)
        delta = RF.freshness("us")
        assert delta == 0

    def test_one_day_old(self, monkeypatch, tmp_path):
        from datetime import timedelta
        today = datetime.now(tz=timezone.utc).date()
        yesterday = (today - timedelta(days=1)).isoformat()
        payload = {"date": yesterday, "quad": "Q1"}
        p = _make_regime_file(tmp_path, payload)
        monkeypatch.setitem(RF._REGION_PATHS, "us", p)
        assert RF.freshness("us") == 1

    def test_missing_date_field_returns_none(self, monkeypatch, tmp_path):
        payload = {"quad": "Q1"}
        p = _make_regime_file(tmp_path, payload)
        monkeypatch.setitem(RF._REGION_PATHS, "us", p)
        assert RF.freshness("us") is None

    def test_bad_date_string_returns_none(self, monkeypatch, tmp_path):
        payload = {"date": "not-a-date", "quad": "Q1"}
        p = _make_regime_file(tmp_path, payload)
        monkeypatch.setitem(RF._REGION_PATHS, "us", p)
        assert RF.freshness("us") is None

    def test_missing_file_returns_none(self, monkeypatch, tmp_path):
        monkeypatch.setitem(RF._REGION_PATHS, "us", tmp_path / "nope.json")
        assert RF.freshness("us") is None

    def test_unknown_region_returns_none(self):
        assert RF.freshness("unknown_xyz") is None
