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
# 3b. Secret guard wire-up: write() must redact secret-shaped data
# ---------------------------------------------------------------------------

def test_write_redacts_mastermind_env_var_in_event_err(monkeypatch, tmp_path):
    """Secret guard: if an event carries a MASTERMIND_ env var name in its err/extra fields,
    write() must redact it in the published JSON.  The artifact must still be built (not aborted).

    Uses a fixture run-event whose err field contains 'MASTERMIND_PASSWORD=abc123' — a realistic
    credential-leak shape that could reach the artifact via error propagation.
    """
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    events = [
        # An error event whose err field contains a secret-shaped string
        {"ts": now, "kind": "guardrail", "book": "flagship", "status": "error",
         "severity": "ADVISORY_ONLY",
         "extra": {"guard": "auth_check", "err": "MASTERMIND_PASSWORD=abc123"}},
    ]
    _patch_events(monkeypatch, tmp_path, events)

    # Inject the secret into the payload via a patched _thesis_counts so it appears in output
    secret_val = "MASTERMIND_PASSWORD=abc123"
    original_thesis = nw_feedback._thesis_counts

    def patched_thesis():
        r = original_thesis()
        r["_test_injected_secret"] = secret_val  # simulate a leak path
        return r

    monkeypatch.setattr(nw_feedback, "_thesis_counts", patched_thesis)

    dest = tmp_path / "site"
    out = nw_feedback.write(dest)
    assert out.exists(), "write() must produce the artifact even with secret-shaped data"

    payload = json.loads(out.read_text())
    serialized = json.dumps(payload)

    # The secret must have been redacted
    assert secret_val not in serialized, (
        "Secret string was NOT redacted from the published JSON. "
        "write() must scan and redact via _redact_secrets() before writing."
    )
    # The artifact is valid — schema and books are present
    assert payload["schema"] == "mastermind_nw_feedback.v1"
    assert "books" in payload


def test_write_redacts_dollar_amount_in_payload(monkeypatch, tmp_path):
    """Secret guard: a $1,234,567 string (position-size shaped) must be redacted from write() output."""
    _patch_events(monkeypatch, tmp_path, [])
    dollar_val = "$1,234,567"
    original_thesis = nw_feedback._thesis_counts

    def patched_thesis():
        r = original_thesis()
        r["_test_injected_dollar"] = dollar_val
        return r

    monkeypatch.setattr(nw_feedback, "_thesis_counts", patched_thesis)

    dest = tmp_path / "site"
    out = nw_feedback.write(dest)
    payload = json.loads(out.read_text())
    serialized = json.dumps(payload)

    assert dollar_val not in serialized, (
        "Dollar-amount string was NOT redacted from the published JSON. "
        "write() must redact via _redact_secrets() before writing."
    )
    assert payload["schema"] == "mastermind_nw_feedback.v1"


def test_write_redacts_hex_token_in_payload(monkeypatch, tmp_path):
    """Secret guard: a 48-char hex token (API-key shaped) must be redacted from write() output."""
    _patch_events(monkeypatch, tmp_path, [])
    # 48-char hex token — matches the long-token pattern in _SECRET_PATTERNS
    hex_token = "a" * 48
    original_thesis = nw_feedback._thesis_counts

    def patched_thesis():
        r = original_thesis()
        r["_test_injected_token"] = hex_token
        return r

    monkeypatch.setattr(nw_feedback, "_thesis_counts", patched_thesis)

    dest = tmp_path / "site"
    out = nw_feedback.write(dest)
    payload = json.loads(out.read_text())
    serialized = json.dumps(payload)

    assert hex_token not in serialized, (
        "48-char hex token was NOT redacted from the published JSON. "
        "write() must redact via _redact_secrets() before writing."
    )
    assert payload["schema"] == "mastermind_nw_feedback.v1"


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

def test_vol_regime_unscored_bear_kept_asymmetric(monkeypatch):
    """Fable ruling 2026-07-06 — asymmetric enforcement: scored_active=False KEEPS 'bear'.

    Enforcement is asymmetric:
      • Tightening (bear) ALWAYS passes, even when scored_active=False.
        Unvalidated caution from a risk-off regime is safe (conservative direction).
      • Loosening (bull) is NEVER allowed from unvalidated data — suppressed to neutral.
        Since this lens is structurally subtract-only (never bull), the suppression is a
        pinned invariant (see test_vol_regime_subtract_only_structural_invariant in test_lenses.py).

    This test was previously asserting direction=='neutral' for scored_active=False (the old,
    symmetric suppress-both behavior).  That behavior has been reverted per the Fable ruling.
    """
    from portfolio import lenses

    # Inject a risk-off vol file with scored_active=False
    fake_vol = {
        "regime": "warning",       # risk_off=True
        "kill_switch": False,
        "vol_target_scalar": 0.8,
        "scored_active": False,    # display-only (not yet validated)
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
    # Asymmetric: tightening (bear) kept even when scored_active=False
    assert row["direction"] == "bear", (
        f"scored_active=False + risk-off must KEEP direction='bear' (asymmetric enforcement). "
        f"Tightening caution is safe regardless of validation tier. Got {row['direction']!r}."
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


# ---------------------------------------------------------------------------
# 6. by_guard / step key sanitization (Issue 5)
# ---------------------------------------------------------------------------

def test_guard_key_sanitization_length_cap(monkeypatch, tmp_path):
    """Guard keys longer than 64 chars must be truncated before appearing in the artifact."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    long_key = "a" * 80  # 80 chars — must be truncated to 64
    events = [
        {"ts": now, "kind": "guardrail", "book": "flagship", "status": "error",
         "severity": "ADVISORY_ONLY", "extra": {"guard": long_key}},
    ]
    _patch_events(monkeypatch, tmp_path, events)
    result = nw_feedback.build()
    books = {b["book_id"]: b for b in result["books"]}
    if "flagship" in books:
        guards = books["flagship"]["gate_failures"]["by_guard"]
        for key in guards:
            assert len(key) <= 64, (
                f"Guard key longer than 64 chars found in artifact: {key!r} ({len(key)} chars)"
            )


def test_guard_key_sanitization_charset(monkeypatch, tmp_path):
    """Guard keys with unsafe characters must be replaced with 'other'."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    events = [
        # Contains uppercase and spaces — unsafe for a public key
        {"ts": now, "kind": "guardrail", "book": "flagship", "status": "error",
         "severity": "ADVISORY_ONLY", "extra": {"guard": "Has UPPERCASE & spaces"}},
        # Normal safe key — must pass through
        {"ts": now, "kind": "guardrail", "book": "flagship", "status": "error",
         "severity": "ADVISORY_ONLY", "extra": {"guard": "safe_guard:v1"}},
    ]
    _patch_events(monkeypatch, tmp_path, events)
    result = nw_feedback.build()
    books = {b["book_id"]: b for b in result["books"]}
    if "flagship" in books:
        guards = books["flagship"]["gate_failures"]["by_guard"]
        # The unsafe key must have been replaced with 'other'
        assert "other" in guards, (
            f"Unsafe guard key was not replaced with 'other'. Found keys: {list(guards.keys())}"
        )
        # The safe key must pass through unchanged (after lowercasing)
        assert "safe_guard:v1" in guards, (
            f"Safe guard key was unexpectedly sanitized. Found keys: {list(guards.keys())}"
        )


def test_sanitize_key_function():
    """Unit test for _sanitize_key: length cap and charset enforcement."""
    # Safe key — passes through (lowercased)
    assert nw_feedback._sanitize_key("safe_key") == "safe_key"
    assert nw_feedback._sanitize_key("peer:freshness.v2-alpha") == "peer:freshness.v2-alpha"
    # Mixed-case key whose lowercase is safe chars — lowercased, NOT 'other'
    assert nw_feedback._sanitize_key("SafeKey") == "safekey"
    # Long key — truncated to 64 chars, then charset checked; all-alpha still safe
    long_safe = "a" * 100
    result = nw_feedback._sanitize_key(long_safe)
    assert len(result) <= 64
    assert result == "a" * 64
    # Unsafe chars (spaces, punctuation other than _:.-) — becomes 'other'
    assert nw_feedback._sanitize_key("key with spaces") == "other"
    assert nw_feedback._sanitize_key("key!@#$") == "other"
    # Empty string — fails the non-empty charset match, becomes 'other'
    assert nw_feedback._sanitize_key("") == "other"
