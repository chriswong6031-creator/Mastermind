"""Deterministic thesis scorer -> Brier/calibration track record (no LLM).

Grades theses whose check_by has passed against realized rel-return, idempotent on id.
Until any thesis resolves, the track record is honestly "building (n=0)". This is the
gate that keeps the bot paper-only until a rolling Brier proves skill.
"""
from __future__ import annotations

from datetime import date

from brain.ledger import all_theses


def _series_at(s, day_iso):
    """Value of a date-indexed Series at or before `day_iso` (the most recent prior point), or None."""
    try:
        import pandas as pd
        d = pd.to_datetime(day_iso)
        sub = s[s.index <= d]
        if len(sub):
            return float(sub.iloc[-1])
    except Exception:
        pass
    return None


def realize_returns(asof, *, vs: str = "SPY") -> dict[str, float]:
    """Realized rel-return {thesis_id: subject_ret - benchmark_ret} for every OPEN, DUE, DIRECTIONAL
    thesis — the input the Brier scorer needs (track_record was always called with realized={} so it
    was permanently stuck at n=0). Marks the stored entry price vs the current price (the same engine
    price store paper_account uses) and nets the benchmark over the same window. Returns {} when
    prices are unavailable (offline/CI), so the track record honestly stays 'building'."""
    out: dict[str, float] = {}
    try:
        from portfolio import paper_account as pa
    except Exception:
        return out
    asof_d = asof if isinstance(asof, date) else date.fromisoformat(asof)
    try:
        spy = pa._fetch_price_series(vs)
    except Exception:
        spy = None
    if spy is None or len(spy) == 0:
        return out
    spy_now = float(spy.iloc[-1])
    for t in all_theses():
        if t.get("status", "open") != "open":
            continue
        cb = t.get("check_by")
        if not cb or date.fromisoformat(cb) > asof_d:
            continue
        chk = (t.get("falsifier") or {}).get("check") or {}
        if chk.get("kind") != "rel_return":               # only directional theses are gradable
            continue
        sub = chk.get("subject_ticker") or t.get("subject")
        entry_px = (t.get("entry_levels") or {}).get("price")
        if not entry_px or float(entry_px) <= 0:
            continue
        try:
            cur_px = pa._current_price(sub)
        except Exception:
            cur_px = None
        spy_entry = _series_at(spy, t.get("state_asof"))
        if not cur_px or cur_px <= 0 or not spy_entry or spy_entry <= 0:
            continue
        sub_ret = cur_px / float(entry_px) - 1.0
        spy_ret = spy_now / spy_entry - 1.0
        out[t["id"]] = round(sub_ret - spy_ret, 4)
    return out


def _brier(rows: list[dict]) -> float | None:
    graded = [r for r in rows if "outcome" in r and "prob_correct" in r]
    if not graded:
        return None
    return round(sum((r["prob_correct"] - r["outcome"]) ** 2 for r in graded) / len(graded), 4)


def track_record(asof: date | None = None, realized: dict[str, float] | None = None) -> dict:
    """Build the rolling track record. `realized` maps thesis id -> realized rel-return;
    absent ids stay open. Returns the portfolio.json track_record block."""
    asof = asof or date.today()
    realized = realized or {}
    rows = []
    for t in all_theses():
        due = t.get("check_by") and date.fromisoformat(t["check_by"]) <= asof
        if due and t["id"] in realized:
            chk = (t.get("falsifier") or {}).get("check") or {}
            # only DIRECTIONAL theses (rel_return) are gradable; a non-directional 'watch' carries
            # kind='none' with no op/threshold — skip it (grading it would KeyError, and a watch is
            # not a bet to be scored right/wrong).
            if chk.get("kind") != "rel_return":
                continue
            r = realized[t["id"]]
            # falsified if op(realized, threshold) holds
            miss = (r < chk["threshold"]) if chk["op"] == "<" else (r > chk["threshold"])
            rows.append({**t, "outcome": 0 if miss else 1, "realized": r})
    hits = sum(r["outcome"] for r in rows)
    n = len(rows)
    return {
        "n": n, "hits": hits,
        "hit_rate": round(hits / n, 3) if n else None,
        "brier": _brier(rows),
        "skill": None,                # vs climatology base; populated once n is meaningful
        "status": "building" if n == 0 else "scoring",
    }
