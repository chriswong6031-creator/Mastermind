"""Tests for the FOUR P2 rework candidacy sources wired into brain/intake.py.

These sources wire already-built leaf modules (rotation_intake / divergence_clue /
neural_web_context / regime_frame + universe_triage) into the intake funnel. THE CARDINAL
RULE under test: with every flag at its default (OFF) each source is INERT — it returns {}
and contributes nothing, so the funnel is BYTE-IDENTICAL to pre-rework behaviour. The other
half of the proof (the existing intake funnel tests still pass with flags off) lives in
tests/test_intake.py, unchanged.

Each source is then exercised FLAG-ON with its leaf module monkeypatched, asserting it emits
the expected candidate(s) with the correct score / lean / reason. Finally each source's
fail-soft contract is checked: a leaf that raises → the source returns {} (no propagation).

The intake sources import their leaves LAZILY (``from brain import rotation_intake`` inside
the function), so patching ``brain.rotation_intake.active_calls`` (etc.) is what the source
resolves at call time.
"""
import bot  # noqa: F401 — triggers the vendor/macro_src path setup (mirrors test_intake)

import pytest

from brain import intake


# The rework flags — cleared here so every test starts from the default-OFF invariant regardless of
# the ambient shell environment. Includes the two buy-board LEARNING-loop flags (STANDOUT_UNGATED /
# BOARD_LEARNING) so the standout byte-identical-when-off baseline holds in every test too.
_REWORK_FLAGS = (
    "MASTERMIND_ROTATION_IN",
    "MASTERMIND_DIVERGENCE_CLUE",
    "MASTERMIND_NW_DECISION",
    "MASTERMIND_UNIVERSE_TRIAGE",
    "MASTERMIND_STANDOUT_UNGATED",
    "MASTERMIND_BOARD_LEARNING",
)


@pytest.fixture(autouse=True)
def _flags_off(monkeypatch):
    """Ensure none of the four rework flags are set — the default-OFF baseline for every test.
    Flag-on tests re-set the specific flag they exercise (a later setenv wins)."""
    for f in _REWORK_FLAGS:
        monkeypatch.delenv(f, raising=False)


# =========================================================================== #
# 1. BYTE-IDENTICAL — with no flag set, every new source returns {} directly,
#    and the funnel (build / tickers) runs without error.
# =========================================================================== #
def test_all_four_sources_inert_when_flags_unset():
    assert intake._from_rotation_in() == {}
    assert intake._from_divergence_clue() == {}
    assert intake._from_neural_web() == {}
    assert intake._from_cycles_bottoming() == {}


def test_build_and_tickers_run_with_flags_off(monkeypatch):
    # neutralise the on-disk vendored artifacts so the funnel exercises the seed fallback path
    # deterministically (no dependence on what the macro side has built), exactly as test_intake.
    monkeypatch.setattr(intake, "_read", lambda rel: None)
    monkeypatch.setattr(intake, "_from_open_theses", lambda: {})
    out = intake.build(limit=5)                       # must not raise
    assert out["n_universe"] == len(intake._SEED)     # pure seed fallback (all real sources empty)
    assert intake.tickers(limit=5) == [c["ticker"] for c in out["candidates"]]


def test_all_four_registered_in_loader_registry():
    # the four sources participate in the funnel (registered), even though they contribute {} off.
    for name in ("rotation_in", "divergence_clue", "neural_web", "cycles_bottoming"):
        assert name in intake._SIMPLE_SOURCES
        assert name in intake._LOADERS
        assert hasattr(intake, intake._LOADERS[name])


# =========================================================================== #
# 2. ROTATION-IN — flag {watch,starter}: expand active calls to member candidates
# =========================================================================== #
def test_rotation_in_emits_members_when_armed(monkeypatch):
    monkeypatch.setenv("MASTERMIND_ROTATION_IN", "watch")
    import brain.rotation_intake as ri
    monkeypatch.setattr(ri, "active_calls", lambda asof=None: [
        {"call_id": "c1", "state": "TURNING", "confidence": 0.8,
         "target": "XLK", "falsifier": "sector rolls over"},
    ])
    monkeypatch.setattr(ri, "expand", lambda call: [
        {"ticker": "NVDA", "score": 0.4}, {"ticker": "avgo", "score": 0.1}])

    out = intake._from_rotation_in()
    assert set(out) == {"NVDA", "AVGO"}
    # TURNING band 0.50 × confidence 0.80 = 0.40
    assert out["NVDA"]["score"] == pytest.approx(0.40)
    assert out["NVDA"]["lean"] == 1
    assert out["NVDA"]["reason"] == "rotation_in c1 TURNING"
    assert out["NVDA"]["confidence"] == pytest.approx(0.80)
    assert out["NVDA"]["falsifier"] == "sector rolls over"


def test_rotation_in_state_band_and_no_confidence(monkeypatch):
    monkeypatch.setenv("MASTERMIND_ROTATION_IN", "starter")
    import brain.rotation_intake as ri
    monkeypatch.setattr(ri, "active_calls", lambda asof=None: [
        {"call_id": "c2", "state": "CONFIRMED", "target": "XLE"}])   # no confidence → treated as 1.0
    monkeypatch.setattr(ri, "expand", lambda call: [{"ticker": "XOM", "score": None}])
    out = intake._from_rotation_in()
    assert out["XOM"]["score"] == pytest.approx(0.65)   # CONFIRMED band, unscaled
    assert out["XOM"]["confidence"] is None


def test_rotation_in_skips_terminal_state(monkeypatch):
    monkeypatch.setenv("MASTERMIND_ROTATION_IN", "watch")
    import brain.rotation_intake as ri
    monkeypatch.setattr(ri, "active_calls", lambda asof=None: [
        {"call_id": "c3", "state": "EXPIRED", "target": "XLF"}])     # not in the score band
    monkeypatch.setattr(ri, "expand", lambda call: [{"ticker": "JPM", "score": None}])
    assert intake._from_rotation_in() == {}


def test_rotation_in_still_inert_at_shadowlike_value(monkeypatch):
    # any value outside {watch, starter} (incl. an unknown token) is OFF.
    monkeypatch.setenv("MASTERMIND_ROTATION_IN", "shadow")
    import brain.rotation_intake as ri
    monkeypatch.setattr(ri, "active_calls", lambda asof=None: [
        {"call_id": "c4", "state": "EARLY", "target": "XLK"}])
    monkeypatch.setattr(ri, "expand", lambda call: [{"ticker": "NVDA", "score": None}])
    assert intake._from_rotation_in() == {}


# =========================================================================== #
# 3. DIVERGENCE-CLUE — flag on: emit one candidate per scanned clue row
# =========================================================================== #
def test_divergence_clue_emits_when_armed(monkeypatch):
    monkeypatch.setenv("MASTERMIND_DIVERGENCE_CLUE", "1")
    import brain.divergence_clue as dc
    monkeypatch.setattr(dc, "clue_flag_enabled", lambda: True)
    monkeypatch.setattr(dc, "scan", lambda asof=None, **kw: [
        {"ticker": "AAPL", "sector": "XLK", "sector_etf": "XLK", "score": 0.55,
         "safe_haven": True, "falsifier": {"kind": "rel_return", "value": 0}},
    ])
    out = intake._from_divergence_clue()
    assert list(out) == ["AAPL"]
    assert out["AAPL"]["score"] == pytest.approx(0.55)
    assert out["AAPL"]["lean"] == 1
    assert out["AAPL"]["reason"] == "divergence_clue XLK safe_haven=True"
    assert out["AAPL"]["falsifier"] == {"kind": "rel_return", "value": 0}


def test_divergence_clue_inert_when_flag_helper_false(monkeypatch):
    # the source trusts the leaf's own flag helper: even if the env var is set, a helper
    # returning False keeps the source inert (single source of truth for the gate).
    monkeypatch.setenv("MASTERMIND_DIVERGENCE_CLUE", "1")
    import brain.divergence_clue as dc
    monkeypatch.setattr(dc, "clue_flag_enabled", lambda: False)
    monkeypatch.setattr(dc, "scan", lambda asof=None, **kw: [{"ticker": "AAPL", "score": 0.5}])
    assert intake._from_divergence_clue() == {}


# =========================================================================== #
# 4. NEURAL-WEB — mode >= candidacy: emit decision_signals candidacy per NW name
# =========================================================================== #
def test_neural_web_emits_when_candidacy_armed(monkeypatch):
    monkeypatch.setenv("MASTERMIND_NW_DECISION", "candidacy")
    import brain.neural_web_context as nw
    monkeypatch.setattr(nw, "nw_decision_mode", lambda: "candidacy")
    monkeypatch.setattr(nw, "context", lambda: {"candidate_context": {"MSFT": {}, "meta": {}}})

    def _sig(ticker):
        if ticker.upper() == "MSFT":
            return {"candidacy": {"state": "BOTTOMING", "score": 0.50, "lean": 1}, "inert": False}
        return {"candidacy": None, "inert": False}
    monkeypatch.setattr(nw, "decision_signals", _sig)

    out = intake._from_neural_web()
    assert list(out) == ["MSFT"]
    assert out["MSFT"]["score"] == pytest.approx(0.50)
    assert out["MSFT"]["lean"] == 1
    assert out["MSFT"]["reason"] == "nw bottom=BOTTOMING"


def test_neural_web_inert_below_candidacy(monkeypatch):
    # shadow is on the ladder but BELOW candidacy → the source must stay inert.
    monkeypatch.setenv("MASTERMIND_NW_DECISION", "shadow")
    import brain.neural_web_context as nw
    monkeypatch.setattr(nw, "nw_decision_mode", lambda: "shadow")
    monkeypatch.setattr(nw, "context", lambda: {"candidate_context": {"MSFT": {}}})
    monkeypatch.setattr(nw, "decision_signals",
                        lambda t: {"candidacy": {"state": "BOTTOMING", "score": 0.5, "lean": 1}})
    assert intake._from_neural_web() == {}


def test_neural_web_skips_names_without_candidacy(monkeypatch):
    monkeypatch.setenv("MASTERMIND_NW_DECISION", "vote")   # vote is >= candidacy
    import brain.neural_web_context as nw
    monkeypatch.setattr(nw, "nw_decision_mode", lambda: "vote")
    monkeypatch.setattr(nw, "context", lambda: {"candidate_context": {"AMD": {}}})
    monkeypatch.setattr(nw, "decision_signals", lambda t: {"candidacy": None, "inert": False})
    assert intake._from_neural_web() == {}


# =========================================================================== #
# 5. CYCLES-BOTTOMING — flag on: entry_favored ∧ osc_slope>0 sectors → sector ETF
# =========================================================================== #
def test_cycles_bottoming_emits_when_armed(monkeypatch):
    monkeypatch.setenv("MASTERMIND_UNIVERSE_TRIAGE", "1")
    import brain.regime_frame as rf
    monkeypatch.setattr(rf, "cycles", lambda: {
        "XLV": {"entry_favored": True, "osc_slope": 0.8, "phase": "Recovery"},   # qualifies
        "XLE": {"entry_favored": True, "osc_slope": -0.3, "phase": "Trough"},    # slope down → skip
        "XLK": {"entry_favored": False, "osc_slope": 1.2, "phase": "Peak"},      # not favored → skip
    })
    # universe_triage corroboration reachable but empty (no triage-favored tag)
    import brain.universe_triage as ut
    monkeypatch.setattr(ut, "favored_sectors", lambda: [])

    out = intake._from_cycles_bottoming()
    assert list(out) == ["XLV"]
    assert out["XLV"]["score"] == pytest.approx(0.4)
    assert out["XLV"]["lean"] == 1
    assert out["XLV"]["reason"] == "cycle_bottoming XLV"


def test_cycles_bottoming_notes_triage_corroboration(monkeypatch):
    monkeypatch.setenv("MASTERMIND_UNIVERSE_TRIAGE", "1")
    import brain.regime_frame as rf
    import brain.universe_triage as ut
    monkeypatch.setattr(rf, "cycles", lambda: {
        "XLV": {"entry_favored": True, "osc_slope": 0.5, "phase": "Recovery"}})
    monkeypatch.setattr(ut, "favored_sectors", lambda: ["XLV"])
    out = intake._from_cycles_bottoming()
    assert out["XLV"]["reason"] == "cycle_bottoming XLV (triage-favored)"


def test_cycles_bottoming_corroboration_absence_is_soft(monkeypatch):
    # universe_triage raising must not sink the source — it just drops the corroboration note.
    monkeypatch.setenv("MASTERMIND_UNIVERSE_TRIAGE", "1")
    import brain.regime_frame as rf
    import brain.universe_triage as ut
    monkeypatch.setattr(rf, "cycles", lambda: {
        "XLV": {"entry_favored": True, "osc_slope": 0.5, "phase": "Recovery"}})

    def _boom():
        raise RuntimeError("triage read failed")
    monkeypatch.setattr(ut, "favored_sectors", _boom)
    out = intake._from_cycles_bottoming()
    assert out["XLV"]["reason"] == "cycle_bottoming XLV"   # no corroboration, still emitted


# =========================================================================== #
# 6. FAIL-SOFT — a leaf that raises → the source returns {} (no propagation)
# =========================================================================== #
def test_rotation_in_fail_soft(monkeypatch):
    monkeypatch.setenv("MASTERMIND_ROTATION_IN", "watch")
    import brain.rotation_intake as ri

    def _boom(asof=None):
        raise RuntimeError("rotation read blew up")
    monkeypatch.setattr(ri, "active_calls", _boom)
    assert intake._from_rotation_in() == {}   # must not raise


def test_divergence_clue_fail_soft(monkeypatch):
    monkeypatch.setenv("MASTERMIND_DIVERGENCE_CLUE", "1")
    import brain.divergence_clue as dc
    monkeypatch.setattr(dc, "clue_flag_enabled", lambda: True)

    def _boom(asof=None, **kw):
        raise RuntimeError("scan blew up")
    monkeypatch.setattr(dc, "scan", _boom)
    assert intake._from_divergence_clue() == {}


def test_neural_web_fail_soft(monkeypatch):
    monkeypatch.setenv("MASTERMIND_NW_DECISION", "candidacy")
    import brain.neural_web_context as nw
    monkeypatch.setattr(nw, "nw_decision_mode", lambda: "candidacy")

    def _boom():
        raise RuntimeError("context blew up")
    monkeypatch.setattr(nw, "context", _boom)
    assert intake._from_neural_web() == {}


def test_cycles_bottoming_fail_soft(monkeypatch):
    monkeypatch.setenv("MASTERMIND_UNIVERSE_TRIAGE", "1")
    import brain.regime_frame as rf

    def _boom():
        raise RuntimeError("cycles blew up")
    monkeypatch.setattr(rf, "cycles", _boom)
    assert intake._from_cycles_bottoming() == {}


# =========================================================================== #
# 7. INTEGRATION — an armed source flows through build() into the funnel
# =========================================================================== #
def test_armed_source_flows_into_build(monkeypatch):
    # divergence_clue armed → its clue must appear as a merged candidate with provenance.
    monkeypatch.setattr(intake, "_read", lambda rel: None)          # no vendored artifacts
    monkeypatch.setattr(intake, "_from_open_theses", lambda: {})
    monkeypatch.setenv("MASTERMIND_DIVERGENCE_CLUE", "1")
    import brain.divergence_clue as dc
    monkeypatch.setattr(dc, "clue_flag_enabled", lambda: True)
    monkeypatch.setattr(dc, "scan", lambda asof=None, **kw: [
        {"ticker": "AAPL", "sector": "XLK", "score": 0.55, "safe_haven": True, "falsifier": None}])

    cands = {c["ticker"]: c for c in intake.build(limit=20)["candidates"]}
    assert "AAPL" in cands
    assert "divergence_clue" in cands["AAPL"]["sources"]
    assert cands["AAPL"]["lean"] == 1


# =========================================================================== #
# 8. STANDOUT LEARNING LOOP — the gate_go candidacy fix + the board-edge trust multiplier.
#    Two flags (MASTERMIND_STANDOUT_UNGATED / MASTERMIND_BOARD_LEARNING), both default OFF ⇒
#    _from_standouts is BYTE-IDENTICAL to today (a gate_go=False board is skipped, scores un-shrunk).
# =========================================================================== #

# a gate_go=False standout board carrying one buy name (AAPL) — the board the dashboard flagged NO-GO.
_GATE_NOGO_BOARD = {
    "gate_go": False,
    "buy": [{"ticker": "AAPL", "label": "BUY ZONE", "conviction": 0.8,
             "entry_signal": {"stop": 180.0, "buy_zone": 195.0, "entry_grade": "A"}}],
}
# a gate-cleared board (gate_go True) — ingested unconditionally today.
_GATE_GO_BOARD = {
    "gate_go": True,
    "buy": [{"ticker": "AAPL", "label": "BUY ZONE", "conviction": 0.8}],
}


def _patch_standouts(monkeypatch, board):
    """Make intake._read return `board` for the standouts artifact only (None otherwise)."""
    monkeypatch.setattr(
        intake, "_read",
        lambda rel: board if rel == "factordata/us_standouts.json" else None)


def test_standouts_flags_off_skips_gate_nogo_byte_identical(monkeypatch):
    # (a) both flags OFF + gate_go=False → the WHOLE source is skipped, exactly as today.
    _patch_standouts(monkeypatch, _GATE_NOGO_BOARD)
    assert intake._from_standouts() == {}


def test_standouts_flags_off_still_ingests_gate_go_board(monkeypatch):
    # control: a gate_go=True board is still ingested un-shrunk with flags off (nothing regressed).
    _patch_standouts(monkeypatch, _GATE_GO_BOARD)
    out = intake._from_standouts()
    assert set(out) == {"AAPL"}
    assert out["AAPL"]["score"] == pytest.approx(0.8)          # raw conviction, un-shrunk
    assert out["AAPL"]["reason"] == "buy-board: BUY ZONE"      # today's reason string


def test_standout_ungated_admits_gate_nogo_as_candidacy(monkeypatch):
    # (b) MASTERMIND_STANDOUT_UNGATED on + gate_go=False → the board's names appear as candidates.
    monkeypatch.setenv("MASTERMIND_STANDOUT_UNGATED", "1")
    _patch_standouts(monkeypatch, _GATE_NOGO_BOARD)
    out = intake._from_standouts()
    assert set(out) == {"AAPL"}                                 # NOT skipped anymore
    assert out["AAPL"]["lean"] == 1
    # base conviction (0.8) is damped on the ungated path (candidacy, not sizing).
    assert out["AAPL"]["score"] == pytest.approx(round(0.8 * intake._UNGATED_SCORE_DAMP, 3))
    # reason is tagged with the event-edge rationale (the operator's requested tag).
    assert "gate_go=false event-edge" in out["AAPL"]["reason"]
    # provenance entry-risk levels still carried through.
    assert out["AAPL"]["stop"] == 180.0 and out["AAPL"]["entry_grade"] == "A"


def test_standout_ungated_flows_into_build(monkeypatch):
    # integration: the ungated board name surfaces as a merged candidate through build().
    monkeypatch.setenv("MASTERMIND_STANDOUT_UNGATED", "1")
    _patch_standouts(monkeypatch, _GATE_NOGO_BOARD)
    monkeypatch.setattr(intake, "_from_open_theses", lambda: {})
    cands = {c["ticker"]: c for c in intake.build(limit=20)["candidates"]}
    assert "AAPL" in cands
    assert "standout" in cands["AAPL"]["sources"]


def test_board_learning_shrinks_standout_scores(monkeypatch):
    # (c) MASTERMIND_BOARD_LEARNING on + a WEAK board → standout scores shrunk by 0.75.
    monkeypatch.setenv("MASTERMIND_BOARD_LEARNING", "1")
    _patch_standouts(monkeypatch, _GATE_GO_BOARD)          # gate-cleared so the source ingests
    import brain.board_learning as bl
    monkeypatch.setattr(bl, "standout_trust_multiplier", lambda: 0.75)
    out = intake._from_standouts()
    # base 0.8 × weak-board multiplier 0.75 = 0.6
    assert out["AAPL"]["score"] == pytest.approx(round(0.8 * 0.75, 3))


def test_board_learning_noop_when_board_empty(monkeypatch):
    # an empty/insufficient board → multiplier 1.0 → scores UNCHANGED even with the flag on.
    monkeypatch.setenv("MASTERMIND_BOARD_LEARNING", "1")
    _patch_standouts(monkeypatch, _GATE_GO_BOARD)
    import brain.board_learning as bl
    monkeypatch.setattr(bl, "standout_trust_multiplier", lambda: 1.0)   # insufficient-board neutral
    out = intake._from_standouts()
    assert out["AAPL"]["score"] == pytest.approx(0.8)      # no shrink applied


def test_board_learning_off_leaves_scores_unshrunk(monkeypatch):
    # flag OFF → the multiplier is NEVER consulted; even a would-be-weak board leaves scores untouched.
    _patch_standouts(monkeypatch, _GATE_GO_BOARD)
    import brain.board_learning as bl

    def _should_not_be_called():
        raise AssertionError("board_learning must not be consulted when the flag is OFF")
    monkeypatch.setattr(bl, "standout_trust_multiplier", _should_not_be_called)
    out = intake._from_standouts()
    assert out["AAPL"]["score"] == pytest.approx(0.8)


def test_board_learning_fail_soft_leaves_scores(monkeypatch):
    # flag ON but the multiplier RAISES → scores are left untouched (fail-soft, no propagation).
    monkeypatch.setenv("MASTERMIND_BOARD_LEARNING", "1")
    _patch_standouts(monkeypatch, _GATE_GO_BOARD)
    import brain.board_learning as bl

    def _boom():
        raise RuntimeError("learning loop blew up")
    monkeypatch.setattr(bl, "standout_trust_multiplier", _boom)
    out = intake._from_standouts()                         # must not raise
    assert out["AAPL"]["score"] == pytest.approx(0.8)      # untouched
