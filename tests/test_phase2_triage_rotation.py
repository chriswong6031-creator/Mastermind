"""Isolated unit tests for the two flag-gated phase2 additions:

  CHANGE 1 — universe-triage leadership reduce-sector SUPPRESSION
             (flag MASTERMIND_UNIVERSE_TRIAGE; helper `_suppress_reduce_sectors`).
  CHANGE 2 — pre-ignition rotation-in watchlist enrollment mode
             (flag MASTERMIND_ROTATION_IN; helper `_rotation_in_mode`).

These exercise ONLY the new pure/flag helpers in isolation — they do NOT call phase2.run()
(the full-build path is expensive and touches build state; see the module SAFETY note). The
cardinal guarantee under test: with both flags at their default (OFF), the new code is a no-op
and the leadership selection is byte-identical to today.
"""
import pytest

from bot import phase2


# ─────────────────────────── flag helpers: default OFF ───────────────────────────

def test_universe_triage_flag_default_off(monkeypatch):
    monkeypatch.delenv("MASTERMIND_UNIVERSE_TRIAGE", raising=False)
    assert phase2._universe_triage_enabled() is False


@pytest.mark.parametrize("val", ["1", "true", "yes", "on", "TRUE", "On"])
def test_universe_triage_flag_on_tokens(monkeypatch, val):
    monkeypatch.setenv("MASTERMIND_UNIVERSE_TRIAGE", val)
    assert phase2._universe_triage_enabled() is True


@pytest.mark.parametrize("val", ["0", "false", "no", "off", "", "garbage"])
def test_universe_triage_flag_off_tokens(monkeypatch, val):
    monkeypatch.setenv("MASTERMIND_UNIVERSE_TRIAGE", val)
    assert phase2._universe_triage_enabled() is False


def test_rotation_in_mode_default_off(monkeypatch):
    monkeypatch.delenv("MASTERMIND_ROTATION_IN", raising=False)
    assert phase2._rotation_in_mode() == "off"


@pytest.mark.parametrize("val,expected", [
    ("off", "off"), ("watch", "watch"), ("starter", "starter"),
    ("WATCH", "watch"), ("Starter", "starter"),
    ("shadow", "off"), ("", "off"), ("garbage", "off"),
])
def test_rotation_in_mode_ladder(monkeypatch, val, expected):
    monkeypatch.setenv("MASTERMIND_ROTATION_IN", val)
    assert phase2._rotation_in_mode() == expected


# ─────────────────── CHANGE 1 — _suppress_reduce_sectors (pure) ───────────────────

def _leaders(*tickers):
    """Synthetic _leaders_pre rows (only the `ticker` key is read by the helper)."""
    return [{"ticker": t} for t in tickers]


def test_suppress_disabled_returns_input_unchanged_same_object():
    """INVARIANT (i): flag OFF ⇒ reduce set empty AND `leaders is leaders_pre` (SAME object) ⇒
    byte-identical. action_fn must never be consulted when disabled."""
    pre = _leaders("XLK", "XLF", "XLE")

    def _boom(_t):
        raise AssertionError("action_fn must NOT be called when disabled")

    leaders, reduce_secs = phase2._suppress_reduce_sectors(pre, set(), False, _boom)
    assert leaders is pre                 # SAME object — not a copy
    assert reduce_secs == set()


def test_suppress_drops_new_reduce_sector():
    pre = _leaders("XLK", "XLE", "XLF")
    action = {"XLK": "favor", "XLE": "reduce", "XLF": "neutral"}.get

    leaders, reduce_secs = phase2._suppress_reduce_sectors(pre, set(), True, action)
    assert reduce_secs == {"XLE"}
    assert [s["ticker"] for s in leaders] == ["XLK", "XLF"]   # order preserved, XLE dropped


def test_suppress_exempts_held_reduce_sector():
    """INVARIANT (iii): a HELD leadership ticker at 'reduce' is EXEMPT (never suppressed).
    `held` is compared upper-cased; the leader ticker matches case-insensitively."""
    pre = _leaders("XLK", "XLE")
    action = {"XLK": "favor", "XLE": "reduce"}.get

    leaders, reduce_secs = phase2._suppress_reduce_sectors(pre, {"XLE"}, True, action)
    assert reduce_secs == set()           # XLE is held → exempt
    assert leaders is pre                 # nothing dropped ⇒ same object returned


def test_suppress_no_reduce_returns_same_object():
    """When enabled but nothing is at 'reduce', the input object is returned unchanged (no copy),
    so the lw-on-pre-count path stays byte-identical."""
    pre = _leaders("XLK", "XLF")
    action = {"XLK": "favor", "XLF": "neutral"}.get

    leaders, reduce_secs = phase2._suppress_reduce_sectors(pre, set(), True, action)
    assert reduce_secs == set()
    assert leaders is pre


def test_suppress_fail_soft_on_action_fn_error():
    """Fail-soft: any exception from action_fn ⇒ NO suppression (empty set, input unchanged).
    A triage fault can only leave the book unchanged, never partially suppress."""
    pre = _leaders("XLK", "XLE")

    def _raises(_t):
        raise RuntimeError("triage artifact unreadable")

    leaders, reduce_secs = phase2._suppress_reduce_sectors(pre, set(), True, _raises)
    assert reduce_secs == set()
    assert leaders is pre


def test_lw_uses_pre_count_freed_goes_to_cash():
    """INVARIANT (ii): the per-leg weight `lw` is ALWAYS computed on len(leaders_pre), so suppressing
    a leg reduces total leadership exposure (freed → cash) and NEVER up-weights a survivor.

    Replicate the caller's arithmetic exactly: lw = lead_budget / max(1, len(_leaders_pre))."""
    pre = _leaders("XLK", "XLE", "XLF", "XLV")   # 4 pre-suppression legs
    lead_budget = 0.40
    lw = round(lead_budget / max(1, len(pre)), 4)   # == 0.10, on the PRE count

    action = {"XLK": "favor", "XLE": "reduce", "XLF": "neutral", "XLV": "favor"}.get
    leaders, reduce_secs = phase2._suppress_reduce_sectors(pre, set(), True, action)

    assert reduce_secs == {"XLE"}
    # each surviving leg keeps lw (0.10), NOT lead_budget/len(leaders) (which would be 0.1333):
    assert lw == 0.10
    gross_leadership = round(lw * len(leaders), 4)
    assert gross_leadership == 0.30                 # 3 legs × 0.10 — the dropped 0.10 is freed to cash
    # the freed budget equals exactly the suppressed legs × lw:
    assert round(lw * len(reduce_secs), 4) == 0.10


def test_suppress_matches_disabled_when_no_reduce():
    """Cross-check: enabled-but-nothing-reduces yields the identical (leaders, set) as disabled."""
    pre = _leaders("XLK", "XLF")
    neutral = lambda _t: "neutral"  # noqa: E731

    off_leaders, off_set = phase2._suppress_reduce_sectors(pre, set(), False, neutral)
    on_leaders, on_set = phase2._suppress_reduce_sectors(pre, set(), True, neutral)
    assert off_leaders is on_leaders is pre
    assert off_set == on_set == set()
