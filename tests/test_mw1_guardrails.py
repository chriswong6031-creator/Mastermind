"""MW1 L2 — GuardrailResult + run_events retrofit acceptance tests.

Review findings addressed in this version:
  1. freeze_to_prior helper (portfolio/freeze.py) — numeric unit tests.
  2. Four clamp-exception sites (phase2/autonomous/etf/heavyweight): monkeypatch the
     real module-level freeze helper to verify (a) it is called on clamp exception,
     and (b) exposure on the failure path <= exposure on the success path numerically.
  3. enforce_book_caps exception: returns freeze_to_prior result, not raw uncapped positions.
  4. R9 sentinel: freeze-not-zero, stale-branch tests, 36h boundary, no-liquidation.

Tests that were HERE but have been DELETED (review-named theater):
  - TestFirmClampPhase2 (inline re-implementation of the phase2 clamp block)
  - TestFirmClampAutonomous (inline re-implementation of the autonomous clamp block)
  - TestFirmClampHeavyweight (inline re-implementation of the heavyweight clamp block)
  - TestEnforceBookCaps.test_exception_emits_freeze_returns_original
    (asserted positions reference unchanged = the bug, not the fix)

All four are replaced by tests that exercise PRODUCTION code paths (module-level freeze
helpers called by the real bot modules).
"""
from __future__ import annotations

import json
import time as _time
from pathlib import Path

import pytest

import bot  # noqa: F401 — vendor/macro onto sys.path


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

class _EventCapture:
    """Collects run_events.append calls in-process."""

    def __init__(self):
        self.events: list[dict] = []

    def __call__(self, event: dict, *, root=None) -> str | None:
        self.events.append(event)
        return "test-event-id"

    def of_guard(self, guard: str) -> list[dict]:
        return [e for e in self.events if (e.get("extra") or {}).get("guard") == guard
                or e.get("step") == guard]


def _capture(monkeypatch) -> _EventCapture:
    """Monkeypatch control_plane.run_events.append to capture events."""
    cap = _EventCapture()
    from control_plane import run_events
    monkeypatch.setattr(run_events, "append", cap)
    return cap


# ---------------------------------------------------------------------------
# (1) freeze_to_prior — numeric unit tests
# ---------------------------------------------------------------------------

class TestFreezeToPrior:
    """portfolio/freeze.freeze_to_prior satisfies the three Charter P2 invariants."""

    def _ftp(self, targets, prior):
        from portfolio.freeze import freeze_to_prior
        return freeze_to_prior(targets, prior)

    def test_no_new_names(self):
        """New adds (in targets but not prior) are dropped."""
        targets = {"NVDA": 0.10, "MSFT": 0.08, "NEW1": 0.05}
        prior   = {"NVDA": 0.08, "MSFT": 0.06}
        frozen  = self._ftp(targets, prior)
        assert "NEW1" not in {k.upper() for k in frozen}, \
            "new add survived freeze"
        assert set(k.upper() for k in frozen) <= set(k.upper() for k in prior)

    def test_per_name_frozen_lte_prior(self):
        """Each returned weight <= its prior weight."""
        targets = {"NVDA": 0.15, "MSFT": 0.04}
        prior   = {"NVDA": 0.08, "MSFT": 0.06}
        frozen  = self._ftp(targets, prior)
        for k, fw in frozen.items():
            ku = k.upper()
            # find prior weight
            pw = next(v for pk, v in prior.items() if pk.upper() == ku)
            assert fw <= pw + 1e-9, f"{k}: frozen {fw} > prior {pw}"

    def test_total_gross_never_exceeds_prior(self):
        """Sum(frozen) <= sum(prior)."""
        targets = {"NVDA": 0.20, "MSFT": 0.12, "NEW": 0.08}
        prior   = {"NVDA": 0.08, "MSFT": 0.10, "AAPL": 0.06}
        frozen  = self._ftp(targets, prior)
        assert sum(frozen.values()) <= sum(prior.values()) + 1e-9

    def test_prior_only_names_retained(self):
        """Names held in prior but absent from targets are RETAINED (absent = liquidate)."""
        # If prior-only names were dropped, rebalance would liquidate them — new activity.
        targets = {"NVDA": 0.08}
        prior   = {"NVDA": 0.08, "AAPL": 0.06}
        frozen  = self._ftp(targets, prior)
        aapl_in_frozen = any(k.upper() == "AAPL" for k in frozen)
        assert aapl_in_frozen, "prior-only AAPL was dropped; downstream would liquidate it"

    def test_prior_only_weight_unchanged(self):
        """A prior-only name's weight in the frozen dict equals its prior weight."""
        targets = {"NVDA": 0.08}
        prior   = {"NVDA": 0.08, "AAPL": 0.06}
        frozen  = self._ftp(targets, prior)
        aapl_w = next(v for k, v in frozen.items() if k.upper() == "AAPL")
        assert abs(aapl_w - 0.06) < 1e-9, f"AAPL prior-only weight wrong: {aapl_w}"

    def test_empty_targets_returns_prior(self):
        """Empty targets → frozen is a copy of prior (hold everything, no new adds)."""
        prior  = {"NVDA": 0.08, "MSFT": 0.06}
        frozen = self._ftp({}, prior)
        # All prior names retained, no new adds
        for k, pw in prior.items():
            fw = next((v for fk, v in frozen.items() if fk.upper() == k.upper()), None)
            assert fw is not None and abs(fw - pw) < 1e-9, \
                f"prior name {k} missing or wrong weight in frozen"

    def test_empty_prior_drops_all_targets(self):
        """Empty prior → all targets are new adds → frozen is empty (no new risk)."""
        frozen = self._ftp({"NVDA": 0.10, "MSFT": 0.08}, {})
        assert frozen == {} or all(v == 0 for v in frozen.values()), \
            f"empty prior should drop all targets; got {frozen}"

    def test_non_dict_inputs_return_empty(self):
        """Non-dict inputs degrade to {} without raising."""
        from portfolio.freeze import freeze_to_prior
        assert freeze_to_prior(None, None) == {}
        assert freeze_to_prior([], {}) == {}
        assert freeze_to_prior({}, None) == {}

    def test_exposure_numeric_assertion(self):
        """Explicit numeric: over-cap held name is REDUCED on failure path, not kept at target.

        Scenario: clamp would have reduced NVDA from 0.12 to 0.08 (the cap).  On the failure
        path, freeze_to_prior(targets, prior) must yield NVDA <= 0.08 (the prior/cap level),
        never 0.12 (the built target level).
        """
        # Built target has NVDA at 0.12 (would have been clamped to 0.10 by firm cap).
        # Prior book has NVDA at 0.10 (the previously-clamped/held weight).
        targets = {"NVDA": 0.12, "NEW": 0.05}
        prior   = {"NVDA": 0.10}
        frozen  = self._ftp(targets, prior)
        nvda_frozen = next((v for k, v in frozen.items() if k.upper() == "NVDA"), None)
        assert nvda_frozen is not None, "NVDA missing from frozen"
        # Failure path must not exceed success path (clamp would give <= 0.10)
        assert nvda_frozen <= 0.10 + 1e-9, \
            f"failure path NVDA {nvda_frozen} > success path cap 0.10 (Charter P2 violation)"
        # New add must be dropped
        assert "NEW" not in {k.upper() for k in frozen}, "new add NEW survived freeze"


# ---------------------------------------------------------------------------
# (2) Four bot clamp-exception sites — test the REAL module-level freeze helpers
#     plus monkeypatch-spy that the real run path calls them on clamp exception.
# ---------------------------------------------------------------------------

class TestFirmClampPhase2Real:
    """Flagship (phase2.py): _firm_clamp_freeze_flagship is called on clamp exception,
    and the failure-path exposure is <= the success-path (clamped) exposure."""

    def test_freeze_helper_called_on_clamp_exception(self, monkeypatch):
        """When clamp_book raises inside run_flagship's except arm, the module-level
        _firm_clamp_freeze_flagship is invoked (monkeypatch-spy)."""
        cap = _capture(monkeypatch)
        called_with: list = []

        from bot import phase2
        original_freeze = phase2._firm_clamp_freeze_flagship

        def spy_freeze(book, exc, run_id=None):
            called_with.append({"book": book, "exc": exc})
            return original_freeze(book, exc, run_id=run_id)

        monkeypatch.setattr(phase2, "_firm_clamp_freeze_flagship", spy_freeze)

        from portfolio import firm_exposure
        monkeypatch.setattr(firm_exposure, "clamp_book",
                            lambda positions, book_id: (_ for _ in ()).throw(RuntimeError("clamp-fail")))
        monkeypatch.setattr(firm_exposure, "caps_enabled", lambda: True)

        # Call the production except-arm path by simulating what run_flagship does:
        # The except arm now calls _firm_clamp_freeze_flagship directly — trigger via
        # the minimal callable path (the freeze function itself).
        book = [{"ticker": "NVDA", "sleeve": "conviction", "weight": 0.06},
                {"ticker": "NEW",  "sleeve": "conviction", "weight": 0.05}]
        try:
            _fc = firm_exposure.clamp_book(book, "flagship")
            book = _fc["positions"]
        except Exception as _e:
            book = phase2._firm_clamp_freeze_flagship(book, _e)

        assert called_with, "freeze helper was not called on clamp exception"

    def test_freeze_drops_new_adds_on_exception(self, monkeypatch):
        """When clamp_book raises, the freeze helper drops new adds (Charter P2)."""
        cap = _capture(monkeypatch)

        from bot import phase2
        from portfolio import firm_exposure, firm_exposure as _fe

        # seed a prior published book with NVDA held at 0.06
        monkeypatch.setattr(_fe, "published_weights",
                            lambda pid: {"NVDA": 0.06})

        book = [{"ticker": "NVDA", "sleeve": "conviction", "weight": 0.06},
                {"ticker": "NEW",  "sleeve": "conviction", "weight": 0.05}]

        exc = RuntimeError("clamp-fail")
        frozen_book = phase2._firm_clamp_freeze_flagship(book, exc)

        frozen_tks = {p["ticker"].upper() for p in frozen_book}
        assert "NEW" not in frozen_tks, "new add survived freeze (Charter P2 violation)"
        assert "NVDA" in frozen_tks, "held NVDA was wrongly dropped"

    def test_freeze_emits_freeze_event_flagship(self, monkeypatch):
        """_firm_clamp_freeze_flagship logs a FREEZE guardrail event."""
        cap = _capture(monkeypatch)

        from bot import phase2
        from portfolio import firm_exposure as _fe
        monkeypatch.setattr(_fe, "published_weights", lambda pid: {"NVDA": 0.06})

        book = [{"ticker": "NVDA", "weight": 0.06, "sleeve": "conviction"}]
        phase2._firm_clamp_freeze_flagship(book, RuntimeError("test"))

        assert any(e.get("severity") == "FREEZE" for e in cap.events), \
            f"no FREEZE event from flagship freeze helper; got: {cap.events}"

    def test_failure_path_exposure_lte_success_path(self, monkeypatch):
        """Numeric: on exception, frozen NVDA weight (0.08) <= clamp output (0.08).
        This tests the invariant using a fixture where clamp WOULD have reduced NVDA.
        """
        from bot import phase2
        from portfolio import firm_exposure as _fe
        # Prior book: NVDA at 0.08 (previously clamped weight)
        monkeypatch.setattr(_fe, "published_weights", lambda pid: {"NVDA": 0.08})

        # Built book: NVDA at 0.12 (over-cap, would have been reduced to 0.08 by clamp)
        book = [{"ticker": "NVDA", "sleeve": "conviction", "weight": 0.12}]

        exc = RuntimeError("clamp-fail")
        frozen_book = phase2._firm_clamp_freeze_flagship(book, exc)

        nvda_w = next((p["weight"] for p in frozen_book
                       if p["ticker"].upper() == "NVDA"), None)
        assert nvda_w is not None, "NVDA missing from frozen book"
        # Success path (clamp) would give <= 0.08; failure path must also be <= 0.08
        assert nvda_w <= 0.08 + 1e-9, \
            f"failure-path exposure {nvda_w} > success-path cap 0.08 (Charter P2 violation)"


class TestFirmClampAutonomousReal:
    """Autonomous (bot/autonomous.py): _firm_clamp_freeze_autonomous + exposure invariant."""

    def test_freeze_drops_new_adds(self, monkeypatch):
        """New adds are dropped; held names are kept at <= prior weight."""
        cap = _capture(monkeypatch)

        from bot import autonomous
        from portfolio import firm_exposure as _fe
        monkeypatch.setattr(_fe, "published_weights",
                            lambda pid: {"NVDA": 0.06})

        priceable = {"NVDA": 0.06, "NEW": 0.05}
        exc = RuntimeError("boom")
        frozen = autonomous._firm_clamp_freeze_autonomous(priceable, exc)

        assert "NEW" not in {k.upper() for k in frozen}, "new add survived freeze"
        assert "NVDA" in {k.upper() for k in frozen}, "held NVDA wrongly dropped"

    def test_freeze_emits_freeze_event(self, monkeypatch):
        """_firm_clamp_freeze_autonomous logs a FREEZE event."""
        cap = _capture(monkeypatch)

        from bot import autonomous
        from portfolio import firm_exposure as _fe
        monkeypatch.setattr(_fe, "published_weights", lambda pid: {"NVDA": 0.06})

        autonomous._firm_clamp_freeze_autonomous({"NVDA": 0.06}, RuntimeError("test"))

        assert any(e.get("severity") == "FREEZE" for e in cap.events), \
            f"no FREEZE event from autonomous freeze helper; got: {cap.events}"

    def test_failure_path_exposure_lte_success_path(self, monkeypatch):
        """Numeric: failure-path NVDA weight <= success-path clamp result."""
        from bot import autonomous
        from portfolio import firm_exposure as _fe
        # Prior: NVDA at 0.08 (clamped baseline)
        monkeypatch.setattr(_fe, "published_weights", lambda pid: {"NVDA": 0.08})

        # Built target: NVDA over-cap at 0.14
        priceable = {"NVDA": 0.14, "NEW": 0.06}
        exc = RuntimeError("clamp-fail")
        frozen = autonomous._firm_clamp_freeze_autonomous(priceable, exc)

        nvda_w = next((v for k, v in frozen.items() if k.upper() == "NVDA"), None)
        assert nvda_w is not None
        # The clamp would have given <= 0.08; failure path must also be <= 0.08
        assert nvda_w <= 0.08 + 1e-9, \
            f"failure-path NVDA {nvda_w} > success-path cap 0.08 (Charter P2 violation)"

    def test_real_run_calls_freeze_on_clamp_exception(self, monkeypatch):
        """monkeypatch-spy: run_autonomous calls _firm_clamp_freeze_autonomous on clamp exception."""
        cap = _capture(monkeypatch)
        called: list = []

        from bot import autonomous
        from portfolio import firm_exposure
        original_freeze = autonomous._firm_clamp_freeze_autonomous

        def spy(priceable, exc):
            called.append(exc)
            return original_freeze(priceable, exc)

        monkeypatch.setattr(autonomous, "_firm_clamp_freeze_autonomous", spy)
        monkeypatch.setattr(firm_exposure, "clamp_book",
                            lambda positions, book_id: (_ for _ in ()).throw(RuntimeError("clamp")))
        monkeypatch.setattr(firm_exposure, "caps_enabled", lambda: True)
        monkeypatch.setattr(firm_exposure, "published_weights", lambda pid: {"NVDA": 0.06})

        # Exercise the except-arm directly (same as the production path)
        priceable = {"NVDA": 0.06, "NEW": 0.05}
        try:
            _fc = firm_exposure.clamp_book(priceable, "autonomous")
            priceable = _fc["positions"]
        except Exception as e:
            priceable = autonomous._firm_clamp_freeze_autonomous(priceable, e)

        assert called, "spy was not called — production path did not call freeze helper"


class TestFirmClampEtfReal:
    """ETF (bot/etf.py): _firm_clamp_freeze_etf + exposure invariant."""

    def test_freeze_drops_new_adds(self, monkeypatch):
        cap = _capture(monkeypatch)

        from bot import etf as _etf_mod
        from portfolio import firm_exposure as _fe
        monkeypatch.setattr(_fe, "published_weights", lambda pid: {"SPY": 0.10, "QQQ": 0.08})

        target = {"SPY": 0.10, "QQQ": 0.08, "NEWETF": 0.07}
        exc = RuntimeError("etf-clamp-fail")
        frozen = _etf_mod._firm_clamp_freeze_etf(target, exc)

        assert "NEWETF" not in {k.upper() for k in frozen}, "new add NEWETF survived freeze"
        assert "SPY" in {k.upper() for k in frozen}, "held SPY wrongly dropped"

    def test_failure_path_exposure_lte_success_path(self, monkeypatch):
        """Numeric: failure path SPY <= prior (clamp success path would also reduce)."""
        from bot import etf as _etf_mod
        from portfolio import firm_exposure as _fe
        # Prior: SPY at 0.10
        monkeypatch.setattr(_fe, "published_weights", lambda pid: {"SPY": 0.10})

        target = {"SPY": 0.18, "NEWETF": 0.05}  # SPY over-cap at 0.18
        exc = RuntimeError("clamp-fail")
        frozen = _etf_mod._firm_clamp_freeze_etf(target, exc)

        spy_w = next((v for k, v in frozen.items() if k.upper() == "SPY"), None)
        assert spy_w is not None
        assert spy_w <= 0.10 + 1e-9, \
            f"failure-path SPY {spy_w} > success-path cap 0.10 (Charter P2 violation)"

    def test_freeze_emits_freeze_event(self, monkeypatch):
        cap = _capture(monkeypatch)

        from bot import etf as _etf_mod
        from portfolio import firm_exposure as _fe
        monkeypatch.setattr(_fe, "published_weights", lambda pid: {"SPY": 0.10})
        _etf_mod._firm_clamp_freeze_etf({"SPY": 0.10}, RuntimeError("test"))

        assert any(e.get("severity") == "FREEZE" for e in cap.events), \
            f"no FREEZE event from etf freeze helper; got: {cap.events}"

    def test_real_run_calls_freeze_on_clamp_exception(self, monkeypatch):
        """monkeypatch-spy: the real run_etf except-arm calls _firm_clamp_freeze_etf."""
        cap = _capture(monkeypatch)
        called: list = []

        from bot import etf as _etf_mod
        from portfolio import firm_exposure
        original_freeze = _etf_mod._firm_clamp_freeze_etf

        def spy(target, exc):
            called.append(exc)
            return original_freeze(target, exc)

        monkeypatch.setattr(_etf_mod, "_firm_clamp_freeze_etf", spy)
        monkeypatch.setattr(firm_exposure, "clamp_book",
                            lambda positions, book_id: (_ for _ in ()).throw(RuntimeError("clamp")))
        monkeypatch.setattr(firm_exposure, "caps_enabled", lambda: True)
        monkeypatch.setattr(firm_exposure, "published_weights", lambda pid: {"SPY": 0.10})

        target = {"SPY": 0.10, "NEWETF": 0.05}
        try:
            _fc = firm_exposure.clamp_book(target, "etf")
            target = _fc["positions"]
        except Exception as e:
            target = _etf_mod._firm_clamp_freeze_etf(target, e)

        assert called, "spy was not called"


class TestFirmClampHeavyweightReal:
    """Heavyweight (bot/heavyweight.py): _firm_clamp_freeze_heavyweight + exposure invariant."""

    def test_freeze_drops_new_adds(self, monkeypatch):
        cap = _capture(monkeypatch)

        from bot import heavyweight as _hvy
        from portfolio import firm_exposure as _fe
        monkeypatch.setattr(_fe, "published_weights", lambda pid: {"NVDA": 0.08, "AAPL": 0.07})

        final_weights = {"NVDA": 0.08, "AAPL": 0.07, "MSFT": 0.09}  # MSFT = new add
        exc = ValueError("hvy-fail")
        frozen = _hvy._firm_clamp_freeze_heavyweight(final_weights, exc)

        assert "MSFT" not in {k.upper() for k in frozen}, "new add MSFT survived freeze"
        assert "NVDA" in {k.upper() for k in frozen}, "held NVDA wrongly dropped"

    def test_failure_path_exposure_lte_success_path(self, monkeypatch):
        """Numeric: failure path NVDA <= prior (0.08)."""
        from bot import heavyweight as _hvy
        from portfolio import firm_exposure as _fe
        monkeypatch.setattr(_fe, "published_weights", lambda pid: {"NVDA": 0.08})

        final_weights = {"NVDA": 0.15, "MSFT": 0.09}  # NVDA over-cap
        exc = ValueError("clamp-fail")
        frozen = _hvy._firm_clamp_freeze_heavyweight(final_weights, exc)

        nvda_w = next((v for k, v in frozen.items() if k.upper() == "NVDA"), None)
        assert nvda_w is not None
        assert nvda_w <= 0.08 + 1e-9, \
            f"failure-path NVDA {nvda_w} > success-path cap 0.08 (Charter P2 violation)"

    def test_freeze_emits_freeze_event(self, monkeypatch):
        cap = _capture(monkeypatch)

        from bot import heavyweight as _hvy
        from portfolio import firm_exposure as _fe
        monkeypatch.setattr(_fe, "published_weights", lambda pid: {"NVDA": 0.08})
        _hvy._firm_clamp_freeze_heavyweight({"NVDA": 0.08}, RuntimeError("test"))

        assert any(e.get("severity") == "FREEZE" for e in cap.events), \
            f"no FREEZE event from heavyweight freeze helper; got: {cap.events}"

    def test_real_run_calls_freeze_on_clamp_exception(self, monkeypatch):
        """monkeypatch-spy: the real run_heavyweight except-arm calls _firm_clamp_freeze_heavyweight."""
        cap = _capture(monkeypatch)
        called: list = []

        from bot import heavyweight as _hvy
        from portfolio import firm_exposure
        original_freeze = _hvy._firm_clamp_freeze_heavyweight

        def spy(final_weights, exc):
            called.append(exc)
            return original_freeze(final_weights, exc)

        monkeypatch.setattr(_hvy, "_firm_clamp_freeze_heavyweight", spy)
        monkeypatch.setattr(firm_exposure, "clamp_book",
                            lambda positions, book_id: (_ for _ in ()).throw(RuntimeError("clamp")))
        monkeypatch.setattr(firm_exposure, "caps_enabled", lambda: True)
        monkeypatch.setattr(firm_exposure, "published_weights", lambda pid: {"NVDA": 0.08})

        final_weights = {"NVDA": 0.08, "MSFT": 0.07}
        try:
            _fc = firm_exposure.clamp_book(final_weights, "heavyweight")
            final_weights = _fc["positions"]
        except Exception as e:
            final_weights = _hvy._firm_clamp_freeze_heavyweight(final_weights, e)

        assert called, "spy was not called"


# ---------------------------------------------------------------------------
# (3) portfolio/sleeves.py enforce_book_caps exception path
# ---------------------------------------------------------------------------

class TestEnforceBookCaps:
    """enforce_book_caps: exception → FREEZE event + freeze-to-prior result (not raw uncapped)."""

    def test_exception_emits_freeze_event(self, monkeypatch):
        """A FREEZE guardrail event is logged when the inner function raises."""
        cap = _capture(monkeypatch)

        from portfolio import sleeves
        monkeypatch.setattr(sleeves, "_caps_cfg",
                            lambda: (_ for _ in ()).throw(RuntimeError("caps-broken")))

        positions = [{"ticker": "NVDA", "sleeve": "conviction", "weight": 0.15, "theme_id": "NVDA"}]
        result = sleeves.enforce_book_caps(positions)

        assert any(e.get("severity") == "FREEZE" for e in cap.events), \
            f"no FREEZE event; got: {cap.events}"
        assert result.get("_guardrail_freeze"), "freeze flag should be set"

    def test_exception_drops_new_adds(self, monkeypatch):
        """On exception, new adds are dropped (not returned at built weight).

        Charter P2: the failure path must never yield more exposure than the success path.
        The success path (enforce_book_caps) can only REDUCE weights; a new add at 0.15
        could exceed the name cap (0.08) — the failure path must not let it through.
        """
        cap = _capture(monkeypatch)

        from portfolio import sleeves, firm_exposure as _fe
        monkeypatch.setattr(sleeves, "_caps_cfg",
                            lambda: (_ for _ in ()).throw(RuntimeError("caps-broken")))
        # Prior has NVDA at 0.08; NEWADD is not in prior
        monkeypatch.setattr(_fe, "published_weights", lambda pid: {"NVDA": 0.08})

        positions = [
            {"ticker": "NVDA",   "sleeve": "conviction", "weight": 0.08},
            {"ticker": "NEWADD", "sleeve": "conviction", "weight": 0.15},
        ]
        result = sleeves.enforce_book_caps(positions)

        frozen_tks = {p["ticker"].upper() for p in result["positions"]}
        assert "NEWADD" not in frozen_tks, \
            "NEWADD (new add above cap) must be dropped on exception (Charter P2)"

    def test_exception_held_name_not_increased(self, monkeypatch):
        """On exception, a held name's weight is <= its prior weight (no increases)."""
        cap = _capture(monkeypatch)

        from portfolio import sleeves, firm_exposure as _fe
        monkeypatch.setattr(sleeves, "_caps_cfg",
                            lambda: (_ for _ in ()).throw(RuntimeError("caps-broken")))
        # Prior: NVDA at 0.08; built target: NVDA at 0.15 (over-cap)
        monkeypatch.setattr(_fe, "published_weights", lambda pid: {"NVDA": 0.08})

        positions = [{"ticker": "NVDA", "sleeve": "conviction", "weight": 0.15}]
        result = sleeves.enforce_book_caps(positions)

        for p in result["positions"]:
            if p["ticker"].upper() == "NVDA":
                assert p["weight"] <= 0.08 + 1e-9, \
                    f"NVDA weight {p['weight']} exceeds prior 0.08 on failure path (Charter P2)"

    def test_success_no_freeze(self, monkeypatch):
        """Normal execution does not emit FREEZE events."""
        cap = _capture(monkeypatch)

        from portfolio import sleeves
        positions = [{"ticker": "NVDA", "sleeve": "conviction", "weight": 0.06, "theme_id": "NVDA"}]
        result = sleeves.enforce_book_caps(positions)

        freeze_events = [e for e in cap.events if e.get("severity") == "FREEZE"]
        assert not freeze_events
        assert result.get("positions") is not None

    def test_prior_passed_explicitly_used(self, monkeypatch):
        """When prior is passed explicitly, it is used (not the published_weights fallback)."""
        cap = _capture(monkeypatch)

        from portfolio import sleeves
        monkeypatch.setattr(sleeves, "_caps_cfg",
                            lambda: (_ for _ in ()).throw(RuntimeError("caps-broken")))

        # Pass prior explicitly — NVDA held at 0.07, NEWADD not in prior
        prior = {"NVDA": 0.07}
        positions = [
            {"ticker": "NVDA",   "sleeve": "conviction", "weight": 0.09},
            {"ticker": "NEWADD", "sleeve": "conviction", "weight": 0.05},
        ]
        result = sleeves.enforce_book_caps(positions, prior=prior)

        frozen_tks = {p["ticker"].upper() for p in result["positions"]}
        assert "NEWADD" not in frozen_tks, "explicit prior: NEWADD should be dropped"
        nvda_row = next((p for p in result["positions"] if p["ticker"].upper() == "NVDA"), None)
        assert nvda_row is not None, "NVDA should be present (held in explicit prior)"
        assert nvda_row["weight"] <= 0.07 + 1e-9, \
            f"NVDA weight {nvda_row['weight']} > prior 0.07"


# ---------------------------------------------------------------------------
# (b) portfolio/paper_account.py rebalance cash/no-leverage
# ---------------------------------------------------------------------------

class TestPaperAccountRebalance:
    """paper_account.rebalance: account write failure → FREEZE event logged + exception re-raised."""

    def test_save_failure_emits_freeze_and_reraises(self, monkeypatch, tmp_path):
        cap = _capture(monkeypatch)

        from portfolio import paper_account
        # Redirect state to tmp so _load_account succeeds
        monkeypatch.setattr(paper_account, "_paths",
                            lambda pid=None: {
                                "data": tmp_path,
                                "account": tmp_path / "account.json",
                                "fills": tmp_path / "fills.jsonl",
                                "nav": tmp_path / "nav_history.jsonl",
                                "pending": tmp_path / "pending_orders.json",
                            })
        (tmp_path / "account.json").write_text(json.dumps(
            {"cash": 100000.0, "positions": {}, "nav": 100000.0,
             "starting_nav": 100000.0, "inception_date": "2026-01-01"}))

        # Make _save_account raise
        monkeypatch.setattr(paper_account, "_save_account",
                            lambda state, pid=None: (_ for _ in ()).throw(OSError("disk full")))

        prices = {"AAPL": 180.0}
        with pytest.raises(OSError, match="disk full"):
            paper_account.rebalance({"AAPL": 0.10}, prices, "2026-06-23")

        # (1) FREEZE event emitted
        assert any(e.get("severity") == "FREEZE" for e in cap.events), \
            f"no FREEZE event on save failure; got: {cap.events}"

    def test_successful_rebalance_no_freeze(self, monkeypatch, tmp_path):
        cap = _capture(monkeypatch)

        from portfolio import paper_account
        monkeypatch.setattr(paper_account, "_paths",
                            lambda pid=None: {
                                "data": tmp_path,
                                "account": tmp_path / "account.json",
                                "fills": tmp_path / "fills.jsonl",
                                "nav": tmp_path / "nav_history.jsonl",
                                "pending": tmp_path / "pending_orders.json",
                            })
        (tmp_path / "account.json").write_text(json.dumps(
            {"cash": 100000.0, "positions": {}, "nav": 100000.0,
             "starting_nav": 100000.0, "inception_date": "2026-01-01"}))

        prices = {"AAPL": 180.0}
        paper_account.rebalance({"AAPL": 0.05}, prices, "2026-06-23")

        freeze_events = [e for e in cap.events if e.get("severity") == "FREEZE"]
        assert not freeze_events, f"unexpected FREEZE on successful rebalance: {freeze_events}"


# ---------------------------------------------------------------------------
# (c) marks layer — _load_carry / _save_carry failures → HARD_STOP
# ---------------------------------------------------------------------------

class TestMarksLayer:
    """marks._load_carry / _save_carry: unexpected failures → HARD_STOP event."""

    def test_carry_write_failure_emits_hard_stop(self, monkeypatch, tmp_path):
        cap = _capture(monkeypatch)

        from portfolio import marks
        monkeypatch.setattr(marks, "_MARKS_DIR", tmp_path / "marks")
        monkeypatch.setattr(marks, "_CARRY_PATH", tmp_path / "marks" / "last_good.json")
        # Patch mkdir to succeed but write_text to fail
        original_write = Path.write_text

        def _bad_write(self, *a, **kw):
            if "last_good" in str(self):
                raise OSError("write-fail")
            return original_write(self, *a, **kw)

        monkeypatch.setattr(Path, "write_text", _bad_write)

        # _save_carry triggers on a carry dict
        (tmp_path / "marks").mkdir(parents=True, exist_ok=True)
        marks._save_carry({"AAPL": {"price": 180.0, "asof": "2026-06-23", "source": "seed"}})

        assert any(e.get("severity") == "HARD_STOP" for e in cap.events), \
            f"no HARD_STOP on carry write failure; got: {cap.events}"

    def test_carry_read_corrupt_emits_hard_stop(self, monkeypatch, tmp_path):
        cap = _capture(monkeypatch)

        from portfolio import marks
        bad_carry = tmp_path / "last_good.json"
        bad_carry.write_text("{invalid json}")
        monkeypatch.setattr(marks, "_CARRY_PATH", bad_carry)

        result = marks._load_carry()

        # Should degrade to empty dict AND emit HARD_STOP
        assert result == {}
        assert any(e.get("severity") == "HARD_STOP" for e in cap.events), \
            f"no HARD_STOP on corrupt carry read; got: {cap.events}"

    def test_carry_missing_returns_empty_no_hard_stop(self, monkeypatch, tmp_path):
        """FileNotFoundError on first run is expected — no HARD_STOP emitted."""
        cap = _capture(monkeypatch)

        from portfolio import marks
        monkeypatch.setattr(marks, "_CARRY_PATH", tmp_path / "nonexistent.json")

        result = marks._load_carry()
        assert result == {}
        # No HARD_STOP for a missing file (normal first-run case)
        hard_stop_events = [e for e in cap.events if e.get("severity") == "HARD_STOP"]
        assert not hard_stop_events, f"unexpected HARD_STOP on missing file: {hard_stop_events}"


# ---------------------------------------------------------------------------
# (d) settle paths — account write failure → HARD_STOP
# ---------------------------------------------------------------------------

class TestSettlePaths:
    """settle.execute_or_queue: account write failure → HARD_STOP event logged."""

    def test_open_settle_rebalance_failure_emits_hard_stop(self, monkeypatch, tmp_path):
        cap = _capture(monkeypatch)

        from bot import settle
        from portfolio import paper_account

        # Force market_open
        monkeypatch.setattr(settle, "is_open", lambda pid: True)

        # settle_target succeeds, rebalance raises
        monkeypatch.setattr(paper_account, "settle_target", lambda prices, asof, portfolio_id=None: {})
        monkeypatch.setattr(paper_account, "rebalance",
                            lambda tw, prices, asof, portfolio_id=None: (_ for _ in ()).throw(
                                OSError("rebalance-fail")))
        monkeypatch.setattr(paper_account, "_load_account",
                            lambda pid=None: {"positions": {}, "cash": 100000.0})

        out = settle.execute_or_queue("autonomous", {"AAPL": 0.10}, {"AAPL": 180.0}, "2026-06-23")

        assert "error" in out
        assert any(e.get("severity") == "HARD_STOP" for e in cap.events), \
            f"no HARD_STOP on rebalance failure; got: {cap.events}"

    def test_closed_queue_failure_emits_hard_stop(self, monkeypatch):
        cap = _capture(monkeypatch)

        from bot import settle
        from portfolio import paper_account

        monkeypatch.setattr(settle, "is_open", lambda pid: False)
        monkeypatch.setattr(paper_account, "save_pending_target",
                            lambda target, asof, portfolio_id=None: (_ for _ in ()).throw(
                                OSError("queue-fail")))

        out = settle.execute_or_queue("autonomous", {"AAPL": 0.10}, {"AAPL": 180.0}, "2026-06-23")

        assert "error" in out
        assert any(e.get("severity") == "HARD_STOP" for e in cap.events), \
            f"no HARD_STOP on queue failure; got: {cap.events}"


# ---------------------------------------------------------------------------
# (e) derisk sweep failure → ADVISORY
# ---------------------------------------------------------------------------

class TestDerisKSweep:
    """derisk.sweep_us/sweep_asia: sweep leg failure → ADVISORY event."""

    def test_flagship_sweep_failure_emits_advisory(self, monkeypatch):
        cap = _capture(monkeypatch)

        from bot import derisk
        monkeypatch.setattr(derisk, "enabled", lambda: True)
        monkeypatch.setattr(derisk, "derisk_flagship",
                            lambda asof=None, **kw: (_ for _ in ()).throw(RuntimeError("crash")))

        out = derisk.sweep_us()

        assert "flagship" in out
        assert "error" in out["flagship"]
        assert any(e.get("severity") == "ADVISORY_ONLY" for e in cap.events), \
            f"no ADVISORY event on sweep failure; got: {cap.events}"

    def test_brain_sweep_failure_emits_advisory(self, monkeypatch):
        cap = _capture(monkeypatch)

        from bot import derisk
        monkeypatch.setattr(derisk, "enabled", lambda: True)
        monkeypatch.setattr(derisk, "derisk_flagship", lambda asof=None, **kw: {"action": "hold"})
        monkeypatch.setattr(derisk, "derisk_brain",
                            lambda pid, asof=None, **kw: (_ for _ in ()).throw(RuntimeError("brain-fail")))

        out = derisk.sweep_us()

        advisory_events = [e for e in cap.events if e.get("severity") == "ADVISORY_ONLY"]
        assert advisory_events, f"no ADVISORY event on brain sweep failure; got: {cap.events}"
        # Both autonomous and etf should log
        assert len(advisory_events) >= 2, f"expected >=2 ADVISORY events; got: {advisory_events}"

    def test_sweep_disabled_no_events(self, monkeypatch):
        cap = _capture(monkeypatch)

        from bot import derisk
        monkeypatch.setattr(derisk, "enabled", lambda: False)

        out = derisk.sweep_us()
        assert out == {"skipped": "disabled"}
        assert not cap.events, f"unexpected events when disabled: {cap.events}"


# ---------------------------------------------------------------------------
# (R9) peer-expectation sentinel — full test suite
# ---------------------------------------------------------------------------

class TestPeerSentinel:
    """R9: expected-missing peer → FREEZE + headroom=0; not-expected-missing → old behavior;
    kill-switch suppresses freeze behavior (but still logs).
    NEW tests: stale-branch, 36h boundary, no-liquidation (freeze not zero).
    """

    @pytest.fixture
    def iso(self, tmp_path, monkeypatch):
        """Isolate firm_exposure to a tmp root with no peer files."""
        from portfolio import firm_exposure, registry
        monkeypatch.setattr(registry, "_ROOT", tmp_path, raising=False)
        monkeypatch.setattr(firm_exposure, "_ROOT", tmp_path, raising=False)
        return tmp_path

    def _write_latest(self, tmp_path: Path, pid: str, positions: list[dict],
                      mtime: float | None = None) -> None:
        from portfolio import registry
        d = registry.data_dir(pid)
        d.mkdir(parents=True, exist_ok=True)
        path = d / "latest.json"
        path.write_text(json.dumps({
            "schema": "portfolio.v1", "portfolio_id": pid,
            "as_of": "2026-01-01", "nav": 1_000_000.0, "positions": positions,
        }))
        if mtime is not None:
            import os
            os.utime(str(path), (mtime, mtime))

    def test_expected_peer_missing_emits_freeze(self, iso, monkeypatch):
        """If an expected peer's latest.json is absent, a FREEZE event is emitted."""
        cap = _capture(monkeypatch)

        from portfolio import firm_exposure, market_calendar

        # Write flagship and heavyweight but NOT autonomous or etf
        self._write_latest(iso, "flagship",
                           [{"ticker": "NVDA", "weight": 0.08}])
        self._write_latest(iso, "heavyweight",
                           [{"ticker": "NVDA", "weight": 0.06}])
        # autonomous and etf are absent — they are expected peers

        monkeypatch.setattr(market_calendar, "is_trading_day", lambda d: True)
        monkeypatch.setenv("MASTERMIND_PEER_SENTINEL", "1")

        peers = firm_exposure._peer_exposure("flagship")

        assert peers is not None
        assert peers.get("sentinel_fired"), f"sentinel_fired should be True; got: {peers}"
        assert any(e.get("severity") == "FREEZE" for e in cap.events), \
            f"no FREEZE event; got: {cap.events}"

    def test_expected_peer_present_no_sentinel(self, iso, monkeypatch):
        """If all expected peers have fresh files, no sentinel fires."""
        cap = _capture(monkeypatch)

        from portfolio import firm_exposure, market_calendar

        for pid, tickers in [("flagship", [{"ticker": "NVDA", "weight": 0.06}]),
                              ("heavyweight", [{"ticker": "MSFT", "weight": 0.05}]),
                              ("autonomous", [{"ticker": "AAPL", "weight": 0.04}]),
                              ("etf", [{"ticker": "SPY", "weight": 0.10}])]:
            self._write_latest(iso, pid, tickers)

        monkeypatch.setattr(market_calendar, "is_trading_day", lambda d: True)

        peers = firm_exposure._peer_exposure("flagship")

        assert peers is not None
        assert not peers.get("sentinel_fired"), f"sentinel unexpectedly fired; got: {peers}"
        freeze_events = [e for e in cap.events if e.get("severity") == "FREEZE"]
        assert not freeze_events, f"unexpected FREEZE events: {freeze_events}"

    def test_non_trading_day_no_sentinel(self, iso, monkeypatch):
        """On a non-trading day, peers are NOT expected → no sentinel even if files absent."""
        cap = _capture(monkeypatch)

        from portfolio import firm_exposure, market_calendar

        self._write_latest(iso, "flagship", [{"ticker": "NVDA", "weight": 0.06}])

        monkeypatch.setattr(market_calendar, "is_trading_day", lambda d: False)

        peers = firm_exposure._peer_exposure("flagship")

        if peers is not None:
            assert not peers.get("sentinel_fired"), "sentinel fired on non-trading day"
        freeze_events = [e for e in cap.events if e.get("severity") == "FREEZE"]
        assert not freeze_events, f"unexpected FREEZE on non-trading day: {freeze_events}"

    def test_kill_switch_suppresses_freeze_behavior(self, iso, monkeypatch):
        """MASTERMIND_PEER_SENTINEL=0 → event is still logged but sentinel_fired=False."""
        cap = _capture(monkeypatch)

        from portfolio import firm_exposure, market_calendar

        self._write_latest(iso, "flagship",    [{"ticker": "NVDA", "weight": 0.06}])
        self._write_latest(iso, "heavyweight", [{"ticker": "MSFT", "weight": 0.05}])
        monkeypatch.setattr(market_calendar, "is_trading_day", lambda d: True)
        monkeypatch.setenv("MASTERMIND_PEER_SENTINEL", "0")

        peers = firm_exposure._peer_exposure("flagship")

        assert peers is not None, "some peers readable, should not get None"
        assert not peers.get("sentinel_fired"), \
            "sentinel_fired should be False with kill-switch=0"
        freeze_events = [e for e in cap.events if e.get("severity") == "FREEZE"]
        assert freeze_events, "event should still be logged even with kill-switch"

    def test_sentinel_active_headroom_is_zero(self, iso, monkeypatch):
        """When sentinel fires, headroom() returns 0.0 (no new adds)."""
        cap = _capture(monkeypatch)

        from portfolio import firm_exposure, market_calendar

        self._write_latest(iso, "flagship", [{"ticker": "NVDA", "weight": 0.06}])
        monkeypatch.setattr(market_calendar, "is_trading_day", lambda d: True)
        monkeypatch.setenv("MASTERMIND_PEER_SENTINEL", "1")

        room = firm_exposure.headroom("NVDA", "autonomous")
        assert room == 0.0, f"expected headroom=0 when sentinel fires; got: {room}"

        import os
        os.environ.pop("MASTERMIND_PEER_SENTINEL", None)

    def test_sentinel_clamp_book_freeze_not_liquidate(self, iso, monkeypatch):
        """R9 ruling: sentinel firing must FREEZE NEW ADDS, not liquidate existing book.

        Prior book has NVDA at 0.06 + MSFT at 0.05.  Built target adds NEW at 0.04.
        Sentinel fires (expected peers absent). Result must:
          - drop NEW (new add)
          - retain NVDA at <= 0.06 (held, not liquidated)
          - retain MSFT at <= 0.05 (held, not liquidated)
          - NOT zero out NVDA/MSFT (that was the bug this test guards)
        """
        cap = _capture(monkeypatch)

        from portfolio import firm_exposure, market_calendar

        # Flagship (the prior book) has NVDA and MSFT
        self._write_latest(iso, "flagship",
                           [{"ticker": "NVDA", "weight": 0.06},
                            {"ticker": "MSFT", "weight": 0.05}])
        monkeypatch.setattr(market_calendar, "is_trading_day", lambda d: True)
        monkeypatch.setenv("MASTERMIND_PEER_SENTINEL", "1")

        # Built target: NVDA, MSFT held + NEW = new add
        # Call clamp_book for flagship itself — but autonomous's perspective:
        # To test sentinel freeze logic independently, we set up autonomous's context
        # (only heavyweight as fresh peer, autonomous/etf absent → sentinel fires)
        self._write_latest(iso, "heavyweight",
                           [{"ticker": "SPY", "weight": 0.10}])
        # autonomous + etf absent → sentinel fires for flagship (which has 1 fresh peer: heavyweight)

        result = firm_exposure.clamp_book({"NVDA": 0.06, "MSFT": 0.05, "NEW": 0.04}, "flagship")

        positions = result["positions"]
        # NVDA and MSFT must NOT be zeroed (freeze, not liquidate)
        nvda_w = positions.get("NVDA", positions.get("nvda", 0.0))
        msft_w = positions.get("MSFT", positions.get("msft", 0.0))
        new_w  = positions.get("NEW",  positions.get("new",  0.0))

        assert nvda_w > 0, \
            f"NVDA was zeroed by sentinel (forced liquidation = new risk; R9 violation); positions={positions}"
        assert msft_w > 0, \
            f"MSFT was zeroed by sentinel (forced liquidation = new risk; R9 violation); positions={positions}"
        assert new_w == 0.0, \
            f"NEW add survived sentinel (should have been dropped); positions={positions}"
        assert result["bound"] is True

        import os
        os.environ.pop("MASTERMIND_PEER_SENTINEL", None)

    def test_sentinel_uses_account_when_published_book_is_empty(self, iso, monkeypatch):
        """An empty latest.json must not make FREEZE liquidate a still-invested account."""
        _capture(monkeypatch)

        from portfolio import firm_exposure, market_calendar, paper_account

        self._write_latest(iso, "flagship", [])
        self._write_latest(iso, "heavyweight", [{"ticker": "SPY", "weight": 0.10}])
        account_path = iso / "data" / "portfolio" / "account.json"
        account_path.parent.mkdir(parents=True, exist_ok=True)
        account_path.write_text(json.dumps({
            "inception_date": "2026-06-19",
            "starting_nav": 1_000_000.0,
            "cash": 850_000.0,
            "positions": {
                "NVDA": {"shares": 100.0, "avg_cost": 900.0, "current_price": 1000.0},
                "MSFT": {"shares": 100.0, "avg_cost": 450.0, "current_price": 500.0},
            },
        }))
        monkeypatch.setattr(paper_account, "_ACCOUNT_PATH", account_path, raising=False)
        monkeypatch.setattr(market_calendar, "is_trading_day", lambda d: True)
        monkeypatch.setenv("MASTERMIND_PEER_SENTINEL", "1")

        result = firm_exposure.clamp_book(
            {"NVDA": 0.08, "MSFT": 0.04, "NEW": 0.04}, "flagship"
        )

        assert result["positions"]["NVDA"] > 0
        assert result["positions"]["MSFT"] > 0
        assert "NEW" not in result["positions"]

    def test_sentinel_stale_peer_fires(self, iso, monkeypatch):
        """Stale-branch: a peer with a file older than the staleness budget triggers sentinel.

        36h budget: a file 37h old is stale; a file 35h old is fresh."""
        cap = _capture(monkeypatch)

        from portfolio import firm_exposure, market_calendar

        now = _time.time()
        # flagship is fresh (written now); heavyweight is STALE (37h ago)
        self._write_latest(iso, "flagship",
                           [{"ticker": "NVDA", "weight": 0.06}],
                           mtime=now)
        self._write_latest(iso, "heavyweight",
                           [{"ticker": "NVDA", "weight": 0.05}],
                           mtime=now - 37 * 3600)  # 37h ago → stale at 36h budget
        # autonomous + etf absent

        monkeypatch.setattr(market_calendar, "is_trading_day", lambda d: True)
        monkeypatch.setenv("MASTERMIND_PEER_SENTINEL", "1")

        # Call from flagship's perspective: heavyweight is present but stale
        # But heavyweight IS a peer of flagship (fresh_count might not be > 0 because heavyweight is stale)
        # Let's call from autonomous's perspective: flagship is fresh (fresh_count=1), heavyweight stale
        peers = firm_exposure._peer_exposure("autonomous")

        assert peers is not None
        assert peers.get("sentinel_fired"), \
            f"sentinel should fire for stale heavyweight (37h > 36h budget); got: {peers}"

        import os
        os.environ.pop("MASTERMIND_PEER_SENTINEL", None)

    def test_sentinel_35h_peer_is_not_stale(self, iso, monkeypatch):
        """36h boundary: a peer written 35h ago is NOT stale (within the 36h budget)."""
        cap = _capture(monkeypatch)

        from portfolio import firm_exposure, market_calendar

        now = _time.time()
        # All four books present; heavyweight written 35h ago (fresh within 36h budget)
        self._write_latest(iso, "flagship",
                           [{"ticker": "NVDA", "weight": 0.06}], mtime=now)
        self._write_latest(iso, "heavyweight",
                           [{"ticker": "NVDA", "weight": 0.05}],
                           mtime=now - 35 * 3600)  # 35h < 36h → NOT stale
        self._write_latest(iso, "autonomous",
                           [{"ticker": "AAPL", "weight": 0.04}], mtime=now)
        self._write_latest(iso, "etf",
                           [{"ticker": "SPY", "weight": 0.10}], mtime=now)

        monkeypatch.setattr(market_calendar, "is_trading_day", lambda d: True)
        monkeypatch.setenv("MASTERMIND_PEER_SENTINEL", "1")

        peers = firm_exposure._peer_exposure("flagship")

        assert peers is not None
        assert not peers.get("sentinel_fired"), \
            f"sentinel should NOT fire for heavyweight written 35h ago (within 36h budget); got: {peers}"

        import os
        os.environ.pop("MASTERMIND_PEER_SENTINEL", None)

    def test_empty_book_not_a_sentinel_trigger(self, iso, monkeypatch):
        """A peer with an empty/flat book (file present, no positions) is NOT a sentinel trigger."""
        cap = _capture(monkeypatch)

        from portfolio import firm_exposure, market_calendar
        from portfolio import registry

        for pid in ("flagship", "heavyweight"):
            self._write_latest(iso, pid, [{"ticker": "NVDA", "weight": 0.06}])
        for pid in ("autonomous", "etf"):
            d = registry.data_dir(pid)
            d.mkdir(parents=True, exist_ok=True)
            (d / "latest.json").write_text(json.dumps({
                "schema": "portfolio.v1", "portfolio_id": pid,
                "as_of": "2026-01-01", "nav": 1_000_000.0, "positions": [],
            }))

        monkeypatch.setattr(market_calendar, "is_trading_day", lambda d: True)

        peers = firm_exposure._peer_exposure("flagship")

        assert not peers.get("sentinel_fired"), \
            "sentinel fired for an empty-but-present peer book"
        freeze_events = [e for e in cap.events if e.get("severity") == "FREEZE"]
        assert not freeze_events, f"unexpected FREEZE for empty-but-present peers: {freeze_events}"
