"""Arm A of the pre-registered desk A/B experiment — a HISTORICAL backtest of two desk LEVERS
(L2 CONCENTRATION, L3 ENTRY-TIMING) applied ACROSS several economically-distinct baseline families,
run through the project's FROZEN multiple-testing gauntlet (DSR re-deflated @ effective-N / PBO-CSCV
/ BH-FDR / locked 2022+ holdout). Spec: docs/design/desk/AB_EXPERIMENT.md §4.1.

WHY THIS WAS BROADENED (the two weaknesses this rewrite fixes)
─────────────────────────────────────────────────────────────
The first cut of Arm A had FOUR candidates that were all variants of ONE 12-1 momentum baseline
(baseline / concentrated / entry-timed / combo). Two problems followed:

  1. effective_n == 1. The four series were ~collinear (same ranking, just different frac / a tilt),
     so cluster.effective_n collapsed the whole pool to a single bet. The DSR re-deflation at
     n_trials=1 is then NOT a multiple-testing haircut at all — the headline "we deflated for the
     trials" was meaningless. A real multiple-testing-honest pool must span DE-CORRELATED signals so
     n_eff > 1 and the DSR / PBO / BH-FDR haircut bites.
  2. momentum-only artifact risk. "Does adding entry-timing (L3) help?" answered on a single
     momentum baseline cannot tell a real lever from a quirk of the momentum factor. The only honest
     way to claim L3 is a GENERAL desk lever is to show its edge survives across SEVERAL distinct
     baselines — not just one.

THE FIX: keep the lever STRUCTURE (baseline · +L2 concentration · +L3 entry-timing · +combo) but
apply it across FIVE economically-distinct baseline FAMILIES (mirroring how loop.factor_zoo builds a
diverse pool that RAISES the multiple-testing bar):

  * MOM     — 12-1 momentum decile (leadership / trend).
  * LOWVOL  — long the lowest-trailing-vol names (defensive; ~negatively correlated with momentum).
  * QUALITY — trend CONSISTENCY: long the steadiest climbers (high % up-days). A path-quality factor,
              distinct from raw return level.
  * REVERSAL— short-term (1-month) reversal: long the recent LOSERS. By construction ~anti-correlated
              with momentum — the strongest de-correlator in the pool.
  * COMPOSITE — a multi-factor blend: the average cross-sectional rank of {momentum, low-vol,
              consistency, (anti-)reversal}. The closest HONEST stand-in for "the engine's confluence
              pick" given the historical panel (see PROXY note + Goal-2 disclaimer below).

Each family is crossed with the SAME two levers:

  * BaselineLever        — the family signal at frac=0.10 (the control for that family).
  * ConcentrationLever   — L2: the SAME family signal held at frac=0.05 (≈ half the names → more
                           concentrated). Isolates the concentration effect for that family.
  * EntryTimingLever     — L3: the family signal BLENDED (equal-weight average of cross-sectional
                           pct-ranks) with a leakage-free entry-quality screen — prefer high
                           'consistency' (% up-days) and PENALIZE over-extension (price near its own
                           252d-high AND a parabolic ~1-month run). frac=0.10.
  * ComboLever (L2+L3)   — the entry-timed family signal held at frac=0.05.

→ 5 families × 4 levers = 20 candidates. The verdict makes the KEY comparison explicit PER FAMILY
(does L3 and/or L2 improve risk-adjusted return, and does that improvement survive the gauntlet?) and
adds a CROSS-FAMILY read: is L3's edge consistent across families, or a momentum-only artifact?

HONEST FRAMING — STILL A PRICE-FACTOR PROXY for the desk levers, NOT the levers themselves.
The desk's real L2/L3 act on the Conviction Index and on stockdata entry technicals (conviction.ext,
ladder.urgency, alpha.rs, eq_grade) — none of which exist in the historical panel, which has only
single-name closes + PIT membership. The COMPOSITE family is the closest honest stand-in for the
engine's confluence pick. We did NOT fabricate a confluence panel — see the Goal-2 investigation:

GOAL-2 (engine-confluence baseline) — NOT AVAILABLE as a historical panel.
The engine's real confluence / Conviction Index is computed on-the-fly for the live book and is NOT
persisted as a leakage-free historical cross-sectional time series. The only persisted confluence
record is data/signal_history/signals.jsonl — a FORWARD decision log that (a) covers only a couple
of recent 2026 dates, (b) records only the names that were in that day's candidate pool (NOT the full
S&P-1500 cross-section needed to rank), and (c) is computed from snapshot lens/regime/universe files
that are overwritten in place (past values cannot be reconstructed). It cannot back-fill a 2002→2026
cross-sectional baseline. Rather than fake one, this arm relies on the multi-factor COMPOSITE family
as the closest honest stand-in, and states the limitation in the verdict json's proxy_disclaimer.

Every lever SUBCLASSES the audited loop.factor_experiment.FactorCandidate (like factor_zoo's
ZooFactor), so it inherits the no-look-ahead / PIT-membership-as-of-t / $5-floor / NaN-free-lookback
/ monthly-rebalance / long-top-frac / SPY-col-0-benchmark hygiene — the levers ride that base, they
do NOT reinvent it. Every entry-timing / composite signal is computed from history THROUGH t ONLY
(leakage-free).

The whole pool is judged by the SAME frozen modules the engine backtest / factor zoo use
(loop.harness.score, loop.cluster.effective_n, loop.pbo.cscv, engine.validation) — the judge is
untouched. A wider, de-correlated pool RAISES the bar (DSR re-deflated at effective-N over the whole
pool, PBO + BH-FDR across the family); it does not lower it.

Per §4.1, a lever "passes Arm A" iff: DSR > 0, PBO < 0.5, survives BH-FDR at q=0.10 across the
candidate set, AND holds in the locked 2022+ holdout (one-shot, no peeking). The verdict json makes
the COMPARISON explicit (each lever's Sharpe/DSR/PBO-pass/FDR-survival/holdout-Sharpe + a delta vs
its OWN family baseline + a one-line verdict), plus a per-family lever summary and a cross-family
L3/L2 consistency read.

Heavy + holdout-burning → on-demand only (scripts/run_desk_levers.py); the dashboard reads the
persisted data/backtest/desk_levers.json. Never raises (honest stub on missing data/infra), mirroring
loop/engine_backtest.py.
"""
from __future__ import annotations

import json
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_OUT = _ROOT / "data" / "backtest" / "desk_levers.json"

# Pre-registered fracs (§4.1): baseline/timing long-decile (0.10); concentration/combo (0.05).
_FRAC_BASE = 0.10
_FRAC_CONC = 0.05

# The economically-distinct baseline families. Each is (family_key, kind, label). `kind` is consumed
# by the family `_signal` below; the COMPOSITE is special-cased (blends the other four).
_FAMILIES = [
    ("mom",      "momentum",    "12-1 momentum decile (leadership/trend)"),
    ("lowvol",   "lowvol",      "low trailing-vol (defensive)"),
    ("quality",  "consistency", "trend consistency (steadiest climbers)"),
    ("reversal", "reversal",    "1-month short-term reversal (recent losers)"),
    ("composite", "composite",  "multi-factor blend (closest honest stand-in for the confluence pick)"),
]
# family_key → signal `kind` (the family key is the human label; the kind drives the _signal switch).
_FAMILY_KIND = {f[0]: f[1] for f in _FAMILIES}
# The four lever variants applied to every family.
_BASE, _L2, _L3, _COMBO = "baseline", "l2_concentration", "l3_entry_timing", "combo_l2_l3"
_LEVERS = [_BASE, _L2, _L3, _COMBO]


def _cand_name(family: str, lever: str) -> str:
    return f"{family}__{lever}"


# ─────────────────────────────────────────────────────────────────────────────
# the desk-lever candidate set — subclasses the audited FactorCandidate (reuses materialize)
# ─────────────────────────────────────────────────────────────────────────────
def _lever_classes():
    """Import the audited base lazily and return (DeskLever, build_candidates, fe). Raises if the
    base/numpy/pandas are unavailable — callers wrap this in the build() try/except so the module
    never raises to the user.

    DeskLever is a SINGLE FactorCandidate subclass parameterised by (family, lever). It owns:
      * the per-family BASE signal (`_family_signal`) — momentum / lowvol / consistency / reversal /
        composite, all leakage-free (history through t only);
      * the L3 entry-timing TRANSFORM (`_entry_timed`) — blend the family base with the entry-quality
        screen (consistency + non-extension), as the average of cross-sectional pct-ranks;
      * the L2 concentration is just `frac` (0.05 vs 0.10), handled entirely by the base materialize.
    """
    import numpy as np
    import pandas as pd
    from loop import factor_experiment as fe

    class DeskLever(fe.FactorCandidate):
        """One desk-lever candidate = (baseline FAMILY) × (lever). Higher score = more attractive
        (we long the top `frac`). Inherits all of FactorCandidate.materialize's hygiene (PIT-as-of-t,
        $5 floor, NaN-free lookback, monthly rebalance, no look-ahead, SPY col 0 = benchmark)."""

        def __init__(self, family, lever, frac):
            self.family = family
            self.kind = _FAMILY_KIND[family]
            self.lever = lever
            # Generous, family-independent lookback windows so materialize requires the SAME history
            # for every family (need = max(lookback+skip+1, vol_win+1)). This keeps the eligible
            # cross-section ALIGNED across families — a fair, apples-to-apples comparison — and means
            # the rebalance dates coincide (so cluster.effective_n correlates like-for-like windows).
            super().__init__(_cand_name(family, lever), "momentum", lookback=252, skip=21,
                             side="long", frac=float(frac), vol_win=120, mem_df=None)
            # tag the spec so de-dup / fingerprint sees a distinct candidate per (family, lever)
            self.spec["family"] = family
            self.spec["lever"] = lever
            import hashlib as _h
            self.spec_hash = _h.sha256(json.dumps(self.spec, sort_keys=True).encode()).hexdigest()

        # ── per-family BASELINE signal (history through t only) ──────────────────────────────────
        def _family_signal(self, hist, t, kind=None):
            """Compute the baseline signal for a given `kind` (defaults to this candidate's own
            family kind). Higher = more attractive (we long the top). All windows clamp to available
            history → no IndexError, no look-ahead (data through t only)."""
            kind = kind or self.kind
            px = hist
            names = px.columns
            rets = px.pct_change()
            lb = self.spec["lookback"]
            if kind == "momentum":
                # 12-1 momentum: return from t-252 to t-21 (reuse the audited base momentum signal)
                return super()._signal(hist, t)
            if kind == "lowvol":
                # long the LOWEST trailing vol → negate vol so high score = low vol. Clamp the window.
                vw = self.spec["vol_win"]
                win = rets.iloc[-min(vw, len(rets) - 1):]
                return -win.std()
            if kind == "consistency":
                # long the STEADIEST climbers: fraction of up-days over the lookback (path quality).
                win = rets.iloc[-min(lb, len(rets) - 1):]
                return (win > 0).mean()
            if kind == "reversal":
                # 1-month short-term reversal: long recent LOSERS → negate the last ~21-day return.
                k = min(22, len(px))
                return -(px.iloc[-1] / px.iloc[-k] - 1.0)
            if kind == "composite":
                # multi-factor blend: equal-weight average of the cross-sectional pct-ranks of the
                # four distinct sub-signals. The closest honest stand-in for the confluence pick.
                ranks = []
                for k in ("momentum", "lowvol", "consistency", "reversal"):
                    s = self._family_signal(hist, t, kind=k)
                    s = s.reindex(names)
                    s = s[np.isfinite(s)]
                    if len(s) >= 10:
                        ranks.append(s.rank(pct=True))
                if not ranks:
                    return pd.Series(np.nan, index=names)
                return pd.concat(ranks, axis=1).mean(axis=1).reindex(names)
            raise ValueError(kind)

        # ── L3 ENTRY-TIMING transform: blend the family base with an entry-quality screen ─────────
        def _entry_timed(self, hist, t):
            """The family BASE signal blended (avg of cross-sectional pct-ranks) with a leakage-free
            entry-quality screen — prefer high 'consistency' (% up-days) and NOT over-extended:

              1. family base signal       (rank high = the family's own attractiveness)
              2. consistency = % up-days   (rank high = steady climber, not choppy)
              3. NON-extension preference  = - rank(extension), where
                 extension = (price / trailing-252d-high)  *  max(0, ~21d_return)
                 → high only when BOTH near the 252w-high AND ripping in the last month (parabolic),
                   so ranking its NEGATION up-weights the un-stretched names.

            All windows clamp to available history (no IndexError; no look-ahead)."""
            px = hist
            names = px.columns
            rets = px.pct_change()
            lb = self.spec["lookback"]
            base = self._family_signal(hist, t)                          # 1) family base
            win = rets.iloc[-min(lb, len(rets) - 1):]
            consistency = (win > 0).mean()                               # 2) % up-days
            hi = px.iloc[-min(252, len(px)):].max()
            prox = px.iloc[-1] / hi                                      # ∈ (0,1]; near 1 ⇒ at its high
            r21 = px.iloc[-1] / px.iloc[-min(22, len(px))] - 1.0         # ~1-month return
            extension = prox * r21.clip(lower=0.0)                       # only up-moves count
            non_extension = -extension                                   # high ⇒ NOT extended  3)
            ranks = []
            for s in (base, consistency, non_extension):
                s = s.reindex(names)
                s = s[np.isfinite(s)]
                if len(s) >= 10:
                    ranks.append(s.rank(pct=True))
            if not ranks:
                return pd.Series(np.nan, index=names)
            return pd.concat(ranks, axis=1).mean(axis=1).reindex(names)

        def _signal(self, hist, t):
            # L3 / combo apply the entry-timing transform; baseline / L2 use the family base directly.
            if self.lever in (_L3, _COMBO):
                return self._entry_timed(hist, t)
            return self._family_signal(hist, t)

    def build_candidates(mem):
        """The full (family × lever) roster — 5 families × 4 levers = 20 candidates. L2/combo run at
        frac=0.05 (concentrated), baseline/L3 at frac=0.10. mem is attached so eligibility uses PIT
        membership as-of t."""
        cands = []
        for family, _kind, _label in _FAMILIES:
            for lever in _LEVERS:
                frac = _FRAC_CONC if lever in (_L2, _COMBO) else _FRAC_BASE
                c = DeskLever(family, lever, frac)
                c._mem = mem
                cands.append(c)
        return cands

    return DeskLever, build_candidates, fe


def build_levers(mem):
    """The full pre-registered desk-lever candidate roster (§4.1, broadened): every economically-
    distinct baseline family crossed with {baseline, L2 concentration, L3 entry-timing, combo}."""
    _DeskLever, build_candidates, _ = _lever_classes()
    return build_candidates(mem)


# ─────────────────────────────────────────────────────────────────────────────
# driver — score every lever through the frozen gauntlet, make the comparison explicit
# ─────────────────────────────────────────────────────────────────────────────
def _lever_row(c, m, fdr_h, dsr_pos, pbo_ok, fdr_rej, passes_is, holdout_sharpe, holdout_holds):
    return {
        "name": c.name,
        "family": c.family,
        "lever": c.lever,
        "frac": float(c.spec["frac"]),
        "sharpe": round(float(m["sharpe"]), 3),
        "dsr": round(float(m["dsr"]), 3),
        "dsr_verdict": m["dsr_verdict"],
        "dsr_positive": bool(dsr_pos),
        "cagr": round(float(m["cagr"]), 2),
        "maxdd": round(float(m["maxdd"]), 2),
        "beats_spy": bool(m["beats_spy"]),
        "beats_6040": bool(m["beats_6040"]),
        "crisis_pass": bool(m["crisis_pass"]),
        "fold_robust": bool(m["fold_robust"]),
        "p_value": round(float(m["p_value"]), 4),
        "fdr_q": round(float(fdr_h["q"]), 4) if fdr_h.get("q") is not None else None,
        "fdr_survive": bool(fdr_rej),
        "pbo_pass": bool(pbo_ok),
        "in_sample_pass": bool(passes_is),
        "holdout_sharpe": round(float(holdout_sharpe), 3) if holdout_sharpe is not None else None,
        "holdout_holds": holdout_holds,
    }


def build(asof: str | None = None, write: bool = True) -> dict:
    """Score the desk-lever roster (5 baseline families × 4 levers) through the frozen multiple-
    testing gauntlet and return (and optionally persist) a verdict that makes the lever-vs-baseline
    COMPARISON explicit — per family AND across families.

    Gauntlet (identical calls to loop.factor_zoo / loop.engine_backtest, on the frozen modules):
      1. loop.harness.score      — in-sample Sharpe / net-return series / 6040 + crisis + fold gates
      2. loop.cluster.effective_n — re-deflate DSR at the effective trial count over the WHOLE pool
      3. engine.validation.deflated_sharpe / dsr_verdict / ret_moments
      4. loop.pbo.cscv           — PBO / CSCV over the whole pool
      5. engine.validation.benjamini_hochberg — BH-FDR at q=0.10 across the candidate set
      6. loop.harness.score on the LOCKED 2022+ holdout (one-shot) for the in-sample passers

    Pre-registered Arm-A pass (§4.1): DSR > 0 AND PBO < 0.5 AND survives BH-FDR q=0.10 AND holds in
    the 2022+ holdout. Heavy + holdout-burning. Never raises (honest stub on missing data/infra).
    """
    try:
        import bot  # noqa: F401 — vendor/macro on sys.path before importing loop/engine deps
        from loop import factor_experiment as fe, harness, cluster, pbo
        from engine import validation as V

        closes, mem, idx, hyg = fe.load_panel()
        bill = fe.bill_yield()
        n_names = len([c for c in closes.columns if c != "SPY"])
        insample = closes[closes.index < fe.HOLDOUT_START]
        holdout = closes[closes.index >= fe.HOLDOUT_START]
        s6040 = harness.sixty_forty_sharpe(insample, bill)

        # 1) score every lever IN-SAMPLE through the frozen harness (n_eff=1; re-deflated below)
        cands = build_levers(mem)
        pool, base = {}, {}
        for c in cands:
            m = harness.score(c, insample, bill, n_eff=1, sixty40_sharpe=s6040)
            if m is None:
                continue
            pool[c.spec_hash] = m["net"]
            base[c.spec_hash] = (c, m)
        if not base:
            return _stub(asof, "no desk-lever candidate scored (degenerate panel)")

        # 2) effective-N over the WHOLE pool → re-deflate every DSR for the real trial count
        n_eff = cluster.effective_n(pool, rho=fe.RHO)
        for h, (c, m) in base.items():
            mo = V.ret_moments(m["net"])
            if mo:
                sr_d, skew, kurt, T = mo
                d = V.deflated_sharpe(sr_d, skew, kurt, T, n_trials=max(1, n_eff),
                                      trading_year=252) or {"dsr": 0.0}
                m["dsr"], m["dsr_verdict"] = float(d["dsr"]), V.dsr_verdict(d["dsr"])

        # 2b) cluster labels → which families actually de-correlate (audit for the n_eff claim)
        labels = cluster.assign_label(pool, rho=fe.RHO)
        clusters_by_label = {}
        for h, (c, _m) in base.items():
            clusters_by_label.setdefault(labels.get(h), []).append(c.name)

        # 3) PBO / CSCV over the whole pool
        pbo_res = pbo.cscv(pool, S=fe.PBO_S)
        pbo_val = pbo_res.get("pbo")
        pbo_ok = (pbo_val is None or pbo_val < 0.50)

        # 4) BH-FDR across the family of levers (q=0.10)
        fdr = V.benjamini_hochberg({h: base[h][1]["p_value"] for h in base}, alpha=fe.BH_ALPHA)
        fdr_survivors = sum(1 for h in fdr if fdr[h]["reject"])

        # 5) per-lever §4.1 Arm-A gate (DSR>0 AND PBO<0.5 AND BH-FDR survive) → one-shot holdout
        rows_by_name = {}
        for h, (c, m) in base.items():
            dsr_pos = float(m["dsr"]) > 0.0
            fdr_rej = bool(fdr.get(h, {}).get("reject"))
            passes_is = bool(dsr_pos and pbo_ok and fdr_rej)
            ho_sharpe, holdout_holds = None, None
            if passes_is:
                mh = harness.score(c, holdout, bill, n_eff=1, sixty40_sharpe=s6040)
                ho_sharpe = float(mh["sharpe"]) if mh else 0.0
                # "holds in the 2022+ holdout": positive out-of-sample Sharpe AND retains ≥ half the
                # in-sample edge (the same confirmation predicate factor_zoo / engine_backtest use).
                holdout_holds = bool(ho_sharpe > 0 and ho_sharpe >= 0.5 * float(m["sharpe"]))
            rows_by_name[c.name] = _lever_row(c, m, fdr.get(h, {}), dsr_pos, pbo_ok,
                                              fdr_rej, passes_is, ho_sharpe, holdout_holds)

        # 6) Arm-A pass + explicit delta vs the lever's OWN family baseline
        levers = []
        for c in cands:
            if c.name not in rows_by_name:
                continue
            r = rows_by_name[c.name]
            arm_a_pass = bool(r["dsr_positive"] and r["pbo_pass"] and r["fdr_survive"]
                              and r.get("holdout_holds") is True)
            is_baseline = (c.lever == _BASE)
            r["is_baseline"] = is_baseline
            r["arm_a_pass"] = arm_a_pass
            # delta vs the SAME family's baseline (None for the baseline itself / missing metrics)
            fam_base = rows_by_name.get(_cand_name(c.family, _BASE), {})
            b_sharpe, b_dsr, b_ho = fam_base.get("sharpe"), fam_base.get("dsr"), fam_base.get("holdout_sharpe")
            r["delta_vs_baseline"] = None if is_baseline else {
                "sharpe": (round(r["sharpe"] - b_sharpe, 3) if b_sharpe is not None else None),
                "dsr": (round(r["dsr"] - b_dsr, 3) if b_dsr is not None else None),
                "holdout_sharpe": (round(r["holdout_sharpe"] - b_ho, 3)
                                   if (r["holdout_sharpe"] is not None and b_ho is not None)
                                   else None),
            }
            r["verdict_line"] = _verdict_line(r, arm_a_pass)
            levers.append(r)

        # stable, readable order: family order, then baseline → L2 → L3 → combo within each family
        fam_order = {f[0]: i for i, f in enumerate(_FAMILIES)}
        lev_order = {lv: i for i, lv in enumerate(_LEVERS)}
        levers.sort(key=lambda x: (fam_order.get(x["family"], 99), lev_order.get(x["lever"], 99)))

        # 7) per-family lever summary + cross-family L3/L2 consistency read (the surfaced comparison)
        per_family = _per_family_summary(rows_by_name)
        cross_family = _cross_family_read(per_family)

        best = max(base.values(), key=lambda cm: cm[1]["dsr"])[1]
        # pool fingerprint — the holdout is peeked once PER POOL; a changed pool burns a fresh one.
        import hashlib
        pool_fp = hashlib.sha256(
            ("|".join(sorted(c.name for c, _ in base.values()))).encode()).hexdigest()[:16]
        verdict = {
            "as_of": str(asof or "")[:10] or None,
            "status": "ok",
            "experiment": "AB_EXPERIMENT §4.1 Arm A (historical backtest of desk levers L2/L3, "
                          "broadened across economically-distinct baseline families)",
            "proxy_disclaimer": (
                "PRICE-FACTOR PROXY for the desk levers — the historical panel has only single-name "
                "closes + PIT membership, NOT the Conviction Index or the stockdata entry technicals "
                "the live desk acts on. The levers are tested across FIVE economically-distinct "
                "baseline families (12-1 momentum, low-vol, trend-consistency/quality, 1-month "
                "reversal, and a multi-factor composite) so the multiple-testing deflation is honest "
                "(effective_n > 1) and L3's edge can be checked for cross-family consistency rather "
                "than a momentum-only artifact. GOAL-2 (engine-confluence baseline): NOT AVAILABLE as "
                "a historical panel — the engine's real confluence / Conviction Index is computed "
                "on-the-fly for the live book and is NOT persisted as a leakage-free historical "
                "cross-sectional time series. The only confluence record is data/signal_history/"
                "signals.jsonl, a forward decision log covering only a couple of 2026 dates, only the "
                "names in each day's candidate pool (not the full cross-section), built from snapshot "
                "files overwritten in place — it cannot back-fill a 2002→2026 baseline. Rather than "
                "fabricate one, the multi-factor COMPOSITE family is the closest honest stand-in for "
                "the confluence pick. This arm can CLEAR or KILL the factorable levers; it cannot "
                "justify the LLM seat build (see §5/§7)."),
            "pool_fingerprint": pool_fp,
            "holdout_burned": True,
            "arm_a_bar": ("a lever passes iff DSR>0 AND PBO<0.5 AND survives BH-FDR q=0.10 AND holds "
                          "in the locked 2022+ holdout (one-shot, no peeking)"),
            "methodology": {
                "harness": "loop.harness (frozen: cost_bps=3.0, embargo=63d, 5-fold purged CV, 3 crises)",
                "tests": ["DSR re-deflated @ effective-N (whole pool)", "PBO/CSCV", "BH-FDR q=0.10",
                          "fold-robust", "one-shot 2022+ holdout"],
                "universe": "S&P 1500 PIT membership (survivorship-safe; deep + delisted)",
                "in_sample": f"{insample.index[0].date()}..{insample.index[-1].date()}",
                "holdout": f"{holdout.index[0].date()}..{holdout.index[-1].date()}",
                "design": ("5 economically-distinct baseline FAMILIES × 4 levers {baseline, L2 "
                           "concentration frac=0.05, L3 entry-timing, combo} = 20 candidates; a wider, "
                           "DE-CORRELATED pool raises the multiple-testing bar (effective_n > 1)"),
                "families": {f[0]: f[2] for f in _FAMILIES},
                "levers": {
                    "baseline": "family signal, frac=0.10 (the per-family control)",
                    "l2_concentration": "SAME family signal, frac=0.05 (L2 concentration proxy)",
                    "l3_entry_timing": ("family signal blended (avg pct-rank) with an entry-quality "
                                        "screen {consistency + non-extension}, frac=0.10 (L3 proxy)"),
                    "combo_l2_l3": "entry-timed family signal, frac=0.05 (L2+L3 proxy)",
                },
                "holdout_discipline": ("one-shot per pool_fingerprint; re-running with a DIFFERENT "
                                       "fingerprint burns a fresh holdout (statistical discipline, "
                                       "not code-enforced — run deliberately)"),
            },
            "panel": {"n_names": n_names, "n_dates": len(idx),
                      "span": f"{idx[0].date()}..{idx[-1].date()}",
                      "hygiene_clamped": int(hyg.get("n_clamped", 0))},
            "verdict": {
                "n_candidates": len(base), "effective_n": int(n_eff),
                "n_families": len(_FAMILIES), "n_levers_per_family": len(_LEVERS),
                "clusters": clusters_by_label,
                "effective_n_note": ("with 5 de-correlated baseline families the pool no longer "
                                     "collapses to one bet (the original momentum-only pool gave "
                                     "effective_n=1) — the DSR/PBO/BH-FDR haircut is now honest"),
                "pbo": round(float(pbo_val), 3) if pbo_val is not None else None, "pbo_pass": pbo_ok,
                "best_sharpe": round(float(best["sharpe"]), 3),
                "best_dsr": round(float(best["dsr"]), 3), "best_dsr_verdict": best["dsr_verdict"],
                "fdr_survivors": fdr_survivors,
                "arm_a_passers": [r["name"] for r in levers if r["arm_a_pass"]],
                "per_family": per_family,
                "cross_family_read": cross_family,
            },
            "levers": levers,
            "honest_read": ("A wider, DE-CORRELATED pool RAISES the bar (DSR re-deflated at the "
                            "effective-N over the whole pool, PBO + BH-FDR across the family) and lets "
                            "us test whether L3's (and L2's) edge is CONSISTENT across baselines or a "
                            "momentum-only artifact. These are PRICE-FACTOR PROXIES for the desk "
                            "levers, not the levers themselves, and the COMPOSITE family is the "
                            "closest honest stand-in for the confluence pick (no historical engine "
                            "confluence panel exists — see proxy_disclaimer). Arm A can clear or kill "
                            "the factorable L2/L3, but per §5/§7 it cannot justify the LLM seat build "
                            "(that needs the forward H4/H5 increments from Arm B). The 2022+ holdout "
                            "is the out-of-sample verdict; forward paper grading remains the ultimate "
                            "judge."),
        }
        if write:
            try:
                _OUT.parent.mkdir(parents=True, exist_ok=True)
                _OUT.write_text(json.dumps(verdict, indent=2, default=str))
            except Exception:  # noqa: BLE001
                pass
        return verdict
    except Exception as exc:  # noqa: BLE001
        return _stub(asof, str(exc)[:200])


# ─────────────────────────────────────────────────────────────────────────────
# comparison helpers — surface the lever-vs-baseline read per family and across families
# ─────────────────────────────────────────────────────────────────────────────
def _improves(row: dict, base_row: dict) -> bool | None:
    """Does this lever improve RISK-ADJUSTED return vs its own family baseline? We use DSR (the
    multiple-testing-deflated Sharpe) as the risk-adjusted yardstick. None if either is missing."""
    if not row or not base_row:
        return None
    rd, bd = row.get("dsr"), base_row.get("dsr")
    if rd is None or bd is None:
        return None
    return bool(rd > bd)


def _per_family_summary(rows_by_name: dict) -> dict:
    """For each family: the baseline metrics and whether L2 / L3 / combo improve risk-adjusted return
    (DSR) AND whether the improved lever passes the full Arm-A gauntlet for that family."""
    out = {}
    for family, _kind, label in _FAMILIES:
        b = rows_by_name.get(_cand_name(family, _BASE))
        if not b:
            continue
        fam = {"label": label,
               "baseline": {"sharpe": b.get("sharpe"), "dsr": b.get("dsr"),
                            "arm_a_pass": b.get("arm_a_pass")}}
        for lv, key in ((_L2, "l2"), (_L3, "l3"), (_COMBO, "combo")):
            r = rows_by_name.get(_cand_name(family, lv))
            if not r:
                fam[key] = None
                continue
            fam[key] = {
                "sharpe": r.get("sharpe"), "dsr": r.get("dsr"),
                "delta_dsr": (round(r["dsr"] - b["dsr"], 3)
                              if (r.get("dsr") is not None and b.get("dsr") is not None) else None),
                "improves_risk_adj": _improves(r, b),
                "arm_a_pass": r.get("arm_a_pass"),
                "survives_gauntlet": bool(r.get("arm_a_pass")),
            }
        out[family] = fam
    return out


def _cross_family_read(per_family: dict) -> dict:
    """The headline question: is L3's (and L2's) edge CONSISTENT across baseline families, or a
    momentum-only artifact? Count, across families, how often each lever (a) improves risk-adjusted
    return vs its own baseline and (b) survives the full Arm-A gauntlet."""
    def _tally(key):
        improves = [f[key]["improves_risk_adj"] for f in per_family.values()
                    if f.get(key) and f[key].get("improves_risk_adj") is not None]
        survives = [f[key]["survives_gauntlet"] for f in per_family.values()
                    if f.get(key) is not None]
        n_fam = len(survives)
        n_improve = sum(1 for x in improves if x)
        n_survive = sum(1 for x in survives if x)
        improve_families = [fam for fam, f in per_family.items()
                            if f.get(key) and f[key].get("improves_risk_adj")]
        survive_families = [fam for fam, f in per_family.items()
                            if f.get(key) and f[key].get("survives_gauntlet")]
        # "momentum-only artifact" iff it ONLY helps/survives in the mom family
        mom_only_improve = (improve_families == ["mom"]) if improve_families else False
        mom_only_survive = (survive_families == ["mom"]) if survive_families else False
        if n_fam == 0:
            verdict = "no families scored"
        elif n_survive == 0:
            verdict = f"{key.upper()} survives the gauntlet in 0/{n_fam} families — no historical edge"
        elif mom_only_survive:
            verdict = (f"{key.upper()} survives ONLY in the momentum family — likely a momentum-only "
                       f"artifact, NOT a general desk lever")
        elif n_survive >= 2:
            verdict = (f"{key.upper()} survives in {n_survive}/{n_fam} families "
                       f"({', '.join(survive_families)}) — a CONSISTENT cross-family edge")
        else:
            verdict = (f"{key.upper()} survives in {n_survive}/{n_fam} families "
                       f"({', '.join(survive_families)}) — only weakly cross-family; treat with caution")
        return {
            "n_families": n_fam,
            "improves_risk_adj_in_families": improve_families,
            "n_improves_risk_adj": n_improve,
            "survives_gauntlet_in_families": survive_families,
            "n_survives_gauntlet": n_survive,
            "momentum_only_improve": mom_only_improve,
            "momentum_only_survive": mom_only_survive,
            "verdict": verdict,
        }
    return {"l3_entry_timing": _tally("l3"), "l2_concentration": _tally("l2"), "combo_l2_l3": _tally("combo")}


def _verdict_line(r: dict, arm_a_pass: bool) -> str:
    """One-line per-lever verdict per the §4.1 bars (family-aware)."""
    fam = r.get("family", "?")
    if r.get("is_baseline"):
        tag = "PASSES Arm A" if arm_a_pass else "fails Arm A"
        return (f"[{fam}] baseline (control): {tag} — DSR={r['dsr']} PBO_pass={r['pbo_pass']} "
                f"FDR_survive={r['fdr_survive']} holdout_holds={r.get('holdout_holds')}")
    d = r.get("delta_vs_baseline") or {}
    ddsr = d.get("dsr")
    delta = f" (ΔDSR_vs_{fam}_baseline={ddsr:+.3f})" if isinstance(ddsr, (int, float)) else ""
    if arm_a_pass:
        return (f"[{fam}] PASSES Arm A vs its baseline{delta} — positive DSR, PBO<0.5, survives "
                f"BH-FDR q=0.10, and holds in the 2022+ holdout")
    # name the first failing leg, in §4.1 order
    if not r["dsr_positive"]:
        leg = "DSR not > 0"
    elif not r["pbo_pass"]:
        leg = "PBO >= 0.5 (overfit risk)"
    elif not r["fdr_survive"]:
        leg = "does not survive BH-FDR q=0.10"
    elif r.get("holdout_holds") is not True:
        leg = "does not hold in the 2022+ holdout"
    else:
        leg = "unknown"
    return f"[{fam}] fails Arm A — {leg}{delta}"


def load() -> dict:
    """Cheap read of the persisted verdict (for the API). Honest stub if none yet."""
    try:
        return json.loads(_OUT.read_text())
    except Exception:  # noqa: BLE001
        return {"status": "unavailable",
                "note": "no desk-lever run recorded yet — run scripts/run_desk_levers.py"}


def _stub(asof, reason: str) -> dict:
    return {"as_of": str(asof or "")[:10] or None, "status": "unavailable", "error": reason,
            "note": "desk-lever Arm-A backtest could not run in this environment (data/network)"}
