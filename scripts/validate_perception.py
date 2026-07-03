"""scripts/validate_perception.py — the PERCEPTION VALIDATION HARNESS (W-E.1 task E1.4).

WHAT THIS IS
------------
The pre-registered walk-forward battery for the W-E perception organs, per build-plan §3 and the
charter (P3 — every object stamps an HONEST pass/fail with its cold-start label; the nowcast
precedent: a FAIL ships advisory and SAYS so). It runs three jobs and writes one markdown verdict
per object under research/eyes/validation_runs/<object>_<date>.md:

  1. rotation_tensor composite   — defensive-episode detection vs SPY 5d fwd max-drawdown.
                                    Gate: AUC > 0.55 AND the signal fires on < 10% of days.
  2. CRASH-RISK alarm            — the alarm's forward drawdown-discrimination (AUC vs SPY 5d fwd
                                    max-drawdown), same gate.
  3. label_vs_planes disagreement — conflict-days vs SPY 5d fwd max-drawdown, same gate.

THE HONEST DATA REALITY (P3 — read before trusting a verdict)
-------------------------------------------------------------
Only PRICE parquets have deep history. The regime file (risk_radar drawdown_prob, vol_shock,
market_gamma, the Q1/Q2/Q3/Q4 label) is a SINGLE live SNAPSHOT (data/regime/latest.json) plus a
4-row live risk_radar forward-log — there is NO historical regime series over 2011-2026. Therefore:

  * rotation_tensor is FULLY computable — it reads only price parquets via its injectable series_fn,
    replayed at each asof. This job produces a REAL walk-forward verdict.
  * CRASH-RISK and label_vs_planes are UNCOMPUTABLE with vendored history — their inputs (the
    forward-drawdown scare, the vol/gamma structure, the regime label) do not exist historically.
    The harness reports them FAIL / cold_start=true (⇒ ship ADVISORY, the severity-notch seam stays
    DARK) and names exactly which vendored series would make them computable. This is the honest
    result, not a bug: fabricating a proxy and calling it "the alarm" would violate P2/P3.

THE WAVE CONTRACT — THIS HARNESS WIRES NOTHING
----------------------------------------------
It reads read-only price data and writes markdown verdicts. It changes NO sizing path, arms NO
notch, flips NO plane to validated. The verdicts DECIDE what E2/E3 may arm; a human (Fable) reads
them and makes that call. Run deliberately:

    python scripts/validate_perception.py                 # all three jobs, live price data
    python scripts/validate_perception.py --start 2015-01-01
    python scripts/validate_perception.py --job rotation_tensor
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

# The read-only macro-dashboard price parquets (the only deep history we have). Falls back to the
# vendored copy if the sibling dashboard checkout is not present.
_MACRO_YAHOO_CANDIDATES = [
    Path("/Users/chriswong/Documents/Cluade/Macro Dashboard/data/yahoo"),
    _ROOT / "vendor" / "macro" / "data" / "yahoo",
]
_RUNS_DIR = _ROOT / "research" / "eyes" / "validation_runs"


# ---------------------------------------------------------------------------
# doctrine config (pre-registered gate parameters; fallbacks mirror doctrine.yml)
# ---------------------------------------------------------------------------

def _cfg() -> dict[str, Any]:
    try:
        from brain import regime_frame as rf
        block = rf._doctrine().get("perception_validation") or {}
        return block if isinstance(block, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _cfg_get(key: str, default: Any) -> Any:
    v = _cfg().get(key)
    return default if v is None else v


# ---------------------------------------------------------------------------
# price loading (read-only) + forward-drawdown label
# ---------------------------------------------------------------------------

def _yahoo_dir() -> Path:
    for p in _MACRO_YAHOO_CANDIDATES:
        if p.exists():
            return p
    return _MACRO_YAHOO_CANDIDATES[-1]


def _load_close(ticker: str):
    """Read-only close Series for a ticker, or None. Never raises."""
    import pandas as pd
    p = _yahoo_dir() / f"{ticker}.parquet"
    if not p.exists():
        return None
    try:
        return pd.read_parquet(p, columns=["close"])["close"].sort_index()
    except Exception:  # noqa: BLE001
        return None


def _fwd_drawdown_events(close, fwd: int, dd_bps: int):
    """Label each session with the forward `fwd`-session max-drawdown, and a boolean event flag.

    max-drawdown = min over the next `fwd` closes of (close_{t+k}/close_t - 1); an "event" is a
    drawdown at or beyond -dd_bps/10000 (e.g. dd_bps=200 → -2%). Returns (fwd_dd Series aligned to
    each session's date, event Series of {0,1}), both truncated to sessions with a full forward
    window. Deterministic, causal (the label is strictly forward of the signal date)."""
    import numpy as np
    import pandas as pd
    c = close.dropna().sort_index()
    vals = c.values
    idx = c.index
    n = len(vals)
    fwd_dd = np.full(n, np.nan)
    for t in range(n - fwd):
        window = vals[t + 1 : t + 1 + fwd]
        if len(window) < fwd:
            continue
        fwd_dd[t] = float(window.min() / vals[t] - 1.0)
    dd = pd.Series(fwd_dd, index=idx)
    thresh = -dd_bps / 10000.0
    event = (dd <= thresh).astype(float)
    event[dd.isna()] = np.nan
    return dd, event


def _auc(y, p) -> Optional[float]:
    """Rank-based ROC-AUC (Mann-Whitney U), no sklearn. None if a class is empty (mirrors
    brain.distill._auc)."""
    try:
        import numpy as np
        y = np.asarray(y).astype(int)
        p = np.asarray(p, dtype=float)
        n_pos, n_neg = int((y == 1).sum()), int((y == 0).sum())
        if n_pos == 0 or n_neg == 0:
            return None
        order = np.argsort(p, kind="mergesort")
        ranks = np.empty(len(p), dtype=float)
        ranks[order] = np.arange(1, len(p) + 1)
        return round(float((ranks[y == 1].sum() - n_pos * (n_pos + 1) / 2.0) / (n_pos * n_neg)), 4)
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# JOB 1 — rotation_tensor composite (FULLY computable from price parquets)
# ---------------------------------------------------------------------------

def _walk_dates(close, start: str, end: Optional[str], stride: int):
    """Trading dates from `close`'s index within [start, end], subsampled by `stride`."""
    import pandas as pd
    c = close.dropna().sort_index()
    lo = pd.Timestamp(start)
    hi = pd.Timestamp(end) if end else c.index.max()
    dts = [d for d in c.index if lo <= d <= hi]
    return dts[::stride]


def run_rotation_tensor(start: str, end: Optional[str], stride: int) -> dict[str, Any]:
    """Walk-forward the rotation_tensor DEFENSIVE-episode signal vs SPY 5d fwd max-drawdown.

    Signal at each asof: a DEFENSIVE headline episode with dR confirming (same sign as R on the
    leading pair) at percentile >= episode_extreme_pctile (the rarest defensive rotations). This is
    a boolean fires/quiet flag AND a magnitude (the episode percentile), so we score AUC of the
    magnitude and the fire-rate of the boolean.

    Computable end-to-end: rotation_tensor.assemble reads only price parquets via the injected
    series_fn, replayed at each asof. Returns the verdict dict.
    """
    from brain import rotation_tensor as rt

    bench = str(_cfg_get("benchmark", "SPY"))
    fwd = int(_cfg_get("fwd_sessions", 5))
    dd_bps = int(_cfg_get("drawdown_bps_min", 200))
    ext_pctile = float(_cfg_get("episode_extreme_pctile", 0.95))
    auc_gate = float(_cfg_get("auc_gate", 0.55))
    fires_max = float(_cfg_get("fires_max_frac", 0.10))

    spy = _load_close(bench)
    if spy is None:
        return {"status": "no_data", "note": f"{bench} parquet not found in {_yahoo_dir()}"}
    fwd_dd, event = _fwd_drawdown_events(spy, fwd, dd_bps)

    # cache each ticker's series once (assemble is called per-date; caching is the speed win)
    _cache: dict[str, Any] = {}

    def sfn(t: str):
        if t not in _cache:
            _cache[t] = _load_close(t if t != "SMH" else "SMH")
        return _cache[t]

    dates = _walk_dates(spy, start, end, stride)
    # only keep dates that have a resolved forward-drawdown label
    signal_mag: list[float] = []
    signal_fire: list[int] = []
    labels: list[int] = []
    n_eval = 0
    n_episode = 0
    for d in dates:
        ev = event.get(d)
        if ev is None or (isinstance(ev, float) and ev != ev):  # NaN → no forward window
            continue
        asof = d.strftime("%Y-%m-%d")
        try:
            out = rt.assemble(series_fn=sfn, asof=asof)
        except Exception:  # noqa: BLE001
            continue
        he = out.get("headline_episode") if isinstance(out, dict) else None
        # magnitude signal: defensive-episode percentile, else 0 (no defensive rotation → no signal)
        mag = 0.0
        fire = 0
        if isinstance(he, dict) and he.get("direction") == "defensive":
            n_episode += 1
            pct = he.get("percentile")
            mag = float(pct) if pct is not None else 0.0
            # dR confirming: the leading top_pair's dR shares sign with its R (gap widening)
            tps = (out.get("rs_velocity") or {}).get("top_pairs") or []
            dr_confirms = True
            if tps:
                lead = tps[0]
                R = lead.get("R_bps_day")
                dR = lead.get("dR_bps_day")
                if R is not None and dR is not None:
                    dr_confirms = (R >= 0) == (dR >= 0)
            if mag >= ext_pctile and dr_confirms:
                fire = 1
        signal_mag.append(mag)
        signal_fire.append(int(fire))
        labels.append(int(ev))
        n_eval += 1

    auc = _auc(labels, signal_mag) if n_eval else None
    n_fires = int(sum(signal_fire))
    fire_frac = round(n_fires / n_eval, 4) if n_eval else None
    base_rate = round(sum(labels) / n_eval, 4) if n_eval else None
    # conditional event rate on fire vs base (the "does it front-run" read)
    cond = [labels[i] for i in range(n_eval) if signal_fire[i] == 1]
    cond_rate = round(sum(cond) / len(cond), 4) if cond else None

    auc_pass = auc is not None and auc > auc_gate
    fires_pass = fire_frac is not None and fire_frac < fires_max
    gate_pass = bool(auc_pass and fires_pass)

    return {
        "status": "ok",
        "object": "rotation_tensor",
        "computable": True,
        "cold_start": False,
        "window": {"start": start, "end": end or "latest", "stride": stride},
        "n_eval": n_eval,
        "n_defensive_episodes": n_episode,
        "n_fires": n_fires,
        "fire_frac": fire_frac,
        "base_event_rate": base_rate,
        "cond_event_rate_on_fire": cond_rate,
        "auc": auc,
        "gate": {"auc_gate": auc_gate, "fires_max_frac": fires_max,
                 "auc_pass": bool(auc_pass), "fires_pass": bool(fires_pass)},
        "verdict": "PASS" if gate_pass else "FAIL",
        "arms": "rotation_tensor composite may enter D as a shrink-only plane" if gate_pass
                else "rotation_tensor stays DISPLAY-ONLY (advisory plane; cannot size)",
    }


# ---------------------------------------------------------------------------
# JOBS 2 & 3 — CRASH-RISK / label_vs_planes (UNCOMPUTABLE with vendored history)
# ---------------------------------------------------------------------------

def _uncomputable_verdict(object_name: str, needs: str, precedent: str) -> dict[str, Any]:
    """An HONEST cold-start FAIL for a job whose inputs do not exist over the walk-forward window.

    Per P3 (the nowcast precedent): the object ships ADVISORY, its severity/tilt seam stays DARK,
    and the verdict names exactly which vendored series would make the gate computable. This is a
    truthful non-result, NOT a proxy dressed up as a pass."""
    return {
        "status": "uncomputable",
        "object": object_name,
        "computable": False,
        "cold_start": True,
        "reason": ("the walk-forward inputs do not exist over 2011-2026 with vendored data — "
                   "the regime file is a single live snapshot, not a historical series"),
        "needs": needs,
        "precedent": precedent,
        "verdict": "FAIL",
        "arms": (f"{object_name} ships ADVISORY / cold_start=true — the notch/tilt seam stays DARK "
                 "until a forward-graded historical series exists to gate it"),
    }


def run_crash_risk() -> dict[str, Any]:
    """CRASH-RISK alarm gate — UNCOMPUTABLE with vendored history (honest FAIL, ship advisory).

    The alarm's legs are the risk_radar forward drawdown-scare (drawdown_prob.h21 lift under a hot
    dominant_scare), vol_shock structure, and a read-only dealer-gamma classification — ALL embedded
    in regime/latest.json, which is a single live snapshot. The live risk_radar forward-log has only
    ~4 rows (2026-06-23..26), far below any AUC power. So the AUC>0.55 + <10%-of-days gate cannot be
    run over the parquet window. Honest verdict: FAIL / cold_start (⇒ notch stays dark, per E0.2's
    notch_eligible=False and the build-plan §3 "negative shadow Brier ⇒ do-not-build" caution)."""
    return _uncomputable_verdict(
        "crash_risk",
        needs=("a DAILY historical series of regime.risk_radar.drawdown_prob (h21 + base + lift + "
               "dominant_scare), regime.vol_shock, and regime.market_gamma — i.e. the dashboard must "
               "vendor a dated risk_radar/forward_log with drawdown_prob, or replay its engine over "
               "history (handoff H4). Today only ~4 live forward-log rows exist."),
        precedent=("regime_nowcast (walk-forward gate failed 0.354 → ships ADVISORY-ONLY). CRASH-RISK "
                   "ships the same way: advisory plane of the view, severity notch DARK."),
    )


def run_label_vs_planes() -> dict[str, Any]:
    """label_vs_planes disagreement gate — UNCOMPUTABLE with vendored history (honest FAIL).

    The disagreement is between the regime LABEL (Q1..Q4 + confidence) and the validated-plane
    consensus (risk_radar / mtf_signals / cycles). Reconstructing a conflict-day series over
    2011-2026 needs a historical market_view — which needs historical regime blocks AND historical
    per-ticker mtf_signals — none of which are vendored. The one place a conflict IS witnessed is the
    frozen 06-26..07-01 incident fixture (asserted in tests/test_market_view.py + the incident
    replay), but 5 sessions cannot power an AUC gate. Honest verdict: FAIL / cold_start — the
    disagreement plane ships ADVISORY (may annotate/shrink, never release a cap) until a forward log
    of conflict-days accrues live."""
    return _uncomputable_verdict(
        "label_vs_planes",
        needs=("a DAILY historical market_view series (label direction + validated-plane consensus) "
               "— which needs historical regime blocks + historical per-ticker mtf_signals. NONE are "
               "vendored. The live forward log of conflict-days (E1.3 wake trigger) must accrue "
               "months of resolved calls before an AUC gate has power."),
        precedent=("the incident fixture PROVES the conflict fires on the 06-26..07-01 tape (5 "
                   "sessions), but that is a replay assert, not a walk-forward gate. Ships ADVISORY."),
    )


# ---------------------------------------------------------------------------
# markdown emit (one file per object, pre-registered gate + honest verdict)
# ---------------------------------------------------------------------------

def _fmt(v: Any) -> str:
    return "—" if v is None else str(v)


def _emit_md(res: dict[str, Any], run_date: str) -> Path:
    obj = res.get("object", "unknown")
    path = _RUNS_DIR / f"{obj}_{run_date}.md"
    lines: list[str] = []
    verdict = res.get("verdict", "?")
    lines.append(f"# Perception validation — `{obj}` — {verdict}")
    lines.append("")
    lines.append(f"*Run {datetime.now(tz=timezone.utc).isoformat()} · harness "
                 "`scripts/validate_perception.py` (W-E.1 task E1.4) · charter P3 (honest pass/fail)*")
    lines.append("")
    lines.append("> This verdict DECIDES what E2/E3 may arm. The harness WIRES NOTHING — a human "
                 "(Fable) reads this and makes the arming call.")
    lines.append("")

    if res.get("status") == "no_data":
        lines.append(f"**INDETERMINATE — no data.** {res.get('note')}")
        path.write_text("\n".join(lines) + "\n")
        return path

    if not res.get("computable", True):
        lines.append("## Verdict: FAIL (cold-start, UNCOMPUTABLE with vendored history)")
        lines.append("")
        lines.append(f"**Reason.** {res.get('reason')}")
        lines.append("")
        lines.append(f"**What would make it computable.** {res.get('needs')}")
        lines.append("")
        lines.append(f"**Precedent (P3).** {res.get('precedent')}")
        lines.append("")
        lines.append(f"**Arming decision.** {res.get('arms')}")
        lines.append("")
        lines.append("Per the charter's degrade-never-fabricate rule, the harness does NOT "
                     "substitute a price-only proxy and call it this alarm — a truthful non-result "
                     "is the correct output.")
        path.write_text("\n".join(lines) + "\n")
        return path

    g = res.get("gate", {})
    lines.append(f"## Verdict: {verdict}")
    lines.append("")
    lines.append(f"**Pre-registered gate.** AUC > {g.get('auc_gate')} AND fires on "
                 f"< {int(float(g.get('fires_max_frac', 0.1)) * 100)}% of sessions.")
    lines.append("")
    w = res.get("window", {})
    lines.append(f"**Window.** {w.get('start')} → {w.get('end')} (stride {w.get('stride')} sessions), "
                 f"{_fmt(res.get('n_eval'))} evaluable sessions.")
    lines.append("")
    lines.append("| metric | value | gate |")
    lines.append("|---|---|---|")
    lines.append(f"| AUC (signal vs SPY 5d fwd max-drawdown event) | {_fmt(res.get('auc'))} | "
                 f"> {g.get('auc_gate')} → {'PASS' if g.get('auc_pass') else 'FAIL'} |")
    lines.append(f"| fire fraction | {_fmt(res.get('fire_frac'))} | "
                 f"< {g.get('fires_max_frac')} → {'PASS' if g.get('fires_pass') else 'FAIL'} |")
    lines.append(f"| base event rate | {_fmt(res.get('base_event_rate'))} | (reference) |")
    lines.append(f"| conditional event rate on fire | {_fmt(res.get('cond_event_rate_on_fire'))} | "
                 "(lift-over-base read) |")
    lines.append(f"| defensive episodes seen | {_fmt(res.get('n_defensive_episodes'))} | — |")
    lines.append(f"| times fired | {_fmt(res.get('n_fires'))} | — |")
    lines.append("")
    lines.append(f"**Arming decision.** {res.get('arms')}")
    lines.append("")
    if verdict == "FAIL":
        lines.append("Auto-demotion falsifier (build-plan §3): the composite stays DISPLAY-ONLY — a "
                     "negative shadow Brier vs coin-flip would put it on the do-not-size list.")
    path.write_text("\n".join(lines) + "\n")
    return path


# ---------------------------------------------------------------------------
# entrypoint
# ---------------------------------------------------------------------------

def run(job: str, start: str, end: Optional[str], stride: int, write: bool = True) -> dict[str, Any]:
    """Run one/all jobs; emit markdown per object; return the results dict keyed by object."""
    _RUNS_DIR.mkdir(parents=True, exist_ok=True)
    run_date = date.today().isoformat()
    results: dict[str, Any] = {}

    if job in ("all", "rotation_tensor"):
        results["rotation_tensor"] = run_rotation_tensor(start, end, stride)
    if job in ("all", "crash_risk"):
        results["crash_risk"] = run_crash_risk()
    if job in ("all", "label_vs_planes"):
        results["label_vs_planes"] = run_label_vs_planes()

    if write:
        for res in results.values():
            if isinstance(res, dict) and res.get("object"):
                p = _emit_md(res, run_date)
                res["_md"] = str(p)
    return results


def _parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Perception validation harness (W-E.1 E1.4)")
    ap.add_argument("--job", default="all",
                    choices=["all", "rotation_tensor", "crash_risk", "label_vs_planes"])
    ap.add_argument("--start", default=None, help="walk-forward start (default: doctrine walk_start_full)")
    ap.add_argument("--end", default=None, help="walk-forward end (default: latest session)")
    ap.add_argument("--stride", type=int, default=1, help="subsample every Nth session (default 1)")
    ap.add_argument("--no-write", action="store_true", help="do not emit markdown files")
    return ap.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> None:
    args = _parse_args(argv)
    start = args.start or str(_cfg_get("walk_start_full", "2011-01-01"))
    results = run(args.job, start, args.end, max(1, args.stride), write=not args.no_write)
    for obj, res in results.items():
        v = res.get("verdict", res.get("status"))
        extra = ""
        if res.get("computable", True) and res.get("status") == "ok":
            extra = f" auc={res.get('auc')} fire_frac={res.get('fire_frac')} n={res.get('n_eval')}"
        print(f"{obj:18s} {v:12s}{extra}")
        if res.get("_md"):
            print(f"  -> {res['_md']}")


if __name__ == "__main__":
    main()
