"""tests/test_treasury_context.py — W-TW.1 reader battery.

Tests:
  * Fail-soft: absent file / malformed JSON / stale as_of / wrong schema /
    is_context_only False / missing as_of → empty context, no raise.
  * Staleness boundary: as_of 5 calendar days old → fresh; 6 days → stale.
  * Per-process cache + _reset_context_cache().
  * snapshot shape: fresh → full compact dict; absent → {"stale": True, "asof": None}.
  * Prose sentinel: 'TW_PROSE_SENTINEL_XYZ' (planted in event summary/analysis,
    tga_impulse.summary_en, plumbing.headline_summary_en, and an unknown extra event
    field) MUST NOT appear in seat_prompt_block output — even at max_chars=99999 —
    nor in snapshot(). Unknown extra fields in event rows are tolerated.
  * seat_prompt_block bounded + contains the TGA/NetLiq numbers + mechanism suffix.
  * audit_row statuses: absent / stale / present.
  * Flag helper: enabled() defaults OFF; responds to 1/true/yes.
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import pytest  # noqa: F401 — monkeypatch/tmp_path fixtures

_ROOT = Path(__file__).resolve().parent.parent
_FIXTURE_PATH = _ROOT / "tests" / "fixtures" / "treasury_watch.json"

PROSE_SENTINEL = "TW_PROSE_SENTINEL_XYZ"


def _today() -> str:
    return date.today().isoformat()


def _days_ago(n: int) -> str:
    return (date.today() - timedelta(days=n)).isoformat()


def _load_fixture() -> dict:
    return json.loads(_FIXTURE_PATH.read_text())


def _write_fixture(tmp_path: Path, raw: dict) -> Path:
    p = tmp_path / "treasury_watch.json"
    p.write_text(json.dumps(raw))
    return p


def _fresh_fixture(tmp_path: Path, *, as_of: str | None = None, extra: dict | None = None) -> Path:
    """Write a fresh copy of the v1 fixture to tmp_path with today's as_of (or custom)."""
    raw = _load_fixture()
    raw["as_of"] = as_of if as_of is not None else _today()
    if extra:
        raw.update(extra)
    return _write_fixture(tmp_path, raw)


def _patch_path(monkeypatch, path: Path | None) -> None:
    """Point treasury_context._ARTIFACT_PATH at path (or a non-existent path if None)."""
    import brain.treasury_context as TC
    monkeypatch.setattr(TC, "_ARTIFACT_PATH", path or Path("/nonexistent/treasury_watch.json"))
    TC._reset_context_cache()


# ---------------------------------------------------------------------------
# fail-soft battery
# ---------------------------------------------------------------------------

class TestFailSoft:
    def test_absent_file_returns_empty_no_raise(self, monkeypatch):
        import brain.treasury_context as TC
        _patch_path(monkeypatch, None)
        assert TC.context() == {}
        assert TC.snapshot() == {"stale": True, "asof": None}
        assert TC.seat_prompt_block() == ""
        ar = TC.audit_row()
        assert ar["status"] == "absent"

    def test_malformed_json_returns_empty_no_raise(self, monkeypatch, tmp_path):
        import brain.treasury_context as TC
        bad = tmp_path / "treasury_watch.json"
        bad.write_text("{not valid json")
        _patch_path(monkeypatch, bad)
        assert TC.context() == {}
        assert TC.seat_prompt_block() == ""
        ar = TC.audit_row()
        assert ar["status"] == "absent"

    def test_stale_as_of_at_six_days_returns_empty_no_raise(self, monkeypatch, tmp_path):
        import brain.treasury_context as TC
        stale_file = _fresh_fixture(tmp_path, as_of=_days_ago(6))
        _patch_path(monkeypatch, stale_file)
        assert TC.context() == {}
        assert TC.seat_prompt_block() == ""
        ar = TC.audit_row()
        assert ar["status"] == "stale"
        assert ar["asof"] == _days_ago(6)

    def test_wrong_schema_returns_empty_no_raise(self, monkeypatch, tmp_path):
        import brain.treasury_context as TC
        wrong_schema = _fresh_fixture(tmp_path, extra={"schema": "wrong_schema.v99"})
        _patch_path(monkeypatch, wrong_schema)
        assert TC.context() == {}
        ar = TC.audit_row()
        assert ar["status"] == "absent"

    def test_is_context_only_false_returns_empty(self, monkeypatch, tmp_path):
        import brain.treasury_context as TC
        not_ctx = _fresh_fixture(tmp_path, extra={"is_context_only": False})
        _patch_path(monkeypatch, not_ctx)
        assert TC.context() == {}

    def test_missing_as_of_returns_empty(self, monkeypatch, tmp_path):
        import brain.treasury_context as TC
        raw = _load_fixture()
        raw["as_of"] = ""
        p = _write_fixture(tmp_path, raw)
        _patch_path(monkeypatch, p)
        assert TC.context() == {}


# ---------------------------------------------------------------------------
# staleness boundary — _STALE_DAYS = 5 calendar days
# ---------------------------------------------------------------------------

class TestStalenessBoundary:
    def test_five_days_old_is_fresh(self, monkeypatch, tmp_path):
        import brain.treasury_context as TC
        f = _fresh_fixture(tmp_path, as_of=_days_ago(5))
        _patch_path(monkeypatch, f)
        assert TC.context() != {}
        assert TC.audit_row()["status"] == "present"
        assert TC.snapshot()["stale"] is False

    def test_six_days_old_is_stale(self, monkeypatch, tmp_path):
        import brain.treasury_context as TC
        f = _fresh_fixture(tmp_path, as_of=_days_ago(6))
        _patch_path(monkeypatch, f)
        assert TC.context() == {}
        assert TC.audit_row()["status"] == "stale"
        assert TC.snapshot() == {"stale": True, "asof": None}


# ---------------------------------------------------------------------------
# per-process cache
# ---------------------------------------------------------------------------

class TestCache:
    def test_cache_is_per_process(self, monkeypatch, tmp_path):
        """Two calls with same state return same object without re-reading."""
        import brain.treasury_context as TC
        fresh_file = _fresh_fixture(tmp_path)
        _patch_path(monkeypatch, fresh_file)
        c1 = TC.context()
        c2 = TC.context()
        assert c1 is c2  # same cached object

    def test_reset_cache_forces_reread(self, monkeypatch, tmp_path):
        import brain.treasury_context as TC
        _patch_path(monkeypatch, None)
        assert TC.context() == {}
        # now point to a valid file and reset
        fresh_file = _fresh_fixture(tmp_path)
        monkeypatch.setattr(TC, "_ARTIFACT_PATH", fresh_file)
        TC._reset_context_cache()
        assert TC.context() != {}


# ---------------------------------------------------------------------------
# snapshot shape
# ---------------------------------------------------------------------------

class TestSnapshot:
    def test_fresh_snapshot_shape_and_values(self, monkeypatch, tmp_path):
        import brain.treasury_context as TC
        fresh_file = _fresh_fixture(tmp_path)
        _patch_path(monkeypatch, fresh_file)
        s = TC.snapshot()
        assert s["stale"] is False
        assert s["asof"] == _today()
        assert s["tga_bn"] == 744.637
        assert s["tga_asof"] == "2026-07-09"
        assert s["chg_1d_bn"] == -4.607
        assert s["chg_5d_bn"] == -38.5
        assert s["chg_since_quarter_end_bn"] == -174.508
        assert s["quarter_end_date"] == "2026-06-30"
        assert s["netliq_bn"] == 5990.0
        assert s["netliq_chg_20d_bn"] == 65.0
        assert s["netliq_pctile_expanding"] == 84
        assert s["headline_state"] == "stress_liquidity_expansion"
        assert s["rrp_buffer_state"] == "exhausted"
        imp = s["impulse"]
        assert imp == {
            "direction": "drawdown",
            "magnitude_bn": -174.508,
            "days": 7,
            "quarter_end_adjacent": True,
        }
        top = s["top_event"]
        assert top == {
            "title": "Treasury cash drawdown -174.5bn since quarter end",
            "tone": "tailwind",
            "importance": 78,
        }

    def test_absent_snapshot_is_stale_none(self, monkeypatch):
        import brain.treasury_context as TC
        _patch_path(monkeypatch, None)
        assert TC.snapshot() == {"stale": True, "asof": None}

    def test_impulse_none_when_inactive(self, monkeypatch, tmp_path):
        import brain.treasury_context as TC
        raw = _load_fixture()
        raw["as_of"] = _today()
        raw["tga_impulse"]["active"] = False
        p = _write_fixture(tmp_path, raw)
        _patch_path(monkeypatch, p)
        assert TC.snapshot()["impulse"] is None

    def test_impulse_none_when_null(self, monkeypatch, tmp_path):
        import brain.treasury_context as TC
        f = _fresh_fixture(tmp_path, extra={"tga_impulse": None})
        _patch_path(monkeypatch, f)
        assert TC.snapshot()["impulse"] is None

    def test_top_event_none_when_no_live_event(self, monkeypatch, tmp_path):
        import brain.treasury_context as TC
        raw = _load_fixture()
        raw["as_of"] = _today()
        for ev in raw["events"]:
            ev["live"] = False
        p = _write_fixture(tmp_path, raw)
        _patch_path(monkeypatch, p)
        assert TC.snapshot()["top_event"] is None

    def test_quarter_end_null_tolerated(self, monkeypatch, tmp_path):
        import brain.treasury_context as TC
        raw = _load_fixture()
        raw["as_of"] = _today()
        raw["tga"]["quarter_end"] = None
        p = _write_fixture(tmp_path, raw)
        _patch_path(monkeypatch, p)
        s = TC.snapshot()
        assert s["chg_since_quarter_end_bn"] is None
        assert s["quarter_end_date"] is None
        # prompt still renders, without the Q-end segment
        block = TC.seat_prompt_block()
        assert "TGA $744.6bn" in block
        assert "Q-end" not in block


# ---------------------------------------------------------------------------
# prose sentinel — builder/LLM prose MUST NOT reach a prompt (structural exclusion)
# ---------------------------------------------------------------------------

class TestProseExclusion:
    def test_fixture_actually_contains_sentinel(self):
        """Guard: the fixture must plant the sentinel in every prose-like field."""
        raw = _load_fixture()
        assert PROSE_SENTINEL in json.dumps(raw["events"])
        assert PROSE_SENTINEL in json.dumps(raw["tga_impulse"])
        assert PROSE_SENTINEL in json.dumps(raw["plumbing"])

    def test_sentinel_not_in_seat_prompt_block(self, monkeypatch, tmp_path):
        import brain.treasury_context as TC
        fresh_file = _fresh_fixture(tmp_path)
        _patch_path(monkeypatch, fresh_file)
        block = TC.seat_prompt_block()
        assert block, "prompt block should be non-empty for a fresh fixture"
        assert PROSE_SENTINEL not in block, (
            f"prose '{PROSE_SENTINEL}' must never appear in seat_prompt_block"
        )

    def test_sentinel_not_in_seat_prompt_block_large_max_chars(self, monkeypatch, tmp_path):
        """Even with max_chars=99999 the sentinel must not appear — exclusion is
        structural (fields never read), not a truncation artifact."""
        import brain.treasury_context as TC
        fresh_file = _fresh_fixture(tmp_path)
        _patch_path(monkeypatch, fresh_file)
        block = TC.seat_prompt_block(max_chars=99999)
        assert PROSE_SENTINEL not in block

    def test_sentinel_not_in_snapshot(self, monkeypatch, tmp_path):
        import brain.treasury_context as TC
        fresh_file = _fresh_fixture(tmp_path)
        _patch_path(monkeypatch, fresh_file)
        assert PROSE_SENTINEL not in json.dumps(TC.snapshot())


# ---------------------------------------------------------------------------
# seat_prompt_block content + bounds
# ---------------------------------------------------------------------------

class TestSeatPromptBlock:
    def test_contains_tga_and_netliq_numbers(self, monkeypatch, tmp_path):
        import brain.treasury_context as TC
        fresh_file = _fresh_fixture(tmp_path)
        _patch_path(monkeypatch, fresh_file)
        block = TC.seat_prompt_block()
        assert f"TREASURY asof={_today()} (TGA data)" in block
        assert "TGA $744.6bn" in block
        assert "Δ5d -38.5" in block
        assert "Δ since Q-end(06-30) -174.5" in block
        assert "-> cash into bank reserves (mechanical liquidity injection)" in block
        assert "NetLiq $5990bn" in block
        assert "Δ20d +65.0" in block
        assert "(84th pctile expanding)" in block
        assert "RRP buffer: exhausted" in block
        assert "plumbing: stress_liquidity_expansion" in block

    def test_impulse_and_event_lines(self, monkeypatch, tmp_path):
        import brain.treasury_context as TC
        fresh_file = _fresh_fixture(tmp_path)
        _patch_path(monkeypatch, fresh_file)
        block = TC.seat_prompt_block()
        assert "Impulse: drawdown -174.5bn/7bd since 2026-06-30 (quarter-end adjacent)" in block
        assert "Event: Treasury cash drawdown -174.5bn since quarter end (tone=tailwind imp=78)" in block

    def test_drain_phrasing_when_tga_builds(self, monkeypatch, tmp_path):
        import brain.treasury_context as TC
        raw = _load_fixture()
        raw["as_of"] = _today()
        raw["tga"]["quarter_end"]["chg_since_bn"] = 80.25
        p = _write_fixture(tmp_path, raw)
        _patch_path(monkeypatch, p)
        block = TC.seat_prompt_block()
        assert "-> cash drained from reserves" in block
        assert "cash into bank reserves" not in block

    def test_no_mechanism_suffix_inside_band(self, monkeypatch, tmp_path):
        """|chg since Q-end| < 50bn → no mechanism phrasing at all."""
        import brain.treasury_context as TC
        raw = _load_fixture()
        raw["as_of"] = _today()
        raw["tga"]["quarter_end"]["chg_since_bn"] = -12.0
        p = _write_fixture(tmp_path, raw)
        _patch_path(monkeypatch, p)
        block = TC.seat_prompt_block()
        assert "cash into bank reserves" not in block
        assert "cash drained from reserves" not in block

    def test_bounded_at_default_max_chars(self, monkeypatch, tmp_path):
        import brain.treasury_context as TC
        fresh_file = _fresh_fixture(tmp_path)
        _patch_path(monkeypatch, fresh_file)
        assert len(TC.seat_prompt_block()) <= 700

    def test_bounded_at_small_max_chars(self, monkeypatch, tmp_path):
        import brain.treasury_context as TC
        fresh_file = _fresh_fixture(tmp_path)
        _patch_path(monkeypatch, fresh_file)
        block = TC.seat_prompt_block(max_chars=100)
        assert len(block) <= 100
        assert block.startswith("TREASURY asof=")

    def test_empty_when_absent(self, monkeypatch):
        import brain.treasury_context as TC
        _patch_path(monkeypatch, None)
        assert TC.seat_prompt_block() == ""

    def test_empty_when_stale(self, monkeypatch, tmp_path):
        import brain.treasury_context as TC
        stale_file = _fresh_fixture(tmp_path, as_of=_days_ago(6))
        _patch_path(monkeypatch, stale_file)
        assert TC.seat_prompt_block() == ""


# ---------------------------------------------------------------------------
# audit_row statuses
# ---------------------------------------------------------------------------

class TestAuditRow:
    def test_absent_status(self, monkeypatch):
        import brain.treasury_context as TC
        _patch_path(monkeypatch, None)
        ar = TC.audit_row()
        assert ar == {"status": "absent", "asof": None, "age_days": None,
                      "n_events": 0, "headline_state": None}

    def test_stale_status(self, monkeypatch, tmp_path):
        import brain.treasury_context as TC
        stale = _fresh_fixture(tmp_path, as_of=_days_ago(6))
        _patch_path(monkeypatch, stale)
        ar = TC.audit_row()
        assert ar["status"] == "stale"
        assert ar["age_days"] == 6
        assert ar["n_events"] == 0
        assert ar["headline_state"] is None

    def test_present_status(self, monkeypatch, tmp_path):
        import brain.treasury_context as TC
        fresh = _fresh_fixture(tmp_path)
        _patch_path(monkeypatch, fresh)
        ar = TC.audit_row()
        assert ar["status"] == "present"
        assert ar["asof"] == _today()
        assert ar["age_days"] == 0
        assert ar["n_events"] == 1
        assert ar["headline_state"] == "stress_liquidity_expansion"


# ---------------------------------------------------------------------------
# flag helper — default OFF
# ---------------------------------------------------------------------------

class TestFlagHelper:
    def test_default_off(self, monkeypatch):
        import brain.treasury_context as TC
        monkeypatch.delenv("MASTERMIND_TREASURY_CONTEXT", raising=False)
        assert TC.enabled() is False

    def test_on_when_set_to_1(self, monkeypatch):
        import brain.treasury_context as TC
        monkeypatch.setenv("MASTERMIND_TREASURY_CONTEXT", "1")
        assert TC.enabled() is True

    def test_on_when_set_to_true(self, monkeypatch):
        import brain.treasury_context as TC
        monkeypatch.setenv("MASTERMIND_TREASURY_CONTEXT", "true")
        assert TC.enabled() is True

    def test_on_when_set_to_yes(self, monkeypatch):
        import brain.treasury_context as TC
        monkeypatch.setenv("MASTERMIND_TREASURY_CONTEXT", "yes")
        assert TC.enabled() is True

    def test_off_when_set_to_0(self, monkeypatch):
        import brain.treasury_context as TC
        monkeypatch.setenv("MASTERMIND_TREASURY_CONTEXT", "0")
        assert TC.enabled() is False
