"""Tests for control_plane.packet_gate — DecisionPacket boundary wire (ruling R6).

Coverage:
  - gate_mode: off/shadow/enforce/default behaviour; unrecognised value → shadow
  - process off: skipped=True, ok=True, packet=None, zero events written
  - process shadow + valid: ok=True, shadowed=False, packet_id present, event written
  - process shadow + invalid: ok=True (shadow invariant), shadowed=True, rejection logged,
    packet_id present — book proceeds unchanged (byte-equivalent)
  - process enforce + valid: ok=True, packet_id present, event written
  - process enforce + invalid: ok=False, rejection_id present, event written (packet_rejected)
  - P2: enforce rejection → fallback exposure <= proceed-path exposure
  - never-raise: garbage submission/prior_book never crashes process()
  - governance conformance: MASTERMIND_PACKET_GATE in flags.KNOWN_FLAGS
  - governance conformance: MASTERMIND_PACKET_GATE in authority_map.yml at A6
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _valid_sub(n_holdings: int = 2) -> dict:
    """A submission dict that produces a valid packet when processed.

    All governance fields meet the substance floor (>= 15 chars, >= 3 words).
    """
    tickers = ["NVDA", "MSFT", "AMZN", "GOOG", "META"]
    holdings = [
        {"ticker": tickers[i % len(tickers)],
         "weight": 0.20,
         "rationale": f"Holding {tickers[i % len(tickers)]} for strong secular growth in cloud and AI.",
         "conviction": "high"}
        for i in range(n_holdings)
    ]
    return {
        "holdings": holdings,
        "summary": "Concentrated tech book with strong risk/reward given current regime.",
        "sold_note": "Trimmed AMZN on stretched valuation vs peers.",
        "falsifiers": ["If SPY falls more than five percent in three days without a bounce this book is wrong."],
        "evidence_planes": ["regime", "fundamentals", "flows"],
        "expected_failure_mode": "A surprise rate shock compresses growth multiples across the portfolio.",
    }


def _invalid_sub() -> dict:
    """A submission dict missing required governance fields (empty holdings → invalid)."""
    return {
        "holdings": [],          # empty — validate should reject this
        "summary": "Nothing to trade today.",
    }


def _prior_book() -> dict:
    """A minimal prior book for delta computation."""
    return {
        "positions": {
            "NVDA": {"weight": 0.20, "avg_cost": 500.0, "shares": 4},
        },
        "cash": 8000.0,
        "nav": 10000.0,
    }


def _read_events(root: Path) -> list[dict]:
    p = root / "data" / "governance" / "run_events.jsonl"
    if not p.exists():
        return []
    rows = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                rows.append(json.loads(line))
            except Exception:
                pass
    return rows


def _read_rejections(root: Path) -> list[dict]:
    p = root / "data" / "governance" / "packet_rejections.jsonl"
    if not p.exists():
        return []
    rows = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                rows.append(json.loads(line))
            except Exception:
                pass
    return rows


# ===========================================================================
# gate_mode
# ===========================================================================

class TestGateMode:
    def test_default_is_shadow(self, monkeypatch):
        monkeypatch.delenv("MASTERMIND_PACKET_GATE", raising=False)
        from control_plane.packet_gate import gate_mode
        assert gate_mode() == "shadow"

    def test_explicit_off(self, monkeypatch):
        monkeypatch.setenv("MASTERMIND_PACKET_GATE", "off")
        from control_plane.packet_gate import gate_mode
        assert gate_mode() == "off"

    def test_explicit_shadow(self, monkeypatch):
        monkeypatch.setenv("MASTERMIND_PACKET_GATE", "shadow")
        from control_plane.packet_gate import gate_mode
        assert gate_mode() == "shadow"

    def test_explicit_enforce(self, monkeypatch):
        monkeypatch.setenv("MASTERMIND_PACKET_GATE", "enforce")
        from control_plane.packet_gate import gate_mode
        assert gate_mode() == "enforce"

    def test_unrecognised_falls_back_to_shadow(self, monkeypatch):
        monkeypatch.setenv("MASTERMIND_PACKET_GATE", "INVALID_VALUE")
        from control_plane.packet_gate import gate_mode
        assert gate_mode() == "shadow"

    def test_case_insensitive(self, monkeypatch):
        monkeypatch.setenv("MASTERMIND_PACKET_GATE", "ENFORCE")
        from control_plane.packet_gate import gate_mode
        assert gate_mode() == "enforce"


# ===========================================================================
# process — OFF mode
# ===========================================================================

class TestProcessOff:
    def test_off_returns_skipped_ok(self, monkeypatch, tmp_path):
        monkeypatch.setenv("MASTERMIND_PACKET_GATE", "off")
        from control_plane.packet_gate import process
        result = process("autonomous", _valid_sub(), _prior_book(),
                         run_events_root=tmp_path, rejections_root=tmp_path)
        assert result.ok is True
        assert result.skipped is True
        assert result.packet is None
        assert result.packet_id is None

    def test_off_writes_zero_events(self, monkeypatch, tmp_path):
        monkeypatch.setenv("MASTERMIND_PACKET_GATE", "off")
        from control_plane.packet_gate import process
        process("autonomous", _valid_sub(), _prior_book(),
                run_events_root=tmp_path, rejections_root=tmp_path)
        assert _read_events(tmp_path) == []

    def test_off_writes_zero_rejections(self, monkeypatch, tmp_path):
        monkeypatch.setenv("MASTERMIND_PACKET_GATE", "off")
        from control_plane.packet_gate import process
        process("autonomous", _invalid_sub(), {},
                run_events_root=tmp_path, rejections_root=tmp_path)
        assert _read_rejections(tmp_path) == []


# ===========================================================================
# process — SHADOW mode (default)
# ===========================================================================

class TestProcessShadow:
    def test_shadow_valid_ok_true(self, monkeypatch, tmp_path):
        monkeypatch.setenv("MASTERMIND_PACKET_GATE", "shadow")
        from control_plane.packet_gate import process
        r = process("autonomous", _valid_sub(), _prior_book(),
                    extras={"run_id": "run1", "asof": "2026-07-06",
                            "mandate": "Manage the autonomous paper book with full discretion.",
                            "falsifiers": ["If SPY falls more than five percent this thesis is wrong."],
                            "evidence_planes": ["regime", "flows"],
                            "expected_failure_mode": "Surprise rate shock compresses growth multiples broadly."},
                    run_events_root=tmp_path, rejections_root=tmp_path)
        assert r.ok is True
        assert r.shadowed is False

    def test_shadow_valid_packet_id_present(self, monkeypatch, tmp_path):
        monkeypatch.setenv("MASTERMIND_PACKET_GATE", "shadow")
        from control_plane.packet_gate import process
        r = process("autonomous", _valid_sub(), _prior_book(),
                    extras={"run_id": "run1", "asof": "2026-07-06",
                            "mandate": "Manage the autonomous paper book with full discretion.",
                            "falsifiers": ["If the market breaks down on heavy volume this thesis is wrong."],
                            "evidence_planes": ["regime", "flows"],
                            "expected_failure_mode": "Unexpected macro shock forces risk-off rotation."},
                    run_events_root=tmp_path, rejections_root=tmp_path)
        assert r.packet_id is not None and len(r.packet_id) == 16

    def test_shadow_valid_writes_accepted_event(self, monkeypatch, tmp_path):
        monkeypatch.setenv("MASTERMIND_PACKET_GATE", "shadow")
        from control_plane.packet_gate import process
        process("autonomous", _valid_sub(), _prior_book(),
                extras={"run_id": "r", "asof": "2026-07-06",
                        "mandate": "Manage the autonomous paper book with full discretion.",
                        "falsifiers": ["If the market drops sharply on high volume this thesis is wrong."],
                        "evidence_planes": ["regime", "technicals"],
                        "expected_failure_mode": "Unexpected rate shock compresses growth multiples."},
                run_events_root=tmp_path, rejections_root=tmp_path)
        events = _read_events(tmp_path)
        kinds = [e.get("kind") for e in events]
        assert "packet_accepted" in kinds

    def test_shadow_invalid_ok_true_invariant(self, monkeypatch, tmp_path):
        """Shadow mode ALWAYS returns ok=True — the shadow invariant (Charter P8)."""
        monkeypatch.setenv("MASTERMIND_PACKET_GATE", "shadow")
        from control_plane.packet_gate import process
        r = process("autonomous", _invalid_sub(), {},
                    run_events_root=tmp_path, rejections_root=tmp_path)
        assert r.ok is True, "shadow invariant violated: ok must be True even for invalid packets"

    def test_shadow_invalid_shadowed_flag(self, monkeypatch, tmp_path):
        monkeypatch.setenv("MASTERMIND_PACKET_GATE", "shadow")
        from control_plane.packet_gate import process
        r = process("autonomous", _invalid_sub(), {},
                    run_events_root=tmp_path, rejections_root=tmp_path)
        assert r.shadowed is True

    def test_shadow_invalid_rejection_logged(self, monkeypatch, tmp_path):
        """Invalid packet in shadow mode is logged but DOES NOT block the book."""
        monkeypatch.setenv("MASTERMIND_PACKET_GATE", "shadow")
        from control_plane.packet_gate import process
        r = process("autonomous", _invalid_sub(), {},
                    run_events_root=tmp_path, rejections_root=tmp_path)
        # rejection written to the governance ledger
        rejections = _read_rejections(tmp_path)
        assert len(rejections) >= 1

    def test_shadow_invalid_packet_rejected_event(self, monkeypatch, tmp_path):
        monkeypatch.setenv("MASTERMIND_PACKET_GATE", "shadow")
        from control_plane.packet_gate import process
        process("autonomous", _invalid_sub(), {},
                run_events_root=tmp_path, rejections_root=tmp_path)
        events = _read_events(tmp_path)
        kinds = [e.get("kind") for e in events]
        assert "packet_rejected" in kinds

    def test_shadow_invalid_errors_nonempty(self, monkeypatch, tmp_path):
        monkeypatch.setenv("MASTERMIND_PACKET_GATE", "shadow")
        from control_plane.packet_gate import process
        r = process("autonomous", _invalid_sub(), {},
                    run_events_root=tmp_path, rejections_root=tmp_path)
        assert len(r.errors) > 0


# ===========================================================================
# process — ENFORCE mode
# ===========================================================================

class TestProcessEnforce:
    def test_enforce_valid_ok_true(self, monkeypatch, tmp_path):
        monkeypatch.setenv("MASTERMIND_PACKET_GATE", "enforce")
        from control_plane.packet_gate import process
        r = process("autonomous", _valid_sub(), _prior_book(),
                    extras={"run_id": "r", "asof": "2026-07-06",
                            "mandate": "Manage the autonomous paper book with full discretion.",
                            "falsifiers": ["If SPY falls more than five percent on high volume this thesis is wrong."],
                            "evidence_planes": ["regime", "fundamentals"],
                            "expected_failure_mode": "Sudden rate spike compresses all growth multiples portfolio-wide."},
                    run_events_root=tmp_path, rejections_root=tmp_path)
        assert r.ok is True

    def test_enforce_valid_packet_id_present(self, monkeypatch, tmp_path):
        monkeypatch.setenv("MASTERMIND_PACKET_GATE", "enforce")
        from control_plane.packet_gate import process
        r = process("autonomous", _valid_sub(), _prior_book(),
                    extras={"run_id": "r", "asof": "2026-07-06",
                            "mandate": "Manage the autonomous paper book with full discretion.",
                            "falsifiers": ["If the regime flips to risk-off and macro deteriorates this is wrong."],
                            "evidence_planes": ["regime"],
                            "expected_failure_mode": "Macro deterioration compresses the entire growth sleeve."},
                    run_events_root=tmp_path, rejections_root=tmp_path)
        assert r.packet_id is not None

    def test_enforce_invalid_ok_false(self, monkeypatch, tmp_path):
        """Enforce mode rejects invalid packets: ok=False."""
        monkeypatch.setenv("MASTERMIND_PACKET_GATE", "enforce")
        from control_plane.packet_gate import process
        r = process("autonomous", _invalid_sub(), {},
                    run_events_root=tmp_path, rejections_root=tmp_path)
        assert r.ok is False

    def test_enforce_invalid_rejection_id_set(self, monkeypatch, tmp_path):
        monkeypatch.setenv("MASTERMIND_PACKET_GATE", "enforce")
        from control_plane.packet_gate import process
        r = process("autonomous", _invalid_sub(), {},
                    run_events_root=tmp_path, rejections_root=tmp_path)
        assert r.rejection_id is not None

    def test_enforce_invalid_writes_rejection(self, monkeypatch, tmp_path):
        monkeypatch.setenv("MASTERMIND_PACKET_GATE", "enforce")
        from control_plane.packet_gate import process
        process("autonomous", _invalid_sub(), {},
                run_events_root=tmp_path, rejections_root=tmp_path)
        assert len(_read_rejections(tmp_path)) >= 1

    def test_enforce_invalid_writes_rejected_event(self, monkeypatch, tmp_path):
        monkeypatch.setenv("MASTERMIND_PACKET_GATE", "enforce")
        from control_plane.packet_gate import process
        process("autonomous", _invalid_sub(), {},
                run_events_root=tmp_path, rejections_root=tmp_path)
        events = _read_events(tmp_path)
        assert any(e.get("kind") == "packet_rejected" for e in events)


# ===========================================================================
# P2: rejection path cannot increase exposure vs. the proceed path
# ===========================================================================

class TestP2ExposureInvariant:
    """Charter P2: a rejected packet MUST NOT yield more exposure than the
    Brain-errored (no-trade) path.  Exposure proxy = gross weight of traded positions.

    This tests the calling pattern from bot/autonomous.py:
        if not _pgr.ok:
            decided = False      ← carry-forward / no-trade = exposure unchanged
    The key invariant is:
        gross(fallback) <= gross(proceed-with-valid-packet)
    """

    def _run_enforce_rejection(self, tmp_path, monkeypatch):
        """Simulate the bot calling process() in enforce mode with invalid submission."""
        monkeypatch.setenv("MASTERMIND_PACKET_GATE", "enforce")
        from control_plane.packet_gate import process
        return process("autonomous", _invalid_sub(), _prior_book(),
                       run_events_root=tmp_path, rejections_root=tmp_path)

    def test_rejection_ok_is_false(self, monkeypatch, tmp_path):
        r = self._run_enforce_rejection(tmp_path, monkeypatch)
        assert r.ok is False

    def test_fallback_decided_false_means_zero_delta_trades(self, monkeypatch, tmp_path):
        """When ok=False the bot sets decided=False → target dict is empty → no trades.

        Gross exposure of "no trades" (the fallback path) is <= gross exposure of
        "valid submission accepted" (the proceed path), satisfying P2.
        Modelled here by asserting that an ok=False result leads to zero target holdings.
        """
        r = self._run_enforce_rejection(tmp_path, monkeypatch)
        # The caller (bot) would do:
        #   if not _pgr.ok:
        #       decided = False
        # Decided=False → target = {} → 0 net new buys → exposure change = 0
        # The existing held positions are CARRIED (not sold), so exposure never increases.
        assert r.ok is False
        # In the bot the fallback means: no submission → target is empty → no rebalance.
        # gross_fallback = 0 new positions (carry prior) <= gross_proceed (buy into submission)
        # We assert ok=False is the trigger; the inequality is structural (0 buys <= k buys).
        target_fallback = {}  # what bot assigns when decided=False
        gross_fallback = sum(float(v) for v in target_fallback.values())
        # valid submission would have 2 holdings at 0.20 each
        gross_proceed = sum(float(h["weight"]) for h in _valid_sub()["holdings"])
        assert gross_fallback <= gross_proceed


# ===========================================================================
# never-raise contract
# ===========================================================================

class TestNeverRaise:
    """process() must NEVER propagate exceptions — the gate can never block the book."""

    def test_none_submission(self, monkeypatch, tmp_path):
        monkeypatch.setenv("MASTERMIND_PACKET_GATE", "shadow")
        from control_plane.packet_gate import process
        r = process("autonomous", None, {},
                    run_events_root=tmp_path, rejections_root=tmp_path)
        assert isinstance(r.ok, bool)

    def test_garbage_submission(self, monkeypatch, tmp_path):
        monkeypatch.setenv("MASTERMIND_PACKET_GATE", "enforce")
        from control_plane.packet_gate import process
        r = process("autonomous", {"bad": "data", "holdings": "NOT_A_LIST"}, None,
                    run_events_root=tmp_path, rejections_root=tmp_path)
        assert isinstance(r.ok, bool)

    def test_none_prior_book(self, monkeypatch, tmp_path):
        monkeypatch.setenv("MASTERMIND_PACKET_GATE", "shadow")
        from control_plane.packet_gate import process
        r = process("etf", _valid_sub(), None,
                    run_events_root=tmp_path, rejections_root=tmp_path)
        assert isinstance(r.ok, bool)

    def test_both_none(self, monkeypatch, tmp_path):
        monkeypatch.setenv("MASTERMIND_PACKET_GATE", "enforce")
        from control_plane.packet_gate import process
        r = process("china", None, None,
                    run_events_root=tmp_path, rejections_root=tmp_path)
        assert isinstance(r.ok, bool)

    def test_unwritable_root_still_returns_result(self, monkeypatch, tmp_path):
        monkeypatch.setenv("MASTERMIND_PACKET_GATE", "shadow")
        from control_plane.packet_gate import process
        # pass a path that cannot be created (file in place of dir)
        bad_root = tmp_path / "bad_root.txt"
        bad_root.write_text("not a dir")
        r = process("hk", _valid_sub(), _prior_book(),
                    run_events_root=bad_root, rejections_root=bad_root)
        assert isinstance(r.ok, bool)


# ===========================================================================
# to_meta helper
# ===========================================================================

class TestPacketResultMeta:
    def test_to_meta_has_expected_keys(self, monkeypatch, tmp_path):
        monkeypatch.setenv("MASTERMIND_PACKET_GATE", "shadow")
        from control_plane.packet_gate import process
        r = process("autonomous", _valid_sub(), _prior_book(),
                    extras={"run_id": "r", "asof": "2026-07-06", "mandate": "m",
                            "falsifiers": ["x"], "evidence_planes": [], "expected_failure_mode": "y"},
                    run_events_root=tmp_path, rejections_root=tmp_path)
        meta = r.to_meta()
        assert "packet_id" in meta
        assert "ok" in meta
        assert "skipped" in meta
        assert "shadowed" in meta
        assert "errors" in meta

    def test_to_meta_is_json_serialisable(self, monkeypatch, tmp_path):
        monkeypatch.setenv("MASTERMIND_PACKET_GATE", "shadow")
        from control_plane.packet_gate import process
        r = process("autonomous", _valid_sub(), _prior_book(),
                    extras={"run_id": "r", "asof": "2026-07-06", "mandate": "m",
                            "falsifiers": ["x"], "evidence_planes": [], "expected_failure_mode": "y"},
                    run_events_root=tmp_path, rejections_root=tmp_path)
        # must not raise
        json.dumps(r.to_meta())


# ===========================================================================
# governance conformance
# ===========================================================================

class TestGovernanceConformance:
    def test_packet_gate_flag_in_known_flags(self):
        """MASTERMIND_PACKET_GATE must be registered in flags.KNOWN_FLAGS (P8 / hook guard)."""
        from control_plane import flags
        assert "MASTERMIND_PACKET_GATE" in flags.KNOWN_FLAGS, (
            "MASTERMIND_PACKET_GATE must be in KNOWN_FLAGS for the model-routing guard "
            "and governance audit trail."
        )

    def test_packet_gate_in_authority_map_at_a6(self):
        """MASTERMIND_PACKET_GATE must be in config/authority_map.yml at authority_level A6.

        The authority_map.yml has a nested structure: top-level keys are sections
        (e.g. 'flags', 'references'), and flags are under 'flags:'.
        """
        import yaml
        _ROOT = Path(__file__).resolve().parent.parent
        amap_path = _ROOT / "config" / "authority_map.yml"
        assert amap_path.exists(), f"authority_map.yml not found at {amap_path}"
        with amap_path.open(encoding="utf-8") as f:
            amap = yaml.safe_load(f)
        # flags live under the 'flags' section key
        flags_section = amap.get("flags") or amap  # fallback: top-level (backwards compat)
        assert "MASTERMIND_PACKET_GATE" in flags_section, (
            "MASTERMIND_PACKET_GATE must have an entry in config/authority_map.yml under 'flags:'"
        )
        entry = flags_section["MASTERMIND_PACKET_GATE"]
        assert entry.get("authority_level") == "A6", (
            f"Expected MASTERMIND_PACKET_GATE at A6 (book-lifecycle / Fable-human boundary), "
            f"got {entry.get('authority_level')!r}"
        )


class TestNonMutation:
    """process() must never mutate the Brain's submission dict or the prior book —
    shadow-mode zero-behavior-change depends on it (wire-review nit)."""

    def test_process_does_not_mutate_inputs(self, tmp_path):
        import copy
        from control_plane import packet_gate
        submission = {
            "holdings": [{"ticker": "NVDA", "weight": 0.30}],
            "falsifiers": ["If breadth deteriorates for five straight sessions exit the book."],
            "mandate": "Free-form autonomous book seeking asymmetric upside within caps.",
            "expected_failure_mode": "Crowded momentum unwind takes the whole book down at once.",
        }
        prior = {"cash": 50000.0,
                 "positions": {"AAA": {"shares": 100.0, "avg_cost": 190.0, "current_price": 200.0}}}
        sub_before, prior_before = copy.deepcopy(submission), copy.deepcopy(prior)
        res = packet_gate.process("autonomous", submission, prior,
                                  run_events_root=tmp_path, rejections_root=tmp_path)
        assert submission == sub_before
        assert prior == prior_before
        pkt = getattr(res, "packet", None)
        if pkt is not None and getattr(pkt, "falsifiers", None):
            assert pkt.falsifiers is not submission["falsifiers"]  # no aliasing

    def test_rejection_ledger_caps_list_field_prose(self, tmp_path):
        from control_plane.decision_packet import record_rejection
        import json as _json
        huge = "x" * 5000
        record_rejection({"book": "etf", "falsifiers": [huge] * 30,
                          "evidence_planes": [huge]}, ["some error"], root=tmp_path)
        ledger = tmp_path / "data" / "governance" / "packet_rejections.jsonl"
        row = _json.loads(ledger.read_text().splitlines()[-1])
        pk = row.get("packet") or {}
        fals = pk.get("falsifiers") or []
        assert len(fals) <= 21                                   # 20 + truncation marker
        assert all(len(str(f)) <= 1020 for f in fals)
        assert len(str((pk.get("evidence_planes") or [""])[0])) <= 1020
