"""Tests for brain/key_rotor.py — OAuth key-pool rotation layer.

Covers:
  1. classify_failure — table of patterns and expected results
  2. Cooling horizons — auth clears on ok row; window/weekly do NOT
  3. candidates() ordering — cooling last; enabled-set filter + R-V10-3 fallback
  4. Legacy token suppression when numbered slots present (V11 rule)
  5. Rotation loop in cli_bridge.reason — slot A cooled, result from slot B
  6. export: key_events.jsonl written with 14d filter

All tests use tmp_path or monkeypatch to avoid writing the real data/ tree.
"""
from __future__ import annotations

import asyncio
import importlib
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

import bot  # noqa: F401 — vendor/macro bootstrap


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _ts(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _write_row(ledger: Path, row: dict) -> None:
    ledger.parent.mkdir(parents=True, exist_ok=True)
    with ledger.open("a") as fh:
        fh.write(json.dumps(row) + "\n")


# ---------------------------------------------------------------------------
# 1. classify_failure — table-driven tests
# ---------------------------------------------------------------------------

class TestClassifyFailure:
    @pytest.fixture(autouse=True)
    def _import(self):
        from brain import key_rotor as _kr
        self.kr = _kr

    def test_org_disabled_banner_auth(self):
        text = "Your organization has disabled Claude subscription access for Claude Code · Use an Anthropic API key instead"
        result = self.kr.classify_failure(text)
        assert result is not None
        assert result[0] == "auth"
        assert "disabled claude subscription access" in result[1]

    def test_window_limit_with_reset_time(self):
        text = "Claude usage limit reached. Your limit will reset at 3pm"
        result = self.kr.classify_failure(text)
        assert result is not None
        assert result[0] == "window"

    def test_weekly_limit(self):
        text = "You've reached your weekly usage limit"
        result = self.kr.classify_failure(text)
        assert result is not None
        assert result[0] == "weekly"

    def test_long_text_returns_none(self):
        """Text > 600 chars must not be classified — avoids cooling on a legitimate analysis."""
        long_text = "rate limit " + ("x" * 5000)
        assert self.kr.classify_failure(long_text) is None

    def test_none_returns_none(self):
        assert self.kr.classify_failure(None) is None

    def test_sdk_oddity_alone_returns_none(self):
        """The SDK oddity phrase alone must NOT classify."""
        assert self.kr.classify_failure("Claude Code returned an error result: success") is None

    def test_sdk_oddity_with_auth_phrase_classifies(self):
        """SDK oddity + auth phrase → classified as auth."""
        text = "Claude Code returned an error result: Your organization has disabled Claude subscription access"
        result = self.kr.classify_failure(text)
        assert result is not None
        assert result[0] == "auth"

    def test_401_is_auth(self):
        result = self.kr.classify_failure("HTTP 401 Unauthorized")
        assert result is not None
        assert result[0] == "auth"

    def test_429_is_window(self):
        result = self.kr.classify_failure("429 Too Many Requests")
        assert result is not None
        assert result[0] == "window"

    def test_529_is_window(self):
        result = self.kr.classify_failure("529 Overloaded")
        assert result is not None
        assert result[0] == "window"

    def test_rate_limit_weekly_context(self):
        text = "rate limit reached. Resets next week"
        result = self.kr.classify_failure(text)
        assert result is not None
        assert result[0] == "weekly"

    def test_403_forbidden_is_auth(self):
        result = self.kr.classify_failure("403 Forbidden")
        assert result is not None
        assert result[0] == "auth"

    def test_403_without_forbidden_is_window(self):
        # "403" alone matches window (it's in the _WINDOW_PHRASES... wait, it's not)
        # Actually "403" is NOT in window phrases; only auth has 403+forbidden check.
        # A plain "403" without forbidden/permission should NOT match auth or weekly.
        # It also doesn't match window phrases. So it should return None.
        result = self.kr.classify_failure("Error: 403 error occurred")
        # No "forbidden" or "permission" → not auth; no rate-limit phrases → not window
        assert result is None

    def test_token_expired_is_auth(self):
        result = self.kr.classify_failure("oauth token has expired")
        assert result is not None
        assert result[0] == "auth"

    def test_revoked_is_auth(self):
        result = self.kr.classify_failure("Token revoked by issuer")
        assert result is not None
        assert result[0] == "auth"

    def test_overloaded_is_window(self):
        result = self.kr.classify_failure("Service overloaded, please retry")
        assert result is not None
        assert result[0] == "window"

    def test_quota_is_window(self):
        result = self.kr.classify_failure("Quota exceeded")
        assert result is not None
        assert result[0] == "window"

    def test_auth_takes_precedence_over_weekly(self):
        """401 + week mention → auth wins (auth evaluated first)."""
        text = "401 unauthorized — weekly limit also exceeded"
        result = self.kr.classify_failure(text)
        assert result is not None
        assert result[0] == "auth"


class TestClassifySourceStrictness:
    """source='text' (model-visible result) must use STRICT banner phrases only —
    a short legitimate trading answer containing loose substrings must never cool
    a healthy key.  source='error' (exception/error-field) keeps the loose lists."""

    @pytest.fixture(autouse=True)
    def _import(self):
        from brain import key_rotor as _kr
        self.kr = _kr

    @pytest.mark.parametrize("text", [
        "The stock is overloaded with buyers; quota concerns remain limited.",
        "Import quota cuts lifted copper 429 points off the lows.",
        "Fed rate limit expectations shifted after the print.",
        "HTTP 401 mentioned in the filing footnote.",
        "403 Forbidden",
        "Token revoked by issuer",
    ])
    def test_loose_substrings_do_not_classify_result_text(self, text):
        assert self.kr.classify_failure(text, source="text") is None

    @pytest.mark.parametrize("text,kind", [
        ("Service overloaded, please retry", "window"),
        ("429 Too Many Requests", "window"),
        ("HTTP 401 Unauthorized", "auth"),
        ("403 Forbidden", "auth"),
        ("Token revoked by issuer", "auth"),
    ])
    def test_loose_substrings_still_classify_error_source(self, text, kind):
        result = self.kr.classify_failure(text, source="error")
        assert result is not None and result[0] == kind

    @pytest.mark.parametrize("text,kind", [
        ("Your organization has disabled Claude subscription access for Claude Code", "auth"),
        ("Invalid bearer token", "auth"),
        ("Claude usage limit reached. Your limit will reset at 3pm", "window"),
        ("You've reached your weekly usage limit", "weekly"),
    ])
    def test_banner_phrases_classify_result_text(self, text, kind):
        result = self.kr.classify_failure(text, source="text")
        assert result is not None and result[0] == kind

    def test_default_source_is_error(self):
        assert self.kr.classify_failure("Quota exceeded") is not None
        assert self.kr.classify_failure("Quota exceeded", source="text") is None


# ---------------------------------------------------------------------------
# 2. Cooling horizons
# ---------------------------------------------------------------------------

class TestCoolingHorizons:
    @pytest.fixture()
    def ledger(self, tmp_path) -> Path:
        return tmp_path / "data" / "metabolism" / "key_ledger.jsonl"

    @pytest.fixture(autouse=True)
    def _import(self):
        from brain import key_rotor as _kr
        self.kr = _kr

    def test_no_cooling_rows_not_cooling(self, ledger, tmp_path):
        assert self.kr.is_cooling("claude_code_oauth_3", root=tmp_path) is False

    def test_auth_cleared_by_later_ok_row(self, ledger, tmp_path):
        """auth cooling is cleared early when a later ok row appears."""
        cool_ts = _now() - timedelta(hours=1)
        ok_ts = _now()
        _write_row(ledger, {
            "schema": "metabolism.key_ledger.v1",
            "ts": _ts(cool_ts),
            "key_id": "claude_code_oauth_3",
            "cycle_id": "", "stage": "cooling", "est_tokens": 0,
            "outcome": "auth_failed",
            "cool_kind": "auth",
            "reset_hint": _ts(_now() + timedelta(hours=23)),
        })
        _write_row(ledger, {
            "schema": "metabolism.key_ledger.v1",
            "ts": _ts(ok_ts),
            "key_id": "claude_code_oauth_3",
            "cycle_id": "", "stage": "pm", "est_tokens": 500,
            "outcome": "ok",
        })
        # Auth cleared by ok row → not cooling
        assert self.kr.is_cooling("claude_code_oauth_3", root=tmp_path) is False

    def test_window_not_cleared_by_ok_row(self, ledger, tmp_path):
        """window cooling is NOT cleared by a later ok row — only by reset_hint passage."""
        cool_ts = _now() - timedelta(minutes=30)
        ok_ts = _now()
        future_reset = _now() + timedelta(hours=4)
        _write_row(ledger, {
            "schema": "metabolism.key_ledger.v1",
            "ts": _ts(cool_ts),
            "key_id": "claude_code_oauth_3",
            "cycle_id": "", "stage": "cooling", "est_tokens": 0,
            "outcome": "rate_limited",
            "cool_kind": "window",
            "reset_hint": _ts(future_reset),
        })
        _write_row(ledger, {
            "schema": "metabolism.key_ledger.v1",
            "ts": _ts(ok_ts),
            "key_id": "claude_code_oauth_3",
            "cycle_id": "", "stage": "pm", "est_tokens": 100,
            "outcome": "ok",
        })
        # window cooling NOT cleared by ok → still cooling
        assert self.kr.is_cooling("claude_code_oauth_3", root=tmp_path) is True

    def test_window_cleared_by_reset_hint_passage(self, ledger, tmp_path):
        """window cooling IS cleared after reset_hint passes."""
        past_reset = _now() - timedelta(hours=1)
        _write_row(ledger, {
            "schema": "metabolism.key_ledger.v1",
            "ts": _ts(_now() - timedelta(hours=6)),
            "key_id": "claude_code_oauth_3",
            "cycle_id": "", "stage": "cooling", "est_tokens": 0,
            "outcome": "rate_limited",
            "cool_kind": "window",
            "reset_hint": _ts(past_reset),
        })
        assert self.kr.is_cooling("claude_code_oauth_3", root=tmp_path) is False

    def test_weekly_not_cleared_by_ok_row(self, ledger, tmp_path):
        """weekly cooling is NOT cleared by an ok row."""
        future_reset = _now() + timedelta(days=6)
        _write_row(ledger, {
            "schema": "metabolism.key_ledger.v1",
            "ts": _ts(_now() - timedelta(hours=1)),
            "key_id": "claude_code_oauth_5",
            "cycle_id": "", "stage": "cooling", "est_tokens": 0,
            "outcome": "rate_limited",
            "cool_kind": "weekly",
            "reset_hint": _ts(future_reset),
        })
        _write_row(ledger, {
            "schema": "metabolism.key_ledger.v1",
            "ts": _ts(_now()),
            "key_id": "claude_code_oauth_5",
            "cycle_id": "", "stage": "pm", "est_tokens": 200,
            "outcome": "ok",
        })
        assert self.kr.is_cooling("claude_code_oauth_5", root=tmp_path) is True

    def test_dual_horizon_both_active(self, ledger, tmp_path):
        """When both window and auth horizons are active, key is still cooling."""
        _write_row(ledger, {
            "schema": "metabolism.key_ledger.v1",
            "ts": _ts(_now() - timedelta(hours=2)),
            "key_id": "claude_code_oauth_4",
            "cycle_id": "", "stage": "cooling", "est_tokens": 0,
            "outcome": "rate_limited",
            "cool_kind": "window",
            "reset_hint": _ts(_now() + timedelta(hours=3)),
        })
        _write_row(ledger, {
            "schema": "metabolism.key_ledger.v1",
            "ts": _ts(_now() - timedelta(hours=1)),
            "key_id": "claude_code_oauth_4",
            "cycle_id": "", "stage": "cooling", "est_tokens": 0,
            "outcome": "auth_failed",
            "cool_kind": "auth",
            "reset_hint": _ts(_now() + timedelta(hours=23)),
        })
        assert self.kr.is_cooling("claude_code_oauth_4", root=tmp_path) is True


# ---------------------------------------------------------------------------
# 3. candidates() ordering
# ---------------------------------------------------------------------------

class TestCandidates:
    @pytest.fixture(autouse=True)
    def _import(self):
        from brain import key_rotor as _kr
        self.kr = _kr

    def test_cooling_key_sorts_last(self, tmp_path, monkeypatch):
        """A cooling key appears after non-cooling keys."""
        ledger = tmp_path / "data" / "metabolism" / "key_ledger.jsonl"
        # Mark slot 3 as cooling
        _write_row(ledger, {
            "schema": "metabolism.key_ledger.v1",
            "ts": _ts(_now()),
            "key_id": "claude_code_oauth_3",
            "cycle_id": "", "stage": "cooling", "est_tokens": 0,
            "outcome": "rate_limited",
            "cool_kind": "window",
            "reset_hint": _ts(_now() + timedelta(hours=4)),
        })
        # Present slots: 3 (cooling) and 5 (not cooling)
        monkeypatch.delenv("METAB_KEYS_ENABLED", raising=False)
        for n in range(1, 8):
            monkeypatch.delenv(f"CLAUDE_CODE_OAUTH_TOKEN_{n}", raising=False)
        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN_3", "tok3")
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN_5", "tok5")

        cands = self.kr.candidates(root=tmp_path)
        assert len(cands) == 2
        ids = [c["key_id"] for c in cands]
        # 5 (non-cooling) must come before 3 (cooling)
        assert ids.index("claude_code_oauth_5") < ids.index("claude_code_oauth_3")
        assert cands[1]["cooling"] is True

    def test_enabled_set_filter(self, tmp_path, monkeypatch):
        """METAB_KEYS_ENABLED filters to only enabled slots."""
        monkeypatch.setenv("METAB_KEYS_ENABLED", "5,6")
        for n in range(1, 8):
            monkeypatch.delenv(f"CLAUDE_CODE_OAUTH_TOKEN_{n}", raising=False)
        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN_3", "tok3")
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN_5", "tok5")
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN_6", "tok6")

        cands = self.kr.candidates(root=tmp_path)
        ids = [c["key_id"] for c in cands]
        assert "claude_code_oauth_3" not in ids  # 3 not in enabled set
        assert "claude_code_oauth_5" in ids
        assert "claude_code_oauth_6" in ids

    def test_r_v10_3_fallback_when_filter_removes_all(self, tmp_path, monkeypatch):
        """R-V10-3: if enabled filter removes ALL present slots, fall back to unfiltered."""
        monkeypatch.setenv("METAB_KEYS_ENABLED", "7")  # slot 7 not present
        for n in range(1, 8):
            monkeypatch.delenv(f"CLAUDE_CODE_OAUTH_TOKEN_{n}", raising=False)
        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN_3", "tok3")
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN_5", "tok5")

        cands = self.kr.candidates(root=tmp_path)
        ids = [c["key_id"] for c in cands]
        # Fallback: all present slots returned
        assert "claude_code_oauth_3" in ids
        assert "claude_code_oauth_5" in ids

    def test_legacy_only_when_no_numbered_slots(self, tmp_path, monkeypatch):
        """Legacy token included ONLY when zero numbered slots are present."""
        for n in range(1, 8):
            monkeypatch.delenv(f"CLAUDE_CODE_OAUTH_TOKEN_{n}", raising=False)
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok_legacy")
        monkeypatch.delenv("METAB_KEYS_ENABLED", raising=False)

        cands = self.kr.candidates(root=tmp_path)
        assert len(cands) == 1
        assert cands[0]["key_id"] == "legacy"
        assert cands[0]["env_name"] == "CLAUDE_CODE_OAUTH_TOKEN"

    def test_legacy_suppressed_when_numbered_slot_present(self, tmp_path, monkeypatch):
        """Legacy token NOT included when numbered slots are present (V11 rule)."""
        for n in range(1, 8):
            monkeypatch.delenv(f"CLAUDE_CODE_OAUTH_TOKEN_{n}", raising=False)
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok_legacy")
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN_5", "tok5")
        monkeypatch.delenv("METAB_KEYS_ENABLED", raising=False)

        cands = self.kr.candidates(root=tmp_path)
        ids = [c["key_id"] for c in cands]
        assert "legacy" not in ids
        assert "claude_code_oauth_5" in ids

    def test_load_ordering(self, tmp_path, monkeypatch):
        """Non-cooling candidates are ordered by ascending window_load then slot number."""
        ledger = tmp_path / "data" / "metabolism" / "key_ledger.jsonl"
        # Give slot 3 more recent sessions (higher window_load) than slot 5
        for _ in range(3):
            _write_row(ledger, {
                "schema": "metabolism.key_ledger.v1",
                "ts": _ts(_now() - timedelta(minutes=10)),
                "key_id": "claude_code_oauth_3",
                "cycle_id": "", "stage": "pm", "est_tokens": 100, "outcome": "ok",
            })
        _write_row(ledger, {
            "schema": "metabolism.key_ledger.v1",
            "ts": _ts(_now() - timedelta(minutes=10)),
            "key_id": "claude_code_oauth_5",
            "cycle_id": "", "stage": "pm", "est_tokens": 100, "outcome": "ok",
        })

        monkeypatch.delenv("METAB_KEYS_ENABLED", raising=False)
        for n in range(1, 8):
            monkeypatch.delenv(f"CLAUDE_CODE_OAUTH_TOKEN_{n}", raising=False)
        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN_3", "tok3")
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN_5", "tok5")

        cands = self.kr.candidates(root=tmp_path)
        ids = [c["key_id"] for c in cands]
        # slot 5 (load=1) before slot 3 (load=3)
        assert ids[0] == "claude_code_oauth_5"
        assert ids[1] == "claude_code_oauth_3"


# ---------------------------------------------------------------------------
# 4. Rotation loop: slot A cooled, result from slot B
# ---------------------------------------------------------------------------

class TestRotationLoop:
    """Monkeypatch _via_sdk to simulate slot A returning org-disabled banner, slot B ok."""

    _ORG_DISABLED = (
        "Your organization has disabled Claude subscription access for Claude Code "
        "· Use an Anthropic API key instead"
    )

    def _setup_slots(self, monkeypatch, tmp_path):
        """Set CLAUDE_CODE_OAUTH_TOKEN_3 and _5; disable all others."""
        for n in range(1, 8):
            monkeypatch.delenv(f"CLAUDE_CODE_OAUTH_TOKEN_{n}", raising=False)
        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
        monkeypatch.delenv("METAB_KEYS_ENABLED", raising=False)
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN_3", "tok_slot3_value")
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN_5", "tok_slot5_value")

    def test_slot_a_cooled_result_from_slot_b(self, tmp_path, monkeypatch):
        """Slot 3 returns org-disabled banner → cooled; result comes from slot 5."""
        import brain.key_rotor as _kr
        import brain.cost_guard as _cg
        import importlib
        _kr = importlib.reload(_kr)
        _cg = importlib.reload(_cg)
        # Redirect ledger to tmp_path
        monkeypatch.setattr(_kr, "_mm_root", lambda: tmp_path)
        # Redirect cost_guard _DIR so _record_cli_cost does NOT write real data/
        monkeypatch.setattr(_cg, "_DIR", tmp_path / "cost")
        monkeypatch.setattr(_cg, "_ROOT", tmp_path)

        self._setup_slots(monkeypatch, tmp_path)

        import brain.cli_bridge as _cb
        _cb = importlib.reload(_cb)

        call_count = {"n": 0}

        async def _fake_via_sdk(prompt, mdl, role, system, append_system, tools, dirs,
                                turns, workdir, perm, mcp_servers, resume, arm,
                                run_id=None, env_name=None):
            call_count["n"] += 1
            if env_name == "CLAUDE_CODE_OAUTH_TOKEN_3":
                # Return the org-disabled banner as text (the crux case)
                return {
                    "ok": True,  # looks like success
                    "text": self._ORG_DISABLED,
                    "model": mdl, "role": role, "armed": arm,
                    "tools_used": [], "cost_usd": None,
                    "session_id": None, "usage": None, "backend": "sdk",
                }
            elif env_name == "CLAUDE_CODE_OAUTH_TOKEN_5":
                return {
                    "ok": True,
                    "text": "The market is bullish.",
                    "model": mdl, "role": role, "armed": arm,
                    "tools_used": [], "cost_usd": 0.05,
                    "session_id": "sess-xyz",
                    "usage": {"input_tokens": 100, "output_tokens": 50},
                    "backend": "sdk",
                }
            raise AssertionError(f"Unexpected env_name: {env_name}")

        monkeypatch.setattr(_cb, "_via_sdk", _fake_via_sdk)
        monkeypatch.setattr(_cb, "_SDK", True)
        # Patch key_rotor import in cli_bridge to use our tmp_path-redirected version
        monkeypatch.setattr(_cb, "__builtins__", _cb.__builtins__)

        # Reload key_rotor in cli_bridge's namespace
        import sys
        sys.modules["brain.key_rotor"] = _kr

        result = asyncio.run(_cb.reason("what is the market doing?", log_run=False))

        assert result["ok"] is True
        assert result["text"] == "The market is bullish."
        assert result.get("key_id") == "claude_code_oauth_5"
        assert call_count["n"] == 2  # slot 3 attempted first, then slot 5

        # Verify slot 3 got an auth cooling row in the ledger
        ledger = tmp_path / "data" / "metabolism" / "key_ledger.jsonl"
        assert ledger.exists(), "ledger was not written"
        rows = [json.loads(l) for l in ledger.read_text().splitlines() if l.strip()]
        cooling_rows = [r for r in rows if r.get("key_id") == "claude_code_oauth_3"
                        and r.get("outcome") == "auth_failed"]
        assert len(cooling_rows) == 1, f"Expected 1 auth_failed row for slot 3, got: {cooling_rows}"

        # Verify slot 5 got an ok row in the ledger
        ok_rows = [r for r in rows if r.get("key_id") == "claude_code_oauth_5"
                   and r.get("outcome") == "ok"]
        assert len(ok_rows) == 1, f"Expected 1 ok row for slot 5, got: {ok_rows}"


# ---------------------------------------------------------------------------
# 5. cost_guard: key_id accumulates under "keys" sub-dict
# ---------------------------------------------------------------------------

class TestCostGuardKeyAttribution:
    @pytest.fixture()
    def cg(self, tmp_path, monkeypatch):
        import brain.cost_guard as _cg
        _cg = importlib.reload(_cg)
        monkeypatch.setattr(_cg, "_DIR", tmp_path / "cost")
        monkeypatch.setattr(_cg, "_ROOT", tmp_path)
        return _cg

    def test_key_id_accumulates_keys_sub_dict(self, cg):
        """record(key_id=...) writes a 'keys' sub-dict at book level."""
        cg.record("flagship", 0.5, "2026-07-17",
                  seat="pm", input_tokens=200, output_tokens=50,
                  key_id="claude_code_oauth_3")
        raw = json.loads(cg._path("2026-07-17").read_text())
        bk = raw["flagship"]
        assert "keys" in bk, f"'keys' sub-dict missing from book: {bk}"
        ke = bk["keys"]["claude_code_oauth_3"]
        assert ke["usd"] == pytest.approx(0.5)
        assert ke["input_tokens"] == 200
        assert ke["output_tokens"] == 50
        assert ke["calls"] == 1

    def test_key_id_accumulates_across_calls(self, cg):
        """Multiple record() calls with same key_id accumulate."""
        cg.record("flagship", 0.3, "2026-07-17", key_id="claude_code_oauth_3",
                  input_tokens=100, output_tokens=30)
        cg.record("flagship", 0.2, "2026-07-17", key_id="claude_code_oauth_3",
                  input_tokens=80, output_tokens=20)
        raw = json.loads(cg._path("2026-07-17").read_text())
        ke = raw["flagship"]["keys"]["claude_code_oauth_3"]
        assert ke["usd"] == pytest.approx(0.5)
        assert ke["input_tokens"] == 180
        assert ke["calls"] == 2

    def test_multiple_key_ids_separate(self, cg):
        """Different key_ids accumulate separately."""
        cg.record("flagship", 0.5, "2026-07-17", key_id="claude_code_oauth_3",
                  input_tokens=100, output_tokens=50)
        cg.record("flagship", 0.3, "2026-07-17", key_id="claude_code_oauth_5",
                  input_tokens=60, output_tokens=30)
        raw = json.loads(cg._path("2026-07-17").read_text())
        keys = raw["flagship"]["keys"]
        assert "claude_code_oauth_3" in keys
        assert "claude_code_oauth_5" in keys
        assert keys["claude_code_oauth_3"]["usd"] == pytest.approx(0.5)
        assert keys["claude_code_oauth_5"]["usd"] == pytest.approx(0.3)

    def test_summary_shape_unchanged_without_keys(self, cg):
        """summary() output shape is unchanged when no key_id is provided."""
        cg.record("flagship", 1.5, "2026-07-17", seat="pm", input_tokens=500)
        s = cg.summary("2026-07-17")
        # Shape contract: asof, cap, enabled, total, books
        assert set(s.keys()) >= {"asof", "cap", "enabled", "total", "books"}
        assert s["books"]["flagship"]["spent"] == pytest.approx(1.5)
        assert "over" in s["books"]["flagship"]

    def test_summary_shape_unchanged_with_keys(self, cg):
        """summary() output shape is unchanged even when key_id data is present."""
        cg.record("flagship", 1.5, "2026-07-17", seat="pm", input_tokens=500,
                  key_id="claude_code_oauth_3")
        s = cg.summary("2026-07-17")
        # shape contract still holds
        assert set(s.keys()) >= {"asof", "cap", "enabled", "total", "books"}
        assert s["books"]["flagship"]["spent"] == pytest.approx(1.5)
        # summary() does NOT expose by_key (that's in build_cost_summary only)
        assert "by_key" not in s

    def test_build_cost_summary_by_key(self, cg):
        """build_cost_summary() exposes by_key in totals_30d."""
        cg.record("flagship", 0.8, "2026-07-17", key_id="claude_code_oauth_3",
                  input_tokens=200, output_tokens=80)
        cg.record("flagship", 0.4, "2026-07-17", key_id="claude_code_oauth_5",
                  input_tokens=100, output_tokens=40)
        s = cg.build_cost_summary("2026-07-17")
        by_key = s["totals_30d"].get("by_key", {})
        assert by_key.get("claude_code_oauth_3", 0) == pytest.approx(0.8)
        assert by_key.get("claude_code_oauth_5", 0) == pytest.approx(0.4)

    def test_no_key_id_no_keys_sub_dict(self, cg):
        """record() without key_id does NOT create 'keys' sub-dict."""
        cg.record("flagship", 0.5, "2026-07-17", seat="pm", input_tokens=100)
        raw = json.loads(cg._path("2026-07-17").read_text())
        bk = raw["flagship"]
        # keys sub-dict should not be present when key_id was never passed
        assert "keys" not in bk


# ---------------------------------------------------------------------------
# 6. export: key_events.jsonl written with 14-day filter
# ---------------------------------------------------------------------------

class TestKeyEventsExport:
    def test_key_events_written_14d_filter(self, tmp_path):
        """_write_key_events writes only rows within the last 14 days."""
        # Set up a fake Mastermind root with a key ledger
        mm_root = tmp_path / "mm"
        ledger = mm_root / "data" / "metabolism" / "key_ledger.jsonl"
        ledger.parent.mkdir(parents=True, exist_ok=True)

        now = _now()
        old_row = json.dumps({
            "schema": "metabolism.key_ledger.v1",
            "ts": _ts(now - timedelta(days=20)),  # 20 days old → outside window
            "key_id": "claude_code_oauth_3",
            "cycle_id": "", "stage": "pm", "est_tokens": 100, "outcome": "ok",
        })
        new_row = json.dumps({
            "schema": "metabolism.key_ledger.v1",
            "ts": _ts(now - timedelta(days=3)),  # 3 days old → inside window
            "key_id": "claude_code_oauth_5",
            "cycle_id": "", "stage": "pm", "est_tokens": 200, "outcome": "ok",
        })
        ledger.write_text(old_row + "\n" + new_row + "\n")

        # Macro root (destination)
        macro_root = tmp_path / "macro"
        macro_root.mkdir(parents=True, exist_ok=True)

        # Patch _ROOT in export_macro_snapshot to mm_root
        import scripts.export_macro_snapshot as _exp
        orig_root = _exp._ROOT
        try:
            _exp._ROOT = mm_root
            _exp._write_key_events(macro_root)
        finally:
            _exp._ROOT = orig_root

        key_events_path = macro_root / "data" / "mastermind" / "key_events.jsonl"
        assert key_events_path.exists(), "key_events.jsonl was not written"
        rows = [json.loads(l) for l in key_events_path.read_text().splitlines() if l.strip()]
        assert len(rows) == 1, f"Expected 1 row (recent only), got {len(rows)}: {rows}"
        assert rows[0]["key_id"] == "claude_code_oauth_5"

    def test_key_events_absent_ledger_skips_silently(self, tmp_path):
        """_write_key_events skips when key_ledger.jsonl is absent."""
        mm_root = tmp_path / "mm"
        macro_root = tmp_path / "macro"
        macro_root.mkdir(parents=True, exist_ok=True)
        # No ledger created

        import scripts.export_macro_snapshot as _exp
        orig_root = _exp._ROOT
        try:
            _exp._ROOT = mm_root  # no ledger here
            _exp._write_key_events(macro_root)  # should not raise
        finally:
            _exp._ROOT = orig_root

        key_events_path = macro_root / "data" / "mastermind" / "key_events.jsonl"
        assert not key_events_path.exists()

    def test_key_events_max_rows_cap(self, tmp_path):
        """_write_key_events caps at _KEY_EVENTS_MAX_ROWS most-recent rows."""
        import scripts.export_macro_snapshot as _exp

        mm_root = tmp_path / "mm"
        ledger = mm_root / "data" / "metabolism" / "key_ledger.jsonl"
        ledger.parent.mkdir(parents=True, exist_ok=True)

        now = _now()
        # Write 30 rows within the 14-day window, then patch MAX_ROWS to 10
        lines = []
        for i in range(30):
            lines.append(json.dumps({
                "schema": "metabolism.key_ledger.v1",
                "ts": _ts(now - timedelta(days=i % 13)),
                "key_id": f"claude_code_oauth_{(i % 7) + 1}",
                "cycle_id": "", "stage": "pm", "est_tokens": 50, "outcome": "ok",
            }))
        ledger.write_text("\n".join(lines) + "\n")

        macro_root = tmp_path / "macro"
        macro_root.mkdir(parents=True, exist_ok=True)

        orig_root = _exp._ROOT
        orig_max = _exp._KEY_EVENTS_MAX_ROWS
        try:
            _exp._ROOT = mm_root
            _exp._KEY_EVENTS_MAX_ROWS = 10
            _exp._write_key_events(macro_root)
        finally:
            _exp._ROOT = orig_root
            _exp._KEY_EVENTS_MAX_ROWS = orig_max

        key_events_path = macro_root / "data" / "mastermind" / "key_events.jsonl"
        rows = [l for l in key_events_path.read_text().splitlines() if l.strip()]
        assert len(rows) == 10, f"Expected 10 rows (capped), got {len(rows)}"


# ---------------------------------------------------------------------------
# 7. Macro federation (FEDERATION DOWN) — candidates() reorders/skips on macro health
# ---------------------------------------------------------------------------

def _macro_metab_dir(root: Path) -> Path:
    """The vendored macro metabolism dir under a test root (mirrors _macro_vendor_root)."""
    d = root / "vendor" / "macro" / "data" / "metabolism"
    d.parent.mkdir(parents=True, exist_ok=True)
    return d


def _write_macro_budget(root: Path, per_key: dict, *, ts: datetime | None = None) -> None:
    d = _macro_metab_dir(root)
    d.mkdir(parents=True, exist_ok=True)
    (d / "key_budget_status.json").write_text(json.dumps({
        "schema": "metab_budget_status.v1",
        "ts": _ts(ts or _now()),
        "per_key": per_key,
    }))


def _write_macro_ledger(root: Path, rows: list[dict]) -> None:
    d = _macro_metab_dir(root)
    d.mkdir(parents=True, exist_ok=True)
    (d / "key_ledger.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")


class TestMacroFederation:
    @pytest.fixture(autouse=True)
    def _import(self):
        from brain import key_rotor as _kr
        self.kr = _kr

    def _present(self, monkeypatch, *slots: int):
        monkeypatch.delenv("METAB_KEYS_ENABLED", raising=False)
        for n in range(1, 8):
            monkeypatch.delenv(f"CLAUDE_CODE_OAUTH_TOKEN_{n}", raising=False)
        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
        for n in slots:
            monkeypatch.setenv(f"CLAUDE_CODE_OAUTH_TOKEN_{n}", f"tok{n}")

    def test_missing_macro_data_fail_open_identical(self, tmp_path, monkeypatch):
        """No macro dir → ordering identical to bot-only (fail-open invariant P2)."""
        self._present(monkeypatch, 3, 5)
        # slot 3 higher load than 5 → bot-only order is 5,3
        ledger = tmp_path / "data" / "metabolism" / "key_ledger.jsonl"
        for _ in range(3):
            _write_row(ledger, {"schema": "metabolism.key_ledger.v1",
                                "ts": _ts(_now() - timedelta(minutes=5)),
                                "key_id": "claude_code_oauth_3", "cycle_id": "",
                                "stage": "pm", "est_tokens": 1, "outcome": "ok"})
        health = self.kr._macro_key_health(root=tmp_path)
        assert health["fresh"] is False
        cands = self.kr.candidates(root=tmp_path)
        assert [c["key_id"] for c in cands] == ["claude_code_oauth_5", "claude_code_oauth_3"]

    def test_pct_weekly_ascending_order(self, tmp_path, monkeypatch):
        """Fresh macro: non-cooling keys order by pct_weekly ascending."""
        self._present(monkeypatch, 2, 3, 5)
        _write_macro_budget(tmp_path, {
            "5": {"pct_weekly": 12.0}, "2": {"pct_weekly": 45.0}, "3": {"pct_weekly": 70.0}})
        cands = self.kr.candidates(root=tmp_path)
        assert [c["key_id"] for c in cands] == [
            "claude_code_oauth_5", "claude_code_oauth_2", "claude_code_oauth_3"]

    def test_95pct_soft_demoted_not_dropped(self, tmp_path, monkeypatch):
        """A key at >=95% pct_weekly goes to the BACK of non-cooling but is NOT dropped."""
        self._present(monkeypatch, 3, 5)
        _write_macro_budget(tmp_path, {
            "3": {"pct_weekly": 97.0}, "5": {"pct_weekly": 50.0}})
        cands = self.kr.candidates(root=tmp_path)
        ids = [c["key_id"] for c in cands]
        assert ids == ["claude_code_oauth_5", "claude_code_oauth_3"]  # 3 demoted, still present

    def test_all_demoted_still_present(self, tmp_path, monkeypatch):
        """If ALL keys are >=95%, none is dropped — all still tried (soft demotion)."""
        self._present(monkeypatch, 3, 5)
        _write_macro_budget(tmp_path, {
            "3": {"pct_weekly": 99.0}, "5": {"pct_weekly": 96.0}})
        cands = self.kr.candidates(root=tmp_path)
        assert set(c["key_id"] for c in cands) == {"claude_code_oauth_3", "claude_code_oauth_5"}

    def test_auth_dead_dropped(self, tmp_path, monkeypatch):
        """A key with a recent uncleared macro auth-death is DROPPED from candidates."""
        self._present(monkeypatch, 3, 4, 5)
        _write_macro_budget(tmp_path, {
            "3": {"pct_weekly": 10.0}, "4": {"pct_weekly": 10.0}, "5": {"pct_weekly": 10.0}})
        _write_macro_ledger(tmp_path, [{
            "schema": "metabolism.key_ledger.v1", "ts": _ts(_now() - timedelta(hours=3)),
            "key_id": "4", "stage": "cooling", "outcome": "auth_failed",
            "cool_kind": "auth", "reset_hint": _ts(_now() + timedelta(hours=24))}])
        cands = self.kr.candidates(root=tmp_path)
        ids = [c["key_id"] for c in cands]
        assert "claude_code_oauth_4" not in ids
        assert "claude_code_oauth_3" in ids and "claude_code_oauth_5" in ids

    def test_auth_dead_cleared_by_later_ok_not_dropped(self, tmp_path, monkeypatch):
        """A later macro ok row after the auth-death → NOT dropped (recovered server-side)."""
        self._present(monkeypatch, 4, 5)
        _write_macro_budget(tmp_path, {"4": {"pct_weekly": 10.0}, "5": {"pct_weekly": 20.0}})
        _write_macro_ledger(tmp_path, [
            {"schema": "metabolism.key_ledger.v1", "ts": _ts(_now() - timedelta(hours=5)),
             "key_id": "4", "stage": "cooling", "outcome": "auth_failed", "cool_kind": "auth",
             "reset_hint": _ts(_now() + timedelta(hours=24))},
            {"schema": "metabolism.key_ledger.v1", "ts": _ts(_now() - timedelta(hours=1)),
             "key_id": "4", "stage": "pm", "est_tokens": 1, "outcome": "ok"}])
        cands = self.kr.candidates(root=tmp_path)
        assert "claude_code_oauth_4" in [c["key_id"] for c in cands]

    def test_stale_auth_death_ignored(self, tmp_path, monkeypatch):
        """An auth-death older than 7d is NOT trusted to drop the key (may be rotated since)."""
        self._present(monkeypatch, 4, 5)
        _write_macro_budget(tmp_path, {"4": {"pct_weekly": 10.0}, "5": {"pct_weekly": 20.0}})
        _write_macro_ledger(tmp_path, [{
            "schema": "metabolism.key_ledger.v1", "ts": _ts(_now() - timedelta(days=9)),
            "key_id": "4", "stage": "cooling", "outcome": "auth_failed",
            "cool_kind": "auth", "reset_hint": _ts(_now() - timedelta(days=8))}])
        cands = self.kr.candidates(root=tmp_path)
        assert "claude_code_oauth_4" in [c["key_id"] for c in cands]

    def test_stale_budget_fail_open(self, tmp_path, monkeypatch):
        """A budget file with ts older than 36h is ignored entirely (fail-open)."""
        self._present(monkeypatch, 3, 5)
        _write_macro_budget(tmp_path, {"3": {"pct_weekly": 99.0}, "5": {"pct_weekly": 1.0}},
                            ts=_now() - timedelta(hours=40))
        health = self.kr._macro_key_health(root=tmp_path)
        assert health["fresh"] is False
        # ordering falls back to bot heuristic (equal load → slot order 3,5)
        cands = self.kr.candidates(root=tmp_path)
        assert [c["key_id"] for c in cands] == ["claude_code_oauth_3", "claude_code_oauth_5"]

    def test_auth_dead_never_strands_pool(self, tmp_path, monkeypatch):
        """If EVERY present key is macro-auth-dead, keep them all (never strand the loop)."""
        self._present(monkeypatch, 3, 5)
        _write_macro_budget(tmp_path, {"3": {"pct_weekly": 10.0}, "5": {"pct_weekly": 10.0}})
        _write_macro_ledger(tmp_path, [
            {"schema": "metabolism.key_ledger.v1", "ts": _ts(_now() - timedelta(hours=2)),
             "key_id": "3", "stage": "cooling", "outcome": "auth_failed", "cool_kind": "auth",
             "reset_hint": _ts(_now() + timedelta(hours=24))},
            {"schema": "metabolism.key_ledger.v1", "ts": _ts(_now() - timedelta(hours=2)),
             "key_id": "5", "stage": "cooling", "outcome": "auth_failed", "cool_kind": "auth",
             "reset_hint": _ts(_now() + timedelta(hours=24))}])
        cands = self.kr.candidates(root=tmp_path)
        assert set(c["key_id"] for c in cands) == {"claude_code_oauth_3", "claude_code_oauth_5"}

    def test_federation_never_adds_or_uncools(self, tmp_path, monkeypatch):
        """Federation cannot add a key absent from env, nor un-cool a bot-cooling key."""
        self._present(monkeypatch, 3)  # only slot 3 present in env
        # macro claims slot 6 is pristine — but it's not in env, so it must NOT appear.
        _write_macro_budget(tmp_path, {"3": {"pct_weekly": 50.0}, "6": {"pct_weekly": 1.0}})
        # slot 3 is bot-side cooling (window) — macro data must not un-cool it.
        _write_row(tmp_path / "data" / "metabolism" / "key_ledger.jsonl", {
            "schema": "metabolism.key_ledger.v1", "ts": _ts(_now()),
            "key_id": "claude_code_oauth_3", "cycle_id": "", "stage": "cooling",
            "est_tokens": 0, "outcome": "rate_limited", "cool_kind": "window",
            "reset_hint": _ts(_now() + timedelta(hours=4))})
        cands = self.kr.candidates(root=tmp_path)
        ids = [c["key_id"] for c in cands]
        assert "claude_code_oauth_6" not in ids  # never added
        assert ids == ["claude_code_oauth_3"]     # present, still cooling
        assert cands[0]["cooling"] is True         # not un-cooled


# ---------------------------------------------------------------------------
# 8. Bot key-pool status writer (FEDERATION UP) — mastermind.key_pool_status.v1
# ---------------------------------------------------------------------------

class TestPoolStatusWriter:
    @pytest.fixture(autouse=True)
    def _import(self):
        from brain import key_rotor as _kr
        self.kr = _kr

    def _present(self, monkeypatch, *slots: int):
        monkeypatch.delenv("METAB_KEYS_ENABLED", raising=False)
        for n in range(1, 8):
            monkeypatch.delenv(f"CLAUDE_CODE_OAUTH_TOKEN_{n}", raising=False)
        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
        for n in slots:
            monkeypatch.setenv(f"CLAUDE_CODE_OAUTH_TOKEN_{n}", f"tok{n}")

    def test_view_exact_schema(self, tmp_path, monkeypatch):
        """pool_status_view returns exactly {schema, ts, keys[]} with the exact key fields."""
        self._present(monkeypatch, 3, 5)
        view = self.kr.pool_status_view(root=tmp_path)
        assert view["schema"] == "mastermind.key_pool_status.v1"
        assert set(view.keys()) == {"schema", "ts", "keys"}
        assert len(view["keys"]) == 2
        for k in view["keys"]:
            assert set(k.keys()) == {
                "key_id", "enabled", "cooling", "cool_kind",
                "reset_hint", "last_outcome", "last_ts"}

    def test_view_reports_cooling_fields(self, tmp_path, monkeypatch):
        """A cooling key reports cooling=True + its cool_kind + reset_hint + last_outcome."""
        self._present(monkeypatch, 3, 5)
        rh = _ts(_now() + timedelta(hours=4))
        _write_row(tmp_path / "data" / "metabolism" / "key_ledger.jsonl", {
            "schema": "metabolism.key_ledger.v1", "ts": _ts(_now()),
            "key_id": "claude_code_oauth_3", "cycle_id": "", "stage": "cooling",
            "est_tokens": 0, "outcome": "rate_limited", "cool_kind": "weekly",
            "reset_hint": rh})
        view = self.kr.pool_status_view(root=tmp_path)
        by_id = {k["key_id"]: k for k in view["keys"]}
        assert by_id["claude_code_oauth_3"]["cooling"] is True
        assert by_id["claude_code_oauth_3"]["cool_kind"] == "weekly"
        assert by_id["claude_code_oauth_3"]["reset_hint"] == rh
        assert by_id["claude_code_oauth_3"]["last_outcome"] == "rate_limited"
        assert by_id["claude_code_oauth_5"]["cooling"] is False
        assert by_id["claude_code_oauth_5"]["cool_kind"] is None

    def test_enabled_flag_reflects_metab_keys_enabled(self, tmp_path, monkeypatch):
        """enabled reflects METAB_KEYS_ENABLED membership."""
        self._present(monkeypatch, 3, 5)
        monkeypatch.setenv("METAB_KEYS_ENABLED", "5")
        view = self.kr.pool_status_view(root=tmp_path)
        by_id = {k["key_id"]: k for k in view["keys"]}
        assert by_id["claude_code_oauth_5"]["enabled"] is True
        assert by_id["claude_code_oauth_3"]["enabled"] is False

    def test_writer_atomic_and_parseable(self, tmp_path, monkeypatch):
        """write_pool_status writes a parseable file at the default rel path; no temp left behind."""
        self._present(monkeypatch, 3)
        out = self.kr.write_pool_status(root=tmp_path)
        assert out is not None and out.exists()
        assert out == tmp_path / "data" / "mastermind" / "key_pool_status.json"
        doc = json.loads(out.read_text())
        assert doc["schema"] == "mastermind.key_pool_status.v1"
        # no stray temp file
        assert not (out.parent / f".{out.name}.tmp").exists()

    def test_legacy_only_pool_view(self, tmp_path, monkeypatch):
        """When only the legacy bare token is present, the view lists 'legacy'."""
        for n in range(1, 8):
            monkeypatch.delenv(f"CLAUDE_CODE_OAUTH_TOKEN_{n}", raising=False)
        monkeypatch.delenv("METAB_KEYS_ENABLED", raising=False)
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok_legacy")
        view = self.kr.pool_status_view(root=tmp_path)
        assert [k["key_id"] for k in view["keys"]] == ["legacy"]
