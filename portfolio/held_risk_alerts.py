"""Alert governor for portfolio held-risk desk (W3).

Deterministic, pure-logic module: reads previous + new risk_state, fires on
TRANSITIONS only (role worsened OR new elevated lane joins), with per-role
cooldowns. Appends to alerts.jsonl; sends Discord; no advice verbs.

PRD-R4: review language only; no advice verbs (buy/sell/add/trim as imperatives).
PRD-R7: no shares/notional/entry price in alert text.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import tempfile
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger("mastermind.pfolio.alerts")

_REPO_ROOT = Path(__file__).resolve().parent.parent
_ALERT_STATE_PATH = _REPO_ROOT / "data" / "portfolio_watch" / "alert_state.json"
_ALERTS_LOG_PATH = _REPO_ROOT / "data" / "portfolio_watch" / "alerts.jsonl"

# Role ladder order (lower index = lower severity; worsening = higher index)
_ROLE_ORDER = ["ok", "info", "monitor", "review", "tighten", "trim_review", "exit_review"]

# Cooldowns in sessions per role
_COOLDOWNS: dict[str, int] = {
    "monitor": 5,
    "review": 3,
    "tighten": 2,
    "trim_review": 2,
    "exit_review": 1,
}

# Role display labels (PRD-R4)
_ROLE_LABELS = {
    "exit_review": "Exit Review",
    "trim_review": "Take-Profit Review",
    "tighten": "Tighten",
    "review": "Review",
    "monitor": "Monitor",
    "info": "Info",
    "ok": "Ok",
}

# Banned advice verbs regex (PRD-R4)
_ADVICE_VERB_RE = re.compile(
    r'\b(buy|sell|add|trim|purchase|exit|close|short|long)\b',
    re.IGNORECASE,
)


def _role_index(role: str) -> int:
    try:
        return _ROLE_ORDER.index(role)
    except ValueError:
        return 0


def _load_alert_state() -> dict:
    try:
        if _ALERT_STATE_PATH.exists():
            return json.loads(_ALERT_STATE_PATH.read_text())
    except Exception:
        pass
    return {"as_of": None, "cooldown": {}, "last_roles": {}, "last_lanes": {}}


def _save_alert_state(state: dict) -> None:
    try:
        _ALERT_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp_fd, tmp_path = tempfile.mkstemp(dir=_ALERT_STATE_PATH.parent, suffix=".tmp")
        with os.fdopen(tmp_fd, "w") as f:
            json.dump(state, f, indent=2, default=str)
        os.replace(tmp_path, _ALERT_STATE_PATH)
    except Exception as exc:
        log.warning("alert_state save failed: %s", exc)


def _append_alert(record: dict) -> bool:
    """Append alert record to alerts.jsonl. Returns True if newly written, False if deduplicated."""
    try:
        _ALERTS_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        alert_id = record.get("alert_id", "")
        if alert_id and _ALERTS_LOG_PATH.exists():
            existing = _ALERTS_LOG_PATH.read_text()
            if alert_id in existing:
                return False  # already logged
        with open(_ALERTS_LOG_PATH, "a") as f:
            f.write(json.dumps(record, default=str) + "\n")
        return True
    except Exception as exc:
        log.warning("alerts.jsonl append failed: %s", exc)
        return False


def _send_discord(headline: str) -> None:
    """POST alert headline to Discord webhook. Fail-soft."""
    webhook_url = (
        os.environ.get("DISCORD_WEBHOOK_PORTFOLIO")
        or os.environ.get("DISCORD_WEBHOOK_URL")
    )
    if not webhook_url:
        return
    try:
        payload = json.dumps({"content": headline[:1900]}).encode()
        req = urllib.request.Request(
            webhook_url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status >= 400:
                log.warning("Discord webhook returned %s", resp.status)
    except Exception as exc:
        log.warning("Discord send failed (fail-soft): %s", exc)


def _make_alert_id(ticker: str, role: str, alert_date: str, lane_names: list[str]) -> str:
    initials = "".join(sorted(n[:2] for n in lane_names)) if lane_names else "xx"
    return f"pfolio:{ticker}:{role}:{alert_date}:{initials}"


def _elevated_lanes(pos: dict) -> set[str]:
    """Return set of lane names currently elevated for a position."""
    lanes = pos.get("lanes") or {}
    return {name for name, lane in lanes.items() if lane.get("state") == "elevated"}


def _check_cooldown(ticker: str, role: str, alert_state: dict) -> bool:
    """Return True if alert should fire (not in cooldown), False if suppressed."""
    cooldown_sessions = _COOLDOWNS.get(role, 1)
    cd = alert_state.get("cooldown", {}).get(ticker, {})
    sessions_since = cd.get("sessions_since", 999)
    last_role = cd.get("role")

    if last_role == role and sessions_since < cooldown_sessions:
        return False
    return True


def _build_headline(ticker: str, role: str, lane_names: list[str], reasons: list[str], alert_date: str) -> str:
    """Build alert headline per PRD-R4 template. No advice verbs."""
    role_label = _ROLE_LABELS.get(role, role)
    pairs = []
    for name, reason in zip(lane_names[:3], reasons[:3]):
        if reason:
            pairs.append(f"{name}: {reason[:80]}")
        else:
            pairs.append(name)
    detail = "; ".join(pairs) if pairs else "see risk lanes"
    headline = f"PORTFOLIO · {ticker} {role_label} — {detail} ({alert_date})"
    # Verify no advice verbs (PRD-R4)
    if _ADVICE_VERB_RE.search(headline):
        headline = _ADVICE_VERB_RE.sub("[review]", headline)
    return headline


def run_alerts(prev_state: dict | None, new_state: dict, *, today: date | None = None) -> list[dict]:
    """
    Run alert governor: compare prev_state vs new_state, fire on transitions.

    Args:
        prev_state: previous portfolio_risk_state.v1 (None on first run)
        new_state: new portfolio_risk_state.v1
        today: run date (defaults to date.today())

    Returns:
        list of alert records fired this run.
    """
    today_d = today or date.today()
    today_str = today_d.isoformat()

    alert_state = _load_alert_state()
    fired: list[dict] = []

    # Advance sessions_since ONLY when the stored as_of date differs from today
    # (cooldowns are per trading SESSION, not per scheduler run).
    # Cooldown CHECKS still happen every run so intraday transitions can fire.
    last_as_of = alert_state.get("as_of")
    new_session = (last_as_of != today_str)
    if new_session:
        for ticker, cd in list(alert_state.get("cooldown", {}).items()):
            cd["sessions_since"] = cd.get("sessions_since", 0) + 1

    prev_positions: dict[str, dict] = {}
    if prev_state:
        for pos in (prev_state.get("positions") or []):
            t = (pos.get("ticker") or "").upper()
            if t:
                prev_positions[t] = pos

    for pos in (new_state.get("positions") or []):
        ticker = (pos.get("ticker") or "").upper()
        if not ticker:
            continue

        role = pos.get("role", "ok")
        elevated = _elevated_lanes(pos)
        prev_pos = prev_positions.get(ticker)
        prev_role = prev_pos.get("role", "ok") if prev_pos else "ok"
        prev_elevated = _elevated_lanes(prev_pos) if prev_pos else set()

        # Determine if we should fire
        role_worsened = _role_index(role) > _role_index(prev_role)
        new_lanes = elevated - prev_elevated
        same_role_new_lane = (role == prev_role) and bool(new_lanes) and _role_index(role) > _role_index("ok")

        should_fire = role_worsened or same_role_new_lane

        if not should_fire:
            continue

        # Only fire for non-trivial roles
        if role in ("ok", "info"):
            continue

        # Check cooldown
        if not _check_cooldown(ticker, role, alert_state):
            continue

        # Build alert
        active_lanes = sorted(elevated)
        reasons = []
        lanes_dict = pos.get("lanes") or {}
        for lane_name in active_lanes[:3]:
            lane = lanes_dict.get(lane_name, {})
            lane_reasons = lane.get("reasons") or []
            reasons.append(lane_reasons[0] if lane_reasons else "")

        alert_id = _make_alert_id(ticker, role, today_str, active_lanes)
        headline = _build_headline(ticker, role, active_lanes, reasons, today_str)

        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "source": "portfolio_desk",
            "type": role,
            "ticker": ticker,
            "position_id": str(pos.get("position_id", ticker)),
            "headline": headline,
            "detail": "; ".join(r for r in reasons[:5] if r),
            "lanes": active_lanes,
            "reasons": reasons,
            "alert_id": alert_id,
        }

        if _append_alert(record):
            _send_discord(headline)
            fired.append(record)

            # Update cooldown state
            if "cooldown" not in alert_state:
                alert_state["cooldown"] = {}
            alert_state["cooldown"][ticker] = {
                "role": role,
                "last_alert": today_str,
                "sessions_since": 0,
            }

    alert_state["as_of"] = today_str
    alert_state["last_roles"] = {
        (pos.get("ticker") or "").upper(): pos.get("role", "ok")
        for pos in (new_state.get("positions") or [])
        if (pos.get("ticker") or "").strip()
    }
    _save_alert_state(alert_state)

    return fired
