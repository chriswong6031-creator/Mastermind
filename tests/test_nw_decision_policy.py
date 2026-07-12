"""tests/test_nw_decision_policy.py — A5 typed decision-policy chokepoint battery.

Covers `nw_decision_mode()` (the off|shadow|candidacy|shrink|vote ladder) and
`decision_signals(ticker)` in brain/neural_web_context.py.

Discipline under test:
  * mode 'off'         → decision_signals is a byte-identical no-op (all None/inert)
                         regardless of row content.
  * mode 'candidacy'   → WATCH→0.35, BOTTOMING/CONFIRMED→0.50, lean +1; other state → None.
  * fdr_cleared False  → per-name inert (candidacy None) even in candidacy mode.
  * mode 'shrink'      → conflicts>=2 → entry_shrink 0.7; conflicts<2 → None; candidacy still works.
  * clean_in_conflicted → True when conflicts==0 AND market contradiction_count>=3.
  * absent/stale context → inert default, never raises.
  * nw_decision_mode ladder parsing + default off.

Mirrors tests/test_neural_web_context.py: we monkeypatch _ARTIFACT_PATH at a
tmp_path copy of the v1 fixture (with today's as_of) and reset the process cache
around each case via _reset_context_cache().
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_FIXTURE_PATH = _ROOT / "tests" / "fixtures" / "mastermind_context.json"


def _today() -> str:
    return date.today().isoformat()


def _stale_date() -> str:
    return (date.today() - timedelta(days=10)).isoformat()


def _write_fixture(
    tmp_path: Path,
    *,
    as_of: str | None = None,
    candidate_context: dict | None = None,
    contradiction_records: list | None = None,
) -> Path:
    """Write a fresh v1 fixture with a synthetic candidate_context + market contradictions.

    * as_of defaults to today (fresh).
    * candidate_context, when provided, REPLACES the fixture's rows entirely.
    * contradiction_records, when provided, REPLACES lobes.contradictions.records
      (which drives market_plane().contradiction_count).
    """
    raw = json.loads(_FIXTURE_PATH.read_text())
    raw["as_of"] = as_of if as_of is not None else _today()
    for _lobe_name, lobe in (raw.get("lobes") or {}).items():
        if isinstance(lobe, dict) and "as_of" in lobe:
            lobe["as_of"] = raw["as_of"]
    if candidate_context is not None:
        raw["candidate_context"] = candidate_context
    if contradiction_records is not None:
        raw.setdefault("lobes", {}).setdefault("contradictions", {})["records"] = contradiction_records
    p = tmp_path / "mastermind_context.json"
    p.write_text(json.dumps(raw))
    return p


def _patch_path(monkeypatch, path: Path | None) -> None:
    """Point neural_web_context._ARTIFACT_PATH at path (or a non-existent path if None)."""
    import brain.neural_web_context as NWC
    monkeypatch.setattr(
        NWC, "_ARTIFACT_PATH", path or Path("/nonexistent/mastermind_context.json")
    )
    NWC._reset_context_cache()


# Synthetic candidate rows keyed to each derivation branch.
def _cleared_row(state: str, conflicts: int) -> dict:
    return {
        "bottom": {"bottom_state": state},
        "options": {},
        "graph_conflicts": [f"conflict_{i}" for i in range(conflicts)],
        "kernel": {"display_armed": True, "fdr_cleared": True},
        "allowed_behavior": "annotate_only",
    }


def _uncleared_row(state: str, conflicts: int) -> dict:
    row = _cleared_row(state, conflicts)
    row["kernel"] = {"display_armed": True, "fdr_cleared": False}
    return row


# ---------------------------------------------------------------------------
# nw_decision_mode ladder parsing + default off
# ---------------------------------------------------------------------------

class TestDecisionModeLadder:
    def test_default_off(self, monkeypatch):
        import brain.neural_web_context as NWC
        monkeypatch.delenv("MASTERMIND_NW_DECISION", raising=False)
        assert NWC.nw_decision_mode() == "off"

    def test_each_mode_parses(self, monkeypatch):
        import brain.neural_web_context as NWC
        for mode in ("off", "shadow", "candidacy", "shrink", "vote"):
            monkeypatch.setenv("MASTERMIND_NW_DECISION", mode)
            assert NWC.nw_decision_mode() == mode

    def test_case_insensitive_and_whitespace(self, monkeypatch):
        import brain.neural_web_context as NWC
        monkeypatch.setenv("MASTERMIND_NW_DECISION", "  Candidacy  ")
        assert NWC.nw_decision_mode() == "candidacy"

    def test_unrecognized_value_falls_to_off(self, monkeypatch):
        import brain.neural_web_context as NWC
        monkeypatch.setenv("MASTERMIND_NW_DECISION", "banana")
        assert NWC.nw_decision_mode() == "off"

    def test_empty_value_falls_to_off(self, monkeypatch):
        import brain.neural_web_context as NWC
        monkeypatch.setenv("MASTERMIND_NW_DECISION", "")
        assert NWC.nw_decision_mode() == "off"

    def test_mode_ge_monotone_ordering(self):
        import brain.neural_web_context as NWC
        ladder = ["off", "shadow", "candidacy", "shrink", "vote"]
        for i, lo in enumerate(ladder):
            for j, hi in enumerate(ladder):
                # hi >= lo iff its rank is >= lo's rank
                assert NWC._mode_ge(hi, lo) is (j >= i)

    def test_mode_ge_unknown_inputs_are_off_rank(self):
        import brain.neural_web_context as NWC
        # unknown mode ranks as 'off'(0): only ok against 'off' threshold
        assert NWC._mode_ge("banana", "off") is True
        assert NWC._mode_ge("banana", "candidacy") is False
        # unknown threshold ranks as 'off'(0): any mode is >= it
        assert NWC._mode_ge("off", "banana") is True

    def test_independent_from_nw_context_flag(self, monkeypatch):
        """MASTERMIND_NW_DECISION and MASTERMIND_NW_CONTEXT are separate."""
        import brain.neural_web_context as NWC
        monkeypatch.setenv("MASTERMIND_NW_DECISION", "candidacy")
        monkeypatch.delenv("MASTERMIND_NW_CONTEXT", raising=False)
        assert NWC.nw_decision_mode() == "candidacy"
        assert NWC.nw_prompts_enabled() is False  # text flag unaffected


# ---------------------------------------------------------------------------
# mode off → byte-identical no-op regardless of row content
# ---------------------------------------------------------------------------

class TestModeOffNoOp:
    def test_off_all_none_inert_with_rich_row(self, monkeypatch, tmp_path):
        import brain.neural_web_context as NWC
        # A row that WOULD trip every signal at a higher mode.
        rows = {"NVDA": _cleared_row("BOTTOMING", conflicts=3)}
        f = _write_fixture(tmp_path, candidate_context=rows,
                           contradiction_records=[{"s": 1}, {"s": 2}, {"s": 3}, {"s": 4}])
        _patch_path(monkeypatch, f)
        monkeypatch.delenv("MASTERMIND_NW_DECISION", raising=False)  # default off

        sig = NWC.decision_signals("NVDA")
        assert sig == {
            "candidacy": None,
            "entry_shrink": None,
            "clean_in_conflicted": False,
            "inert": True,
            "mode": "off",
        }

    def test_off_explicit_env(self, monkeypatch, tmp_path):
        import brain.neural_web_context as NWC
        rows = {"NVDA": _cleared_row("WATCH", conflicts=0)}
        f = _write_fixture(tmp_path, candidate_context=rows)
        _patch_path(monkeypatch, f)
        monkeypatch.setenv("MASTERMIND_NW_DECISION", "off")
        sig = NWC.decision_signals("NVDA")
        assert sig["inert"] is True
        assert sig["candidacy"] is None
        assert sig["entry_shrink"] is None
        assert sig["clean_in_conflicted"] is False
        assert sig["mode"] == "off"

    def test_shadow_computes_but_signals_still_inert_to_sizing(self, monkeypatch, tmp_path):
        """shadow arms compute+log but is below candidacy/shrink, so no candidacy/shrink signal."""
        import brain.neural_web_context as NWC
        rows = {"NVDA": _cleared_row("BOTTOMING", conflicts=3)}
        f = _write_fixture(tmp_path, candidate_context=rows,
                           contradiction_records=[{"s": 1}, {"s": 2}, {"s": 3}])
        _patch_path(monkeypatch, f)
        monkeypatch.setenv("MASTERMIND_NW_DECISION", "shadow")
        sig = NWC.decision_signals("NVDA")
        # fdr-cleared present name → not per-name inert, but no signal below candidacy
        assert sig["inert"] is False
        assert sig["candidacy"] is None
        assert sig["entry_shrink"] is None
        assert sig["clean_in_conflicted"] is False
        assert sig["mode"] == "shadow"


# ---------------------------------------------------------------------------
# candidacy mode → state → score mapping, lean +1
# ---------------------------------------------------------------------------

class TestCandidacyMode:
    def test_watch_scores_035(self, monkeypatch, tmp_path):
        import brain.neural_web_context as NWC
        rows = {"NVDA": _cleared_row("WATCH", conflicts=0)}
        f = _write_fixture(tmp_path, candidate_context=rows)
        _patch_path(monkeypatch, f)
        monkeypatch.setenv("MASTERMIND_NW_DECISION", "candidacy")
        sig = NWC.decision_signals("NVDA")
        assert sig["candidacy"] == {"state": "WATCH", "score": 0.35, "lean": 1}
        assert sig["inert"] is False
        # entry_shrink not armed below shrink mode
        assert sig["entry_shrink"] is None

    def test_bottoming_scores_050(self, monkeypatch, tmp_path):
        import brain.neural_web_context as NWC
        rows = {"NVDA": _cleared_row("BOTTOMING", conflicts=0)}
        f = _write_fixture(tmp_path, candidate_context=rows)
        _patch_path(monkeypatch, f)
        monkeypatch.setenv("MASTERMIND_NW_DECISION", "candidacy")
        sig = NWC.decision_signals("NVDA")
        assert sig["candidacy"] == {"state": "BOTTOMING", "score": 0.5, "lean": 1}

    def test_confirmed_scores_050(self, monkeypatch, tmp_path):
        import brain.neural_web_context as NWC
        rows = {"NVDA": _cleared_row("CONFIRMED", conflicts=0)}
        f = _write_fixture(tmp_path, candidate_context=rows)
        _patch_path(monkeypatch, f)
        monkeypatch.setenv("MASTERMIND_NW_DECISION", "candidacy")
        sig = NWC.decision_signals("NVDA")
        assert sig["candidacy"] == {"state": "CONFIRMED", "score": 0.5, "lean": 1}

    def test_neutral_state_gives_no_candidacy(self, monkeypatch, tmp_path):
        import brain.neural_web_context as NWC
        rows = {"NVDA": _cleared_row("neutral", conflicts=0)}
        f = _write_fixture(tmp_path, candidate_context=rows)
        _patch_path(monkeypatch, f)
        monkeypatch.setenv("MASTERMIND_NW_DECISION", "candidacy")
        sig = NWC.decision_signals("NVDA")
        assert sig["candidacy"] is None
        assert sig["inert"] is False  # present + cleared → not per-name inert

    def test_state_key_fallback(self, monkeypatch, tmp_path):
        """bottom['state'] is honored when bottom_state is absent."""
        import brain.neural_web_context as NWC
        row = _cleared_row("WATCH", conflicts=0)
        row["bottom"] = {"state": "BOTTOMING"}  # only the 'state' key
        f = _write_fixture(tmp_path, candidate_context={"NVDA": row})
        _patch_path(monkeypatch, f)
        monkeypatch.setenv("MASTERMIND_NW_DECISION", "candidacy")
        sig = NWC.decision_signals("NVDA")
        assert sig["candidacy"] == {"state": "BOTTOMING", "score": 0.5, "lean": 1}


# ---------------------------------------------------------------------------
# fdr_cleared False → per-name inert even in candidacy mode
# ---------------------------------------------------------------------------

class TestFdrNotCleared:
    def test_uncleared_inert_in_candidacy(self, monkeypatch, tmp_path):
        import brain.neural_web_context as NWC
        rows = {"NVDA": _uncleared_row("BOTTOMING", conflicts=3)}
        f = _write_fixture(tmp_path, candidate_context=rows,
                           contradiction_records=[{"s": 1}, {"s": 2}, {"s": 3}])
        _patch_path(monkeypatch, f)
        monkeypatch.setenv("MASTERMIND_NW_DECISION", "candidacy")
        sig = NWC.decision_signals("NVDA")
        assert sig["inert"] is True
        assert sig["candidacy"] is None
        assert sig["entry_shrink"] is None
        assert sig["clean_in_conflicted"] is False

    def test_uncleared_inert_in_shrink(self, monkeypatch, tmp_path):
        import brain.neural_web_context as NWC
        rows = {"NVDA": _uncleared_row("BOTTOMING", conflicts=5)}
        f = _write_fixture(tmp_path, candidate_context=rows)
        _patch_path(monkeypatch, f)
        monkeypatch.setenv("MASTERMIND_NW_DECISION", "shrink")
        sig = NWC.decision_signals("NVDA")
        assert sig["inert"] is True
        assert sig["entry_shrink"] is None  # never shrink a display-armed-only name

    def test_missing_kernel_is_inert(self, monkeypatch, tmp_path):
        import brain.neural_web_context as NWC
        row = _cleared_row("BOTTOMING", conflicts=0)
        del row["kernel"]
        f = _write_fixture(tmp_path, candidate_context={"NVDA": row})
        _patch_path(monkeypatch, f)
        monkeypatch.setenv("MASTERMIND_NW_DECISION", "candidacy")
        sig = NWC.decision_signals("NVDA")
        assert sig["inert"] is True
        assert sig["candidacy"] is None


# ---------------------------------------------------------------------------
# shrink mode → conflict-density entry shrink; candidacy still works
# ---------------------------------------------------------------------------

class TestShrinkMode:
    def test_conflicts_ge_min_shrinks(self, monkeypatch, tmp_path):
        import brain.neural_web_context as NWC
        rows = {"NVDA": _cleared_row("BOTTOMING", conflicts=2)}
        f = _write_fixture(tmp_path, candidate_context=rows)
        _patch_path(monkeypatch, f)
        monkeypatch.setenv("MASTERMIND_NW_DECISION", "shrink")
        sig = NWC.decision_signals("NVDA")
        assert sig["entry_shrink"] == 0.7
        # candidacy still derived at shrink (mode >= candidacy)
        assert sig["candidacy"] == {"state": "BOTTOMING", "score": 0.5, "lean": 1}
        assert sig["inert"] is False

    def test_conflicts_below_min_no_shrink(self, monkeypatch, tmp_path):
        import brain.neural_web_context as NWC
        rows = {"NVDA": _cleared_row("WATCH", conflicts=1)}
        f = _write_fixture(tmp_path, candidate_context=rows)
        _patch_path(monkeypatch, f)
        monkeypatch.setenv("MASTERMIND_NW_DECISION", "shrink")
        sig = NWC.decision_signals("NVDA")
        assert sig["entry_shrink"] is None
        assert sig["candidacy"] == {"state": "WATCH", "score": 0.35, "lean": 1}

    def test_shrink_uses_module_constants(self, monkeypatch, tmp_path):
        """Exactly NW_CONFLICTS_MIN conflicts trips exactly NW_ENTRY_SHRINK."""
        import brain.neural_web_context as NWC
        rows = {"NVDA": _cleared_row("CONFIRMED", conflicts=NWC.NW_CONFLICTS_MIN)}
        f = _write_fixture(tmp_path, candidate_context=rows)
        _patch_path(monkeypatch, f)
        monkeypatch.setenv("MASTERMIND_NW_DECISION", "shrink")
        sig = NWC.decision_signals("NVDA")
        assert sig["entry_shrink"] == NWC.NW_ENTRY_SHRINK

    def test_candidacy_mode_never_shrinks(self, monkeypatch, tmp_path):
        """At candidacy mode (below shrink), heavy conflicts must NOT shrink."""
        import brain.neural_web_context as NWC
        rows = {"NVDA": _cleared_row("BOTTOMING", conflicts=9)}
        f = _write_fixture(tmp_path, candidate_context=rows)
        _patch_path(monkeypatch, f)
        monkeypatch.setenv("MASTERMIND_NW_DECISION", "candidacy")
        sig = NWC.decision_signals("NVDA")
        assert sig["entry_shrink"] is None


# ---------------------------------------------------------------------------
# clean_in_conflicted → conflicts==0 AND market contradiction_count>=3
# ---------------------------------------------------------------------------

class TestCleanInConflicted:
    def test_true_when_clean_and_tape_conflicted(self, monkeypatch, tmp_path):
        import brain.neural_web_context as NWC
        rows = {"NVDA": _cleared_row("WATCH", conflicts=0)}
        f = _write_fixture(tmp_path, candidate_context=rows,
                           contradiction_records=[{"s": 1}, {"s": 2}, {"s": 3}])
        _patch_path(monkeypatch, f)
        monkeypatch.setenv("MASTERMIND_NW_DECISION", "candidacy")
        sig = NWC.decision_signals("NVDA")
        assert sig["clean_in_conflicted"] is True

    def test_false_when_name_has_conflicts(self, monkeypatch, tmp_path):
        import brain.neural_web_context as NWC
        rows = {"NVDA": _cleared_row("WATCH", conflicts=1)}
        f = _write_fixture(tmp_path, candidate_context=rows,
                           contradiction_records=[{"s": 1}, {"s": 2}, {"s": 3}])
        _patch_path(monkeypatch, f)
        monkeypatch.setenv("MASTERMIND_NW_DECISION", "candidacy")
        sig = NWC.decision_signals("NVDA")
        assert sig["clean_in_conflicted"] is False

    def test_false_when_tape_below_contradiction_floor(self, monkeypatch, tmp_path):
        import brain.neural_web_context as NWC
        rows = {"NVDA": _cleared_row("WATCH", conflicts=0)}
        f = _write_fixture(tmp_path, candidate_context=rows,
                           contradiction_records=[{"s": 1}, {"s": 2}])  # only 2 < 3
        _patch_path(monkeypatch, f)
        monkeypatch.setenv("MASTERMIND_NW_DECISION", "candidacy")
        sig = NWC.decision_signals("NVDA")
        assert sig["clean_in_conflicted"] is False

    def test_not_available_below_candidacy(self, monkeypatch, tmp_path):
        """At shadow mode clean_in_conflicted stays False (not derived)."""
        import brain.neural_web_context as NWC
        rows = {"NVDA": _cleared_row("WATCH", conflicts=0)}
        f = _write_fixture(tmp_path, candidate_context=rows,
                           contradiction_records=[{"s": 1}, {"s": 2}, {"s": 3}])
        _patch_path(monkeypatch, f)
        monkeypatch.setenv("MASTERMIND_NW_DECISION", "shadow")
        sig = NWC.decision_signals("NVDA")
        assert sig["clean_in_conflicted"] is False


# ---------------------------------------------------------------------------
# absent / stale context → inert default, no raise
# ---------------------------------------------------------------------------

class TestFailSoft:
    def test_absent_context_inert(self, monkeypatch):
        import brain.neural_web_context as NWC
        _patch_path(monkeypatch, None)
        monkeypatch.setenv("MASTERMIND_NW_DECISION", "shrink")
        sig = NWC.decision_signals("NVDA")
        assert sig["inert"] is True
        assert sig["candidacy"] is None
        assert sig["entry_shrink"] is None
        assert sig["clean_in_conflicted"] is False
        assert sig["mode"] == "shrink"

    def test_stale_context_inert(self, monkeypatch, tmp_path):
        import brain.neural_web_context as NWC
        rows = {"NVDA": _cleared_row("BOTTOMING", conflicts=3)}
        f = _write_fixture(tmp_path, as_of=_stale_date(), candidate_context=rows)
        _patch_path(monkeypatch, f)
        monkeypatch.setenv("MASTERMIND_NW_DECISION", "candidacy")
        sig = NWC.decision_signals("NVDA")
        assert sig["inert"] is True
        assert sig["candidacy"] is None

    def test_unknown_ticker_inert(self, monkeypatch, tmp_path):
        import brain.neural_web_context as NWC
        rows = {"NVDA": _cleared_row("BOTTOMING", conflicts=3)}
        f = _write_fixture(tmp_path, candidate_context=rows)
        _patch_path(monkeypatch, f)
        monkeypatch.setenv("MASTERMIND_NW_DECISION", "candidacy")
        sig = NWC.decision_signals("MSFT")  # not in candidate_context
        assert sig["inert"] is True
        assert sig["candidacy"] is None

    def test_never_raises_on_malformed_row(self, monkeypatch, tmp_path):
        """A structurally broken row must degrade to inert, not raise."""
        import brain.neural_web_context as NWC
        # bottom is a string (not a dict), graph_conflicts is an int
        row = {
            "bottom": "not-a-dict",
            "graph_conflicts": 5,
            "kernel": {"fdr_cleared": True},
        }
        f = _write_fixture(tmp_path, candidate_context={"NVDA": row})
        _patch_path(monkeypatch, f)
        monkeypatch.setenv("MASTERMIND_NW_DECISION", "shrink")
        sig = NWC.decision_signals("NVDA")  # must not raise
        # cleared + present → not per-name inert; bad bottom → no candidacy;
        # non-list conflicts → treated as 0 → no shrink
        assert sig["inert"] is False
        assert sig["candidacy"] is None
        assert sig["entry_shrink"] is None
