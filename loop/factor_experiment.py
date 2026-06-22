#!/usr/bin/env python
"""
single_name_factor_edge.py — STANDALONE EMPIRICAL EXPERIMENT (read-only, no commits).

Question: does ANY standard single-name cross-sectional factor survive the Mastermind
loop's EXISTING frozen statistical gauntlet on the survivorship-safe S&P 1500 panel?

Discipline: we DO NOT re-implement DSR / Sharpe / PBO / BH-FDR. We import the loop's
frozen modules (loop.harness, loop.cluster, loop.pbo, engine.validation) and call them
exactly as loop/iterate.py does, so the judgement is byte-identical to production. The
ONLY thing this script owns is the factor *spec* (the proposer) and the survivorship-safe
panel construction — never the judge.

Run:  python /tmp/single_name_factor_edge.py
"""
from __future__ import annotations

import os
import sys
import hashlib
import json
from datetime import datetime

import numpy as np
import pandas as pd

# --------------------------------------------------------------------------- #
# 0. wire the import path to the LIVE vendored macro stack (no edits to it)
# --------------------------------------------------------------------------- #
REPO = "/Users/chriswong/Documents/Cluade/Mastermind"
MACRO = os.path.join(REPO, "vendor", "macro")
for p in (REPO, MACRO):
    if p not in sys.path:
        sys.path.insert(0, p)

# FROZEN judge + plumbing — imported, never re-implemented
from loop import harness, cluster, pbo                       # noqa: E402
from engine import validation as V                           # noqa: E402
from engine.equity_alloc import bill_yield                   # noqa: E402
from lib import store                                        # noqa: E402

HOLDOUT_START = pd.Timestamp("2022-01-01")     # LOCKED before any scoring (mirrors iterate.py)
PANEL_START = pd.Timestamp("2002-01-01")       # match the loop's "real 2002-2026" window
RHO = 0.90                                      # effective-N clustering threshold (iterate.py)
PBO_S = 12                                       # CSCV blocks (iterate.py)
BH_ALPHA = 0.10                                  # BH-FDR level (iterate.py)
BREADTH = os.path.join(MACRO, "data", "breadth")

def log(s=""):
    print(s, flush=True)


# --------------------------------------------------------------------------- #
# 0b. DATA HYGIENE — the breadth caches carry corrupted ticks (zeros, sub-cent
# placeholders, an unadjusted 0.005->170 jump in CBE = a 33,999x one-day "return").
# Left raw, a single contaminated name in a long decile manufactures a 100,000% CAGR
# — a DATA-BUG false positive, the worst possible outcome. We rebuild a CLEAN price
# panel from CLEAN returns so the FROZEN backtest (which does closes.pct_change())
# only ever sees honest moves. This is applied UNIFORMLY & CAUSALLY to every name;
# it removes artifacts, it does NOT cherry-pick winners. (Reported in the self-audit.)
# --------------------------------------------------------------------------- #
MIN_PRICE = 1.0      # sub-$1 / non-positive ticks -> NaN (placeholder bad data)
RET_CAP = 0.60       # neutralize |daily ret| > 60% (unadjusted split / residual bad tick)
SELECT_FLOOR = 5.0   # a name must trade >= $5 at rebalance to be RANKED (penny-stock screen)


def sanitize_panel(panel: pd.DataFrame) -> pd.DataFrame:
    """Clean-return reconstruction (see module header for the why)."""
    px = panel.where((panel >= MIN_PRICE) & (panel > 0))
    out = pd.DataFrame(index=panel.index, columns=panel.columns, dtype=float)
    n_clamped = 0
    for c in px.columns:
        s = px[c]
        f = s.first_valid_index(); l = s.last_valid_index()
        if f is None:
            continue
        seg = s.loc[f:l]
        valid = seg.notna()
        prev = seg.shift(1)
        r = seg / prev - 1.0
        adjacent = valid & prev.notna()          # both ends valid & adjacent rows
        r = r.where(adjacent, 0.0)                # gap/halt day -> 0 return (flat, no teleport)
        r = r.replace([np.inf, -np.inf], 0.0)
        n_clamped += int((r.abs() > RET_CAP).sum())
        r = r.clip(-RET_CAP, RET_CAP)             # neutralize absurd jumps
        r.iloc[0] = 0.0
        p0 = float(seg.loc[seg.first_valid_index()])
        out.loc[f:l, c] = (p0 * (1.0 + r).cumprod()).values
    out.attrs["n_clamped"] = n_clamped
    return out


# --------------------------------------------------------------------------- #
# 1. survivorship-safe panel: UNION(deep, delisted), PIT membership as-of t
# --------------------------------------------------------------------------- #
def load_panel():
    deep = pd.read_parquet(os.path.join(BREADTH, "_closes_deep.parquet"))
    deli = pd.read_parquet(os.path.join(BREADTH, "_closes_delisted.parquet"))
    # disjoint columns (verified): plain horizontal union keeps delisted names with
    # their REAL final prices — dropping them would be the classic survivorship lie.
    panel = pd.concat([deep, deli], axis=1)
    panel = panel.loc[:, ~panel.columns.duplicated()]
    panel.index = pd.to_datetime(panel.index)
    panel = panel.sort_index()
    panel = panel[panel.index >= PANEL_START]
    raw_max = float(np.nanmax(np.abs(panel.pct_change().replace([np.inf, -np.inf], np.nan).values)))
    panel = sanitize_panel(panel)                 # <-- data hygiene
    n_clamped = int(panel.attrs.get("n_clamped", 0))

    spy = store.read("yahoo", "SPY")["close"].copy()
    spy.index = pd.to_datetime(spy.index)

    # align to the intersection of panel trading days and SPY trading days, >= PANEL_START
    idx = panel.index.intersection(spy.index)
    idx = idx[idx >= PANEL_START]
    panel = panel.reindex(idx)
    spy = spy.reindex(idx)

    mem = pd.read_parquet(os.path.join(BREADTH, "sp1500_pit_membership.parquet"))
    mem["start_date"] = pd.to_datetime(mem["start_date"])
    mem["end_date"] = pd.to_datetime(mem["end_date"])

    # closes for the harness: SPY MUST be column 0 (harness benchmark = closes.iloc[:,0]).
    names = [c for c in panel.columns if c != "SPY"]
    closes = pd.concat([spy.rename("SPY"), panel[names]], axis=1)
    hygiene = {"raw_max_abs_ret": raw_max, "n_clamped": n_clamped}
    return closes, mem, idx, hygiene


def members_asof(mem: pd.DataFrame, t: pd.Timestamp) -> set:
    """PIT S&P 1500 membership as-of t — checks ALL spans for each ticker
    (a ticker can have several disjoint membership intervals)."""
    active = mem[(mem["start_date"] <= t) & ((mem["end_date"].isna()) | (mem["end_date"] > t))]
    return set(active["ticker"].unique())


# --------------------------------------------------------------------------- #
# 2. Candidate-like factor strategies (same attrs harness.score expects:
#    .spec, .spec_hash, .materialize(closes)->W). The proposer — never the judge.
# --------------------------------------------------------------------------- #
def _rebalance_dates(idx: pd.DatetimeIndex) -> pd.DatetimeIndex:
    """Last trading day of each month present in idx (monthly rebalance)."""
    df = pd.Series(idx, index=idx)
    grp = df.groupby([idx.year, idx.month]).last()
    return pd.DatetimeIndex(sorted(grp.values))


class FactorCandidate:
    """A cross-sectional single-name factor. materialize() obeys:
       - rank ONLY PIT members as-of the rebalance date t
       - require a valid price at t AND a full, NaN-free lookback window  (no look-ahead)
       - signal uses ONLY data through t; the position earns t+1.. returns
         (backtest_portfolio applies its own .shift(1) — we must NOT pre-shift)
       - equal weight; long-only -> gross 1.0; long-short -> 0.5 long / 0.5 short
       - SPY column weight = 0 (it is the benchmark, never a holding)
    """
    def __init__(self, name, kind, lookback, skip=0, side="long", frac=0.10,
                 vol_win=60, mem_df=None):
        self.name = name
        self.spec = {"name": name, "kind": kind, "lookback": int(lookback),
                     "skip": int(skip), "side": side, "frac": float(frac),
                     "vol_win": int(vol_win), "rebalance": "monthly", "weight": "equal",
                     "universe": "sp1500_pit", "cost_bps": 3.0}
        self.spec_hash = hashlib.sha256(json.dumps(self.spec, sort_keys=True).encode()).hexdigest()
        self._mem = mem_df

    # ---- signal: higher = more attractive (we long the TOP of this score) ----
    def _signal(self, hist: pd.DataFrame, t) -> pd.Series:
        kind, lb, sk, vw = self.spec["kind"], self.spec["lookback"], self.spec["skip"], self.spec["vol_win"]
        px = hist  # prices through t (inclusive)
        if kind == "momentum":
            # 12-1 style: return from t-lb to t-sk (skip last `sk` days)
            p_now = px.iloc[-1 - sk] if sk > 0 else px.iloc[-1]
            p_then = px.iloc[-1 - lb]
            sig = p_now / p_then - 1.0
        elif kind == "reversal":
            # short-term reversal: LONG the worst recent performers -> negate last-lb return
            sig = -(px.iloc[-1] / px.iloc[-1 - lb] - 1.0)
        elif kind == "lowvol":
            # LONG lowest trailing vol -> negate vol so high score = low vol
            rets = px.iloc[-(vw + 1):].pct_change()
            sig = -rets.iloc[-vw:].std()
        elif kind == "high52w":
            # proximity to trailing-lb high: price / rolling-max, higher = nearer the high
            sig = px.iloc[-1] / px.iloc[-lb:].max()
        else:
            raise ValueError(kind)
        return sig

    def materialize(self, closes: pd.DataFrame) -> pd.DataFrame:
        idx = closes.index
        names = [c for c in closes.columns if c != "SPY"]
        px = closes[names]
        lb, sk, vw = self.spec["lookback"], self.spec["skip"], self.spec["vol_win"]
        need = max(lb + sk + 1, vw + 1)          # rows of history required for a valid signal
        frac, side = self.spec["frac"], self.spec["side"]

        rebs = _rebalance_dates(idx)
        rebs = rebs[rebs >= idx[need]]            # only once enough history exists

        W = pd.DataFrame(0.0, index=idx, columns=closes.columns)
        rows = {}
        for t in rebs:
            loc = idx.get_loc(t)
            hist = px.iloc[: loc + 1]             # data through t ONLY (inclusive) -> no look-ahead
            if len(hist) < need:
                continue
            # eligibility: PIT member as-of t, valid price at t, full NaN-free lookback window
            mem_set = members_asof(self._mem, t)
            window = hist.iloc[-need:]
            valid = window.notna().all(axis=0)                  # no NaN anywhere in the lookback
            last = hist.iloc[-1]
            price_ok = last.notna() & (last >= SELECT_FLOOR)    # $5 floor = penny-stock screen
            eligible = [c for c in names if c in mem_set and bool(valid.get(c, False)) and bool(price_ok.get(c, False))]
            if len(eligible) < 20:                # need a meaningful cross-section to sort
                continue
            sig = self._signal(hist[eligible], t).dropna()
            sig = sig[np.isfinite(sig)]
            if len(sig) < 20:
                continue
            n = len(sig)
            k = max(1, int(round(frac * n)))
            ranked = sig.sort_values(ascending=False)
            longs = ranked.index[:k]
            row = pd.Series(0.0, index=closes.columns)
            if side == "long":
                row[longs] = 1.0 / len(longs)                    # gross 1.0
            else:  # long-short, gross ~1.0 (0.5 long / 0.5 short)
                shorts = ranked.index[-k:]
                row[longs] = 0.5 / len(longs)
                row[shorts] = -0.5 / len(shorts)
            row["SPY"] = 0.0
            rows[t] = row
        if not rows:
            return W
        Wr = pd.DataFrame(rows).T.reindex(columns=closes.columns).sort_index()
        # forward-fill the monthly target weights across daily index (constant between rebalances)
        W = Wr.reindex(idx).ffill().fillna(0.0)
        W["SPY"] = 0.0
        return W


def build_pool(mem):
    """~6-10 economically-distinct factors, including near-duplicate lookback variants
    so the effective-N clustering and PBO actually have something to collapse/penalize."""
    C = []
    # momentum family (12-1 and lookback variants -> near-duplicates by design)
    C.append(FactorCandidate("mom_12_1_topdecile", "momentum", lookback=252, skip=21, side="long", frac=0.10, mem_df=mem))
    C.append(FactorCandidate("mom_12_1_longshort", "momentum", lookback=252, skip=21, side="ls",   frac=0.10, mem_df=mem))
    C.append(FactorCandidate("mom_6_1_topdecile",  "momentum", lookback=126, skip=21, side="long", frac=0.10, mem_df=mem))
    # short-term reversal
    C.append(FactorCandidate("reversal_1m_botdecile", "reversal", lookback=21, side="long", frac=0.10, mem_df=mem))
    # low volatility (quintile + a 120d lookback variant -> near-duplicate)
    C.append(FactorCandidate("lowvol_60d_botquintile",  "lowvol", lookback=0, side="long", frac=0.20, vol_win=60,  mem_df=mem))
    C.append(FactorCandidate("lowvol_120d_botquintile", "lowvol", lookback=0, side="long", frac=0.20, vol_win=120, mem_df=mem))
    # 52-week-high proximity (and a 126d variant)
    C.append(FactorCandidate("high52w_proximity_topdecile", "high52w", lookback=252, side="long", frac=0.10, mem_df=mem))
    C.append(FactorCandidate("high26w_proximity_topdecile", "high52w", lookback=126, side="long", frac=0.10, mem_df=mem))
    return C


# --------------------------------------------------------------------------- #
# 3. MAIN — mirror loop/iterate.py exactly, on the single-name panel
# --------------------------------------------------------------------------- #
def main():
    t0 = datetime.now()
    log("=" * 78)
    log("SINGLE-NAME CROSS-SECTIONAL FACTOR EDGE — frozen Mastermind gauntlet")
    log("=" * 78)

    closes, mem, idx, hyg = load_panel()
    bill = bill_yield()

    n_names = len([c for c in closes.columns if c != "SPY"])
    log(f"\n[PANEL]  names(incl. delisted)={n_names}  dates={len(idx)}  "
        f"span={idx[0].date()} .. {idx[-1].date()}")
    log(f"[PANEL]  benchmark col 0 = {closes.columns[0]} (SPY)  | holdout locked >= {HOLDOUT_START.date()}")
    log(f"[HYGIENE] raw max |daily ret| in panel pre-clean = {hyg['raw_max_abs_ret']:.1f}x "
        f"-> clamped {hyg['n_clamped']} cells at |ret|>{RET_CAP:.0%}; select floor=${SELECT_FLOOR:.0f}")

    insample = closes[closes.index < HOLDOUT_START]
    holdout  = closes[closes.index >= HOLDOUT_START]
    log(f"[SPLIT]  in-sample {insample.index[0].date()}..{insample.index[-1].date()} ({len(insample)}d)  "
        f"| holdout {holdout.index[0].date()}..{holdout.index[-1].date()} ({len(holdout)}d)")

    s6040 = harness.sixty_forty_sharpe(insample, bill)   # NB: panel has no bond col -> uses last col; reported, see audit
    log(f"[6040]   in-sample 60/40-proxy Sharpe hurdle = {s6040:.3f}")

    cands = build_pool(mem)
    log(f"[POOL]   {len(cands)} factor candidates")

    # ---- 1. score every candidate IN-SAMPLE through the frozen harness ----
    pool, base = {}, {}
    log("\n[IN-SAMPLE SCORING] (frozen harness.score; n_eff=1 first pass, re-deflated later)")
    for c in cands:
        m = harness.score(c, insample, bill, n_eff=1, sixty40_sharpe=s6040)
        if m is None:
            log(f"  {c.name:34s}  -> harness returned None (degenerate)")
            continue
        pool[c.spec_hash] = m["net"]
        base[c.spec_hash] = (c, m)

    # ---- 2. effective-N (near-duplicate sleeves collapse to one bet) ----
    n_eff = cluster.effective_n(pool, rho=RHO)
    log(f"\n[EFFECTIVE-N] clusters @ |rho|>={RHO}: n_eff={n_eff} (of {len(pool)} candidates)")
    labels = cluster.assign_label(pool, rho=RHO)
    by_lab = {}
    for h, lab in labels.items():
        by_lab.setdefault(lab, []).append(base[h][0].name)
    for lab, nm in sorted(by_lab.items()):
        log(f"    cluster {lab}: {nm}")

    # ---- 3. re-deflate each Sharpe with n_eff (off the stored net, like iterate.py) ----
    for h, (c, m) in base.items():
        mo = V.ret_moments(m["net"])
        if mo:
            sr_d, skew, kurt, T = mo
            dsr = V.deflated_sharpe(sr_d, skew, kurt, T, n_trials=max(1, n_eff), trading_year=252) or {"dsr": 0.0}
            m["dsr"], m["dsr_verdict"] = float(dsr["dsr"]), V.dsr_verdict(dsr["dsr"])

    # ---- 4. PBO / CSCV over the whole pool ----
    pbo_res = pbo.cscv(pool, S=PBO_S)
    pbo_val = pbo_res.get("pbo")
    log(f"\n[PBO/CSCV] S={pbo_res.get('n_blocks')}  n_splits={pbo_res.get('n_splits')}  "
        f"PBO={pbo_val}  (PBO<0.50 required to pass)")

    # ---- 5. BH-FDR across the family of candidates ----
    fdr = V.benjamini_hochberg({h: base[h][1]["p_value"] for h in base}, alpha=BH_ALPHA)
    fdr_survivors = sum(1 for h in fdr if fdr[h]["reject"])
    log(f"[BH-FDR]  alpha={BH_ALPHA}  survivors (reject null)={fdr_survivors}/{len(base)}")

    # ---- per-strategy in-sample table ----
    log("\n" + "-" * 78)
    log("PER-STRATEGY IN-SAMPLE METRICS (re-deflated DSR @ n_eff)")
    log("-" * 78)
    hdr = f"{'strategy':34s} {'Shrp':>5s} {'DSR':>5s} {'CAGR':>6s} {'maxDD':>6s} {'spy':>3s} {'604':>3s} {'cris':>4s} {'fold':>4s} {'pval':>5s} {'q':>5s} {'rej':>3s}"
    log(hdr)
    rows_sorted = sorted(base.items(), key=lambda kv: -kv[1][1]["dsr"])
    for h, (c, m) in rows_sorted:
        q = fdr.get(h, {}).get("q")
        rej = fdr.get(h, {}).get("reject")
        log(f"{c.name:34s} {m['sharpe']:5.2f} {m['dsr']:5.3f} {m['cagr']:6.1f} {m['maxdd']:6.1f} "
            f"{int(m['beats_spy']):>3d} {int(m['beats_6040']):>3d} {int(m['crisis_pass']):>4d} "
            f"{int(m['fold_robust']):>4d} {m['p_value']:5.3f} {q if q is not None else float('nan'):5.3f} {int(bool(rej)):>3d}")
    log(f"\n[verdict legend] DSR>=0.95 SURVIVES | spy=beats_spy 604=beats_6040 cris=crisis_pass fold=fold_robust rej=BH-reject")

    # ---- 6. in-sample PASS gate (the exact iterate.py conjunction) ----
    log("\n" + "-" * 78)
    log("IN-SAMPLE GATE (exact iterate.py conjunction) + HOLDOUT (touch once) for passers")
    log("-" * 78)
    any_pass = False
    for h, (c, m) in rows_sorted:
        passes_is = (m["dsr_verdict"].startswith("SURVIVES") and m["beats_spy"] and m["beats_6040"]
                     and m["crisis_pass"] and fdr[h]["reject"] and m["fold_robust"]
                     and (pbo_val is None or pbo_val < 0.50))
        if not passes_is:
            # report the first failing leg for transparency
            legs = {
                "dsr_survives": m["dsr_verdict"].startswith("SURVIVES"),
                "beats_spy": m["beats_spy"], "beats_6040": m["beats_6040"],
                "crisis_pass": m["crisis_pass"], "fdr_reject": fdr[h]["reject"],
                "fold_robust": m["fold_robust"], "pbo_ok": (pbo_val is None or pbo_val < 0.50),
            }
            first_fail = next((k for k, v in legs.items() if not v), "?")
            log(f"  {c.name:34s} IN-SAMPLE REJECT (first failing leg: {first_fail})")
            continue
        any_pass = True
        # holdout — touched ONCE per spec_hash; score on the LOCKED slice
        mh = harness.score(c, holdout, bill, n_eff=1, sixty40_sharpe=s6040)
        ho_sharpe = mh["sharpe"] if mh else 0.0
        # holdout.confirms logic: holdout Sharpe > 0 AND >= 0.5 * in-sample Sharpe
        confirms = (ho_sharpe > 0) and (ho_sharpe >= 0.5 * m["sharpe"])
        log(f"  {c.name:34s} IN-SAMPLE PASS -> holdout Sharpe={ho_sharpe:.2f} "
            f"(in-sample {m['sharpe']:.2f}) -> holdout {'CONFIRMS' if confirms else 'REJECTS'}")
    if not any_pass:
        log("  (no candidate cleared the in-sample gate; holdout never touched — correct discipline)")

    log("\n" + "=" * 78)
    log("SUMMARY")
    log("=" * 78)
    log(f"  panel names (incl. delisted) : {n_names}")
    log(f"  date span                    : {idx[0].date()} .. {idx[-1].date()} ({len(idx)} trading days)")
    log(f"  candidates                   : {len(base)}")
    log(f"  effective-N (rho={RHO})        : {n_eff}")
    log(f"  PBO (CSCV S={pbo_res.get('n_blocks')})            : {pbo_val}")
    log(f"  BH-FDR survivors (a={BH_ALPHA})    : {fdr_survivors}")
    log(f"  in-sample gate passers       : {sum(1 for h,(c,m) in base.items() if (m['dsr_verdict'].startswith('SURVIVES') and m['beats_spy'] and m['beats_6040'] and m['crisis_pass'] and fdr[h]['reject'] and m['fold_robust'] and (pbo_val is None or pbo_val<0.50)))}")
    log(f"  elapsed                      : {(datetime.now()-t0).total_seconds():.0f}s")
    log("\nDONE_EXPERIMENT")


if __name__ == "__main__":
    main()
