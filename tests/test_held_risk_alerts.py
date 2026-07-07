"""Tests for portfolio.held_risk_alerts (W3 alert governor).

Coverage:
- Transition-based alerting: role worsened OR new elevated lane → alert fires
- No alert when role unchanged and no new elevated lanes
- Cooldown suppression: within cooldown period → no second alert
- Cooldown expiry: after cooldown sessions → alert re-fires
- Dedup: duplicate alert_id (same ticker/role/date/lanes) → not re-appended
- PRD-R4: no advice verbs in headline
- PRD-R7: no position values (shares, entry_price, entry_date) in alert record
- run_alerts returns list of fired alert dicts

All tests are OFFLINE: no network calls, no Discord calls.
JSONL appends are patched out for most tests (no filesystem needed).
"""
from __future__ import annotations

import json
import re
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers: synthetic state builders
# ---------------------------------------------------------------------------

_ROLE_ORDER = ["ok", "info", "monitor", "review", "tighten", "trim_review", "exit_review"]


def _pos(ticker: str, role: str, lanes: dict | None = None) -> dict:
    """Build a minimal position entry for risk_state."""
    return {
        "position_id": f"pos-{ticker.lower()}",
        "ticker": ticker,
        "role": role,
        "role_label": role.replace("_", " ").title(),
        "elevated_lanes": sum(1 for v in (lanes or {}).values() if v.get("state") == "elevated"),
        "lane_total": 8,
        "lanes": lanes or {},
        "path": {"current_role": role, "sessions_at_role": 1},
        "personality": {"archetype": "growth", "dna_class": "large", "current_mode": None},
        "events": {},
    }


def _state(positions: list[dict], asof: str = "2026-07-07") -> dict:
    return {
        "schema": "portfolio_risk_state.v1",
        "asof": asof,
        "generated_at": f"{asof}T12:00:00Z",
        "market": {},
        "positions": positions,
    }


def _lane(st: str) -> dict:
    return {"state": st, "flags": [], "reasons": [], "asof": "2026-07-07"}


# ---------------------------------------------------------------------------
# run_alerts interface
# ---------------------------------------------------------------------------

def _patch_alert_paths(tmp_path):
    """Context manager that patches alert module paths to tmp_path."""
    import portfolio.held_risk_alerts as hra
    from contextlib import ExitStack
    stack = ExitStack()
    stack.enter_context(patch.object(hra, "_ALERTS_LOG_PATH", tmp_path / "alerts.jsonl"))
    stack.enter_context(patch.object(hra, "_ALERT_STATE_PATH", tmp_path / "alert_state.json"))
    stack.enter_context(patch("portfolio.held_risk_alerts._send_discord", return_value=None))
    return stack


def test_run_alerts_no_prev_no_elevated_no_alert(tmp_path):
    """First run with all-ok roles → no alerts fired."""
    import portfolio.held_risk_alerts as hra
    with patch.object(hra, "_ALERTS_LOG_PATH", tmp_path / "alerts.jsonl"), \
         patch.object(hra, "_ALERT_STATE_PATH", tmp_path / "alert_state.json"), \
         patch.object(hra, "_send_discord", return_value=None):
        pos = _pos("AAPL", "ok")
        new_state = _state([pos])
        fired = hra.run_alerts(None, new_state, today=date(2026, 7, 7))
        assert fired == []


def test_run_alerts_role_worsened_fires_alert(tmp_path):
    """Role worsening ok → monitor triggers alert."""
    import portfolio.held_risk_alerts as hra
    with patch.object(hra, "_ALERTS_LOG_PATH", tmp_path / "alerts.jsonl"), \
         patch.object(hra, "_ALERT_STATE_PATH", tmp_path / "alert_state.json"), \
         patch.object(hra, "_send_discord", return_value=None):
        prev = _state([_pos("AAPL", "ok")])
        new = _state([_pos("AAPL", "monitor",
                            {"macro_sensitivity": _lane("elevated")})])
        fired = hra.run_alerts(prev, new, today=date(2026, 7, 7))
        assert len(fired) == 1
        assert fired[0]["ticker"] == "AAPL"
        assert fired[0]["type"] == "monitor"


def test_run_alerts_new_elevated_lane_fires_alert(tmp_path):
    """No role change but new elevated lane → alert fires."""
    import portfolio.held_risk_alerts as hra
    with patch.object(hra, "_ALERTS_LOG_PATH", tmp_path / "alerts.jsonl"), \
         patch.object(hra, "_ALERT_STATE_PATH", tmp_path / "alert_state.json"), \
         patch.object(hra, "_send_discord", return_value=None):
        prev = _state([_pos("NVDA", "monitor",
                             {"macro_sensitivity": _lane("elevated")})])
        new_lanes = {
            "macro_sensitivity": _lane("elevated"),
            "ownership_flow": _lane("elevated"),  # new elevated lane
        }
        new = _state([_pos("NVDA", "monitor", new_lanes)])
        fired = hra.run_alerts(prev, new, today=date(2026, 7, 7))
        assert len(fired) == 1
        assert "NVDA" in fired[0]["ticker"]


def test_run_alerts_role_same_no_new_lanes_no_alert(tmp_path):
    """Same role, same elevated lanes → no alert."""
    import portfolio.held_risk_alerts as hra
    with patch.object(hra, "_ALERTS_LOG_PATH", tmp_path / "alerts.jsonl"), \
         patch.object(hra, "_ALERT_STATE_PATH", tmp_path / "alert_state.json"), \
         patch.object(hra, "_send_discord", return_value=None):
        lanes = {"macro_sensitivity": _lane("elevated")}
        prev = _state([_pos("MSFT", "monitor", lanes)])
        new = _state([_pos("MSFT", "monitor", lanes)])
        fired = hra.run_alerts(prev, new, today=date(2026, 7, 8))
        assert fired == []


def test_run_alerts_multiple_tickers(tmp_path):
    """Two tickers: one fires, one doesn't."""
    import portfolio.held_risk_alerts as hra
    with patch.object(hra, "_ALERTS_LOG_PATH", tmp_path / "alerts.jsonl"), \
         patch.object(hra, "_ALERT_STATE_PATH", tmp_path / "alert_state.json"), \
         patch.object(hra, "_send_discord", return_value=None):
        prev = _state([
            _pos("AAPL", "ok"),
            _pos("MSFT", "monitor", {"macro_sensitivity": _lane("elevated")}),
        ])
        new = _state([
            _pos("AAPL", "review", {"price_trend": _lane("elevated"),
                                     "ownership_flow": _lane("elevated")}),
            _pos("MSFT", "monitor", {"macro_sensitivity": _lane("elevated")}),  # unchanged
        ])
        fired = hra.run_alerts(prev, new, today=date(2026, 7, 7))
        tickers = [f["ticker"] for f in fired]
        assert "AAPL" in tickers
        assert "MSFT" not in tickers


# ---------------------------------------------------------------------------
# Cooldown suppression
# ---------------------------------------------------------------------------

def test_cooldown_suppresses_repeat_alert(tmp_path):
    """Same role/ticker within cooldown window → second run produces no alert."""
    import portfolio.held_risk_alerts as hra
    with patch.object(hra, "_ALERTS_LOG_PATH", tmp_path / "alerts.jsonl"), \
         patch.object(hra, "_ALERT_STATE_PATH", tmp_path / "alert_state.json"), \
         patch.object(hra, "_send_discord", return_value=None):
        # First run: ok → monitor
        prev = _state([_pos("TST", "ok")])
        new = _state([_pos("TST", "monitor", {"macro_sensitivity": _lane("elevated")})])
        fired1 = hra.run_alerts(prev, new, today=date(2026, 7, 7))
        assert len(fired1) == 1

        # Second run same day / next session: role same → cooldown suppresses
        fired2 = hra.run_alerts(new, new, today=date(2026, 7, 8))
        assert fired2 == [], f"expected suppressed, got: {fired2}"


def test_cooldown_expiry_re_fires(tmp_path):
    """Alert re-fires after cooldown sessions have elapsed."""
    import portfolio.held_risk_alerts as hra

    monitor_cooldown = hra._COOLDOWNS.get("monitor", 5)

    with patch.object(hra, "_ALERTS_LOG_PATH", tmp_path / "alerts.jsonl"), \
         patch.object(hra, "_ALERT_STATE_PATH", tmp_path / "alert_state.json"), \
         patch.object(hra, "_send_discord", return_value=None):
        # First run: fire
        prev = _state([_pos("TST2", "ok")])
        new = _state([_pos("TST2", "monitor", {"macro_sensitivity": _lane("elevated")})])
        fired1 = hra.run_alerts(prev, new, today=date(2026, 7, 7))
        assert len(fired1) == 1

        # Manually advance sessions_since past cooldown in the saved state
        alert_state_path = tmp_path / "alert_state.json"
        if alert_state_path.exists():
            state_data = json.loads(alert_state_path.read_text())
            cd = state_data.get("cooldown", {}).get("TST2", {})
            if cd:
                cd["sessions_since"] = monitor_cooldown + 10
            alert_state_path.write_text(json.dumps(state_data))

        future_date = date(2026, 7, 7) + timedelta(weeks=2)
        # Adding a NEW elevated lane to make the transition fire
        new_with_extra = _state([_pos("TST2", "monitor", {
            "macro_sensitivity": _lane("elevated"),
            "price_trend": _lane("elevated"),  # new lane
        })])
        fired2 = hra.run_alerts(new, new_with_extra, today=future_date)
        # After cooldown expiry AND new lane → should fire
        assert len(fired2) == 1, f"expected re-fire after cooldown expiry, got: {fired2}"

    # Sanity: cooldown constant is valid
    assert isinstance(monitor_cooldown, int) and monitor_cooldown >= 1


# ---------------------------------------------------------------------------
# JSONL dedup
# ---------------------------------------------------------------------------

def test_append_alert_dedup(tmp_path):
    """_append_alert with same alert_id → returns False on second call (dedup)."""
    import portfolio.held_risk_alerts as hra
    alerts_path = tmp_path / "alerts.jsonl"

    with patch.object(hra, "_ALERTS_LOG_PATH", alerts_path):
        record = {
            "alert_id": "AAPL-monitor-2026-07-07-abc",
            "ticker": "AAPL",
            "type": "monitor",
            "headline": "Test alert",
            "ts": "2026-07-07",
            "lanes": ["macro_sensitivity"],
        }
        result1 = hra._append_alert(record)
        result2 = hra._append_alert(record)
        assert result1 is True, "first append should succeed"
        assert result2 is False, "second append of same alert_id should be deduped"

    # Verify only one line in file
    lines = [l for l in alerts_path.read_text().splitlines() if l.strip()]
    assert len(lines) == 1, f"expected 1 line, got {len(lines)}"


# ---------------------------------------------------------------------------
# PRD-R4: no advice verbs in headline
# ---------------------------------------------------------------------------

def test_no_advice_verbs_in_headline():
    """_build_headline must not include advice verbs (buy/sell/add/trim/exit/close)."""
    from portfolio.held_risk_alerts import _build_headline, _ADVICE_VERB_RE

    # _build_headline takes alert_date as a string
    headline = _build_headline("AAPL", "review", ["price_trend", "ownership_flow"],
                               ["below MA50", "insider sellers"], "2026-07-07")
    # Check regex doesn't match any verbs
    matches = _ADVICE_VERB_RE.findall(headline)
    assert matches == [], f"advice verbs found in headline: {matches!r}\nheadline: {headline!r}"


def test_advice_verb_regex_strips_common_verbs():
    """_ADVICE_VERB_RE matches typical advice verbs."""
    from portfolio.held_risk_alerts import _ADVICE_VERB_RE

    for verb in ("buy", "sell", "add", "trim", "purchase", "exit", "close", "short", "long"):
        assert _ADVICE_VERB_RE.search(verb), f"regex should match {verb!r}"


# ---------------------------------------------------------------------------
# PRD-R7: no position values in alert record
# ---------------------------------------------------------------------------

def test_no_position_values_in_alert_record(tmp_path):
    """Alert records must not contain shares, entry_price, entry_date."""
    import portfolio.held_risk_alerts as hra
    with patch.object(hra, "_ALERTS_LOG_PATH", tmp_path / "alerts.jsonl"), \
         patch.object(hra, "_ALERT_STATE_PATH", tmp_path / "alert_state.json"), \
         patch.object(hra, "_send_discord", return_value=None):
        prev = _state([_pos("TSLA", "ok")])
        new = _state([_pos("TSLA", "review", {
            "price_trend": _lane("elevated"),
            "ownership_flow": _lane("elevated"),
        })])
        fired = hra.run_alerts(prev, new, today=date(2026, 7, 7))
        assert len(fired) >= 1, "expected at least one alert"
        for alert in fired:
            assert "shares" not in alert, "PRD-R7: shares must not be in alert"
            assert "entry_price" not in alert, "PRD-R7: entry_price must not be in alert"
            assert "entry_date" not in alert, "PRD-R7: entry_date must not be in alert"
            # Also check headline string
            headline = alert.get("headline", "")
            for forbidden in ("shares", "entry_price", "entry_date", "entry price"):
                assert forbidden not in headline.lower(), \
                    f"PRD-R7 violation: {forbidden!r} found in headline: {headline!r}"


# ---------------------------------------------------------------------------
# _elevated_lanes helper
# ---------------------------------------------------------------------------

def test_elevated_lanes_returns_set_of_names():
    """_elevated_lanes returns a set of lane names with state == 'elevated'."""
    from portfolio.held_risk_alerts import _elevated_lanes

    pos = _pos("X", "monitor", {
        "price_trend": _lane("elevated"),
        "macro_sensitivity": _lane("elevated"),
        "ownership_flow": _lane("ok"),
    })
    result = _elevated_lanes(pos)
    assert "price_trend" in result
    assert "macro_sensitivity" in result
    assert "ownership_flow" not in result


def test_elevated_lanes_empty_when_all_ok():
    """_elevated_lanes returns empty set when all lanes are ok."""
    from portfolio.held_risk_alerts import _elevated_lanes

    pos = _pos("Y", "ok", {"price_trend": _lane("ok"), "ownership_flow": _lane("ok")})
    result = _elevated_lanes(pos)
    assert result == set()


# ---------------------------------------------------------------------------
# _make_alert_id determinism
# ---------------------------------------------------------------------------

def test_make_alert_id_deterministic():
    """Same inputs → same alert_id (deterministic for dedup)."""
    from portfolio.held_risk_alerts import _make_alert_id

    # _make_alert_id takes alert_date as a str
    id1 = _make_alert_id("AAPL", "monitor", "2026-07-07", ["macro_sensitivity"])
    id2 = _make_alert_id("AAPL", "monitor", "2026-07-07", ["macro_sensitivity"])
    assert id1 == id2


def test_make_alert_id_differs_on_lane_change():
    """Different lanes → different alert_id."""
    from portfolio.held_risk_alerts import _make_alert_id

    id1 = _make_alert_id("AAPL", "monitor", "2026-07-07", ["macro_sensitivity"])
    id2 = _make_alert_id("AAPL", "monitor", "2026-07-07", ["price_trend"])
    assert id1 != id2


def test_make_alert_id_differs_on_role_change():
    """Different roles → different alert_id."""
    from portfolio.held_risk_alerts import _make_alert_id

    id1 = _make_alert_id("AAPL", "monitor", "2026-07-07", ["macro_sensitivity"])
    id2 = _make_alert_id("AAPL", "review", "2026-07-07", ["macro_sensitivity"])
    assert id1 != id2


# ---------------------------------------------------------------------------
# _send_discord fail-soft
# ---------------------------------------------------------------------------

def test_send_discord_does_not_raise_on_error():
    """_send_discord must not propagate exceptions (best-effort)."""
    from portfolio.held_risk_alerts import _send_discord

    with patch("portfolio.held_risk_alerts.os.environ.get", return_value="http://fake-webhook.test"), \
         patch("portfolio.held_risk_alerts.urllib.request.urlopen",
               side_effect=Exception("network error")):
        # Must not raise
        _send_discord("Test headline")


def test_send_discord_no_op_without_webhook():
    """_send_discord exits silently when no webhook env var is set."""
    from portfolio.held_risk_alerts import _send_discord

    with patch("portfolio.held_risk_alerts.os.environ.get", return_value=None):
        # Must not raise
        _send_discord("Test headline")


# ---------------------------------------------------------------------------
# Role order constants
# ---------------------------------------------------------------------------

def test_role_order_is_ascending():
    """ROLE_ORDER must go from ok (least severe) to exit_review (most severe)."""
    from portfolio.held_risk_alerts import _ROLE_ORDER
    assert _ROLE_ORDER[0] == "ok"
    assert _ROLE_ORDER[-1] == "exit_review"
    assert "monitor" in _ROLE_ORDER
    assert "tighten" in _ROLE_ORDER


def test_role_cooldowns_exist_for_elevated_roles():
    """_COOLDOWNS must have entries for monitor, review, tighten, trim_review, exit_review."""
    from portfolio.held_risk_alerts import _COOLDOWNS
    for role in ("monitor", "review", "tighten", "trim_review", "exit_review"):
        assert role in _COOLDOWNS, f"missing cooldown for {role}"
        assert isinstance(_COOLDOWNS[role], int) and _COOLDOWNS[role] >= 1


# ---------------------------------------------------------------------------
# BLOCKING-1: Session-gated cooldown counter
# ---------------------------------------------------------------------------

def test_same_date_run_does_not_double_increment_sessions_since(tmp_path):
    """Two runs on the same date must advance sessions_since at most once.

    The second same-date run should NOT increment sessions_since again,
    because cooldown units are trading SESSIONS, not scheduler invocations.
    """
    import portfolio.held_risk_alerts as hra

    with patch.object(hra, "_ALERTS_LOG_PATH", tmp_path / "alerts.jsonl"), \
         patch.object(hra, "_ALERT_STATE_PATH", tmp_path / "alert_state.json"), \
         patch.object(hra, "_send_discord", return_value=None):

        # First run: ok → monitor → alert fires, sessions_since resets to 0
        prev = _state([_pos("B1", "ok")])
        new  = _state([_pos("B1", "monitor", {"macro_sensitivity": _lane("elevated")})])
        fired1 = hra.run_alerts(prev, new, today=date(2026, 7, 7))
        assert len(fired1) == 1, "first run should fire an alert"

        # Read sessions_since after the first run
        state_data1 = json.loads((tmp_path / "alert_state.json").read_text())
        ss_after_run1 = state_data1["cooldown"]["B1"]["sessions_since"]

        # Second run SAME date (simulates intraday scheduler firing again):
        # role unchanged + no new elevated lanes → no alert; but crucially
        # sessions_since must not increment (same date = same session)
        hra.run_alerts(new, new, today=date(2026, 7, 7))
        state_data2 = json.loads((tmp_path / "alert_state.json").read_text())
        ss_after_run2 = state_data2["cooldown"]["B1"]["sessions_since"]

        assert ss_after_run2 == ss_after_run1, (
            f"sessions_since incremented on same-date re-run: "
            f"{ss_after_run1} → {ss_after_run2}"
        )


def test_next_date_run_increments_sessions_since(tmp_path):
    """A run on a new date (different session) must advance sessions_since by 1."""
    import portfolio.held_risk_alerts as hra

    with patch.object(hra, "_ALERTS_LOG_PATH", tmp_path / "alerts.jsonl"), \
         patch.object(hra, "_ALERT_STATE_PATH", tmp_path / "alert_state.json"), \
         patch.object(hra, "_send_discord", return_value=None):

        # Run 1: fire an alert
        prev = _state([_pos("B2", "ok")])
        new  = _state([_pos("B2", "monitor", {"macro_sensitivity": _lane("elevated")})])
        hra.run_alerts(prev, new, today=date(2026, 7, 7))

        state_before = json.loads((tmp_path / "alert_state.json").read_text())
        ss_before = state_before["cooldown"]["B2"]["sessions_since"]

        # Run 2: next calendar date — should increment sessions_since
        hra.run_alerts(new, new, today=date(2026, 7, 8))

        state_after = json.loads((tmp_path / "alert_state.json").read_text())
        ss_after = state_after["cooldown"]["B2"]["sessions_since"]

        assert ss_after == ss_before + 1, (
            f"sessions_since should have incremented by 1 on new date: "
            f"{ss_before} → {ss_after}"
        )
