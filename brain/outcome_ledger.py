"""Outcome ledger — the per-thesis reliability + LENS-EDGE substrate (the self-calibrating gate's input).

Coexists with, and is complementary to, brain/calibration.py (which de-confidences agents with a
shrink-only multiplier). This module does the OTHER half of closing the perception-to-outcome loop:
when a thesis resolves, it records a row joining three things the engine otherwise never connects:
  1. what it PREDICTED   — prob_correct + the falsifier (brain/decision, brain/ledger),
  2. what HAPPENED        — realized rel-return vs SPY and the hit/miss (brain/scorer.realize_returns),
  3. what it SAW          — the point-in-time lens snapshot at decision time (brain/signal_history).

From these it answers the two questions that turn opinion into skill: "when the engine said 60%, was
it right 60%?" (reliability) and "which LENSES/regimes actually predicted?" (lens_edge) — the exact
input a future SELF-CALIBRATING gate consumes to weight lenses by realized edge instead of equal
votes. No resolutions exist until the first cohort matures (~2026-07-17); this is the plumbing so that
cohort is captured cleanly the day it lands.

Append-only JSONL (data/brain/outcome_ledger.jsonl), KEEP-FIRST per thesis_id. Crash-safe / degrade-
never. Decoupled: `realized` may be passed in (to share the track-record's source) or computed via
scorer.realize_returns. Records whose decision predates signal_history carry an empty lens snapshot —
reliability still works from day one; lens_edge compounds as fully-recorded cohorts resolve.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path

from brain.ledger import all_theses

_ROOT = Path(__file__).resolve().parent.parent
_PATH = _ROOT / "data" / "brain" / "outcome_ledger.jsonl"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read() -> list[dict]:
    if not _PATH.exists():
        return []
    out: list[dict] = []
    try:
        for line in _PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                pass
    except Exception:
        return []
    return out


def _scored_ids() -> set[str]:
    return {r.get("thesis_id") for r in _read() if r.get("thesis_id")}


def _realized_map(asof) -> dict[str, float]:
    """{thesis_id: realized rel-return} for due theses. Uses the SAME grader as the track record
    (brain.scorer.realize_returns), guarded so a cold price store degrades to {} rather than raising."""
    try:
        from brain import scorer
        return scorer.realize_returns(asof) or {}
    except Exception:
        return {}


def _lens_snapshot(subject: str, asof_decided: str | None) -> dict:
    """The PIT signal_history row for this name at its decision date (empty if it predates recording)."""
    if not asof_decided:
        return {}
    try:
        from brain import signal_history
        for r in signal_history.load(asof_decided):
            if (r.get("ticker") or "").upper() == (subject or "").upper():
                return r
    except Exception:
        pass
    return {}


def _outcome(check: dict, realized: float) -> int | None:
    """1 = the directional prediction was CORRECT, 0 = falsified. None for non-directional theses."""
    if not isinstance(check, dict) or check.get("kind") != "rel_return":
        return None
    op, thr = check.get("op"), check.get("threshold")
    if op is None or thr is None:
        return None
    miss = (realized < thr) if op == "<" else (realized > thr)
    return 0 if miss else 1


def resolve(asof, realized: dict | None = None, *, theses: list | None = None) -> int:
    """Emit ledger records for every thesis that has resolved (id present in `realized`), joining
    prediction + outcome + the decision-time lens snapshot. KEEP-FIRST per thesis_id. Returns the
    count written. `realized` defaults to scorer.realize_returns(asof). Never raises."""
    try:
        rmap = realized if realized is not None else _realized_map(asof)
        if not rmap:
            return 0
        rows = theses if theses is not None else all_theses()
        by_id = {t.get("id"): t for t in rows}
        already = _scored_ids()
        fresh: list[dict] = []
        asof_resolved = asof if isinstance(asof, str) else (asof.isoformat() if isinstance(asof, date) else None)
        for tid, rel in rmap.items():
            if tid in already or tid not in by_id:
                continue
            t = by_id[tid]
            check = (t.get("falsifier") or {}).get("check") or {}
            outcome = _outcome(check, rel)
            if outcome is None:                       # non-directional (watch/hold) — not a graded bet
                continue
            asof_decided = t.get("state_asof")
            snap = _lens_snapshot(t.get("subject"), asof_decided)
            fresh.append({
                "thesis_id": tid, "subject": t.get("subject"),
                "asof_decided": asof_decided, "asof_resolved": asof_resolved,
                "prob_correct": t.get("prob_correct"), "lean": t.get("lean"),
                "horizon_d": t.get("horizon_d"), "sleeve": t.get("sleeve"),
                "realized_rel": round(float(rel), 4), "outcome": outcome,
                # what it SAW at decision time (empty if decided before signal_history existed)
                "lens_dirs": snap.get("lens_dirs") or {},
                "confluence_at_entry": snap.get("confluence"),
                "size_authority_at_entry": snap.get("size_authority"),
                "quad_at_entry": snap.get("quad"),
                "recorded_at": _now_iso(),
            })
        if not fresh:
            return 0
        _PATH.parent.mkdir(parents=True, exist_ok=True)
        with _PATH.open("a", encoding="utf-8") as fh:
            for r in fresh:
                fh.write(json.dumps(r, default=str, ensure_ascii=False) + "\n")
        return len(fresh)
    except Exception:
        return 0


# ---------------------------------------------------------------------------
# analysis — the substrate the self-calibrating gate (and the honest scorecard) consume
# ---------------------------------------------------------------------------

def load() -> list[dict]:
    return _read()


def summary() -> dict:
    """Brier, hit-rate and calibration error over all graded records (status='building' while n=0)."""
    rows = [r for r in _read() if r.get("outcome") in (0, 1) and r.get("prob_correct") is not None]
    n = len(rows)
    if n == 0:
        return {"n": 0, "status": "building", "brier": None, "hit_rate": None, "calibration_error": None}
    hits = sum(r["outcome"] for r in rows)
    brier = round(sum((r["prob_correct"] - r["outcome"]) ** 2 for r in rows) / n, 4)
    curve = reliability_curve()
    cal_err = (round(sum(abs(b["mean_predicted"] - b["hit_rate"]) * b["n"] for b in curve) / n, 4)
               if curve else None)
    return {"n": n, "status": "scoring", "hits": hits, "hit_rate": round(hits / n, 3),
            "brier": brier, "calibration_error": cal_err}


def reliability_curve(bins: int = 5) -> list[dict]:
    """Reliability diagram: per predicted-probability bucket, mean predicted vs realized hit-rate.
    A well-calibrated engine has hit_rate ≈ mean_predicted in every bucket."""
    rows = [r for r in _read() if r.get("outcome") in (0, 1) and r.get("prob_correct") is not None]
    if not rows:
        return []
    out = []
    for i in range(bins):
        lo, hi = i / bins, (i + 1) / bins
        bucket = [r for r in rows if (lo <= r["prob_correct"] < hi) or (i == bins - 1 and r["prob_correct"] == hi)]
        if not bucket:
            continue
        m = len(bucket)
        out.append({"bin": f"{lo:.2f}-{hi:.2f}", "n": m,
                    "mean_predicted": round(sum(r["prob_correct"] for r in bucket) / m, 3),
                    "hit_rate": round(sum(r["outcome"] for r in bucket) / m, 3)})
    return out


def lens_edge(min_n: int = 1) -> list[dict]:
    """Per (lens, direction) realized hit-rate across fully-recorded resolutions — i.e. when lens X
    read 'bull' at entry, how often was the trade right? This is the empirical reliability the
    self-calibrating gate will weight by (replacing today's equal lens votes). Records with no lens
    snapshot (decided before signal_history) are skipped here; reliability/summary still count them."""
    rows = [r for r in _read() if r.get("outcome") in (0, 1) and r.get("lens_dirs")]
    agg: dict[tuple, list[int]] = {}
    for r in rows:
        for lens, d in (r.get("lens_dirs") or {}).items():
            if d in ("bull", "bear"):
                agg.setdefault((lens, d), []).append(r["outcome"])
    out = []
    for (lens, d), outs in agg.items():
        if len(outs) < min_n:
            continue
        out.append({"lens": lens, "direction": d, "n": len(outs),
                    "hit_rate": round(sum(outs) / len(outs), 3)})
    out.sort(key=lambda x: (-x["n"], -x["hit_rate"]))
    return out


def lens_weights(min_n: int = 20, k: float = 2.0, floor: float = 0.5, ceil: float = 1.5,
                 prior_n: float = 20.0) -> dict:
    """Per-lens RELIABILITY WEIGHT for the self-calibrating gate (portfolio.lenses.synthesize).

    Turns each lens's realized directional hit-rate (lens_edge) into a multiplier on its vote: a lens
    that beat coin-flip earns weight > 1, one at/below 50% is damped toward `floor`. The hit-rate is
    SHRUNK toward 0.5 by sample size (prior_n pseudo-observations) so a thin record barely moves a
    lens — and a lens with fewer than `min_n` total graded reads is omitted entirely (=> it keeps the
    1.0 default, i.e. equal voting). Returns {} until some lens earns a track record, so the gate's
    behaviour is unchanged until the engine has, honestly, learned which eyes to trust."""
    by_lens: dict[str, list[tuple[int, float]]] = {}
    for e in lens_edge(min_n=1):
        by_lens.setdefault(e["lens"], []).append((e["n"], e["hit_rate"]))
    out: dict[str, float] = {}
    for lens, rows in by_lens.items():
        n_tot = sum(n for n, _ in rows)
        if n_tot < min_n:
            continue
        hr = sum(n * h for n, h in rows) / n_tot                     # n-weighted pooled hit-rate
        hr_shrunk = (n_tot * hr + prior_n * 0.5) / (n_tot + prior_n)  # shrink toward coin-flip by sample size
        w = 1.0 + k * (hr_shrunk - 0.5)
        out[lens] = round(max(floor, min(ceil, w)), 3)
    return out
