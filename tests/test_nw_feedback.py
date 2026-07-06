"""Tests for bridge/nw_feedback.py — NW feedback artifact v1.

Coverage:
  1. build() produces correct schema fields from fixture ledgers
  2. Counts are correct given known fixture inputs
  3. Public-surface constraint: no dollar amounts, no API-key-shaped strings, no secrets
  4. Never-raise contract: build() returns a valid payload even on broken inputs
  5. write() writes to the expected path
  6. scored_active enforcement in lenses._vol_regime_row
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

import bot  # noqa: F401  -> bootstrap

from bridge import nw_feedback


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_events(tmp_path: Path, rows: list[dict]) -> Path:
    """Write a run_events.jsonl to tmp_path and return its path."""
    p = tmp_path / "governance" / "run_events.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(r) for r in rows]
    p.write_text("\n".join(lines) + ("\n" if lines else ""))
    return p


def _patch_events(monkeypatch, tmp_path, rows):
    """Monkeypatch nw_feedback._run_events_path to point at a tmp file."""
    p = _make_events(tmp_path, rows)
    monkeypatch.setattr(nw_feedback, "_run_events_path", lambda: p)
    return p


# ---------------------------------------------------------------------------
# 1. Schema fields present
# ---------------------------------------------------------------------------

def test_build_schema_fields_present(monkeypatch, tmp_path):
    """build() returns all required top-level schema fields."""
    _patch_events(monkeypatch, tmp_path, [])
    result = nw_feedback.build()
    assert result["schema"] == "mastermind_nw_feedback.v1"
    assert "generated_at" in result
    assert "window_days" in result
    assert "thesis_counts" in result
    assert "run_counts" in result
    assert "books" in result
    assert isinstance(result["books"], list)


def test_build_thesis_counts_fields(monkeypatch, tmp_path):
    """thesis_counts has open/closed_or_rebuilt/total."""
    _patch_events(monkeypatch, tmp_path, [])
    result = nw_feedback.build()
    tc = result["thesis_counts"]
    assert "open" in tc
    assert "closed_or_rebuilt" in tc
    assert "total" in tc
    assert tc["open"] + tc["closed_or_rebuilt"] == tc["total"]


def test_build_book_entry_fields(monkeypatch, tmp_path):
    """Each book entry has the required governance fields."""
    _patch_events(monkeypatch, tmp_path, [])
    result = nw_feedback.build()
    for book in result["books"]:
        assert "book_id" in book
        assert "gate_failures" in book
        gf = book["gate_failures"]
        assert "by_severity" in gf
        assert "by_guard" in gf
        assert "total" in gf
        assert "rejected_decision_count" in book
        assert "lock_conflict_count" in book
        assert "stale_freeze_count" in book


# ---------------------------------------------------------------------------
# 2. Counts are correct
# ---------------------------------------------------------------------------

def test_gate_failure_counts(monkeypatch, tmp_path):
    """Gate failure counts aggregate correctly from fixture events."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    events = [
        {"ts": now, "kind": "guardrail", "book": "flagship", "status": "error",
         "severity": "ADVISORY_ONLY", "extra": {"guard": "dashboard_write"}},
        {"ts": now, "kind": "guardrail", "book": "flagship", "status": "error",
         "severity": "FREEZE", "extra": {"guard": "peer_freshness"}},
        {"ts": now, "kind": "guardrail", "book": "flagship", "status": "ok",
         "severity": "ADVISORY_ONLY", "extra": {"guard": "some_check"}},  # ok — NOT counted
        {"ts": now, "kind": "guardrail", "book": "heavyweight", "status": "error",
         "severity": "SHRINK", "extra": {"guard": "book_cap"}},
    ]
    _patch_events(monkeypatch, tmp_path, events)
    result = nw_feedback.build()
    books = {b["book_id"]: b for b in result["books"]}

    if "flagship" in books:
        gf = books["flagship"]["gate_failures"]
        assert gf["total"] == 2
        assert gf["by_severity"].get("ADVISORY_ONLY", 0) == 1
        assert gf["by_severity"].get("FREEZE", 0) == 1
        assert gf["by_guard"].get("dashboard_write", 0) == 1
        assert gf["by_guard"].get("peer_freshness", 0) == 1

    if "heavyweight" in books:
        gf = books["heavyweight"]["gate_failures"]
        assert gf["total"] == 1
        assert gf["by_severity"].get("SHRINK", 0) == 1


def test_lock_conflict_count(monkeypatch, tmp_path):
    """lock_conflict events counted correctly per book."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    events = [
        {"ts": now, "kind": "lock_conflict", "book": "flagship", "status": "error"},
        {"ts": now, "kind": "lock_conflict", "book": "flagship", "status": "error"},
        {"ts": now, "kind": "lock_conflict", "book": "autonomous", "status": "error"},
    ]
    _patch_events(monkeypatch, tmp_path, events)
    result = nw_feedback.build()
    books = {b["book_id"]: b for b in result["books"]}
    if "flagship" in books:
        assert books["flagship"]["lock_conflict_count"] == 2
    if "autonomous" in books:
        assert books["autonomous"]["lock_conflict_count"] == 1


def test_stale_freeze_count(monkeypatch, tmp_path):
    """FREEZE-severity events counted correctly per book."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    events = [
        {"ts": now, "kind": "guardrail", "book": "flagship", "status": "error",
         "severity": "FREEZE", "extra": {"guard": "peer_freshness"}},
        {"ts": now, "kind": "guardrail", "book": "flagship", "status": "error",
         "severity": "FREEZE", "extra": {"guard": "anchor_stale"}},
        {"ts": now, "kind": "guardrail", "book": "etf", "status": "error",
         "severity": "ADVISORY_ONLY", "extra": {"guard": "something"}},
    ]
    _patch_events(monkeypatch, tmp_path, events)
    result = nw_feedback.build()
    books = {b["book_id"]: b for b in result["books"]}
    if "flagship" in books:
        assert books["flagship"]["stale_freeze_count"] == 2
    if "etf" in books:
        assert books["etf"]["stale_freeze_count"] == 0


def test_rejected_count_is_integer(monkeypatch, tmp_path):
    """rejected_decision_count is always a non-negative integer."""
    _patch_events(monkeypatch, tmp_path, [])
    result = nw_feedback.build()
    for book in result["books"]:
        cnt = book["rejected_decision_count"]
        assert isinstance(cnt, int)
        assert cnt >= 0


# ---------------------------------------------------------------------------
# 3. Public-surface constraint: no secrets / dollar amounts
# ---------------------------------------------------------------------------

_SECRET_PATTERNS = [
    re.compile(r'\bMASTERMIND_[A-Z_]+\b'),
    re.compile(r'\$[\d,]+'),
    re.compile(r'(?i)\b[A-Za-z0-9+/]{40,}\b'),  # API-key-shaped long tokens
]


def _flatten_strings(obj) -> list[str]:
    """Recursively collect all string values from a nested dict/list structure."""
    result = []
    if isinstance(obj, str):
        result.append(obj)
    elif isinstance(obj, dict):
        for v in obj.values():
            result.extend(_flatten_strings(v))
    elif isinstance(obj, list):
        for item in obj:
            result.extend(_flatten_strings(item))
    return result


def test_no_secrets_in_output(monkeypatch, tmp_path):
    """The built payload must not contain secrets, dollar amounts, or env var names."""
    _patch_events(monkeypatch, tmp_path, [])
    result = nw_feedback.build()
    serialized = json.dumps(result)

    for pat in _SECRET_PATTERNS:
        matches = pat.findall(serialized)
        assert not matches, (
            f"Secret-shaped value found in nw_feedback output (pattern {pat.pattern!r}): "
            f"{matches[:3]}"
        )


def test_no_dollar_amounts(monkeypatch, tmp_path):
    """Output must not contain dollar amounts (no position sizes in dollar terms)."""
    _patch_events(monkeypatch, tmp_path, [])
    result = nw_feedback.build()
    serialized = json.dumps(result)
    assert "$" not in serialized, (
        "Dollar sign found in nw_feedback output — position sizes must not be included."
    )


def test_no_mastermind_env_var_names(monkeypatch, tmp_path):
    """MASTERMIND_ env variable names must not appear in the output."""
    _patch_events(monkeypatch, tmp_path, [])
    result = nw_feedback.build()
    serialized = json.dumps(result)
    # MASTERMIND_VOL_REGIME_SCORED_GATE etc. must not appear as values
    # (they can appear as keys in internal dicts but must not leak as values)
    for s in _flatten_strings(result):
        assert not re.search(r'\bMASTERMIND_[A-Z_]+\b', s), (
            f"MASTERMIND_ env var name leaked into output string: {s!r}"
        )


# ---------------------------------------------------------------------------
# 4. Never-raise contract
# ---------------------------------------------------------------------------

def test_build_never_raises_on_missing_data(monkeypatch, tmp_path):
    """build() returns a valid payload even when all data is unavailable."""
    # Point events to a non-existent path
    monkeypatch.setattr(nw_feedback, "_run_events_path",
                        lambda: tmp_path / "nonexistent" / "run_events.jsonl")
    # Also break the registry import
    with patch("bridge.nw_feedback._thesis_counts", side_effect=RuntimeError("broken")):
        result = nw_feedback.build()
    assert result["schema"] == "mastermind_nw_feedback.v1"
    assert "books" in result


def test_build_never_raises_on_corrupt_events(monkeypatch, tmp_path):
    """build() survives corrupt/malformed event lines."""
    p = tmp_path / "governance" / "run_events.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("not json\n{broken\n{}\n")
    monkeypatch.setattr(nw_feedback, "_run_events_path", lambda: p)
    result = nw_feedback.build()
    assert result["schema"] == "mastermind_nw_feedback.v1"


def test_write_returns_path(monkeypatch, tmp_path):
    """write() writes the JSON file and returns the path."""
    monkeypatch.setattr(nw_feedback, "_run_events_path",
                        lambda: tmp_path / "nonexistent" / "run_events.jsonl")
    dest = tmp_path / "site"
    out = nw_feedback.write(dest)
    assert out == dest / "mastermind" / "nw_feedback.json"
    assert out.exists()
    payload = json.loads(out.read_text())
    assert payload["schema"] == "mastermind_nw_feedback.v1"


# ---------------------------------------------------------------------------
# 5. scored_active enforcement in lenses._vol_regime_row
# ---------------------------------------------------------------------------

def test_vol_regime_unscored_suppressed_from_sizing(monkeypatch):
    """When scored_active=False, vol_regime direction must be 'neutral' (not 'bear')."""
    from portfolio import lenses

    # Inject a risk-off vol file with scored_active=False
    fake_vol = {
        "regime": "warning",       # would be risk_off=True without the gate
        "kill_switch": False,
        "vol_target_scalar": 0.8,
        "scored_active": False,    # NOT validated — must not tighten sizing
        "scored_score": 0.4,
        "ts_slope_state": "warning",
        "fragility_confluence": 0.6,
    }

    def patched_load(rel):
        if "vol" in rel:
            return fake_vol
        return None

    monkeypatch.setattr(lenses, "_load", patched_load)
    monkeypatch.delenv("MASTERMIND_VOL_REGIME_SCORED_GATE", raising=False)

    row = lenses._vol_regime_row()
    assert row["direction"] == "neutral", (
        f"scored_active=False with risk-off regime must produce direction='neutral' "
        f"(not 'bear'), got {row['direction']!r}. "
        "This is docket F7 — unscored vol data must not tighten sizing."
    )
    # The value dict must still carry the raw reading for display
    assert row["value"]["regime"] == "warning"
    assert row["value"]["scored_active"] is False
    assert row["value"]["tier_enforced"] is True


def test_vol_regime_scored_active_true_votes_bear_when_risk_off(monkeypatch):
    """When scored_active=True and risk-off, vol_regime votes 'bear' normally."""
    from portfolio import lenses

    fake_vol = {
        "regime": "warning",
        "kill_switch": False,
        "vol_target_scalar": 0.8,
        "scored_active": True,     # validated — allowed to affect sizing
    }

    def patched_load(rel):
        if "vol" in rel:
            return fake_vol
        return None

    monkeypatch.setattr(lenses, "_load", patched_load)
    monkeypatch.delenv("MASTERMIND_VOL_REGIME_SCORED_GATE", raising=False)

    row = lenses._vol_regime_row()
    assert row["direction"] == "bear"


def test_vol_regime_scored_active_true_neutral_when_not_risk_off(monkeypatch):
    """When scored_active=True and not risk-off, vol_regime is 'neutral'."""
    from portfolio import lenses

    fake_vol = {
        "regime": "normal",
        "kill_switch": False,
        "vol_target_scalar": 1.0,
        "scored_active": True,
    }

    def patched_load(rel):
        if "vol" in rel:
            return fake_vol
        return None

    monkeypatch.setattr(lenses, "_load", patched_load)
    monkeypatch.delenv("MASTERMIND_VOL_REGIME_SCORED_GATE", raising=False)

    row = lenses._vol_regime_row()
    assert row["direction"] == "neutral"


def test_vol_regime_kill_switch_bypasses_scored_active_gate(monkeypatch):
    """MASTERMIND_VOL_REGIME_SCORED_GATE=0 bypasses the scored_active enforcement."""
    from portfolio import lenses

    fake_vol = {
        "regime": "warning",   # risk_off=True
        "kill_switch": False,
        "vol_target_scalar": 0.8,
        "scored_active": False,  # NOT validated, but kill-switch overrides gate
    }

    def patched_load(rel):
        if "vol" in rel:
            return fake_vol
        return None

    monkeypatch.setattr(lenses, "_load", patched_load)
    monkeypatch.setenv("MASTERMIND_VOL_REGIME_SCORED_GATE", "0")

    row = lenses._vol_regime_row()
    # With gate disabled, scored_active=False still uses raw direction
    assert row["direction"] == "bear"


def test_vol_regime_missing_file_returns_missing_row(monkeypatch):
    """When vol file is absent, vol_regime row status is 'missing'."""
    from portfolio import lenses

    monkeypatch.setattr(lenses, "_load", lambda rel: None)
    monkeypatch.delenv("MASTERMIND_VOL_REGIME_SCORED_GATE", raising=False)

    row = lenses._vol_regime_row()
    assert row["status"] == "missing"
    assert row["direction"] is None
