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
from datetime import datetime, timedelta, timezone
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
    assert result["schema"] == "mastermind_nw_feedback.v2"
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
    assert payload["schema"] == "mastermind_nw_feedback.v2"
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
    assert payload["schema"] == "mastermind_nw_feedback.v2"


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
    assert payload["schema"] == "mastermind_nw_feedback.v2"


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
    assert result["schema"] == "mastermind_nw_feedback.v2"
    assert "books" in result


def test_build_never_raises_on_corrupt_events(monkeypatch, tmp_path):
    """build() survives corrupt/malformed event lines."""
    p = tmp_path / "governance" / "run_events.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("not json\n{broken\n{}\n")
    monkeypatch.setattr(nw_feedback, "_run_events_path", lambda: p)
    result = nw_feedback.build()
    assert result["schema"] == "mastermind_nw_feedback.v2"


def test_write_returns_path(monkeypatch, tmp_path):
    """write() writes the JSON file and returns the path."""
    monkeypatch.setattr(nw_feedback, "_run_events_path",
                        lambda: tmp_path / "nonexistent" / "run_events.jsonl")
    dest = tmp_path / "site"
    out = nw_feedback.write(dest)
    assert out == dest / "mastermind" / "nw_feedback.json"
    assert out.exists()
    payload = json.loads(out.read_text())
    assert payload["schema"] == "mastermind_nw_feedback.v2"


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


# ===========================================================================
# v2 tests — new blocks: decision_flow, outcome_mix, context_audit
# ===========================================================================

# ---------------------------------------------------------------------------
# Helpers for v2 ledger fixtures
# ---------------------------------------------------------------------------

def _write_packet_rejections(tmp_path: Path, rows: list[dict]) -> Path:
    """Write a packet_rejections.jsonl fixture and return its path."""
    p = tmp_path / "governance" / "packet_rejections.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(json.dumps(r) for r in rows) + ("\n" if rows else ""))
    return p


def _write_outcome_ledger(tmp_path: Path, rows: list[dict]) -> Path:
    p = tmp_path / "brain" / "outcome_ledger.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(json.dumps(r) for r in rows) + ("\n" if rows else ""))
    return p


def _write_context_audit(tmp_path: Path, rows: list[dict]) -> Path:
    p = tmp_path / "brain" / "nw_context_audit.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(json.dumps(r) for r in rows) + ("\n" if rows else ""))
    return p


# ---------------------------------------------------------------------------
# v2 schema: new top-level fields present
# ---------------------------------------------------------------------------

def test_v2_schema_field_present(monkeypatch, tmp_path):
    """build() v2 includes decision_flow, outcome_mix, context_audit, metric_families."""
    _patch_events(monkeypatch, tmp_path, [])
    monkeypatch.setattr(nw_feedback, "_packet_rejections_path",
                        lambda: tmp_path / "governance" / "packet_rejections.jsonl")
    monkeypatch.setattr(nw_feedback, "_outcome_ledger_path",
                        lambda: tmp_path / "brain" / "outcome_ledger.jsonl")
    monkeypatch.setattr(nw_feedback, "_nw_context_audit_path",
                        lambda: tmp_path / "brain" / "nw_context_audit.jsonl")
    result = nw_feedback.build()
    assert result["schema"] == "mastermind_nw_feedback.v2"
    assert "decision_flow" in result
    assert "outcome_mix" in result
    assert "context_audit" in result
    assert "metric_families" in result


def test_v2_metric_families_shape(monkeypatch, tmp_path):
    """metric_families has live list and blocked list with expected entries."""
    _patch_events(monkeypatch, tmp_path, [])
    result = nw_feedback.build()
    mf = result["metric_families"]
    assert "live" in mf
    assert "blocked" in mf
    assert "context_engagement" in mf["live"]
    assert "decision_flow" in mf["live"]
    assert "outcome_mix" in mf["live"]
    blocked_names = [b["name"] for b in mf["blocked"]]
    assert "fill_slippage_by_context" in blocked_names
    assert "warning_outcome_delta" in blocked_names


# ---------------------------------------------------------------------------
# decision_flow: packet_accepted/rejected counts + rejection error classes
# ---------------------------------------------------------------------------

def test_decision_flow_packet_counts(monkeypatch, tmp_path):
    """decision_flow.by_book has correct packet_accepted/packet_rejected counts per book."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    events = [
        {"ts": now, "kind": "packet_accepted", "book": "flagship", "status": "ok"},
        {"ts": now, "kind": "packet_accepted", "book": "flagship", "status": "ok"},
        {"ts": now, "kind": "packet_rejected", "book": "flagship", "status": "warn"},
        {"ts": now, "kind": "packet_accepted", "book": "autonomous", "status": "ok"},
    ]
    _patch_events(monkeypatch, tmp_path, events)
    monkeypatch.setattr(nw_feedback, "_packet_rejections_path",
                        lambda: tmp_path / "governance" / "packet_rejections.jsonl")
    result = nw_feedback.build()
    by_book = {e["book_id"]: e for e in result["decision_flow"]["by_book"]}
    assert by_book.get("flagship", {}).get("packet_accepted") == 2
    assert by_book.get("flagship", {}).get("packet_rejected") == 1
    assert by_book.get("autonomous", {}).get("packet_accepted") == 1


def test_decision_flow_rejection_error_classes(monkeypatch, tmp_path):
    """rejection_error_classes extracts leading field name, not prose."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    _patch_events(monkeypatch, tmp_path, [])
    # Plant rejection rows with error strings of various field-name prefixes
    rejections = [
        {"ts": now, "book": "flagship", "errors": [
            "falsifiers: must be a non-empty list",
            "expected_failure_mode: fails the substance floor",
            "falsifiers: must be a non-empty list",
        ]},
        {"ts": now, "book": "flagship", "errors": [
            "holdings[0].ticker: required non-empty string",
        ]},
    ]
    p = _write_packet_rejections(tmp_path, rejections)
    monkeypatch.setattr(nw_feedback, "_packet_rejections_path", lambda: p)
    result = nw_feedback.build()
    classes = result["decision_flow"]["rejection_error_classes"]
    # 'falsifiers' appears twice across two rejection rows
    assert classes.get("falsifiers", 0) == 2
    assert classes.get("expected_failure_mode", 0) == 1
    assert classes.get("holdings", 0) == 1


def test_decision_flow_rejection_classes_no_prose(monkeypatch, tmp_path):
    """rejection_error_classes keys are sanitised — no raw prose reaches the output."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    _patch_events(monkeypatch, tmp_path, [])
    rejections = [
        {"ts": now, "book": "flagship", "errors": [
            "falsifiers: the ticker AAPL is wrong for reason X",
        ]},
    ]
    p = _write_packet_rejections(tmp_path, rejections)
    monkeypatch.setattr(nw_feedback, "_packet_rejections_path", lambda: p)
    result = nw_feedback.build()
    classes = result["decision_flow"]["rejection_error_classes"]
    # Only the field name key is present — ticker "AAPL" must not appear as a key
    assert "AAPL" not in classes
    assert "aapl" not in classes
    # The class key is just 'falsifiers'
    assert "falsifiers" in classes


def test_decision_flow_rejection_classes_top10(monkeypatch, tmp_path):
    """rejection_error_classes emits at most 10 classes."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    _patch_events(monkeypatch, tmp_path, [])
    # Create 15 different field names
    errors = [f"field{i:02d}: some prose" for i in range(15)]
    rejections = [{"ts": now, "book": "flagship", "errors": errors}]
    p = _write_packet_rejections(tmp_path, rejections)
    monkeypatch.setattr(nw_feedback, "_packet_rejections_path", lambda: p)
    result = nw_feedback.build()
    classes = result["decision_flow"]["rejection_error_classes"]
    assert len(classes) <= 10, f"Expected at most 10 classes, got {len(classes)}"


def test_decision_flow_window_excludes_old_rejections(monkeypatch, tmp_path):
    """Rejection rows older than window_days are excluded from error class counts."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    old_ts = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat(timespec="seconds")
    _patch_events(monkeypatch, tmp_path, [])
    rejections = [
        {"ts": old_ts, "book": "flagship", "errors": ["falsifiers: old error"]},
        {"ts": now, "book": "flagship", "errors": ["mandate: fresh error"]},
    ]
    p = _write_packet_rejections(tmp_path, rejections)
    monkeypatch.setattr(nw_feedback, "_packet_rejections_path", lambda: p)
    result = nw_feedback.build()
    classes = result["decision_flow"]["rejection_error_classes"]
    # Only 'mandate' from the fresh row; old 'falsifiers' excluded by window
    assert classes.get("falsifiers", 0) == 0, "Old rejection row should be excluded by window"
    assert classes.get("mandate", 0) == 1


def test_decision_flow_missing_ledger_fail_soft(monkeypatch, tmp_path):
    """decision_flow degrades gracefully when packet_rejections.jsonl is absent."""
    _patch_events(monkeypatch, tmp_path, [])
    monkeypatch.setattr(nw_feedback, "_packet_rejections_path",
                        lambda: tmp_path / "nonexistent" / "packet_rejections.jsonl")
    result = nw_feedback.build()
    # Block must still be present, just empty
    assert "decision_flow" in result
    assert result["decision_flow"]["rejection_error_classes"] == {}


# ---------------------------------------------------------------------------
# Poisoned fixture: ticker, dollar amount, MASTERMIND_TOKEN in ledger inputs
# ---------------------------------------------------------------------------

def test_redaction_of_ticker_in_rejection_errors(monkeypatch, tmp_path):
    """A ticker string planted in a rejection error must not appear as a class key."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    _patch_events(monkeypatch, tmp_path, [])
    # Plant a ticker in the error prose — it must not leak as a key
    rejections = [
        {"ts": now, "book": "flagship", "errors": ["TSLA: not a valid field"]}
    ]
    p = _write_packet_rejections(tmp_path, rejections)
    monkeypatch.setattr(nw_feedback, "_packet_rejections_path", lambda: p)
    result = nw_feedback.build()
    classes = result["decision_flow"]["rejection_error_classes"]
    serialized = json.dumps(classes)
    # TSLA downcases to 'tsla' — it would pass charset but it's only 4 chars so
    # _classify_error extracts 'TSLA' → sanitise → 'tsla'. That's a valid key,
    # but the prose "not a valid field" must not appear anywhere.
    assert "not a valid field" not in serialized
    assert "not" not in classes  # prose words must not be keys


def test_redaction_dollar_amount_in_outcome_note(monkeypatch, tmp_path):
    """A $12,345 string planted in an outcome_ledger note must be redacted by write()."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    _patch_events(monkeypatch, tmp_path, [])
    monkeypatch.setattr(nw_feedback, "_packet_rejections_path",
                        lambda: tmp_path / "governance" / "packet_rejections.jsonl")
    # Inject dollar amount via patched _outcome_mix
    def patched_outcome_mix(window_days):
        return {"n_resolved": 1, "by_outcome": {"1": 1}, "_test_leaked": "$12,345"}
    monkeypatch.setattr(nw_feedback, "_outcome_mix", patched_outcome_mix)
    dest = tmp_path / "site"
    out = nw_feedback.write(dest)
    payload = json.loads(out.read_text())
    serialized = json.dumps(payload)
    assert "$12,345" not in serialized, "Dollar amount must be redacted from output"


def test_redaction_mastermind_token_in_context_audit(monkeypatch, tmp_path):
    """A MASTERMIND_TOKEN string planted in context_audit must be redacted."""
    _patch_events(monkeypatch, tmp_path, [])
    monkeypatch.setattr(nw_feedback, "_packet_rejections_path",
                        lambda: tmp_path / "governance" / "packet_rejections.jsonl")
    def patched_context_audit(window_days):
        return {"n_present": 5, "n_stale": 1, "n_absent": 0, "n_runs_total": 6,
                "context_seen_rate": 0.833, "_leaked": "MASTERMIND_TOKEN=abc"}
    monkeypatch.setattr(nw_feedback, "_context_audit", patched_context_audit)
    dest = tmp_path / "site"
    out = nw_feedback.write(dest)
    payload = json.loads(out.read_text())
    serialized = json.dumps(payload)
    assert "MASTERMIND_TOKEN" not in serialized, "MASTERMIND_ token must be redacted"


# ---------------------------------------------------------------------------
# outcome_mix: counts by outcome field, n_resolved, windowing
# ---------------------------------------------------------------------------

def test_outcome_mix_counts_correct(monkeypatch, tmp_path):
    """outcome_mix counts resolved outcomes in window by outcome value."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    rows = [
        {"thesis_id": "t1", "asof_resolved": now[:10], "outcome": 1},
        {"thesis_id": "t2", "asof_resolved": now[:10], "outcome": 0},
        {"thesis_id": "t3", "asof_resolved": now[:10], "outcome": 1},
    ]
    p = _write_outcome_ledger(tmp_path, rows)
    monkeypatch.setattr(nw_feedback, "_outcome_ledger_path", lambda: p)
    result = nw_feedback._outcome_mix(14)
    assert result["n_resolved"] == 3
    assert result["by_outcome"].get("1", 0) == 2
    assert result["by_outcome"].get("0", 0) == 1
    # No thesis_ids in output
    serialized = json.dumps(result)
    for row in rows:
        assert row["thesis_id"] not in serialized


def test_outcome_mix_excludes_old_rows(monkeypatch, tmp_path):
    """outcome_mix excludes rows whose asof_resolved is outside the window."""
    now_date = datetime.now(timezone.utc).date().isoformat()
    old_date = (datetime.now(timezone.utc) - timedelta(days=30)).date().isoformat()
    rows = [
        {"thesis_id": "t1", "asof_resolved": old_date, "outcome": 1},
        {"thesis_id": "t2", "asof_resolved": now_date, "outcome": 0},
    ]
    p = _write_outcome_ledger(tmp_path, rows)
    monkeypatch.setattr(nw_feedback, "_outcome_ledger_path", lambda: p)
    result = nw_feedback._outcome_mix(14)
    assert result["n_resolved"] == 1, "Old outcome row must be excluded by window"
    assert result["by_outcome"].get("0", 0) == 1


def test_outcome_mix_missing_ledger_fail_soft(monkeypatch, tmp_path):
    """_outcome_mix returns absence indicator when ledger is missing."""
    monkeypatch.setattr(nw_feedback, "_outcome_ledger_path",
                        lambda: tmp_path / "nonexistent" / "outcome_ledger.jsonl")
    result = nw_feedback._outcome_mix(14)
    assert result.get("state") == "absent" or result.get("n_resolved", -1) == 0


def test_outcome_mix_corrupt_lines(monkeypatch, tmp_path):
    """_outcome_mix survives corrupt lines in the ledger."""
    p = tmp_path / "brain" / "outcome_ledger.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("not json\n{broken\n")
    monkeypatch.setattr(nw_feedback, "_outcome_ledger_path", lambda: p)
    result = nw_feedback._outcome_mix(14)
    assert "n_resolved" in result


# ---------------------------------------------------------------------------
# context_audit: engagement counts, context_seen_rate, accruing state
# ---------------------------------------------------------------------------

def test_context_audit_counts_correct(monkeypatch, tmp_path):
    """_context_audit counts present/stale/absent and computes context_seen_rate."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    rows = [
        {"ts": now, "run_id": "r1", "status": "present", "asof": "2026-07-01", "age_days": 5, "n_candidates": 3},
        {"ts": now, "run_id": "r2", "status": "present", "asof": "2026-07-01", "age_days": 5, "n_candidates": 3},
        {"ts": now, "run_id": "r3", "status": "stale",   "asof": "2026-06-01", "age_days": 30, "n_candidates": 0},
        {"ts": now, "run_id": "r4", "status": "absent",  "asof": None,         "age_days": None, "n_candidates": 0},
    ]
    p = _write_context_audit(tmp_path, rows)
    monkeypatch.setattr(nw_feedback, "_nw_context_audit_path", lambda: p)
    result = nw_feedback._context_audit(14)
    assert result["n_runs_total"] == 4
    assert result["n_present"] == 2
    assert result["n_stale"] == 1
    assert result["n_absent"] == 1
    assert result["context_seen_rate"] == round(2 / 4, 3)


def test_context_audit_no_sidecar_returns_accruing(monkeypatch, tmp_path):
    """When nw_context_audit.jsonl doesn't exist, context_audit returns accruing state."""
    monkeypatch.setattr(nw_feedback, "_nw_context_audit_path",
                        lambda: tmp_path / "nonexistent" / "nw_context_audit.jsonl")
    result = nw_feedback._context_audit(14)
    assert result.get("state") == "accruing"
    assert result.get("n_runs_total") == 0
    # Must not fabricate counts
    assert "n_present" not in result


def test_context_audit_window_excludes_old_runs(monkeypatch, tmp_path):
    """context_audit excludes runs outside the window."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    old = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat(timespec="seconds")
    rows = [
        {"ts": old, "run_id": "r_old", "status": "present", "asof": "2026-06-01", "age_days": 35, "n_candidates": 1},
        {"ts": now, "run_id": "r_new", "status": "absent",  "asof": None,         "age_days": None, "n_candidates": 0},
    ]
    p = _write_context_audit(tmp_path, rows)
    monkeypatch.setattr(nw_feedback, "_nw_context_audit_path", lambda: p)
    result = nw_feedback._context_audit(14)
    # Only r_new is within the 14-day window
    assert result["n_runs_total"] == 1
    assert result.get("n_present", 0) == 0
    assert result.get("n_absent", 0) == 1


def test_context_audit_in_build_output(monkeypatch, tmp_path):
    """build() includes context_audit from the sidecar correctly end-to-end."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    _patch_events(monkeypatch, tmp_path, [])
    monkeypatch.setattr(nw_feedback, "_packet_rejections_path",
                        lambda: tmp_path / "governance" / "packet_rejections.jsonl")
    monkeypatch.setattr(nw_feedback, "_outcome_ledger_path",
                        lambda: tmp_path / "brain" / "outcome_ledger.jsonl")
    rows = [
        {"ts": now, "run_id": "r1", "status": "present", "asof": "2026-07-01", "age_days": 5, "n_candidates": 2},
    ]
    p = _write_context_audit(tmp_path, rows)
    monkeypatch.setattr(nw_feedback, "_nw_context_audit_path", lambda: p)
    result = nw_feedback.build()
    ca = result["context_audit"]
    assert ca.get("n_runs_total") == 1
    assert ca.get("n_present") == 1
    assert ca.get("context_seen_rate") == 1.0


# ---------------------------------------------------------------------------
# Sidecar append: real seam test via _append_nw_context_audit (MAJOR-3)
# ---------------------------------------------------------------------------

def test_append_nw_context_audit_row_shape(tmp_path):
    """_append_nw_context_audit writes a row with all required fields."""
    from bot.phase2 import _append_nw_context_audit

    audit_row = {
        "status": "present",
        "asof": "2026-07-05",
        "age_days": 1,
        "n_candidates": 5,
    }
    _append_nw_context_audit(tmp_path, "run-001", audit_row)

    audit_path = tmp_path / "data" / "brain" / "nw_context_audit.jsonl"
    assert audit_path.exists(), "sidecar file must be created"
    lines = audit_path.read_text().strip().splitlines()
    assert len(lines) == 1
    parsed = json.loads(lines[0])
    assert set(parsed.keys()) >= {"ts", "run_id", "status", "asof", "age_days", "n_candidates"}
    assert parsed["run_id"] == "run-001"
    assert parsed["status"] == "present"
    assert parsed["asof"] == "2026-07-05"
    assert parsed["age_days"] == 1
    assert parsed["n_candidates"] == 5


def test_append_nw_context_audit_append_only(tmp_path):
    """_append_nw_context_audit is append-only: multiple calls yield multiple rows."""
    from bot.phase2 import _append_nw_context_audit

    for i in range(3):
        _append_nw_context_audit(tmp_path, f"run-{i:03d}", {
            "status": "absent", "asof": None, "age_days": None, "n_candidates": 0,
        })

    audit_path = tmp_path / "data" / "brain" / "nw_context_audit.jsonl"
    lines = [l for l in audit_path.read_text().strip().splitlines() if l]
    assert len(lines) == 3, "must append, not overwrite"
    run_ids = [json.loads(l)["run_id"] for l in lines]
    assert run_ids == ["run-000", "run-001", "run-002"]


def test_append_nw_context_audit_ioerror_swallowed(tmp_path):
    """_append_nw_context_audit swallows IOError — the caller's never-raise contract."""
    from bot.phase2 import _append_nw_context_audit
    from unittest.mock import patch

    # Make mkdir fail to provoke an IOError-family exception inside the function.
    with patch("pathlib.Path.mkdir", side_effect=OSError("disk full")):
        try:
            _append_nw_context_audit(tmp_path, "run-err", {"status": "absent"})
        except Exception as exc:
            pytest.fail(
                f"_append_nw_context_audit must swallow IOError but raised: {exc!r}"
            )


def test_seam_calls_append_nw_context_audit():
    """Source-level assertion: _append_nw_context_audit( is invoked in the nw_context block of phase2.run.

    Rationale: driving phase2.run() in this worktree requires a live DB, live ledgers,
    and a live LLM call — not practical in unit tests.  The weakest acceptable form of
    the MAJOR-3 seam test is a source inspection asserting the extracted function is
    referenced inside the nw_context block (the try/except around _nwc_mod.audit_row()).
    This test will fail if the inline block is restored (removing the extracted call),
    which is the deletion-detection requirement.
    """
    import inspect
    from bot import phase2

    src = inspect.getsource(phase2.run)
    assert "_append_nw_context_audit(" in src, (
        "The nw_context seam in phase2.run must call _append_nw_context_audit(). "
        "Restoring the inline sidecar block would break the real seam test contract. "
        "See MAJOR-3 in the Opus review."
    )


# ---------------------------------------------------------------------------
# Shape stabilization: state field always present (outcome_mix + context_audit)
# ---------------------------------------------------------------------------

def test_outcome_mix_state_ok_when_populated(monkeypatch, tmp_path):
    """outcome_mix emits state='ok' when the ledger has in-window rows."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    rows = [{"thesis_id": "t1", "asof_resolved": now[:10], "outcome": 1}]
    p = _write_outcome_ledger(tmp_path, rows)
    monkeypatch.setattr(nw_feedback, "_outcome_ledger_path", lambda: p)
    result = nw_feedback._outcome_mix(14)
    assert result.get("state") == "ok", (
        f"outcome_mix with populated rows must emit state='ok', got {result.get('state')!r}"
    )


def test_outcome_mix_state_absent_when_missing(monkeypatch, tmp_path):
    """outcome_mix emits state='absent' when the ledger is missing."""
    monkeypatch.setattr(nw_feedback, "_outcome_ledger_path",
                        lambda: tmp_path / "nonexistent" / "outcome_ledger.jsonl")
    result = nw_feedback._outcome_mix(14)
    assert result.get("state") == "absent"


def test_context_audit_state_ok_when_populated(monkeypatch, tmp_path):
    """_context_audit emits state='ok' when the sidecar has in-window rows."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    rows = [{"ts": now, "run_id": "r1", "status": "present", "asof": "2026-07-01",
             "age_days": 5, "n_candidates": 2}]
    p = _write_context_audit(tmp_path, rows)
    monkeypatch.setattr(nw_feedback, "_nw_context_audit_path", lambda: p)
    result = nw_feedback._context_audit(14)
    assert result.get("state") == "ok", (
        f"_context_audit with populated rows must emit state='ok', got {result.get('state')!r}"
    )


def test_context_audit_state_accruing_when_missing(monkeypatch, tmp_path):
    """_context_audit emits state='accruing' when the sidecar is absent."""
    monkeypatch.setattr(nw_feedback, "_nw_context_audit_path",
                        lambda: tmp_path / "nonexistent" / "nw_context_audit.jsonl")
    result = nw_feedback._context_audit(14)
    assert result.get("state") == "accruing"


# ---------------------------------------------------------------------------
# MINOR-2: unknown context status buckets into n_absent
# ---------------------------------------------------------------------------

def test_context_audit_unknown_status_buckets_to_absent(monkeypatch, tmp_path):
    """Unknown status strings are counted as n_absent (conservative: unusable context)."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    rows = [
        {"ts": now, "run_id": "r1", "status": "present"},
        {"ts": now, "run_id": "r2", "status": "degraded"},   # unknown → n_absent
        {"ts": now, "run_id": "r3", "status": "error"},      # unknown → n_absent
        {"ts": now, "run_id": "r4", "status": "absent"},
    ]
    p = _write_context_audit(tmp_path, rows)
    monkeypatch.setattr(nw_feedback, "_nw_context_audit_path", lambda: p)
    result = nw_feedback._context_audit(14)
    assert result["n_runs_total"] == 4
    assert result["n_present"] == 1
    assert result["n_absent"] == 3, (
        f"Unknown statuses 'degraded'+'error' must bucket into n_absent (got n_absent={result['n_absent']})"
    )
    assert result["n_stale"] == 0


# ---------------------------------------------------------------------------
# MINOR-1: prose/ticker outcome value sanitized + capped (MAJOR-1 negative test)
# ---------------------------------------------------------------------------

def test_outcome_mix_prose_outcome_sanitized_and_no_ticker(monkeypatch, tmp_path):
    """Prose/ticker outcome values are sanitized — AAPL and prose must not appear as by_outcome keys."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    # Simulate 502 distinct prose outcomes (only 12 must survive the cap)
    rows = []
    for i in range(20):
        rows.append({"asof_resolved": now[:10], "outcome": f"AAPL beat by {i}%"})
    for i in range(20):
        rows.append({"asof_resolved": now[:10], "outcome": f"TSLA missed by {i}pts"})
    # Also add some simple numeric outcomes that should survive
    for v in [0, 1]:
        rows.append({"asof_resolved": now[:10], "outcome": v})
    p = _write_outcome_ledger(tmp_path, rows)
    monkeypatch.setattr(nw_feedback, "_outcome_ledger_path", lambda: p)

    result = nw_feedback._outcome_mix(14)
    serialized = json.dumps(result)

    # No ticker or prose must appear
    assert "AAPL" not in serialized, "AAPL ticker must not appear in outcome_mix output"
    assert "TSLA" not in serialized, "TSLA ticker must not appear in outcome_mix output"
    assert "beat" not in serialized, "Prose word 'beat' must not appear in outcome_mix keys"
    assert "missed" not in serialized, "Prose word 'missed' must not appear in outcome_mix keys"

    # Cap: at most 12 keys
    assert len(result["by_outcome"]) <= 12, (
        f"by_outcome must be capped at 12 keys, got {len(result['by_outcome'])}"
    )

    # The keys that survive must be sanitized (all safe charset)
    _safe_re = re.compile(r'^[a-z0-9_:.\-]+$')
    for k in result["by_outcome"]:
        assert _safe_re.match(k), f"Unsafe by_outcome key survived sanitization: {k!r}"


def test_outcome_mix_digit_keys_survive_sanitization(monkeypatch, tmp_path):
    """Digit-only outcome values '0'/'1' survive _sanitize_key unchanged (MAJOR-1 guard)."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    rows = [
        {"asof_resolved": now[:10], "outcome": 0},
        {"asof_resolved": now[:10], "outcome": 1},
        {"asof_resolved": now[:10], "outcome": 1},
    ]
    p = _write_outcome_ledger(tmp_path, rows)
    monkeypatch.setattr(nw_feedback, "_outcome_ledger_path", lambda: p)
    result = nw_feedback._outcome_mix(14)
    assert result["by_outcome"].get("0") == 1
    assert result["by_outcome"].get("1") == 2


# ---------------------------------------------------------------------------
# MAJOR-2: _redact_secrets backstops dict KEYS
# ---------------------------------------------------------------------------

def test_redact_secrets_redacts_mastermind_key(tmp_path):
    """_redact_secrets must redact a MASTERMIND_* string that appears as a dict KEY."""
    payload = {
        "schema": "mastermind_nw_feedback.v2",
        "outcome_mix": {
            "MASTERMIND_SECRET_KEY": 42,
            "safe_key": 1,
        },
    }
    result = nw_feedback._redact_secrets(payload)
    serialized = json.dumps(result)
    assert "MASTERMIND_SECRET_KEY" not in serialized, (
        "_redact_secrets must redact MASTERMIND_* dict keys, not just values"
    )
    assert "<redacted>" in serialized


def test_redact_secrets_key_collision_sums_numeric(tmp_path):
    """When key redaction causes a collision, numeric values are summed."""
    # Two keys that both redact to "<redacted>" — values should be summed
    payload = {
        "counts": {
            "MASTERMIND_KEY_A": 3,
            "MASTERMIND_KEY_B": 5,
        }
    }
    result = nw_feedback._redact_secrets(payload)
    counts = result["counts"]
    # Both keys collapsed to "<redacted>"; numeric sum = 8
    assert "<redacted>" in counts
    assert counts["<redacted>"] == 8, (
        f"Colliding redacted numeric keys must be summed (expected 8, got {counts.get('<redacted>')})"
    )
