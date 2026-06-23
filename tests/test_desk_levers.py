"""Offline guards for Arm A of the desk A/B experiment (loop.desk_levers).

These run with NO vendor/macro and NO network: we build a small synthetic close panel + PIT
membership in-memory and assert the broadened desk-lever roster (5 economically-distinct baseline
families × 4 levers) obeys the audited FactorCandidate hygiene (valid weight matrix, SPY weight 0,
no look-ahead), that within EACH family the L2-concentration lever holds FEWER names than the family
baseline (frac 0.05 vs 0.10), that the L3 entry-timing screen produces a DIFFERENT book from the
family baseline, that the families are genuinely distinct (the composite blends them; reversal is
~anti-momentum), and that build() degrades to an honest stub (never crashes) when the panel/infra is
absent.

The heavy frozen-gauntlet run is exercised on real data only via scripts/run_desk_levers.py, not here.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

# Arm A subclasses FactorCandidate, which transitively imports the vendored macro `engine` +
# `lib` stack (loop.factor_experiment lines 37-40). In a site-only macro mirror (no engine
# package checked out) these tests cannot load the base class — skip cleanly; they execute in
# the full-macro / production checkout where the frozen gauntlet actually runs.
pytest.importorskip("engine", reason="vendored macro `engine` package not present in this checkout")

from loop import desk_levers as DL  # noqa: E402


# ─────────────────────────────────────────────────────────────────────────────
# synthetic panel — 40 names with a clean cross-sectional momentum gradient + a SPY col 0
# ─────────────────────────────────────────────────────────────────────────────
@pytest.fixture(scope="module")
def panel():
    """A 600-bday closes frame (SPY = column 0) + a full-coverage PIT membership table.

    Each name Ti has a distinct daily drift so the cross-section ranks cleanly under momentum; a
    little idiosyncratic noise + a couple of names engineered to be 'over-extended' (a late parabolic
    ramp) so the EntryTiming screen has something to act on differently from raw momentum, and a few
    deliberately high-vol names so the low-vol family ranks differently from momentum.
    """
    n_days, n_names = 600, 40
    idx = pd.bdate_range("2020-01-02", periods=n_days)
    rng = np.random.RandomState(7)
    cols = {}
    for i in range(n_names):
        drift = 0.0004 * (i - n_names / 2)                  # monotone momentum gradient
        vol = 0.01 + (0.02 if i % 5 == 0 else 0.0)          # some names noticeably higher-vol
        rets = drift + rng.normal(0, vol, n_days)
        # make a handful of HIGH-momentum names ALSO over-extended (a parabolic last month) so the
        # entry-timing screen down-ranks them relative to pure momentum.
        if i in (37, 38, 39):
            rets[-21:] += 0.03                              # parabolic recent ramp ⇒ extended
        cols[f"T{i:02d}"] = 100.0 * np.cumprod(1.0 + rets)
    closes = pd.DataFrame(cols, index=idx)
    spy = 100.0 * np.cumprod(1.0 + rng.normal(0.0002, 0.008, n_days))
    closes.insert(0, "SPY", spy)                            # SPY MUST be column 0 (harness benchmark)
    mem = pd.DataFrame({
        "ticker": [f"T{i:02d}" for i in range(n_names)],
        "start_date": [pd.Timestamp("2019-01-01")] * n_names,
        "end_date": [pd.NaT] * n_names,
    })
    return closes, mem


def _all_levers(mem):
    """Instantiate the full (family × lever) roster against the supplied PIT membership table."""
    return DL.build_levers(mem)


def _by_name(mem):
    return {c.name: c for c in _all_levers(mem)}


def _reb_rows(W: pd.DataFrame) -> pd.DataFrame:
    """The DISTINCT non-zero target rows (one per rebalance epoch) of a forward-filled weight matrix."""
    nz = W[(W != 0).any(axis=1)]
    return nz[~nz.duplicated()]


def _held(W: pd.DataFrame) -> set:
    """The set of names held in the most recent non-zero target row."""
    last = W[(W != 0).any(axis=1)].iloc[-1]
    return set(W.columns[last != 0])


# ─────────────────────────────────────────────────────────────────────────────
# roster shape: 5 families × 4 levers = 20 distinct candidates
# ─────────────────────────────────────────────────────────────────────────────
def test_roster_is_families_times_levers(panel):
    _closes, mem = panel
    cands = _all_levers(mem)
    assert len(cands) == len(DL._FAMILIES) * len(DL._LEVERS) == 20
    # every (family, lever) pair is present exactly once, with a unique spec_hash
    pairs = {(c.family, c.lever) for c in cands}
    assert len(pairs) == 20
    assert len({c.spec_hash for c in cands}) == 20, "candidates must have distinct spec hashes"
    fams = {c.family for c in cands}
    assert fams == {f[0] for f in DL._FAMILIES}


# ─────────────────────────────────────────────────────────────────────────────
# every lever materializes a VALID weight matrix
# ─────────────────────────────────────────────────────────────────────────────
def test_each_lever_materializes_valid_weights(panel):
    closes, mem = panel
    for c in _all_levers(mem):
        W = c.materialize(closes)
        assert list(W.columns) == list(closes.columns)
        assert (W.index == closes.index).all()
        assert "SPY" in W.columns and float(W["SPY"].abs().sum()) == 0.0   # SPY never held
        rows = _reb_rows(W)
        assert len(rows) >= 1, f"{c.name} produced no positions on this panel"
        for _, row in rows.iterrows():
            assert abs(float(row.sum()) - 1.0) < 1e-9, f"{c.name} weights don't sum to ~1.0"
            assert (row >= -1e-12).all(), f"{c.name} is long-only here, no negative weights"


def test_no_look_ahead_weights_only_change_on_rebalance(panel):
    """Weights are monthly targets forward-filled to the daily index → they only ever CHANGE on a
    rebalance date, and are zero until enough history exists (no look-ahead, no peeking forward)."""
    from loop import factor_experiment as fe
    closes, mem = panel
    base = _by_name(mem)[DL._cand_name("mom", DL._BASE)]
    W = base.materialize(closes)
    idx = closes.index
    need = max(base.spec["lookback"] + base.spec["skip"] + 1, base.spec["vol_win"] + 1)
    # before `need` bars of history exist there can be no position (insufficient lookback)
    assert float(W.iloc[:need].abs().to_numpy().sum()) == 0.0
    # every day where the weight row differs from the previous day must be a month-end rebalance date
    rebs = set(fe._rebalance_dates(idx))
    changed = W.ne(W.shift(1)).any(axis=1)
    changed.iloc[0] = False
    for t in idx[changed]:
        assert t in rebs, f"weights changed off a rebalance date at {t.date()}"


# ─────────────────────────────────────────────────────────────────────────────
# L2 concentration: WITHIN EACH FAMILY, holds FEWER names than that family's baseline
# ─────────────────────────────────────────────────────────────────────────────
def test_concentration_holds_fewer_names_than_baseline_per_family(panel):
    closes, mem = panel
    cands = _by_name(mem)
    for family, _kind, _label in DL._FAMILIES:
        base = cands[DL._cand_name(family, DL._BASE)]
        conc = cands[DL._cand_name(family, DL._L2)]
        n_base = len(_held(base.materialize(closes)))
        n_conc = len(_held(conc.materialize(closes)))
        assert n_conc < n_base, f"[{family}] L2 should hold fewer names: conc={n_conc} base={n_base}"
        assert n_conc >= 1 and n_base >= 1


# ─────────────────────────────────────────────────────────────────────────────
# L3 entry-timing: a DIFFERENT selection from that family's baseline (the screen bites)
# ─────────────────────────────────────────────────────────────────────────────
def test_entry_timing_selection_differs_from_baseline_per_family(panel):
    closes, mem = panel
    cands = _by_name(mem)
    differing = 0
    for family, _kind, _label in DL._FAMILIES:
        base = cands[DL._cand_name(family, DL._BASE)]
        timing = cands[DL._cand_name(family, DL._L3)]
        held_b = _held(base.materialize(closes))
        held_t = _held(timing.materialize(closes))
        assert held_b and held_t
        if held_b != held_t:
            differing += 1
    # the entry-timing screen must bite for at least the momentum family (it's tuned to its extended
    # names); across all families it should change the book in the clear majority.
    mom_base = cands[DL._cand_name("mom", DL._BASE)]
    mom_l3 = cands[DL._cand_name("mom", DL._L3)]
    assert _held(mom_base.materialize(closes)) != _held(mom_l3.materialize(closes)), \
        "entry-timing screen produced the SAME book as raw momentum"
    assert differing >= 3, f"L3 only changed {differing}/{len(DL._FAMILIES)} family books"


def test_entry_timing_signal_is_a_blended_rank(panel):
    """The L3 _signal is the average of cross-sectional pct-ranks → bounded in [0,1] (it never just
    re-emits the raw family signal)."""
    closes, mem = panel
    timing = _by_name(mem)[DL._cand_name("mom", DL._L3)]
    hist = closes[[c for c in closes.columns if c != "SPY"]]
    s = timing._signal(hist, hist.index[-1]).dropna()
    assert len(s) >= 10
    assert (s >= 0).all() and (s <= 1).all()


# ─────────────────────────────────────────────────────────────────────────────
# the baseline families are genuinely DISTINCT signals (the whole point of the broadening)
# ─────────────────────────────────────────────────────────────────────────────
def test_baseline_families_are_economically_distinct(panel):
    """Each family baseline ranks the cross-section differently. We assert the held BOOKS differ
    between the de-correlated families — in particular reversal (long recent losers) must NOT match
    momentum (long recent winners), and the composite must differ from any single family."""
    closes, mem = panel
    cands = _by_name(mem)
    books = {fam: _held(cands[DL._cand_name(fam, DL._BASE)].materialize(closes))
             for fam, _k, _l in DL._FAMILIES}
    # reversal is ~anti-momentum → essentially disjoint top deciles on a monotone-drift panel
    assert books["reversal"] != books["mom"]
    assert len(books["reversal"] & books["mom"]) <= 1, \
        "reversal and momentum should pick near-opposite names"
    # composite is a blend → not identical to any single constituent family
    for fam in ("mom", "lowvol", "quality", "reversal"):
        assert books["composite"] != books[fam], \
            f"composite book is identical to the {fam} family"
    # at least 3 distinct books among the 5 families (real de-correlation, not 5 copies)
    assert len({frozenset(b) for b in books.values()}) >= 3


def test_composite_signal_is_a_blended_rank(panel):
    """The composite family _signal is the average of cross-sectional pct-ranks → bounded in [0,1]."""
    closes, mem = panel
    comp = _by_name(mem)[DL._cand_name("composite", DL._BASE)]
    hist = closes[[c for c in closes.columns if c != "SPY"]]
    s = comp._signal(hist, hist.index[-1]).dropna()
    assert len(s) >= 10
    assert (s >= 0).all() and (s <= 1).all()


# ─────────────────────────────────────────────────────────────────────────────
# comparison helpers: per-family + cross-family reads behave sensibly on synthetic rows
# ─────────────────────────────────────────────────────────────────────────────
def test_cross_family_read_flags_momentum_only_artifact():
    """If a lever only improves+survives in the momentum family, the cross-family read must flag it
    as a momentum-only artifact rather than a general desk lever."""
    rows = {}
    for fam, _k, _l in DL._FAMILIES:
        base_dsr = 1.0
        rows[DL._cand_name(fam, DL._BASE)] = {"dsr": base_dsr, "sharpe": 1.0, "arm_a_pass": True}
        # L3 beats baseline + survives ONLY in mom; ties/loses elsewhere
        improved = fam == "mom"
        rows[DL._cand_name(fam, DL._L3)] = {
            "dsr": base_dsr + (0.3 if improved else -0.1), "sharpe": 1.0,
            "arm_a_pass": improved,
        }
        rows[DL._cand_name(fam, DL._L2)] = {"dsr": base_dsr, "sharpe": 1.0, "arm_a_pass": False}
        rows[DL._cand_name(fam, DL._COMBO)] = {"dsr": base_dsr, "sharpe": 1.0, "arm_a_pass": False}
    per_family = DL._per_family_summary(rows)
    cfr = DL._cross_family_read(per_family)
    l3 = cfr["l3_entry_timing"]
    assert l3["survives_gauntlet_in_families"] == ["mom"]
    assert l3["momentum_only_survive"] is True
    assert "momentum-only artifact" in l3["verdict"]


def test_cross_family_read_flags_consistent_edge():
    """If a lever survives in 2+ families, the cross-family read must call it a CONSISTENT edge."""
    rows = {}
    survive_fams = {"mom", "lowvol", "quality"}
    for fam, _k, _l in DL._FAMILIES:
        rows[DL._cand_name(fam, DL._BASE)] = {"dsr": 1.0, "sharpe": 1.0, "arm_a_pass": True}
        survives = fam in survive_fams
        rows[DL._cand_name(fam, DL._L3)] = {
            "dsr": 1.0 + (0.3 if survives else -0.1), "sharpe": 1.0, "arm_a_pass": survives}
        rows[DL._cand_name(fam, DL._L2)] = {"dsr": 1.0, "sharpe": 1.0, "arm_a_pass": False}
        rows[DL._cand_name(fam, DL._COMBO)] = {"dsr": 1.0, "sharpe": 1.0, "arm_a_pass": False}
    per_family = DL._per_family_summary(rows)
    cfr = DL._cross_family_read(per_family)
    l3 = cfr["l3_entry_timing"]
    assert l3["n_survives_gauntlet"] == 3
    assert l3["momentum_only_survive"] is False
    assert "CONSISTENT cross-family edge" in l3["verdict"]


# ─────────────────────────────────────────────────────────────────────────────
# build() degrades to an honest stub (never raises) when the panel/infra is absent
# ─────────────────────────────────────────────────────────────────────────────
def test_build_never_raises_when_panel_missing(monkeypatch):
    """Force the heavy data load to fail → build() must return an honest stub, never raise."""
    import builtins
    real_import = builtins.__import__

    def _boom(name, *a, **k):
        if name == "bot" or name.startswith("bot.") or name == "loop.factor_experiment":
            raise ImportError("simulated missing panel/infra")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", _boom)
    v = DL.build(asof="2026-06-22", write=False)
    assert v["status"] == "unavailable"
    assert "error" in v or "note" in v


def test_load_missing_is_honest_stub(monkeypatch, tmp_path):
    monkeypatch.setattr(DL, "_OUT", tmp_path / "absent.json")
    v = DL.load()
    assert v["status"] == "unavailable" and "note" in v
