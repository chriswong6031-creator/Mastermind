"""Guards for the L3 entry-timing gate (subtract-only, flag-gated) + the Flagship watchlist.

Fully offline: no vendor/macro engine, no network. The gate's load-bearing logic is two pure,
isolatable pieces — the WITHHOLD predicate (``portfolio.watchlist.timing_withhold``, the exact
mirror of the ``portfolio.desk_ab`` shadow lever) and the default-OFF env flag
(``bot.phase2._timing_gate_enabled``). The watchlist log is redirected into a tmp dir so prod
state is never touched.

Properties pinned down:
  * the gate WITHHOLDS an over-extended / weak-RS / 'avoid' / parabolic / weak-eq fixture and does
    NOT withhold a clean one;
  * with the flag OFF (the default) the gate is INERT — it withholds NOTHING (behavior unchanged);
  * a withheld name lands on the watchlist with a reason;
  * the gate is SUBTRACT-ONLY: the predicate only ever returns a reason (→ drop) or None (→ keep) —
    it can never produce a weight, an add, or an upsize;
  * the predicate matches the shadow lever (desk_ab) name-for-name on the same fixtures;
  * watchlist.append is idempotent per (ticker, date) and the read API returns the parked names.
"""
from __future__ import annotations

import json

import pytest

from portfolio import watchlist


# ── fixtures ──────────────────────────────────────────────────────────────────
def _tech(**kw):
    """An _entry_tech_fields-shaped dict: a CLEAN entry by default (every gate passes)."""
    base = {"pct_vs_200dma": 8.0, "rs": 80.0, "urgency": "now", "eq_grade": "ok", "parabolic": False}
    base.update(kw)
    return base


@pytest.fixture()
def wl_sandbox(tmp_path, monkeypatch):
    """Redirect the watchlist JSONL into a temp dir so prod state is never touched."""
    monkeypatch.setattr(watchlist, "_WATCHLIST", tmp_path / "watchlist.jsonl")
    return tmp_path


# ── the WITHHOLD predicate (subtract-only: reason → drop, None → keep) ─────────
def test_clean_entry_is_not_withheld():
    assert watchlist.timing_withhold(_tech()) is None          # clean → buy proceeds unchanged


def test_extended_grade_is_withheld():
    for g in ("stretched", "parabolic", "extended"):
        assert watchlist.timing_withhold(_tech(eq_grade=g)) is not None


def test_parabolic_flag_is_withheld():
    assert watchlist.timing_withhold(_tech(parabolic=True)) is not None


def test_far_above_200dma_is_withheld():
    assert watchlist.timing_withhold(_tech(pct_vs_200dma=45.0)) is not None   # >= 30 → extended
    assert watchlist.timing_withhold(_tech(pct_vs_200dma=29.9)) is None       # just under the bar


def test_weak_rs_is_withheld():
    assert watchlist.timing_withhold(_tech(rs=20.0)) is not None              # < 50 → weak RS
    assert watchlist.timing_withhold(_tech(rs=50.0)) is None                  # exactly the bar passes


def test_avoid_urgency_is_withheld():
    assert watchlist.timing_withhold(_tech(urgency="avoid")) is not None
    assert watchlist.timing_withhold(_tech(urgency="now")) is None


def test_weak_eq_grade_is_withheld():
    assert watchlist.timing_withhold(_tech(eq_grade="weak")) is not None


def test_fails_open_on_missing_fields():
    # all-None snapshot (the _entry_tech_fields shape when the read fails) → withhold NOTHING
    allnone = {"pct_vs_200dma": None, "rs": None, "urgency": None, "eq_grade": None, "parabolic": None}
    assert watchlist.timing_withhold(allnone) is None
    assert watchlist.timing_withhold({}) is None
    assert watchlist.timing_withhold(None) is None


def test_predicate_is_subtract_only_returns_reason_or_none():
    # the predicate's ONLY outputs are a reason string (drop) or None (keep) — never a weight/size.
    for t in (_tech(), _tech(rs=10.0), _tech(parabolic=True), {}, None):
        out = watchlist.timing_withhold(t)
        assert out is None or isinstance(out, str)


# ── the predicate is IDENTICAL to the shadow lever (desk_ab._timing_ok) ────────
def test_predicate_matches_shadow_lever_on_same_signals():
    """The live gate (watchlist.timing_withhold over _entry_tech_fields) and the forward A/B lever
    (desk_ab._timing_ok over the raw stockdata JSON) must agree name-for-name."""
    from portfolio import desk_ab
    cases = [
        # (stockdata payload, equivalent _entry_tech_fields dict)
        ({"conviction": {"ext": {"grade": "ok"}}, "tech": {"pct_vs_200dma": 8.0},
          "alpha": {"rs": 80}}, _tech()),
        ({"conviction": {"ext": {"grade": "parabolic"}}}, _tech(eq_grade="parabolic")),
        ({"tech": {"pct_vs_200dma": 45.0}}, _tech(pct_vs_200dma=45.0)),
        ({"alpha": {"rs": 20}}, _tech(rs=20.0)),
        ({"ladder": {"entry": {"urgency": "avoid"}}}, _tech(urgency="avoid")),
    ]
    for sd, tech in cases:
        lever_keeps = desk_ab._timing_ok("X", sd)              # True = keep
        gate_withholds = watchlist.timing_withhold(tech) is not None
        assert lever_keeps == (not gate_withholds)             # keep iff not withheld — identical


# ── the env flag: DEFAULT ON (W2.2 'arm the coded withhold'), OPT-OUT to disable ──────────
def test_flag_defaults_on(monkeypatch):
    # W2.2: the brake is now armed by default (it was built, tested and dark while the book bought
    # extended names). Unset env → ON.
    from bot import phase2
    monkeypatch.delenv("MASTERMIND_TIMING_GATE", raising=False)
    assert phase2._timing_gate_enabled() is True               # default ON → gate active


def test_flag_is_opt_out(monkeypatch):
    from bot import phase2
    # explicit falsy values (and empty) DISABLE — the opt-out escape hatch back to the pre-W2 path.
    for v in ("0", "false", "FALSE", "no", "off", ""):
        monkeypatch.setenv("MASTERMIND_TIMING_GATE", v)
        assert phase2._timing_gate_enabled() is False
    # anything else (truthy or arbitrary) leaves the default-ON brake ARMED.
    for v in ("1", "true", "TRUE", "yes", "on", "maybe"):
        monkeypatch.setenv("MASTERMIND_TIMING_GATE", v)
        assert phase2._timing_gate_enabled() is True


def test_opt_out_path_withholds_nothing(monkeypatch):
    """With the flag explicitly OFF, the gate branch in phase2 is never entered: OFF means NO withhold
    regardless of how extended the entry is — the escape hatch back to the pre-W2 buy path."""
    from bot import phase2
    monkeypatch.setenv("MASTERMIND_TIMING_GATE", "0")
    gate_active = phase2._timing_gate_enabled()
    extended = _tech(parabolic=True, rs=5.0, pct_vs_200dma=99.0, urgency="avoid", eq_grade="weak")
    would_withhold = gate_active and (watchlist.timing_withhold(extended) is not None)
    assert would_withhold is False                             # flag OFF → nothing withheld


# ── the parabolic explore guard (W2.2): ε-exploration must NEVER resurrect a parabolic withhold ──
def test_parabolic_withhold_is_non_explorable():
    from bot import phase2
    # the parabolic flag OR a literal 'parabolic' entry-quality grade marks a NON-explorable withhold.
    assert phase2._is_parabolic_withhold(_tech(parabolic=True)) is True
    assert phase2._is_parabolic_withhold(_tech(eq_grade="parabolic")) is True


def test_non_parabolic_withhold_stays_explorable():
    from bot import phase2
    # every OTHER withhold class (extended / weak-RS / 'avoid' / weak-eq) remains explorable.
    assert phase2._is_parabolic_withhold(_tech(pct_vs_200dma=99.0)) is False
    assert phase2._is_parabolic_withhold(_tech(rs=5.0)) is False
    assert phase2._is_parabolic_withhold(_tech(urgency="avoid")) is False
    assert phase2._is_parabolic_withhold(_tech(eq_grade="weak")) is False
    assert phase2._is_parabolic_withhold(_tech(eq_grade="stretched")) is False   # extended != parabolic


def test_parabolic_guard_fails_open_on_bad_input():
    from bot import phase2
    # a malformed/None snapshot returns False — it can never manufacture a parabolic block out of
    # missing data (the guard only ever KEEPS the explorable path for a non-parabolic reason).
    assert phase2._is_parabolic_withhold(None) is False
    assert phase2._is_parabolic_withhold({}) is False
    assert phase2._is_parabolic_withhold({"parabolic": None, "eq_grade": None}) is False


def test_explore_gate_composition_skips_parabolic_only():
    """The load-bearing composition in the phase2 timing-withhold block is:
        if not _is_parabolic_withhold(tech):
            if _maybe_explore(...): continue
    i.e. exploration is consulted for a withheld name IFF the withhold is NOT parabolic. Model that
    decision here (with _maybe_explore stubbed to 'always explores when consulted') and assert a
    parabolic withhold is NEVER explored while every other withhold class still is."""
    from bot import phase2

    def _would_explore(tech):
        # exact mirror of the guarded branch: explore only when NOT a parabolic withhold.
        consulted = not phase2._is_parabolic_withhold(tech)
        maybe_explore_result = True                       # stub: exploration always fires WHEN consulted
        return consulted and maybe_explore_result

    # parabolic withholds → never explored
    assert _would_explore(_tech(parabolic=True)) is False
    assert _would_explore(_tech(eq_grade="parabolic")) is False
    # every other withhold class → explorable (the intended off-policy channel is preserved)
    assert _would_explore(_tech(pct_vs_200dma=99.0)) is True
    assert _would_explore(_tech(rs=5.0)) is True
    assert _would_explore(_tech(urgency="avoid")) is True
    assert _would_explore(_tech(eq_grade="weak")) is True


# ── the watchlist log: idempotent per (ticker, date), readable for re-review ───
def test_append_records_a_withheld_name(wl_sandbox):
    ok = watchlist.append("NVDA", "2026-06-22", "weak RS (20<50)",
                          tech=_tech(rs=20.0), combined=64)
    assert ok is True
    rows = watchlist.for_date("2026-06-22")
    assert len(rows) == 1
    r = rows[0]
    assert r["ticker"] == "NVDA" and r["reason"] == "weak RS (20<50)"
    assert r["combined"] == 64 and r["tech"]["rs"] == 20.0


def test_append_is_idempotent_per_ticker_and_date(wl_sandbox):
    watchlist.append("AVGO", "2026-06-22", "extended", tech=_tech(pct_vs_200dma=45.0))
    watchlist.append("AVGO", "2026-06-22", "extended (re-run, same day)", tech=_tech(pct_vs_200dma=46.0))
    rows = watchlist.for_date("2026-06-22")
    assert len([r for r in rows if r["ticker"] == "AVGO"]) == 1     # one row per (ticker, date)
    assert rows[0]["reason"] == "extended (re-run, same day)"        # the latest write wins
    # a DIFFERENT day for the same name is a distinct record
    watchlist.append("AVGO", "2026-06-23", "extended", tech=_tech(pct_vs_200dma=44.0))
    assert len(watchlist.all_rows()) == 2


def test_read_api_returns_parked_names(wl_sandbox):
    watchlist.append("AAA", "2026-06-22", "weak RS")
    watchlist.append("BBB", "2026-06-22", "avoid")
    watchlist.append("AAA", "2026-06-23", "weak RS again")          # AAA re-parked next day
    assert {r["ticker"] for r in watchlist.for_date("2026-06-22")} == {"AAA", "BBB"}
    # latest() de-dupes to the most-recent record per ticker
    latest = {r["ticker"]: r for r in watchlist.latest()}
    assert set(latest) == {"AAA", "BBB"}
    assert latest["AAA"]["asof"] == "2026-06-23" and latest["AAA"]["reason"] == "weak RS again"


def test_append_tolerates_bad_input(wl_sandbox):
    assert watchlist.append("", "2026-06-22", "x") is False        # no ticker → no row
    assert watchlist.append("AAA", "", "x") is False               # no date → no row
    assert watchlist.all_rows() == []


def test_append_is_durable_across_reads(wl_sandbox):
    watchlist.append("ZZZ", "2026-06-22", "avoid", tech=_tech(urgency="avoid"))
    # re-read straight off disk to prove the JSONL round-trips
    raw = [json.loads(l) for l in (wl_sandbox / "watchlist.jsonl").read_text().splitlines() if l.strip()]
    assert raw[0]["ticker"] == "ZZZ" and raw[0]["tech"]["urgency"] == "avoid"
