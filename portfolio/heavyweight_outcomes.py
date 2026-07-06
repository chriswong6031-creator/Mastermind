"""The Heavyweight book's accountability loop — forward-grade every daily pick vs SPY, and
measure SIZING SKILL specifically.

Heavyweight's whole reason to exist is WEIGHTING: it inherits the firm's names (union of published
books) and its only edge is going BIG on the right ones and SMALL on the rest. So the metric that
matters is not just "did the names beat SPY" but "did the BIGGER bets beat the smaller ones" — the
rank-IC of weight vs realized relative return (``weight_ic``), plus the weight-weighted book edge
per decision date.

This mirrors ``portfolio/etf_outcomes.py`` exactly (same leakage-free labeler, same date-clustered
Newey-West book edge, same conviction breakdown), scoped to the heavyweight book's own dir
(``data/portfolios/heavyweight/outcomes.jsonl``). ``bot/heavyweight.run_heavyweight`` calls ``grade()``
each run (syncs picks from the decision log, resolves matured ones) and feeds ``prompt_line()`` back
into the Brain's daily prompt so it SEES whether its sizing has actually been skillful and
self-corrects.

Pure data + arithmetic, no LLM; every step degrades to "building" rather than raising.
"""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import date, timedelta

PORTFOLIO_ID = "heavyweight"
BENCHMARK = "SPY"
# 21 business days — the same forward window the engine's prediction ledger and brain/calibration
# use, so heavyweight's sizing grade is comparable to the rest of the accountability stack.
HORIZON_D = 21


def _ledger_path():
    from portfolio import registry
    return registry.data_dir(PORTFOLIO_ID) / "outcomes.jsonl"


def _load_ledger() -> list[dict]:
    p = _ledger_path()
    rows: list[dict] = []
    try:
        if p.exists():
            for line in p.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line:
                    rows.append(json.loads(line))
    except Exception:
        pass
    return rows


def _save_ledger(rows: list[dict]) -> None:
    p = _ledger_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("".join(json.dumps(r, default=str) + "\n" for r in rows))
    except Exception:
        pass


# ---------------------------------------------------------------------------
# leakage-free labeler — heavyweight name rel-return vs SPY
# ---------------------------------------------------------------------------

def _label(ticker: str, entry_iso: str, horizon: int, asof: date) -> dict:
    """{status, rel_return, resolved_on} for one pick. status='resolved' once `horizon` trading
    days have elapsed AND a price path exists; else 'open'. Anchors BOTH the name and SPY to the
    same last common close on/before the entry date; never reads a close after `asof`. Never raises."""
    open_ = {"status": "open", "rel_return": None, "resolved_on": None}
    try:
        from brain import outcomes as _oc
        d0 = date.fromisoformat(str(entry_iso)[:10])
        asof_iso = asof.isoformat()
        final_end = d0 + timedelta(days=int(horizon * 1.6) + 8)   # bday→calendar span (weekends/holidays)
        is_final = final_end <= asof                              # window immutable → cacheable
        req_end = min(final_end, asof)                            # never request past asof
        lo = (d0 - timedelta(days=10)).isoformat()
        subj_c = _oc._closes(ticker, lo, req_end.isoformat(), cache=is_final)
        vs_c = _oc._closes(BENCHMARK, lo, req_end.isoformat(), cache=is_final)
        if not subj_c or not vs_c:
            return open_
        common = sorted(d for d in subj_c if d in vs_c and d <= asof_iso)
        if len(common) < 2:
            return open_
        ent = str(entry_iso)[:10]
        on_or_before = [d for d in common if d <= ent]
        anchor = on_or_before[-1] if on_or_before else common[0]
        post = common[common.index(anchor):]                     # anchor + every trading day forward
        if len(post) < 2:
            return open_
        if (len(post) - 1) < horizon:                            # horizon not fully elapsed
            return open_
        idx = min(horizon, len(post) - 1)
        dh = post[idx]
        p0s, p0v = subj_c[anchor], vs_c[anchor]
        if not (p0s and p0v):
            return open_
        rel = (subj_c[dh] / p0s - 1.0) - (vs_c[dh] / p0v - 1.0)
        return {"status": "resolved", "rel_return": round(rel, 4), "resolved_on": asof_iso}
    except Exception:
        return open_


# ---------------------------------------------------------------------------
# grade — record today's picks + resolve matured ones (idempotent)
# ---------------------------------------------------------------------------

def grade(asof: str | date | None = None) -> dict:
    """Sync the outcomes ledger from the book's decision log: ensure one entry per (decision-date,
    ticker) pick, then label every open entry forward and resolve the matured ones. Idempotent per
    run. Returns coverage. Best-effort; never raises."""
    from bot import heavyweight as _hw
    asof = asof or date.today()
    asof_d = asof if isinstance(asof, date) else date.fromisoformat(str(asof)[:10])

    ledger = _load_ledger()
    by_key = {(r.get("asof"), r.get("ticker")): r for r in ledger}
    try:
        decisions = _hw.load_decisions(400)
    except Exception:
        decisions = []
    for dec in decisions:
        d_asof = dec.get("asof")
        if not d_asof:
            continue
        for h in (dec.get("holdings") or []):
            tk = (h.get("ticker") or "").upper().strip()
            if not tk:
                continue
            key = (d_asof, tk)
            if key not in by_key:
                rec = {"asof": d_asof, "ticker": tk, "weight": h.get("weight"),
                       "conviction": h.get("conviction") or "high", "horizon_d": HORIZON_D,
                       "status": "open", "rel_return": None, "resolved_on": None}
                by_key[key] = rec
                ledger.append(rec)
            elif by_key[key].get("status") == "open":
                # a same-asof re-run (e.g. force=True) may revise the allocation — track the latest
                # weight/conviction while still open (a resolved row stays frozen on what was graded)
                ex = by_key[key]
                ex["weight"], ex["conviction"] = h.get("weight"), h.get("conviction") or "high"

    for r in ledger:
        if r.get("status") == "open":
            lab = _label(r["ticker"], r["asof"], int(r.get("horizon_d") or HORIZON_D), asof_d)
            if lab.get("status") == "resolved":
                r["status"], r["rel_return"], r["resolved_on"] = "resolved", lab["rel_return"], lab["resolved_on"]
    _save_ledger(ledger)
    n_res = sum(1 for r in ledger if r.get("status") == "resolved")
    return {"n_total": len(ledger), "n_resolved": n_res, "n_open": len(ledger) - n_res}


# ---------------------------------------------------------------------------
# scorecard — the honest, date-clustered read
# ---------------------------------------------------------------------------

def _thin(pairs: list, horizon: int) -> list:
    """Keep one value per ≥horizon-business-day window so the kept series is ~independent (each
    pick is graded over an overlapping forward window; naive pooling would be overconfident)."""
    try:
        import numpy as np
        import pandas as pd
    except Exception:
        return [v for _, v in sorted(pairs, key=lambda x: x[0])]
    kept, last = [], None
    for d, v in sorted(pairs, key=lambda x: x[0]):
        try:
            ts = pd.Timestamp(d)
        except Exception:
            continue
        if last is None or int(np.busday_count(last.date(), ts.date())) >= horizon:
            kept.append(v)
            last = ts
    return kept


def scorecard(asof: str | date | None = None) -> dict:
    """At-a-glance accountability for the dashboard + the Brain's feedback: hit-rate, avg rel-return,
    a per-conviction breakdown, weight-IC (the PRIMARY sizing-skill metric for Heavyweight), and the
    book-edge (weight-weighted rel-return per date) with a Newey-West t over independent windows.
    'building' until enough resolves."""
    ledger = _load_ledger()
    res = [r for r in ledger if r.get("status") == "resolved" and r.get("rel_return") is not None]
    out = {"status": "building", "n_resolved": len(res), "n_open": len(ledger) - len(res),
           "horizon_d": HORIZON_D, "hit_rate": None, "avg_rel_return": None,
           "by_conviction": {}, "weight_ic": None, "book_edge_mean": None,
           "book_edge_t": None, "book_edge_significant": None,
           "effective_n": 0, "n_decision_dates": 0}
    if not res:
        return out

    rels = [r["rel_return"] for r in res]
    out["hit_rate"] = round(sum(1 for x in rels if x > 0) / len(rels), 3)
    out["avg_rel_return"] = round(sum(rels) / len(rels), 4)

    for band in ("high", "medium", "low"):
        b = [r["rel_return"] for r in res if (r.get("conviction") or "high") == band]
        if b:
            out["by_conviction"][band] = {"n": len(b),
                                          "hit_rate": round(sum(1 for x in b if x > 0) / len(b), 3),
                                          "avg_rel": round(sum(b) / len(b), 4)}

    # book edge per decision date = weight-weighted rel-return (did the day's ALLOCATION beat SPY)
    by_date: dict[str, list] = defaultdict(list)
    for r in res:
        by_date[r["asof"]].append((float(r.get("weight") or 0.0), r["rel_return"]))
    edge_pairs = []
    for d, lst in by_date.items():
        wsum = sum(w for w, _ in lst)
        if wsum > 0:
            edge_pairs.append((d, sum(w * rel for w, rel in lst) / wsum))
    out["n_decision_dates"] = len(edge_pairs)
    edges = _thin(edge_pairs, HORIZON_D)
    out["effective_n"] = len(edges)
    if edges:
        out["book_edge_mean"] = round(sum(edges) / len(edges), 4)
        try:
            import pandas as pd
            from engine.validation import newey_west_tstat
            if len(edges) >= 4:
                nw = newey_west_tstat(pd.Series(edges), lags=2)
                t, se, mean = nw.get("t"), nw.get("se"), nw.get("mean")
                out["book_edge_t"] = round(t, 2) if t is not None else None
                out["book_edge_significant"] = bool(
                    mean is not None and se is not None and (mean - 1.96 * se) > 0)
        except Exception:
            pass

    # weight-IC: does Heavyweight size winners bigger? The CORE skill metric for a concentrator
    # (pooled rank-IC of final enforced weight vs realized rel-return vs SPY).
    try:
        import pandas as pd
        from engine.validation import rank_ic
        ic = rank_ic(pd.Series([float(r.get("weight") or 0.0) for r in res]), pd.Series(rels))
        out["weight_ic"] = round(float(ic), 3) if ic == ic else None   # NaN-safe
    except Exception:
        pass

    out["status"] = "scoring" if out["effective_n"] >= 4 else "building"
    if isinstance(asof, date):
        out["as_of"] = asof.isoformat()
    else:
        out["as_of"] = asof or date.today().isoformat()
    return out


def prompt_line(asof: str | date | None = None) -> str:
    """A one-line track-record summary fed back into the Brain's daily prompt so it sees its own
    realized sizing edge and self-corrects (the perception-to-outcome loop)."""
    sc = scorecard(asof)
    if not sc.get("n_resolved"):
        return f"Track record: no picks graded yet ({sc.get('n_open', 0)} in progress, {sc['horizon_d']}d horizon)."
    parts = [f"{sc['n_resolved']} picks graded vs SPY",
             f"hit-rate {sc['hit_rate'] * 100:.0f}%",
             f"avg rel {sc['avg_rel_return'] * 100:+.1f}%"]
    if sc.get("book_edge_mean") is not None:
        sig = " (sig)" if sc.get("book_edge_significant") else ""
        parts.append(f"book edge {sc['book_edge_mean'] * 100:+.1f}%/date{sig}")
    byc = sc.get("by_conviction") or {}
    if byc.get("high") and byc.get("low"):
        parts.append(f"high-conv hit {byc['high']['hit_rate'] * 100:.0f}% vs low-conv {byc['low']['hit_rate'] * 100:.0f}%")
    if sc.get("weight_ic") is not None:
        parts.append(f"sizing skill (weight-IC) {sc['weight_ic']:+.2f}")
    return "Track record: " + ", ".join(parts) + "."


def summary(asof: str | date | None = None) -> dict:
    """Coverage + scorecard for /api/heavyweight/outcomes."""
    return {"scorecard": scorecard(asof), "horizon_d": HORIZON_D}
