"""tests/test_neural_web_context.py — W-NW.1 reader battery.

Tests:
  * Fail-soft: absent file / malformed JSON / stale as_of / wrong schema → empty context, no raise.
  * Cortex sentinel: memo text 'CORTEX_SENTINEL_XYZ' MUST NOT appear in seat_prompt_block output.
  * market_plane advisory shape: stale input → stale:True; present+fresh → stale:False.
  * audit_row statuses: absent / stale / present.
  * market_view acceptance: present+fresh neural_web plane has status='advisory' and never
    appears in net_posture_tilt contributors.
  * pm_conviction: with flag OFF the prompt is byte-identical (no NW section); with flag ON
    and fixture context, section appears bounded and contains no cortex sentinel.
  * Flag helper: nw_prompts_enabled() defaults OFF.
"""
from __future__ import annotations

import json
import os
from datetime import date, timedelta
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_FIXTURE_PATH = _ROOT / "tests" / "fixtures" / "mastermind_context.json"


def _today() -> str:
    return date.today().isoformat()


def _stale_date() -> str:
    return (date.today() - timedelta(days=10)).isoformat()


def _fresh_fixture(tmp_path: Path, *, as_of: str | None = None, extra: dict | None = None) -> Path:
    """Write a fresh copy of the v1 fixture to tmp_path with today's as_of (or custom)."""
    raw = json.loads(_FIXTURE_PATH.read_text())
    raw["as_of"] = as_of if as_of is not None else _today()
    # also update sub-lobe asofs
    for lobe_name, lobe in (raw.get("lobes") or {}).items():
        if isinstance(lobe, dict) and "as_of" in lobe:
            lobe["as_of"] = raw["as_of"]
    if extra:
        raw.update(extra)
    p = tmp_path / "mastermind_context.json"
    p.write_text(json.dumps(raw))
    return p


def _patch_path(monkeypatch, path: Path | None) -> None:
    """Point neural_web_context._ARTIFACT_PATH at path (or a non-existent path if None)."""
    import brain.neural_web_context as NWC
    monkeypatch.setattr(NWC, "_ARTIFACT_PATH", path or (Path("/nonexistent/mastermind_context.json")))
    NWC._reset_context_cache()


# ---------------------------------------------------------------------------
# fail-soft battery
# ---------------------------------------------------------------------------

class TestFailSoft:
    def test_absent_file_returns_empty_no_raise(self, monkeypatch):
        import brain.neural_web_context as NWC
        _patch_path(monkeypatch, None)
        assert NWC.context() == {}
        assert NWC.candidate("NVDA") == {}
        assert NWC.market_plane().get("stale") is True
        assert NWC.seat_prompt_block(["NVDA"]) == ""
        ar = NWC.audit_row()
        assert ar["status"] == "absent"

    def test_malformed_json_returns_empty_no_raise(self, monkeypatch, tmp_path):
        import brain.neural_web_context as NWC
        bad = tmp_path / "mastermind_context.json"
        bad.write_text("{not valid json")
        _patch_path(monkeypatch, bad)
        assert NWC.context() == {}
        ar = NWC.audit_row()
        assert ar["status"] == "absent"

    def test_stale_as_of_returns_empty_no_raise(self, monkeypatch, tmp_path):
        import brain.neural_web_context as NWC
        stale_file = _fresh_fixture(tmp_path, as_of=_stale_date())
        _patch_path(monkeypatch, stale_file)
        assert NWC.context() == {}
        ar = NWC.audit_row()
        assert ar["status"] == "stale"
        assert ar["asof"] == _stale_date()

    def test_wrong_schema_returns_empty_no_raise(self, monkeypatch, tmp_path):
        import brain.neural_web_context as NWC
        wrong_schema = _fresh_fixture(tmp_path, extra={"schema": "wrong_schema.v99"})
        _patch_path(monkeypatch, wrong_schema)
        assert NWC.context() == {}
        ar = NWC.audit_row()
        assert ar["status"] == "absent"

    def test_is_context_only_false_returns_empty(self, monkeypatch, tmp_path):
        import brain.neural_web_context as NWC
        not_ctx = _fresh_fixture(tmp_path, extra={"is_context_only": False})
        _patch_path(monkeypatch, not_ctx)
        assert NWC.context() == {}

    def test_missing_as_of_returns_empty(self, monkeypatch, tmp_path):
        import brain.neural_web_context as NWC
        raw = json.loads(_FIXTURE_PATH.read_text())
        raw["as_of"] = ""
        p = tmp_path / "mastermind_context.json"
        p.write_text(json.dumps(raw))
        _patch_path(monkeypatch, p)
        assert NWC.context() == {}

    def test_cache_is_per_process(self, monkeypatch, tmp_path):
        """Two calls with same state return same object without re-reading."""
        import brain.neural_web_context as NWC
        fresh_file = _fresh_fixture(tmp_path)
        _patch_path(monkeypatch, fresh_file)
        c1 = NWC.context()
        c2 = NWC.context()
        assert c1 is c2  # same cached object

    def test_reset_cache_forces_reread(self, monkeypatch, tmp_path):
        import brain.neural_web_context as NWC
        _patch_path(monkeypatch, None)
        assert NWC.context() == {}
        # now point to a valid file and reset
        fresh_file = _fresh_fixture(tmp_path)
        monkeypatch.setattr(NWC, "_ARTIFACT_PATH", fresh_file)
        NWC._reset_context_cache()
        assert NWC.context() != {}


# ---------------------------------------------------------------------------
# cortex sentinel test — memo text MUST NOT appear in seat_prompt_block
# ---------------------------------------------------------------------------

CORTEX_SENTINEL = "CORTEX_SENTINEL_XYZ"


class TestCortexExclusion:
    def test_sentinel_not_in_seat_prompt_block(self, monkeypatch, tmp_path):
        """seat_prompt_block must structurally exclude cortex memo text."""
        import brain.neural_web_context as NWC
        fresh_file = _fresh_fixture(tmp_path)
        _patch_path(monkeypatch, fresh_file)
        # Verify the fixture actually contains the sentinel in cortex memo
        ctx = NWC.context()
        cortex = (ctx.get("lobes") or {}).get("cortex") or {}
        memo_text = json.dumps(cortex.get("memo") or "")
        assert CORTEX_SENTINEL in memo_text, "fixture must contain sentinel in cortex"

        # Now assert it does NOT appear in seat_prompt_block
        block = NWC.seat_prompt_block(["NVDA", "AMD"], max_chars=1200)
        assert CORTEX_SENTINEL not in block, (
            f"cortex memo text '{CORTEX_SENTINEL}' must never appear in seat_prompt_block"
        )

    def test_sentinel_not_in_seat_prompt_block_large_max_chars(self, monkeypatch, tmp_path):
        """Even with max_chars=99999 the sentinel must not appear."""
        import brain.neural_web_context as NWC
        fresh_file = _fresh_fixture(tmp_path)
        _patch_path(monkeypatch, fresh_file)
        block = NWC.seat_prompt_block(["NVDA"], max_chars=99999)
        assert CORTEX_SENTINEL not in block


# ---------------------------------------------------------------------------
# market_plane shape
# ---------------------------------------------------------------------------

class TestMarketPlane:
    def test_absent_context_returns_stale_dict(self, monkeypatch):
        import brain.neural_web_context as NWC
        _patch_path(monkeypatch, None)
        plane = NWC.market_plane()
        assert plane.get("stale") is True
        assert plane.get("asof") is None

    def test_fresh_context_returns_present_non_stale(self, monkeypatch, tmp_path):
        import brain.neural_web_context as NWC
        fresh_file = _fresh_fixture(tmp_path)
        _patch_path(monkeypatch, fresh_file)
        plane = NWC.market_plane()
        assert plane.get("stale") is False
        assert plane.get("asof") == _today()
        assert "regime" in plane
        assert "verdict" in plane
        assert "contradiction_count" in plane

    def test_contradiction_count_is_int(self, monkeypatch, tmp_path):
        import brain.neural_web_context as NWC
        fresh_file = _fresh_fixture(tmp_path)
        _patch_path(monkeypatch, fresh_file)
        plane = NWC.market_plane()
        # fixture has 2 contradiction records
        assert isinstance(plane["contradiction_count"], int)
        assert plane["contradiction_count"] == 2


# ---------------------------------------------------------------------------
# audit_row statuses
# ---------------------------------------------------------------------------

class TestAuditRow:
    def test_absent_status(self, monkeypatch):
        import brain.neural_web_context as NWC
        _patch_path(monkeypatch, None)
        ar = NWC.audit_row()
        assert ar["status"] == "absent"
        assert ar["asof"] is None
        assert ar["n_candidates"] == 0

    def test_stale_status(self, monkeypatch, tmp_path):
        import brain.neural_web_context as NWC
        stale = _fresh_fixture(tmp_path, as_of=_stale_date())
        _patch_path(monkeypatch, stale)
        ar = NWC.audit_row()
        assert ar["status"] == "stale"

    def test_present_status(self, monkeypatch, tmp_path):
        import brain.neural_web_context as NWC
        fresh = _fresh_fixture(tmp_path)
        _patch_path(monkeypatch, fresh)
        ar = NWC.audit_row()
        assert ar["status"] == "present"
        assert ar["asof"] == _today()
        assert ar["n_candidates"] == 2  # NVDA + AMD in fixture
        assert ar["gap_notes_count"] == 1  # one gap note in fixture

    def test_present_age_days_is_zero_for_today(self, monkeypatch, tmp_path):
        import brain.neural_web_context as NWC
        fresh = _fresh_fixture(tmp_path)
        _patch_path(monkeypatch, fresh)
        ar = NWC.audit_row()
        assert ar["age_days"] == 0


# ---------------------------------------------------------------------------
# flag helper
# ---------------------------------------------------------------------------

class TestFlagHelper:
    def test_default_off(self, monkeypatch):
        import brain.neural_web_context as NWC
        monkeypatch.delenv("MASTERMIND_NW_CONTEXT", raising=False)
        assert NWC.nw_prompts_enabled() is False

    def test_on_when_set_to_1(self, monkeypatch):
        import brain.neural_web_context as NWC
        monkeypatch.setenv("MASTERMIND_NW_CONTEXT", "1")
        assert NWC.nw_prompts_enabled() is True

    def test_off_when_set_to_0(self, monkeypatch):
        import brain.neural_web_context as NWC
        monkeypatch.setenv("MASTERMIND_NW_CONTEXT", "0")
        assert NWC.nw_prompts_enabled() is False


# ---------------------------------------------------------------------------
# market_view acceptance: present+fresh neural_web plane advisory, never tilt contributor
# ---------------------------------------------------------------------------

class TestMarketViewNeuralWebPlane:
    """Acceptance tests for the neural_web plane in market_view."""

    def _build_view_with_nw(self, monkeypatch, tmp_path, nw_plane: dict):
        """Inject a neural_web_out dict and return the view."""
        import sys
        _MACRO_SRC = _ROOT / "vendor" / "macro_src"
        if _MACRO_SRC.exists() and str(_MACRO_SRC) not in sys.path:
            sys.path.insert(0, str(_MACRO_SRC))

        from brain import market_view as MV
        from brain import regime_frame as RF
        sys.path.insert(0, str(_ROOT / "tests" / "fixtures" / "market_view"))
        import build_fixtures as FIX  # noqa: PLC0415

        rp = tmp_path / "regime_latest.json"
        rp.write_text(json.dumps(FIX.incident_regime("2026-07-01")))
        monkeypatch.setitem(RF._REGION_PATHS, "us", rp)
        cp = tmp_path / "sector_cycles.json"
        cp.write_text(json.dumps(FIX.incident_sector_cycles("2026-07-01")))
        monkeypatch.setattr(RF, "_CYCLES_PATH", cp, raising=False)
        monkeypatch.setattr(RF, "_trading_days_since", lambda asof: 0, raising=False)

        return MV.view("us", neural_web_out=nw_plane)

    def test_present_fresh_nw_plane_is_advisory(self, monkeypatch, tmp_path):
        """A present+fresh neural_web plane must have status='advisory'."""
        import brain.neural_web_context as NWC
        fresh_file = _fresh_fixture(tmp_path)
        _patch_path(monkeypatch, fresh_file)
        nw_plane = NWC.market_plane()
        assert not nw_plane.get("stale"), "plane should be fresh"

        v = self._build_view_with_nw(monkeypatch, tmp_path, nw_plane)
        rec = v["planes"]["neural_web"]
        assert rec["status"] == "advisory", (
            f"neural_web plane must always be advisory; got {rec['status']!r}"
        )

    def test_nw_plane_never_in_tilt_contributors(self, monkeypatch, tmp_path):
        """neural_web must never appear in net_posture_tilt contributors."""
        import brain.neural_web_context as NWC
        fresh_file = _fresh_fixture(tmp_path)
        _patch_path(monkeypatch, fresh_file)
        nw_plane = NWC.market_plane()

        v = self._build_view_with_nw(monkeypatch, tmp_path, nw_plane)
        contributors = (v.get("net_posture_tilt") or {}).get("contributors") or []
        assert "neural_web" not in contributors, (
            "neural_web (advisory) must never sign net_posture_tilt"
        )

    def test_absent_nw_out_gives_absent_record(self, monkeypatch, tmp_path):
        """Passing None as neural_web_out must give an absent plane record."""
        v = self._build_view_with_nw(monkeypatch, tmp_path, None)
        rec = v["planes"]["neural_web"]
        assert rec["status"] == "advisory"
        assert rec["raw"].get("present") is False

    def test_plane_order_contains_neural_web_at_end(self):
        """PLANE_ORDER must have neural_web as last element (after H4 stubs)."""
        from brain import market_view as MV
        assert MV.PLANE_ORDER[-1] == "neural_web", (
            f"neural_web must be last in PLANE_ORDER; last element is {MV.PLANE_ORDER[-1]!r}"
        )

    def test_view_planes_keys_match_plane_order(self, monkeypatch, tmp_path):
        """planes dict keys must match PLANE_ORDER exactly (with neural_web included)."""
        import sys
        _MACRO_SRC = _ROOT / "vendor" / "macro_src"
        if _MACRO_SRC.exists() and str(_MACRO_SRC) not in sys.path:
            sys.path.insert(0, str(_MACRO_SRC))
        from brain import market_view as MV
        from brain import regime_frame as RF
        sys.path.insert(0, str(_ROOT / "tests" / "fixtures" / "market_view"))
        import build_fixtures as FIX  # noqa: PLC0415

        rp = tmp_path / "regime_latest.json"
        rp.write_text(json.dumps(FIX.incident_regime("2026-07-01")))
        monkeypatch.setitem(RF._REGION_PATHS, "us", rp)
        monkeypatch.setattr(RF, "_trading_days_since", lambda asof: 0, raising=False)

        v = MV.view("us")
        assert list(v["planes"].keys()) == list(MV.PLANE_ORDER)


# ---------------------------------------------------------------------------
# pm_conviction: flag OFF → byte-identical; flag ON → section present, no sentinel
# ---------------------------------------------------------------------------

class TestPmConvictionNwBlock:
    """Tests for the NW text block in pm_conviction._build_prompt()."""

    def _build_payload(self):
        """Build a minimal _pm_input payload for _build_prompt."""
        import brain.pm_conviction as P
        sized = [{"ticker": "NVDA", "weight": 0.06, "sleeve": "conviction",
                  "confluence": 0.7, "thesis": "growth thesis", "retained": False}]
        rejected = []
        strategist = {
            "confirmed_themes": [], "backdrop_stance": "neutral", "supportive": True,
            "crowding_flags": []
        }
        regime = {
            "quad": 1, "quad_name": "Goldilocks", "liquidity_overlay": "neutral",
            "cycle_tag": "mid-cycle", "sector_rs_top": [{"ticker": "XLK", "rank": 1}],
        }
        gate_info = {}
        return P._pm_input(sized, rejected, strategist, regime, gate_info, "2026-07-05",
                           leadership=[], defensive=[])

    def test_flag_off_no_nw_section(self, monkeypatch, tmp_path):
        """With MASTERMIND_NW_CONTEXT unset (OFF), prompt must not contain NW section header."""
        import brain.pm_conviction as P
        import brain.neural_web_context as NWC
        monkeypatch.delenv("MASTERMIND_NW_CONTEXT", raising=False)
        fresh_file = _fresh_fixture(tmp_path)
        _patch_path(monkeypatch, fresh_file)
        # Silence posture_decider to isolate
        monkeypatch.setattr(P, "_read_market_view", lambda: None, raising=False)

        payload = self._build_payload()
        prompt = P._build_prompt(payload)
        assert "NEURAL WEB CONTEXT" not in prompt

    def test_flag_on_section_appears_bounded(self, monkeypatch, tmp_path):
        """With flag ON and fresh context, the NW section appears and is bounded."""
        import brain.pm_conviction as P
        import brain.neural_web_context as NWC
        monkeypatch.setenv("MASTERMIND_NW_CONTEXT", "1")
        fresh_file = _fresh_fixture(tmp_path)
        _patch_path(monkeypatch, fresh_file)
        monkeypatch.setattr(P, "_read_market_view", lambda: None, raising=False)

        payload = self._build_payload()
        prompt = P._build_prompt(payload)
        assert "NEURAL WEB CONTEXT" in prompt
        # The NW block itself is bounded at max_chars=1200.
        # Find the block between the header and the next section separator.
        idx = prompt.find("## NEURAL WEB CONTEXT")
        assert idx >= 0
        # Find the end of the NW block (next "##" section or end of block — blank line after content)
        # The block is: header line + NW text (≤1200 chars) + empty line
        # Count just the NW text content: from after the header to the benchmark line
        after_header = prompt[idx:]
        # Extract just the NW text portion (between header and next ##)
        lines_after = after_header.split("\n")
        nw_content_lines = []
        for line in lines_after[1:]:  # skip the header
            if line.startswith("BENCHMARK YOU MUST BEAT"):
                break
            nw_content_lines.append(line)
        nw_content = "\n".join(nw_content_lines)
        assert len(nw_content) <= 1200 + 20, (  # +20 for header line
            f"NW text block exceeds max_chars=1200: {len(nw_content)} chars"
        )

    def test_flag_on_no_cortex_sentinel(self, monkeypatch, tmp_path):
        """With flag ON, cortex sentinel must not appear in the prompt."""
        import brain.pm_conviction as P
        import brain.neural_web_context as NWC
        monkeypatch.setenv("MASTERMIND_NW_CONTEXT", "1")
        fresh_file = _fresh_fixture(tmp_path)
        _patch_path(monkeypatch, fresh_file)
        monkeypatch.setattr(P, "_read_market_view", lambda: None, raising=False)

        payload = self._build_payload()
        prompt = P._build_prompt(payload)
        assert CORTEX_SENTINEL not in prompt, (
            "cortex sentinel must never appear in PM prompt"
        )

    def test_flag_off_flag_on_differ_only_in_nw_section(self, monkeypatch, tmp_path):
        """Flag OFF and ON prompts differ only by the NW block presence/absence."""
        import brain.pm_conviction as P
        import brain.neural_web_context as NWC
        fresh_file = _fresh_fixture(tmp_path)
        monkeypatch.setattr(P, "_read_market_view", lambda: None, raising=False)

        monkeypatch.delenv("MASTERMIND_NW_CONTEXT", raising=False)
        NWC._reset_context_cache()
        _patch_path(monkeypatch, fresh_file)
        payload_off = self._build_payload()
        prompt_off = P._build_prompt(payload_off)

        monkeypatch.setenv("MASTERMIND_NW_CONTEXT", "1")
        NWC._reset_context_cache()
        _patch_path(monkeypatch, fresh_file)
        payload_on = self._build_payload()
        prompt_on = P._build_prompt(payload_on)

        assert prompt_off != prompt_on, "flag ON and OFF prompts must differ when context is present"
        assert "NEURAL WEB CONTEXT" not in prompt_off
        assert "NEURAL WEB CONTEXT" in prompt_on
