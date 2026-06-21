"""Deterministic outcome labeling — the learning signal for the BRAIN flywheel.

Every thesis the bot commits carries a falsifiable `rel_return` check (subject vs SPY over
N business days). This module REPLAYS the realized price path and computes, per thesis:

  * rel_return   — subject return minus the benchmark return over the falsifier horizon
                   (the EXACT quantity `brain.scorer.track_record(realized=…)` grades on),
  * a triple-barrier label — TARGET / STOP / TIME, the first price barrier the path touched,
  * abs_return / benchmark_return, and a resolved/in_progress status.

This is the prerequisite for self-learning: without realized outcomes, the Brier ledger can
never leave "building (n=0)". Pure data + arithmetic, no LLM. Best-effort: a thesis whose
price history isn't available stays `unresolved` rather than raising.

Price source: the macro yahoo parquet store (local, covers SPY + the tracked universe) with a
Polygon daily-aggregates fallback for single names the store doesn't carry.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from brain.ledger import all_theses

# triple-barrier defaults (ABSOLUTE price). The rigorous, graded label is rel_return; the
# barrier is supplementary color for attribution + the UI.
_TARGET_PCT = 0.15
_STOP_PCT = -0.10


def _closes(ticker: str, start: str, end: str) -> dict:
    """{YYYY-MM-DD: close} over [start, end]. Local macro yahoo parquet first (free, covers SPY
    + tracked names), Polygon daily aggregates as the single-name fallback. {} if unavailable."""
    t = (ticker or "").upper().strip()
    if not t:
        return {}
    try:
        import pandas as pd
        from engine import equity_alloc as ea
        s = ea.index_close(t)
        s = s[(s.index >= pd.Timestamp(start)) & (s.index <= pd.Timestamp(end))]
        if len(s) >= 2:
            return {d.strftime("%Y-%m-%d"): float(v) for d, v in s.items() if v == v and v > 0}
    except Exception:  # noqa: BLE001 — ticker not in the local store; fall through to Polygon
        pass
    try:
        from data_layer import polygon
        return polygon.daily_closes(t, start, end)
    except Exception:  # noqa: BLE001
        return {}


def _aligned(subj_c: dict, vs_c: dict, entry_iso: str) -> list:
    """Sorted trading dates present in BOTH series and on/after the entry date."""
    return sorted(d for d in subj_c if d in vs_c and d >= entry_iso)


def label_thesis(thesis: dict, asof: date | None = None) -> dict | None:
    """Label one thesis from its realized price path. Returns None if the thesis has no
    rel_return falsifier; otherwise a dict that always carries a `status`
    (resolved / in_progress / unresolved). Never raises."""
    asof = asof or date.today()
    try:
        chk = (thesis.get("falsifier") or {}).get("check") or {}
        if chk.get("kind") != "rel_return":
            return None
        subj = (chk.get("subject_ticker") or (thesis.get("entry_levels") or {}).get("ticker") or "").upper()
        vs = (chk.get("vs") or "SPY").upper()
        horizon = int(chk.get("horizon_d") or 21)
        entry_iso = str(thesis.get("state_asof") or "")[:10]
        entry_px = (thesis.get("entry_levels") or {}).get("price")
        tid = thesis.get("id")
        base = {"id": tid, "ticker": subj, "vs": vs, "entry_date": entry_iso,
                "horizon_d": horizon, "status": "unresolved", "barrier": None,
                "resolved": False, "rel_return": None, "abs_return": None,
                "vs_return": None, "exit_date": None, "exit_px": None, "entry_px": entry_px}
        if not (subj and entry_iso):
            return base
        d0 = date.fromisoformat(entry_iso)
        # business→calendar buffer (×1.6 + slack) so holidays/weekends don't truncate the window
        end = min(d0 + timedelta(days=int(horizon * 1.6) + 8), asof)
        lo = (d0 - timedelta(days=7)).isoformat()
        hi = (end + timedelta(days=3)).isoformat()
        subj_c, vs_c = _closes(subj, lo, hi), _closes(vs, lo, hi)
        if not subj_c or not vs_c:
            return base
        dates = _aligned(subj_c, vs_c, entry_iso)
        if len(dates) < 2:
            return base
        p0s = float(entry_px) if entry_px else subj_c[dates[0]]
        p0v = vs_c[dates[0]]
        if not (p0s and p0v):
            return base
        resolved = (len(dates) - 1) >= horizon
        idx = min(horizon, len(dates) - 1)
        dh = dates[idx]
        abs_ret = subj_c[dh] / p0s - 1.0
        vs_ret = vs_c[dh] / p0v - 1.0
        rel = abs_ret - vs_ret
        # triple-barrier scan over the realized path up to the horizon (absolute price)
        target_px, stop_px = p0s * (1 + _TARGET_PCT), p0s * (1 + _STOP_PCT)
        barrier, bdate, bpx = None, dh, subj_c[dh]
        for d in dates[1:idx + 1]:
            px = subj_c[d]
            if px >= target_px:
                barrier, bdate, bpx = "target", d, px
                break
            if px <= stop_px:
                barrier, bdate, bpx = "stop", d, px
                break
        if barrier is None and resolved:
            barrier = "time"
        return {**base,
                "status": "resolved" if resolved else "in_progress",
                "resolved": bool(resolved),
                "entry_px": round(p0s, 4),
                "exit_date": bdate, "exit_px": round(bpx, 4),
                "abs_return": round(abs_ret, 4), "vs_return": round(vs_ret, 4),
                "rel_return": round(rel, 4), "barrier": barrier}
    except Exception:  # noqa: BLE001 — labeling is best-effort; never break the build
        return {"id": thesis.get("id"), "status": "unresolved", "resolved": False,
                "rel_return": None, "barrier": None}


def all_labels(asof: date | None = None) -> list:
    """Label every thesis (resolved + in-progress). The learning dataset + UI source."""
    asof = asof or date.today()
    out = []
    for t in all_theses():
        lab = label_thesis(t, asof)
        if lab is not None:
            out.append(lab)
    return out


def realized_returns(asof: date | None = None) -> dict:
    """{thesis_id: rel_return} for theses whose check_by has passed AND whose path resolved —
    exactly the input `brain.scorer.track_record(realized=…)` needs to grade the Brier ledger."""
    asof = asof or date.today()
    out = {}
    for t in all_theses():
        cb = t.get("check_by")
        try:
            due = cb and date.fromisoformat(str(cb)[:10]) <= asof
        except Exception:  # noqa: BLE001
            due = False
        if not due:
            continue
        lab = label_thesis(t, asof)
        if lab and lab.get("resolved") and lab.get("rel_return") is not None:
            out[t["id"]] = lab["rel_return"]
    return out


def summary(asof: date | None = None) -> dict:
    """At-a-glance outcome stats for the dashboard track-record panel."""
    labs = all_labels(asof)
    resolved = [x for x in labs if x.get("resolved")]
    wins = [x for x in resolved if (x.get("rel_return") or 0) > 0]
    avg = round(sum(x["rel_return"] for x in resolved) / len(resolved), 4) if resolved else None
    barriers = {b: sum(1 for x in resolved if x.get("barrier") == b) for b in ("target", "stop", "time")}
    return {"n_total": len(labs), "n_resolved": len(resolved), "n_in_progress": len(labs) - len(resolved),
            "hit_rate": round(len(wins) / len(resolved), 3) if resolved else None,
            "avg_rel_return": avg, "barriers": barriers,
            "as_of": (asof or date.today()).isoformat()}
