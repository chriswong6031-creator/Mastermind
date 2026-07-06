"""Tests for control_plane.decision_packet — DecisionPacket substrate (ruling R6, docket M4).

Coverage:
  - Schema: to_dict/from_dict round-trip; all fields present; packet_version=1
  - validate: accept on a well-formed packet; reject per field (missing/malformed)
  - validate: falsifiers sentinel NOT acceptable; liquidity_notes sentinel OK
  - validate: risk_direction consistency — Brain says 'reduce', delta says increase → error
  - validate: forced correction documented via build_packet_from_submission
  - compute_delta: numeric cases including empty prior, all-new, all-dropped, resized
  - record_rejection: round-trip (ledger file created, row parseable); never-raise on garbage
  - build_packet_from_submission: adapter from a realistic heavyweight submission fixture
  - build_packet_from_submission: forced correction (Brain claims 'reduce', delta says increase)
  - never-raise: validate/record_rejection/build_packet_from_submission on garbage inputs
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _good_packet(**overrides):
    """Return a valid DecisionPacket kwargs dict, with optional field overrides."""
    from control_plane.decision_packet import DecisionPacket, compute_delta
    holdings = [
        {"ticker": "NVDA", "weight": 0.30},
        {"ticker": "MSFT", "weight": 0.25},
    ]
    prior = {"holdings": [{"ticker": "NVDA", "weight": 0.25}]}
    delta = compute_delta(holdings, prior)
    kw = dict(
        book="heavyweight",
        asof="2026-07-06",
        mandate="Concentrate Flagship's best ideas in a 5-8 name book.",
        holdings=holdings,
        evidence_planes=["regime", "flows", "fundamentals"],
        source_provenance=["data/flagship/latest.json"],
        falsifiers=["If SPY falls >5% in 3 days without a bounce this book is wrong."],
        liquidity_notes="All names trade >$500M/day; no capacity concern.",
        portfolio_delta=delta,
        expected_failure_mode="Semiconductor earnings miss creates sector-wide selloff.",
        risk_direction="increase",   # gross went 0.25 → 0.55, so increase is correct
        packet_version=1,
        run_id="run_abc123",
        submitted_at="2026-07-06T21:00:00+00:00",
    )
    kw.update(overrides)
    return DecisionPacket(**kw)


def _prior_empty() -> dict:
    return {}


def _prior_with_holdings() -> dict:
    return {
        "holdings": [
            {"ticker": "NVDA", "weight": 0.25},
            {"ticker": "AMZN", "weight": 0.20},
        ]
    }


# ===========================================================================
# Schema — DecisionPacket dataclass
# ===========================================================================

class TestSchema:
    def test_to_dict_round_trip(self):
        """to_dict + from_dict must recover an equivalent packet."""
        from control_plane.decision_packet import DecisionPacket
        p = _good_packet()
        d = p.to_dict()
        p2 = DecisionPacket.from_dict(d)
        assert p2.book == p.book
        assert p2.asof == p.asof
        assert p2.holdings == p.holdings
        assert p2.falsifiers == p.falsifiers
        assert p2.risk_direction == p.risk_direction
        assert p2.packet_version == 1

    def test_to_dict_is_json_serialisable(self):
        p = _good_packet()
        d = p.to_dict()
        # Must not raise
        s = json.dumps(d)
        assert isinstance(s, str)
        assert "heavyweight" in s

    def test_from_dict_ignores_unknown_keys(self):
        """from_dict must not raise on unknown/future fields."""
        from control_plane.decision_packet import DecisionPacket
        p = _good_packet()
        d = p.to_dict()
        d["future_field_v2"] = "some_value"
        p2 = DecisionPacket.from_dict(d)
        assert p2.book == p.book

    def test_packet_version_is_1(self):
        from control_plane.decision_packet import PACKET_VERSION
        assert PACKET_VERSION == 1
        p = _good_packet()
        assert p.packet_version == 1

    def test_all_fields_in_to_dict(self):
        """Every schema field must appear in to_dict()."""
        p = _good_packet()
        d = p.to_dict()
        required_keys = [
            "book", "asof", "mandate", "holdings", "evidence_planes",
            "source_provenance", "falsifiers", "liquidity_notes",
            "portfolio_delta", "expected_failure_mode", "risk_direction",
            "packet_version", "run_id", "submitted_at",
        ]
        for k in required_keys:
            assert k in d, f"Missing key in to_dict(): {k}"

    def test_risk_direction_override_flag_in_dict(self):
        """_risk_direction_overridden must survive round-trip."""
        from control_plane.decision_packet import DecisionPacket
        p = _good_packet()
        d = p.to_dict()
        # The field is included under its dataclass name
        assert "_risk_direction_overridden" in d


# ===========================================================================
# validate — accept
# ===========================================================================

class TestValidateAccept:
    def test_valid_packet_passes(self):
        from control_plane.decision_packet import validate
        p = _good_packet()
        ok, errors = validate(p, _prior_with_holdings())
        assert ok, f"Expected ok=True, got errors: {errors}"
        assert errors == []

    def test_valid_packet_from_dict_passes(self):
        from control_plane.decision_packet import validate
        p = _good_packet()
        d = p.to_dict()
        ok, errors = validate(d, _prior_with_holdings())
        assert ok, f"Expected ok=True, got errors: {errors}"

    def test_empty_prior_book_passes(self):
        """With no prior book, any holdings increase gross from 0 → should pass."""
        from control_plane.decision_packet import validate, compute_delta
        from control_plane.decision_packet import DecisionPacket
        holdings = [{"ticker": "NVDA", "weight": 0.30}]
        delta = compute_delta(holdings, {})
        p = _good_packet(holdings=holdings, portfolio_delta=delta, risk_direction="increase")
        ok, errors = validate(p, {})
        assert ok, errors

    def test_liquidity_notes_sentinel_acceptable(self):
        """liquidity_notes='<not provided>' is OK in v1."""
        from control_plane.decision_packet import validate
        p = _good_packet(liquidity_notes="<not provided>")
        ok, errors = validate(p, _prior_with_holdings())
        assert ok, f"Sentinel in liquidity_notes should be OK. errors: {errors}"

    def test_weights_summing_to_exactly_1_passes(self):
        from control_plane.decision_packet import validate, compute_delta
        holdings = [
            {"ticker": "NVDA", "weight": 0.50},
            {"ticker": "MSFT", "weight": 0.50},
        ]
        delta = compute_delta(holdings, {})
        p = _good_packet(holdings=holdings, portfolio_delta=delta, risk_direction="increase")
        ok, errors = validate(p, {})
        assert ok, errors

    def test_multiple_falsifiers_passes(self):
        from control_plane.decision_packet import validate
        p = _good_packet(falsifiers=[
            "If SPY drops >5% this is wrong.",
            "If Fed hikes unexpectedly, thesis invalid.",
        ])
        ok, errors = validate(p, _prior_with_holdings())
        assert ok, errors


# ===========================================================================
# validate — reject per field
# ===========================================================================

class TestValidateReject:
    def _assert_rejects(self, packet_or_dict, prior, *, contains: str):
        from control_plane.decision_packet import validate
        ok, errors = validate(packet_or_dict, prior)
        assert not ok, "Expected validation to fail"
        combined = " ".join(errors)
        assert contains.lower() in combined.lower(), (
            f"Expected error containing {contains!r}, got: {errors}"
        )

    def test_missing_book(self):
        self._assert_rejects(_good_packet(book=""), {}, contains="book")

    def test_missing_asof(self):
        self._assert_rejects(_good_packet(asof=""), {}, contains="asof")

    def test_missing_mandate(self):
        self._assert_rejects(_good_packet(mandate=""), {}, contains="mandate")

    def test_mandate_sentinel_rejected(self):
        self._assert_rejects(_good_packet(mandate="<not provided>"), {}, contains="mandate")

    def test_missing_expected_failure_mode(self):
        self._assert_rejects(_good_packet(expected_failure_mode=""), {}, contains="expected_failure_mode")

    def test_expected_failure_mode_sentinel_rejected(self):
        self._assert_rejects(_good_packet(expected_failure_mode="<not provided>"), {}, contains="expected_failure_mode")

    def test_invalid_risk_direction(self):
        self._assert_rejects(_good_packet(risk_direction="sideways"), {}, contains="risk_direction")

    def test_holdings_weight_zero_rejected(self):
        from control_plane.decision_packet import compute_delta
        h = [{"ticker": "NVDA", "weight": 0.0}]
        delta = compute_delta(h, {})
        self._assert_rejects(_good_packet(holdings=h, portfolio_delta=delta), {}, contains="weight")

    def test_holdings_weight_above_1_rejected(self):
        from control_plane.decision_packet import compute_delta
        h = [{"ticker": "NVDA", "weight": 1.5}]
        delta = compute_delta(h, {})
        self._assert_rejects(_good_packet(holdings=h, portfolio_delta=delta), {}, contains="weight")

    def test_holdings_sum_exceeds_1_plus_tolerance(self):
        """Weights summing to e.g. 1.05 must fail."""
        from control_plane.decision_packet import compute_delta
        h = [
            {"ticker": "NVDA", "weight": 0.55},
            {"ticker": "MSFT", "weight": 0.55},
        ]
        delta = compute_delta(h, {})
        self._assert_rejects(_good_packet(holdings=h, portfolio_delta=delta, risk_direction="increase"), {}, contains="sum")

    def test_empty_falsifiers_rejected(self):
        self._assert_rejects(_good_packet(falsifiers=[]), {}, contains="falsifiers")

    def test_falsifiers_with_empty_string_rejected(self):
        self._assert_rejects(_good_packet(falsifiers=[""]), {}, contains="falsifiers")

    def test_falsifiers_sentinel_rejected(self):
        """Sentinel in falsifiers must be rejected."""
        self._assert_rejects(
            _good_packet(falsifiers=["<not provided>"]),
            {},
            contains="falsifiers",
        )

    def test_portfolio_delta_not_dict_rejected(self):
        self._assert_rejects(_good_packet(portfolio_delta=None), {}, contains="portfolio_delta")  # type: ignore

    def test_portfolio_delta_missing_key_rejected(self):
        self._assert_rejects(_good_packet(portfolio_delta={"n_added": 1}), {}, contains="portfolio_delta")

    def test_packet_version_zero_rejected(self):
        self._assert_rejects(_good_packet(packet_version=0), {}, contains="packet_version")

    def test_holdings_non_dict_item_rejected(self):
        from control_plane.decision_packet import compute_delta
        h = ["NVDA"]  # type: ignore
        # delta over a bad list still computes (0)
        self._assert_rejects(_good_packet(holdings=h, portfolio_delta={}), {}, contains="holdings")

    def test_garbage_dict_rejected(self):
        from control_plane.decision_packet import validate
        ok, errors = validate({}, {})
        assert not ok
        assert len(errors) > 0


# ===========================================================================
# validate — risk_direction consistency (forced correction path)
# ===========================================================================

class TestValidateRiskDirectionConsistency:
    def test_brain_says_reduce_delta_says_increase_is_error(self):
        """Brain claims 'reduce' but new gross is much larger than prior — must error."""
        from control_plane.decision_packet import validate, compute_delta
        # Prior: 20% gross.  New: 55% gross.  Brain wrongly claims 'reduce'.
        prior = {"holdings": [{"ticker": "AMZN", "weight": 0.20}]}
        holdings = [
            {"ticker": "NVDA", "weight": 0.30},
            {"ticker": "MSFT", "weight": 0.25},
        ]
        delta = compute_delta(holdings, prior)
        p = _good_packet(holdings=holdings, portfolio_delta=delta, risk_direction="reduce")
        ok, errors = validate(p, prior)
        assert not ok
        combined = " ".join(errors)
        assert "risk_direction" in combined.lower()

    def test_brain_says_increase_delta_says_reduce_is_error(self):
        """Brain claims 'increase' but new gross is much smaller than prior."""
        from control_plane.decision_packet import validate, compute_delta
        # Prior: 80% gross.  New: 10% gross.  Brain wrongly claims 'increase'.
        prior = {
            "holdings": [
                {"ticker": "NVDA", "weight": 0.40},
                {"ticker": "MSFT", "weight": 0.40},
            ]
        }
        holdings = [{"ticker": "NVDA", "weight": 0.10}]
        delta = compute_delta(holdings, prior)
        p = _good_packet(holdings=holdings, portfolio_delta=delta, risk_direction="increase")
        ok, errors = validate(p, prior)
        assert not ok
        combined = " ".join(errors)
        assert "risk_direction" in combined.lower()

    def test_correct_risk_direction_passes(self):
        """Brain correctly claims 'preserve' when gross doesn't change much."""
        from control_plane.decision_packet import validate, compute_delta
        prior = {"holdings": [{"ticker": "NVDA", "weight": 0.30}]}
        holdings = [{"ticker": "NVDA", "weight": 0.305}]   # tiny resize, preserve
        delta = compute_delta(holdings, prior)
        p = _good_packet(holdings=holdings, portfolio_delta=delta, risk_direction="preserve")
        ok, errors = validate(p, prior)
        assert ok, errors

    def test_increase_direction_correct_when_gross_grows(self):
        """Brain claims 'increase' and gross did grow >2pp — passes."""
        from control_plane.decision_packet import validate, compute_delta
        prior = {"holdings": [{"ticker": "NVDA", "weight": 0.20}]}
        holdings = [
            {"ticker": "NVDA", "weight": 0.25},
            {"ticker": "MSFT", "weight": 0.30},
        ]
        delta = compute_delta(holdings, prior)
        p = _good_packet(holdings=holdings, portfolio_delta=delta, risk_direction="increase")
        ok, errors = validate(p, prior)
        assert ok, errors


# ===========================================================================
# compute_delta — numeric cases
# ===========================================================================

class TestComputeDelta:
    def test_empty_prior_and_new(self):
        from control_plane.decision_packet import compute_delta
        d = compute_delta([], {})
        assert d["n_added"]      == 0
        assert d["n_dropped"]    == 0
        assert d["n_resized"]    == 0
        assert d["gross_before"] == 0.0
        assert d["gross_after"]  == 0.0
        assert d["turnover"]     == 0.0

    def test_all_new_names(self):
        """All proposed names are new — n_added = 2, n_dropped = 0."""
        from control_plane.decision_packet import compute_delta
        holdings = [
            {"ticker": "NVDA", "weight": 0.30},
            {"ticker": "MSFT", "weight": 0.25},
        ]
        d = compute_delta(holdings, {})
        assert d["n_added"]   == 2
        assert d["n_dropped"] == 0
        assert abs(d["gross_after"] - 0.55) < 1e-6
        assert d["gross_before"] == 0.0

    def test_all_dropped(self):
        """All prior names are absent from new — n_dropped = 2."""
        from control_plane.decision_packet import compute_delta
        prior = {"holdings": [
            {"ticker": "NVDA", "weight": 0.30},
            {"ticker": "AMZN", "weight": 0.20},
        ]}
        d = compute_delta([], prior)
        assert d["n_dropped"] == 2
        assert d["n_added"]   == 0
        assert d["gross_after"] == 0.0

    def test_resize_counted(self):
        """A name held at different weight is a resize."""
        from control_plane.decision_packet import compute_delta
        prior = {"holdings": [{"ticker": "NVDA", "weight": 0.20}]}
        holdings = [{"ticker": "NVDA", "weight": 0.35}]
        d = compute_delta(holdings, prior)
        assert d["n_resized"] == 1
        assert d["n_added"]   == 0
        assert d["n_dropped"] == 0

    def test_turnover_calculation(self):
        """turnover = sum(abs(Δw)) / 2."""
        from control_plane.decision_packet import compute_delta
        # prior: NVDA=0.30, AMZN=0.20 (total 0.50)
        # new:   NVDA=0.40, MSFT=0.25  (AMZN dropped, MSFT added)
        prior = {"holdings": [
            {"ticker": "NVDA", "weight": 0.30},
            {"ticker": "AMZN", "weight": 0.20},
        ]}
        holdings = [
            {"ticker": "NVDA", "weight": 0.40},
            {"ticker": "MSFT", "weight": 0.25},
        ]
        d = compute_delta(holdings, prior)
        # NVDA: |0.40 - 0.30| = 0.10
        # AMZN: |0.00 - 0.20| = 0.20
        # MSFT: |0.25 - 0.00| = 0.25
        # sum = 0.55 / 2 = 0.275
        assert abs(d["turnover"] - 0.275) < 1e-4
        assert d["n_added"]   == 1   # MSFT
        assert d["n_dropped"] == 1   # AMZN
        assert d["n_resized"] == 1   # NVDA

    def test_paper_account_positions_shape(self):
        """Should handle paper_account._load_account shape: {"positions": {ticker: {...}}}."""
        from control_plane.decision_packet import compute_delta
        prior = {"positions": {"NVDA": {"weight": 0.25, "shares": 100}}}
        holdings = [{"ticker": "NVDA", "weight": 0.30}]
        d = compute_delta(holdings, prior)
        assert d["n_resized"] == 1
        assert abs(d["gross_before"] - 0.25) < 1e-6

    def test_case_insensitive_ticker_matching(self):
        """Ticker matching is case-insensitive."""
        from control_plane.decision_packet import compute_delta
        prior = {"holdings": [{"ticker": "nvda", "weight": 0.25}]}
        holdings = [{"ticker": "NVDA", "weight": 0.25}]
        d = compute_delta(holdings, prior)
        # Same ticker, same weight → no resize, no add, no drop
        assert d["n_resized"] == 0
        assert d["n_added"]   == 0
        assert d["n_dropped"] == 0

    def test_garbage_inputs_return_zeros(self):
        """compute_delta never raises on garbage."""
        from control_plane.decision_packet import compute_delta
        d = compute_delta(None, None)  # type: ignore
        assert isinstance(d, dict)
        assert d["n_added"] == 0


# ===========================================================================
# record_rejection — round-trip and never-raise
# ===========================================================================

class TestRecordRejection:
    def _read_rejections(self, root: Path) -> list[dict]:
        p = root / "data" / "governance" / "packet_rejections.jsonl"
        if not p.exists():
            return []
        rows = []
        for line in p.read_text().splitlines():
            line = line.strip()
            if line:
                rows.append(json.loads(line))
        return rows

    def test_round_trip(self, tmp_path):
        from control_plane.decision_packet import record_rejection
        p = _good_packet()
        d = p.to_dict()
        rid = record_rejection(d, ["error: falsifiers empty"], root=tmp_path)
        assert rid is not None
        assert len(rid) == 16

        rows = self._read_rejections(tmp_path)
        assert len(rows) == 1
        row = rows[0]
        assert row["book"] == "heavyweight"
        assert row["asof"] == "2026-07-06"
        assert "error: falsifiers empty" in row["errors"]
        assert row["rejection_id"] == rid

    def test_multiple_rejections_appended(self, tmp_path):
        from control_plane.decision_packet import record_rejection
        for i in range(3):
            record_rejection({"book": f"b{i}", "asof": "2026-07-06"}, [f"err{i}"], root=tmp_path)
        rows = self._read_rejections(tmp_path)
        assert len(rows) == 3

    def test_never_raises_on_garbage_packet(self, tmp_path):
        from control_plane.decision_packet import record_rejection
        result = record_rejection(None, None, root=tmp_path)  # type: ignore
        # Must not raise; may return None
        assert result is None or isinstance(result, str)

    def test_never_raises_on_unwritable_dir(self, monkeypatch):
        from control_plane import decision_packet as dp_mod
        monkeypatch.setattr(dp_mod, "_rejections_path", lambda *a, **kw: (_ for _ in ()).throw(PermissionError("no")))
        p = _good_packet()
        result = dp_mod.record_rejection(p.to_dict(), ["err"])
        assert result is None

    def test_stored_packet_has_ts(self, tmp_path):
        from control_plane.decision_packet import record_rejection
        p = _good_packet()
        record_rejection(p.to_dict(), ["some error"], root=tmp_path)
        rows = self._read_rejections(tmp_path)
        assert "ts" in rows[0]
        assert rows[0]["ts"]  # non-empty

    def test_long_errors_truncated(self, tmp_path):
        from control_plane.decision_packet import record_rejection
        long_err = "x" * 1000
        record_rejection(_good_packet().to_dict(), [long_err], root=tmp_path)
        rows = self._read_rejections(tmp_path)
        assert len(rows[0]["errors"][0]) <= 310  # 300 + small overhead


# ===========================================================================
# build_packet_from_submission — adapter
# ===========================================================================

class TestBuildPacketFromSubmission:
    def _heavyweight_fixture(self) -> dict:
        """A realistic heavyweight submit_book payload (from brain/heavyweight_mcp.py shape)."""
        return {
            "holdings": [
                {
                    "ticker": "NVDA",
                    "weight": 0.35,
                    "rationale": "Dominant AI-era GPU monopoly; Flagship's top-conviction name.",
                    "conviction": "high",
                },
                {
                    "ticker": "MSFT",
                    "weight": 0.25,
                    "rationale": "Azure AI cycle with multi-year revenue growth; broad moat.",
                    "conviction": "high",
                },
            ],
            "summary": "Concentrated AI-cycle book pressing Flagship's highest-conviction names.",
            "sold_note": "Trimmed AMZN — lower AI purity vs NVDA/MSFT.",
            "gross": 0.60,
        }

    def _prior_book_fixture(self) -> dict:
        """Prior book at 20% gross."""
        return {
            "holdings": [
                {"ticker": "AMZN", "weight": 0.12},
                {"ticker": "NVDA", "weight": 0.08},
            ]
        }

    def test_adapter_produces_packet(self):
        from control_plane.decision_packet import build_packet_from_submission, DecisionPacket
        sub = self._heavyweight_fixture()
        prior = self._prior_book_fixture()
        extras = {
            "run_id": "run_hw_001",
            "asof": "2026-07-06",
            "mandate": "Concentrate Flagship's best 5-8 ideas with 5–50% each.",
            "evidence_planes": ["flagship_book", "regime"],
            "source_provenance": ["data/flagship/latest.json"],
            "falsifiers": ["If Flagship trims NVDA >10pp this book is stale."],
            "expected_failure_mode": "AI capex pullback collapses NVDA earnings.",
        }
        packet = build_packet_from_submission("heavyweight", sub, prior, extras)
        assert isinstance(packet, DecisionPacket)
        assert packet.book == "heavyweight"
        assert packet.asof == "2026-07-06"
        assert len(packet.holdings) == 2
        assert packet.run_id == "run_hw_001"
        assert packet.mandate == extras["mandate"]

    def test_adapter_computes_correct_delta(self):
        """portfolio_delta must be computed, not Brain-supplied."""
        from control_plane.decision_packet import build_packet_from_submission, compute_delta
        sub = self._heavyweight_fixture()
        prior = self._prior_book_fixture()
        packet = build_packet_from_submission("heavyweight", sub, prior, {"asof": "2026-07-06"})
        expected = compute_delta(
            [{"ticker": "NVDA", "weight": 0.35}, {"ticker": "MSFT", "weight": 0.25}],
            prior,
        )
        assert packet.portfolio_delta["n_added"]   == expected["n_added"]
        assert packet.portfolio_delta["n_dropped"] == expected["n_dropped"]
        assert packet.portfolio_delta["gross_after"] == pytest.approx(expected["gross_after"], abs=1e-4)

    def test_adapter_forced_correction_risk_direction(self):
        """Brain claims 'reduce', but gross goes from 0.20 to 0.60 → forced to 'increase'."""
        from control_plane.decision_packet import build_packet_from_submission
        sub = self._heavyweight_fixture()
        prior = self._prior_book_fixture()
        extras = {
            "asof": "2026-07-06",
            "risk_direction": "reduce",    # WRONG — Brain lies
            "falsifiers": ["If NVDA falls >15% the thesis is dead."],
        }
        packet = build_packet_from_submission("heavyweight", sub, prior, extras)
        assert packet.risk_direction == "increase", (
            f"Expected forced correction to 'increase', got {packet.risk_direction!r}"
        )
        assert packet._risk_direction_overridden is True

    def test_adapter_no_override_when_brain_correct(self):
        """No override flag when Brain's claim matches computed direction."""
        from control_plane.decision_packet import build_packet_from_submission
        sub = self._heavyweight_fixture()
        prior = self._prior_book_fixture()
        extras = {
            "asof": "2026-07-06",
            "risk_direction": "increase",  # correct
            "falsifiers": ["If NVDA falls >15% the thesis is dead."],
        }
        packet = build_packet_from_submission("heavyweight", sub, prior, extras)
        assert packet.risk_direction == "increase"
        assert packet._risk_direction_overridden is False

    def test_adapter_absent_narrative_fields_get_sentinel(self):
        """Fields not in extras default to '<not provided>'."""
        from control_plane.decision_packet import build_packet_from_submission, _SENTINEL
        sub = self._heavyweight_fixture()
        packet = build_packet_from_submission("heavyweight", sub, {}, {"asof": "2026-07-06"})
        assert packet.mandate == _SENTINEL
        assert packet.liquidity_notes == _SENTINEL
        assert packet.expected_failure_mode == _SENTINEL

    def test_adapter_packet_validates_with_full_extras(self):
        """A fully-populated adapter output must pass validate()."""
        from control_plane.decision_packet import build_packet_from_submission, validate
        sub = self._heavyweight_fixture()
        prior = self._prior_book_fixture()
        extras = {
            "asof": "2026-07-06",
            "mandate": "Concentrate Flagship's best ideas.",
            "evidence_planes": ["regime", "flagship_book"],
            "source_provenance": ["data/flagship/latest.json"],
            "falsifiers": ["If NVDA trims in Flagship this book is wrong."],
            "liquidity_notes": "All names >$500M/day ADV.",
            "expected_failure_mode": "AI earnings miss.",
            "risk_direction": "increase",
        }
        packet = build_packet_from_submission("heavyweight", sub, prior, extras)
        ok, errors = validate(packet, prior)
        assert ok, f"Fully-specified adapter packet should pass: {errors}"

    def test_adapter_never_raises_on_garbage(self):
        """build_packet_from_submission must never raise, even on garbage inputs."""
        from control_plane.decision_packet import build_packet_from_submission, DecisionPacket
        result = build_packet_from_submission(None, None, None, None)  # type: ignore
        assert isinstance(result, DecisionPacket)

    def test_adapter_submitted_at_is_set(self):
        from control_plane.decision_packet import build_packet_from_submission
        sub = self._heavyweight_fixture()
        packet = build_packet_from_submission("heavyweight", sub, {}, {"asof": "2026-07-06"})
        assert packet.submitted_at != ""
        # should parse as UTC ISO
        from datetime import datetime, timezone
        dt = datetime.fromisoformat(packet.submitted_at)
        assert dt.tzinfo is not None

    def test_adapter_holdings_normalised_uppercase(self):
        """Ticker keys in the returned holdings must be uppercase."""
        from control_plane.decision_packet import build_packet_from_submission
        sub = {
            "holdings": [{"ticker": "nvda", "weight": 0.30, "rationale": "test"}],
            "summary": "test",
        }
        packet = build_packet_from_submission("test_book", sub, {}, {"asof": "2026-07-06"})
        assert all(h["ticker"] == h["ticker"].upper() for h in packet.holdings)


# ===========================================================================
# never-raise on garbage
# ===========================================================================

class TestNeverRaise:
    def test_validate_on_none(self):
        from control_plane.decision_packet import validate
        ok, errors = validate(None, {})  # type: ignore
        assert not ok
        assert len(errors) > 0

    def test_validate_on_string(self):
        from control_plane.decision_packet import validate
        ok, errors = validate("not a packet", {})  # type: ignore
        assert not ok

    def test_record_rejection_on_none(self, tmp_path):
        from control_plane.decision_packet import record_rejection
        # Must not raise
        record_rejection(None, None, root=tmp_path)  # type: ignore

    def test_build_packet_on_all_none(self):
        from control_plane.decision_packet import build_packet_from_submission
        # Must not raise
        build_packet_from_submission(None, None, None, None)  # type: ignore

    def test_compute_delta_on_none(self):
        from control_plane.decision_packet import compute_delta
        # Must not raise
        d = compute_delta(None, None)  # type: ignore
        assert isinstance(d, dict)


# ---------------------------------------------------------------------------
# re-review fixes (2026-07-06): real account shape, substance floor, from_dict
# ---------------------------------------------------------------------------

class TestRealAccountShape:
    """compute_delta shape-1 against the REAL paper_account._load_account shape:
    positions are {shares, avg_cost, current_price} with NO weight key, plus a
    top-level cash float. Weights must be DERIVED (shares*px / NAV)."""

    def _real_account(self):
        # 2 positions: 100 sh @ $200 = 20k, 300 sh @ $100 = 30k; cash 50k → NAV 100k
        return {
            "inception_date": "2026-06-18",
            "starting_nav": 100000.0,
            "cash": 50000.0,
            "positions": {
                "AAA": {"shares": 100.0, "avg_cost": 190.0, "current_price": 200.0},
                "BBB": {"shares": 300.0, "avg_cost": 105.0, "current_price": 100.0},
            },
            "spy_shares": 160.2,
            "spy_inception_price": 624.0,
        }

    def test_weights_derived_from_shares_and_price(self):
        from control_plane.decision_packet import compute_delta
        d = compute_delta([{"ticker": "AAA", "weight": 0.20}], self._real_account())
        assert abs(d["gross_before"] - 0.5) < 1e-6          # 50k invested / 100k NAV
        assert d["n_dropped"] == 1                           # BBB exits
        assert d["n_resized"] == 0                           # AAA 0.20 vs 0.20 held
        assert d["gross_after"] == 0.2

    def test_derisking_detected_not_stamped_increase(self):
        from control_plane.decision_packet import compute_delta
        # cutting to a single 10% position from 50% gross IS a reduction
        d = compute_delta([{"ticker": "AAA", "weight": 0.10}], self._real_account())
        assert d["gross_after"] < d["gross_before"]

    def test_avg_cost_fallback_when_no_current_price(self):
        from control_plane.decision_packet import compute_delta
        acct = self._real_account()
        del acct["positions"]["AAA"]["current_price"]        # falls back to avg_cost 190
        d = compute_delta([], acct)
        # NAV = 50k + 100*190 + 300*100 = 99k; gross_before = 49k/99k
        assert abs(d["gross_before"] - (49000.0 / 99000.0)) < 1e-6


class TestSubstanceFloor:
    JUNK = [".", "n/a", "N/A", "-", "none", "tbd", "?", "x", "short one"]

    @pytest.mark.parametrize("junk", JUNK)
    def test_junk_falsifiers_fail(self, junk):
        from control_plane.decision_packet import validate
        p = _good_packet(falsifiers=[junk])
        ok, errors = validate(p, _prior_with_holdings())
        assert not ok and any("falsifiers[0]" in e for e in errors)

    @pytest.mark.parametrize("junk", [".", "n/a", "tbd"])
    def test_junk_mandate_fails(self, junk):
        from control_plane.decision_packet import validate
        ok, errors = validate(_good_packet(mandate=junk), _prior_with_holdings())
        assert not ok and any("mandate" in e for e in errors)

    def test_junk_expected_failure_mode_fails(self):
        from control_plane.decision_packet import validate
        ok, errors = validate(_good_packet(expected_failure_mode="n/a"),
                              _prior_with_holdings())
        assert not ok

    def test_substantive_prose_still_passes(self):
        from control_plane.decision_packet import validate
        ok, errors = validate(_good_packet(), _prior_with_holdings())
        assert ok, errors


class TestFromDictStructuredErrors:
    def test_partial_dict_yields_per_field_errors_not_typeerror(self):
        from control_plane.decision_packet import DecisionPacket, validate
        p = DecisionPacket.from_dict({"book": "etf"})       # must not raise
        ok, errors = validate(p, {})
        assert not ok
        joined = " ".join(errors)
        assert "mandate" in joined and "falsifiers" in joined
        assert "TypeError" not in joined                     # structured, not opaque


class TestShape3ReservedKeys:
    def test_bookkeeping_scalars_not_counted_as_tickers(self):
        from control_plane.decision_packet import compute_delta
        d = compute_delta([], {"AAA": 0.4, "spy_shares": 160.2, "starting_nav": 1000000.0,
                               "cash_yield_through": 0.05})
        # spy_shares/starting_nav excluded (reserved or out-of-range); cash_yield_through
        # is reserved even though 0.05 looks like a weight
        assert abs(d["gross_before"] - 0.4) < 1e-9
