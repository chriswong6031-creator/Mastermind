"""Empirical calibration — the safe, auto-closing self-learning loop.

Each reasoning agent states a confidence. Calibration measures whether that confidence MATCHED
realized outcomes and emits a per-agent multiplier in (FLOOR, 1.0] that SHRINKS overconfidence
toward reality (de-confidencing only — it never inflates). It is the *safe* half of self-learning:
it changes NO prompts, NO sizing rules, NO permissions — it only re-weights confidence, is bounded
and reversible, and stays inert until an agent has MIN_N resolved decisions (an auditor with 10
trades has opinions; with 100 timestamped ones it has evidence).

  FORGE     graded from the thesis ledger (prob_correct) vs the realized falsifier outcome.
  SENTINEL  graded from committee artifacts (stance + confidence) vs the name's realized rel-return:
            an OPPOSE that preceded underperformance was right; a SUPPORT that preceded
            outperformance was right.

`multiplier(agent)` is what agents read at decision time to de-confidence themselves; the loop is
refreshed (recomputed + persisted) each build from the latest resolved outcomes.
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_PATH = _ROOT / "data" / "brain" / "calibration.json"
_COMMITTEE = _ROOT / "data" / "committee"
_GATE_OFFICER = _ROOT / "data" / "gate_officer"        # data/gate_officer/<asof>/decisions.json
_RISK_OFFICER = _ROOT / "data" / "risk_officer"        # data/risk_officer/<asof>/decisions.json

MIN_N = 12          # below this many resolved decisions, do not adjust (cold-start safety)
FLOOR = 0.5         # never shrink confidence below half (bounded)
_HORIZON = 21       # forward business-day window the seat decisions are graded over


def _mult(reliability: float | None, mean_conf: float | None, n: int) -> float:
    """De-confidence-only multiplier: shrink toward realized reliability, clamp to [FLOOR, 1.0]."""
    if n < MIN_N or not mean_conf or reliability is None:
        return 1.0
    return round(max(FLOOR, min(1.0, reliability / mean_conf)), 3)


def _summarize(rows: list[tuple[int, float]]) -> dict:
    n = len(rows)
    if not n:
        return {"n": 0, "reliability": None, "mean_confidence": None,
                "multiplier": 1.0, "status": "building"}
    rel = sum(o for o, _ in rows) / n
    mc = sum(c for _, c in rows) / n
    return {"n": n, "reliability": round(rel, 3), "mean_confidence": round(mc, 3),
            "multiplier": _mult(rel, mc, n), "status": "scoring" if n >= MIN_N else "building"}


def _forge_reliability(asof: date) -> dict:
    """FORGE: realized falsifier hit-rate vs stated prob_correct, over resolved theses (excluding
    PM-CONVICTION champions, which are graded separately under the `pm` seat).

    The shared body grades against the RAW (pre-de-confidencing) probability so the loop measures the
    model's native overconfidence and converges to a fixed point — grading the already-shrunk value
    would oscillate (shrink → look calibrated → un-shrink → overconfident again)."""
    return _ledger_reliability(asof, lambda t: not _is_pm_thesis(t))


def _sentinel_reliability(asof: date) -> dict:
    """SENTINEL: was its directional stance right? OPPOSE→correct if the name underperformed;
    SUPPORT→correct if it outperformed. CONDITIONAL is non-directional and excluded."""
    if not _COMMITTEE.exists():
        return _summarize([])
    from brain import outcomes
    from brain.ledger import all_theses
    idx = {}
    for t in all_theses():
        subj = ((t.get("entry_levels") or {}).get("ticker") or t.get("subject") or "").upper()
        d = str(t.get("state_asof") or "")[:10]
        if subj and d:
            idx[(subj, d)] = t
    rows: list[tuple[int, float]] = []
    try:
        for datedir in _COMMITTEE.iterdir():
            if not datedir.is_dir():
                continue
            for tdir in datedir.iterdir():
                sf = tdir / "sentinel.json"
                if not sf.exists():
                    continue
                try:
                    s = json.loads(sf.read_text())
                except Exception:  # noqa: BLE001
                    continue
                stance, conf = s.get("stance"), s.get("raw_confidence", s.get("confidence"))
                if stance not in ("OPPOSE", "SUPPORT") or conf is None:
                    continue
                th = idx.get((tdir.name.upper(), datedir.name))
                if not th:
                    continue
                lab = outcomes.label_thesis(th, asof)
                if not (lab and lab.get("resolved") and lab.get("rel_return") is not None):
                    continue
                r = lab["rel_return"]
                correct = (stance == "OPPOSE" and r < 0) or (stance == "SUPPORT" and r >= 0)
                rows.append((1 if correct else 0, float(conf)))
    except Exception:  # noqa: BLE001 — calibration is best-effort, never fatal
        pass
    return _summarize(rows)


# ───────────────────────── P3: per-role grading for the Flagship seats ─────────────────────────
# Each of the four desk seats (STRATEGIST / PM-CONVICTION / GATE OFFICER / RISK OFFICER) is graded
# against realized outcomes and emits its own de-confidence multiplier — identical FLOOR/MIN_N/_mult
# math as FORGE/SENTINEL. The seats that have no thesis dict (strategist members, gate, risk) reuse
# `outcomes.label_thesis` via a synthetic thesis so price-fetch + leakage logic is never re-implemented.


def _label_name(ticker: str, asof_iso: str, asof: date | None = None, horizon: int = _HORIZON,
                vs: str = "SPY"):
    """Label a bare ticker's forward rel_return vs `vs` (default SPY) anchored on `asof_iso`, reusing
    `outcomes.label_thesis` (leakage-free: it caps the window at `asof`). Returns the label dict or
    None. The synthetic thesis carries a rel_return falsifier so label_thesis grades it."""
    from brain import outcomes
    return outcomes.label_thesis({
        "id": f"{asof_iso}-{ticker}",
        "state_asof": asof_iso,
        "entry_levels": {"ticker": ticker},
        "falsifier": {"check": {"kind": "rel_return", "subject_ticker": ticker,
                                "vs": vs, "horizon_d": horizon}},
    }, asof)  # asof caps the price window internally (None → today); leakage-free


def _resolved_rel(ticker: str, decision_iso: str, asof: date | None = None):
    """Return the resolved forward rel_return for `ticker` measured from `decision_iso`, or None if
    the window has not fully elapsed / price is unavailable. Common to gate + risk + strategist."""
    try:
        lab = _label_name(ticker, decision_iso, asof)
    except Exception:  # noqa: BLE001
        return None
    if not (lab and lab.get("resolved") and lab.get("rel_return") is not None):
        return None
    return float(lab["rel_return"])


def _elapsed(d_iso: str, asof: date) -> bool:
    """True only if the decision date is strictly before asof (a fully-elapsed forward window is
    required; same-day or future decisions are leakage and excluded)."""
    try:
        return date.fromisoformat(str(d_iso)[:10]) < asof
    except Exception:  # noqa: BLE001
        return False


def _ledger_reliability(asof: date, pred) -> dict:
    """Shared FORGE-shaped grader over the thesis ledger, filtered by `pred(thesis) -> bool`.
    `_forge_reliability` and `_pm_reliability` are both this body with different predicates."""
    from brain import outcomes
    from brain.ledger import all_theses
    rows: list[tuple[int, float]] = []
    for t in all_theses():
        if not pred(t):
            continue
        cb = t.get("check_by")
        try:
            due = cb and date.fromisoformat(str(cb)[:10]) <= asof
        except Exception:  # noqa: BLE001
            due = False
        if not due:
            continue
        lab = outcomes.label_thesis(t, asof)
        if not (lab and lab.get("resolved") and lab.get("rel_return") is not None):
            continue
        pc = t.get("raw_prob_correct", t.get("prob_correct"))
        if pc is None:
            continue
        chk = (t.get("falsifier") or {}).get("check") or {}
        r, thr, op = lab["rel_return"], chk.get("threshold", 0), chk.get("op", "<")
        miss = (r < thr) if op == "<" else (r > thr)
        rows.append((0 if miss else 1, float(pc)))
    return _summarize(rows)


def _is_pm_thesis(t: dict) -> bool:
    """PM-CONVICTION champions land in theses.jsonl with id '{asof}-{ticker}-conv' (and/or a
    pm_judgment source tag from judgment_book)."""
    tid = str(t.get("id") or t.get("thesis_id") or "")
    return tid.endswith("-conv") or str(t.get("source") or "") in ("pm_judgment", "flagship_judgment")


def _strategist_reliability(asof: date) -> dict:
    """STRATEGIST: did each CONFIRMED-leadership theme play out — did its member basket beat SPY?
    One row per (confirmed theme, date): outcome = 1 iff the MEDIAN member rel_return > 0; confidence
    is the stated theme `leadership` (0–1, default 0.5). Date-clustered by construction."""
    if not _COMMITTEE.exists():
        return _summarize([])
    rows: list[tuple[int, float]] = []
    try:
        for datedir in sorted(_COMMITTEE.iterdir()):
            if not datedir.is_dir() or not _elapsed(datedir.name, asof):
                continue
            sf = datedir / "_FLAGSHIP" / "strategist.json"
            if not sf.exists():
                continue
            try:
                j = json.loads(sf.read_text())
            except Exception:  # noqa: BLE001
                continue
            verdict = j.get("verdict") or {}
            for t in (verdict.get("confirmed_themes") or []):
                if str(t.get("stage") or "").lower() != "confirmed":
                    continue
                rels = []
                for tk in (t.get("names") or []):
                    r = _resolved_rel(str(tk).upper().strip(), datedir.name, asof)
                    if r is not None:
                        rels.append(r)
                if not rels:
                    continue
                rels.sort()
                m = len(rels)
                median = rels[m // 2] if m % 2 else (rels[m // 2 - 1] + rels[m // 2]) / 2.0
                lead = t.get("leadership")
                conf = float(lead) if lead is not None else 0.5
                rows.append((1 if median > 0 else 0, conf))
    except Exception:  # noqa: BLE001
        pass
    return _summarize(rows)


def _pm_reliability(asof: date) -> dict:
    """PM-CONVICTION: hit-rate of its champions' theses (rel_return vs falsifier) — identical shape
    to FORGE, filtered to PM-authored theses."""
    return _ledger_reliability(asof, _is_pm_thesis)


def _seat_decision_reliability(root: Path, asof: date, actions: tuple) -> dict:
    """Shared GATE/RISK grader. Reads <root>/<d>/decisions.json, grades every decision whose action
    is in `actions`. A 'good' veto/exit ⇔ the name's forward rel_return < 0 (it underperformed /
    kept falling). Confidence is implicit 1.0 (the seat acts or it doesn't) → _mult = reliability."""
    if not root.exists():
        return _summarize([])
    rows: list[tuple[int, float]] = []
    try:
        for datedir in sorted(root.iterdir()):
            if not datedir.is_dir() or not _elapsed(datedir.name, asof):
                continue
            df = datedir / "decisions.json"
            if not df.exists():
                continue
            try:
                j = json.loads(df.read_text())
            except Exception:  # noqa: BLE001
                continue
            for dec in ((j.get("result") or {}).get("decisions") or []):
                if str(dec.get("action") or "").lower() not in actions:
                    continue
                tk = str(dec.get("ticker") or "").upper().strip()
                if not tk:
                    continue
                r = _resolved_rel(tk, datedir.name, asof)
                if r is None:
                    continue
                rows.append((1 if r < 0 else 0, 1.0))
    except Exception:  # noqa: BLE001
        pass
    return _summarize(rows)


def _gate_reliability(asof: date) -> dict:
    """GATE OFFICER veto precision: a VETO/WITHHOLD was right iff the name underperformed SPY."""
    return _seat_decision_reliability(_GATE_OFFICER, asof, ("veto", "withhold"))


def _risk_reliability(asof: date) -> dict:
    """RISK OFFICER exit timing: an EXIT was right iff the name kept falling vs SPY post-exit."""
    return _seat_decision_reliability(_RISK_OFFICER, asof, ("exit",))


# ───────────────────── BRAIN BOOKS: free-form per-book self-grading ─────────────────────
# The free-form Brain books (US/autonomous, china, hk, heavyweight) submit a COMPLETE target book
# each trading day, logged to data/portfolios/<id>/decisions.jsonl. We grade each book's own held
# names forward — every holding's rel_return vs the book's benchmark (SPY for US books, FXI for the
# China/HK books) over the same 21-business-day horizon, leakage-free via `_label_name`. A holding is
# "right" iff it beat its benchmark (rel_return >= 0); confidence is the brain's stated `conviction`
# if numeric, else 1.0. Same _summarize/_mult math, identical FLOOR/MIN_N, cold-start inert.

_PORTFOLIOS = _ROOT / "data" / "portfolios"

# map a free-text conviction tag to a confidence in (0, 1]; numeric convictions pass through clamped.
_CONVICTION = {"high": 0.9, "medium": 0.6, "med": 0.6, "low": 0.4}


def _conviction_conf(v) -> float:
    if isinstance(v, (int, float)):
        return max(0.0, min(1.0, float(v)))
    return _CONVICTION.get(str(v or "").strip().lower(), 1.0)


def _book_decisions(portfolio_id: str):
    """Yield decision rows for a Brain book, newest-irrelevant. Best-effort; [] if missing."""
    p = _PORTFOLIOS / portfolio_id / "decisions.jsonl"
    if not p.exists():
        return
    try:
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except Exception:  # noqa: BLE001
                continue
    except Exception:  # noqa: BLE001
        return


def _book_reliability(asof: date, portfolio_id: str, benchmark: str) -> dict:
    """Grade a free-form Brain book's submitted holdings forward vs its benchmark. One row per
    (held name, decision date) whose 21d window has fully elapsed: outcome = beat benchmark
    (rel_return >= 0), confidence = stated conviction. Reuses `_label_name` (leakage-free)."""
    rows: list[tuple[int, float]] = []
    try:
        for row in _book_decisions(portfolio_id):
            d_iso = str(row.get("asof") or "")[:10]
            if not d_iso or not _elapsed(d_iso, asof):
                continue
            for h in (row.get("holdings") or []):
                tk = str(h.get("ticker") or "").upper().strip()
                if not tk:
                    continue
                try:
                    lab = _label_name(tk, d_iso, asof, vs=benchmark)
                except Exception:  # noqa: BLE001
                    continue
                if not (lab and lab.get("resolved") and lab.get("rel_return") is not None):
                    continue
                r = float(lab["rel_return"])
                rows.append((1 if r >= 0 else 0, _conviction_conf(h.get("conviction"))))
    except Exception:  # noqa: BLE001 — calibration never fatal
        pass
    return _summarize(rows)


# ───────────────────────── FAST-ARM: universe-fed reliability ─────────────────────────
# Owned-book grading is starved: ~7-23 names resolve one thesis each per ~month, so a seat needs
# many months to clear MIN_N. The macro engine already logs a forward, leakage-free label on ~1,600
# names every build (portfolio/predictions.py). The seats whose calls map onto bare tickers —
# STRATEGIST (confirmed-theme MEMBER tickers), PM-CONVICTION (single-name picks), and the entry-TIMING
# read ('up' directional calls) — can therefore be graded against that universe log instead, reaching
# effective-n in WEEKS. We keep owned-book grading too and prefer whichever has higher effective-n,
# recording BOTH n's so the chosen path is honest. Same _summarize/_mult machinery; cold-start inert.


def _pred_resolved_index() -> dict:
    """{(TICKER, entry_iso): realized_rel} over the resolved predictions ledger. One read; best-effort
    — an unavailable/empty ledger yields {} (the universe path then simply stays cold)."""
    try:
        from portfolio import predictions
        idx: dict = {}
        for r in predictions._load_ledger():
            if r.get("status") != "resolved" or r.get("realized") is None:
                continue
            tk = str(r.get("ticker") or "").upper().strip()
            d = str(r.get("asof") or "")[:10]
            if tk and d:
                idx[(tk, d)] = float(r["realized"])
        return idx
    except Exception:  # noqa: BLE001
        return {}


def _cluster(rows_by_date: dict) -> dict:
    """Collapse per-date outcome/confidence lists to ONE independent observation per ~horizon window
    (reusing predictions._thin_independent so the kept clusters are genuinely non-overlapping), then
    _summarize. `rows_by_date` maps date_iso -> list[(outcome 0|1, confidence)]. The thinned series'
    length is the effective-n (true bound on power); a dense daily ledger does not inflate it."""
    try:
        from portfolio import predictions
    except Exception:  # noqa: BLE001
        return {**_summarize([]), "effective_n": 0}
    if not rows_by_date:
        return {**_summarize([]), "effective_n": 0}
    # one (outcome_mean, conf_mean) per date, then thin to independent date-clusters
    pairs_o = [(d, sum(o for o, _ in v) / len(v)) for d, v in rows_by_date.items() if v]
    pairs_c = {d: sum(c for _, c in v) / len(v) for d, v in rows_by_date.items() if v}
    kept_dates = _kept_dates(predictions, pairs_o)
    # Each kept date is ONE independent cluster; its outcome is the date-mean hit-rate (a FRACTION in
    # [0,1]) — keeping it fractional preserves the within-cluster breadth precision the universe buys
    # us, rather than discarding it by rounding. _summarize averages these fractions into reliability.
    rows = [(om, pairs_c[d]) for d, om in pairs_o if d in kept_dates]
    s = _summarize(rows)
    s["effective_n"] = len(rows)
    return s


def _kept_dates(predictions, pairs_o: list) -> set:
    """Run predictions._thin_independent but recover WHICH dates survived (it returns values only).
    We tag each value with its date by re-deriving the greedy keep-set deterministically."""
    import numpy as np
    import pandas as pd
    kept, last = set(), None
    for d, _ in sorted(pairs_o, key=lambda x: x[0]):
        try:
            ts = pd.Timestamp(d)
        except Exception:  # noqa: BLE001
            continue
        if last is None or int(np.busday_count(last.date(), ts.date())) >= predictions._HORIZON:
            kept.add(d)
            last = ts
    return kept


def _universe_strategist(asof: date, idx: dict) -> dict:
    """STRATEGIST via the universe log: one row per (confirmed-theme MEMBER ticker, decision date),
    outcome = member beat SPY (realized > 0), confidence = theme `leadership`. Members not yet in the
    resolved predictions ledger are skipped. Date-clustered to independent windows."""
    if not _COMMITTEE.exists() or not idx:
        return _summarize([])
    by_date: dict = {}
    try:
        for datedir in sorted(_COMMITTEE.iterdir()):
            if not datedir.is_dir() or not _elapsed(datedir.name, asof):
                continue
            sf = datedir / "_FLAGSHIP" / "strategist.json"
            if not sf.exists():
                continue
            try:
                j = json.loads(sf.read_text())
            except Exception:  # noqa: BLE001
                continue
            for t in ((j.get("verdict") or {}).get("confirmed_themes") or []):
                if str(t.get("stage") or "").lower() != "confirmed":
                    continue
                lead = t.get("leadership")
                conf = float(lead) if lead is not None else 0.5
                for tk in (t.get("names") or []):
                    rel = idx.get((str(tk).upper().strip(), datedir.name))
                    if rel is None:
                        continue
                    by_date.setdefault(datedir.name, []).append((1 if rel > 0 else 0, conf))
    except Exception:  # noqa: BLE001
        pass
    return _cluster(by_date)


def _universe_pm(asof: date, idx: dict) -> dict:
    """PM-CONVICTION via the universe log: one row per (PM-authored thesis ticker, decision date),
    outcome = name beat SPY, confidence = stated prob_correct. Falls back to owned-book if a name is
    not in the universe ledger."""
    if not idx:
        return _summarize([])
    try:
        from brain.ledger import all_theses
    except Exception:  # noqa: BLE001
        return _summarize([])
    by_date: dict = {}
    for th in all_theses():
        if not _is_pm_thesis(th):
            continue
        tk = ((th.get("entry_levels") or {}).get("ticker") or th.get("subject") or "").upper().strip()
        d = str(th.get("state_asof") or "")[:10]
        if not (tk and d) or not _elapsed(d, asof):
            continue
        rel = idx.get((tk, d))
        if rel is None:
            continue
        pc = th.get("raw_prob_correct", th.get("prob_correct"))
        conf = float(pc) if pc is not None else 0.5
        by_date.setdefault(d, []).append((1 if rel > 0 else 0, conf))
    return _cluster(by_date)


def _universe_timing(asof: date, idx: dict) -> dict:
    """ENTRY-TIMING read via the universe log: did the engine's 'up' directional calls actually go up
    vs SPY? One row per ('up' name, entry date) straight from the predictions ledger; confidence is
    the ledger's stated prob. This is the purest universe-native seat (no committee join needed)."""
    if not idx:
        return _summarize([])
    by_date: dict = {}
    try:
        from portfolio import predictions
        for r in predictions._load_ledger():
            if r.get("status") != "resolved" or r.get("realized") is None:
                continue
            if str(r.get("dir") or "") != "up":
                continue
            d = str(r.get("asof") or "")[:10]
            if not d or not _elapsed(d, asof):
                continue
            prob = r.get("prob")
            conf = float(prob) if prob is not None else 0.5
            by_date.setdefault(d, []).append((1 if float(r["realized"]) > 0 else 0, conf))
    except Exception:  # noqa: BLE001
        pass
    return _cluster(by_date)


def _prefer(owned: dict, universe: dict) -> dict:
    """Combine the owned-book and universe-fed reliabilities for one seat: PREFER whichever has the
    higher effective independent sample (universe-fed effective_n vs owned-book n), but record BOTH so
    the chosen path is auditable. The returned block carries `source`, `n`/effective fields, and the
    two sub-blocks under `paths`. Cold-start inertia is preserved (chosen multiplier 1.0 below MIN_N)."""
    u_n = universe.get("effective_n", 0) or 0
    o_n = owned.get("n", 0) or 0
    chosen, source = (universe, "universe") if u_n > o_n else (owned, "owned_book")
    out = dict(chosen)
    out["source"] = source
    out["owned_n"] = o_n
    out["universe_effective_n"] = u_n
    out["paths"] = {"owned_book": owned, "universe": universe}
    return out


def compute(asof: date | None = None) -> dict:
    asof = asof or date.today()

    def _safe(fn):
        try:
            return fn(asof)
        except Exception:  # noqa: BLE001 — calibration never raises
            return _summarize([])

    # one shared read of the resolved predictions ledger for all universe-fed seats
    try:
        pred_idx = _pred_resolved_index()
    except Exception:  # noqa: BLE001
        pred_idx = {}

    def _fast_arm(owned_fn, universe_fn):
        try:
            return _prefer(_safe(owned_fn), universe_fn(asof, pred_idx))
        except Exception:  # noqa: BLE001
            return _safe(owned_fn)

    return {"as_of": asof.isoformat(), "min_n": MIN_N, "floor": FLOOR,
            "agents": {"forge": _safe(_forge_reliability),
                       "sentinel": _safe(_sentinel_reliability),
                       "strategist": _fast_arm(_strategist_reliability, _universe_strategist),
                       "pm": _fast_arm(_pm_reliability, _universe_pm),
                       "timing": _fast_arm(lambda a: _summarize([]), _universe_timing),
                       "gate": _safe(_gate_reliability),
                       "risk": _safe(_risk_reliability),
                       # free-form Brain books — graded vs their own benchmark (SPY / FXI)
                       "autonomous": _safe(lambda d: _book_reliability(d, "autonomous", "SPY")),
                       "heavyweight": _safe(lambda d: _book_reliability(d, "heavyweight", "SPY")),
                       "china": _safe(lambda d: _book_reliability(d, "china", "FXI")),
                       "hk": _safe(lambda d: _book_reliability(d, "hk", "FXI"))}}


def persist(asof: date | None = None) -> dict:
    """Recompute + write calibration.json. Returns the computed block. Never raises."""
    block = compute(asof)
    try:
        _PATH.parent.mkdir(parents=True, exist_ok=True)
        _PATH.write_text(json.dumps(block, indent=2))
    except Exception:  # noqa: BLE001
        pass
    return block


def load() -> dict:
    try:
        return json.loads(_PATH.read_text())
    except Exception:  # noqa: BLE001
        return {}


def multiplier(agent: str) -> float:
    """The de-confidencing multiplier an agent applies to its stated confidence (1.0 if unknown)."""
    try:
        m = ((load().get("agents") or {}).get(agent) or {}).get("multiplier")
        return float(m) if m is not None else 1.0
    except Exception:  # noqa: BLE001
        return 1.0
