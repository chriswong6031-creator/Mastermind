"""Universe-wide forward prediction log + date-clustered cross-sectional scoring — the sample unlock.

A labeled forward prediction does NOT require owning the name. The engine already forms a directional
opinion (`ladder.dir` / `ladder.score`) on ~1,600 names every build; logging + forward-labeling ALL
of them — not just the ~7 owned — turns a handful of resolved theses per *month* into hundreds, so
calibration and edge-measurement reach statistical power in *weeks*, not years.

Three invariants make it honest:
  * LEAKAGE-FREE — every prediction is graded ONLY on prices at/after its own entry date, capped at
    asof (never reads a close after asof). Same rel-return definition as brain.outcomes.
  * ISOLATED — writes only under data/shadow/predictions/; never the prod conviction ledger or its
    Brier track record (mixing 1,600 low-conviction breadth bets into that would corrupt it).
  * DATE-CLUSTERED STATS — predictions from one build share a regime, so they are NOT independent.
    We reuse the macro engine's validation helpers (rank_ic / ic_summary / newey_west_tstat /
    brier_reliability) so every metric carries an autocorrelation-aware CI and an effective-n
    (# distinct entry-date clusters), never a raw n=1,600 that would massively overstate confidence.

Price source: the macro breadth panel (`_closes_deep` + `_closes_delisted`, ~1,500 names, local,
survivorship-safe) — one parquet load, vectorized, NO per-name network calls. Best-effort: a name
absent from the panel simply stays unresolved.
"""
from __future__ import annotations

import glob
import json
from collections import defaultdict
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_PRED_DIR = _ROOT / "data" / "shadow" / "predictions"
_LEDGER = _PRED_DIR / "ledger.jsonl"
_STOCKDATA = _ROOT / "vendor" / "macro" / "site" / "stockdata"
_BREADTH = _ROOT / "vendor" / "macro" / "data" / "breadth"

_HORIZON = 21          # business days — matches the conviction sleeve falsifier
_MAX_RESOLVED = 60_000  # soft cap so the ledger can't grow without bound
_MIN_NAMES_PER_DATE = 10   # rank_ic needs a real cross-section
_MIN_DATES = 8             # INDEPENDENT (non-overlapping) clusters; also the newey_west n-floor
_HAC_LAGS = 2              # residual-correlation guard AFTER thinning to non-overlapping windows

_panel = None          # cached (DataFrame, dict-of-Series) — loaded once per process
_panel_tried = False
_spy = None
_spy_tried = False


def _g(d: dict, k: str) -> dict:
    v = d.get(k)
    return v if isinstance(v, dict) else {}


def _prob(score) -> float:
    """A cheap, monotone confidence from ladder.score∈[-100,100] → [0.35,0.85]. No LLM."""
    try:
        return round(max(0.35, min(0.85, 0.5 + 0.25 * (float(score) / 100.0))), 3)
    except (TypeError, ValueError):
        return 0.5


# ─────────────────────────────────────────────────────────────────────────────
# the engine's per-name directional universe (already computed — zero new compute)
# ─────────────────────────────────────────────────────────────────────────────
def universe() -> list:
    """Every name the engine has a usable directional opinion on: {ticker, dir, score, band, price}."""
    out = []
    for f in glob.glob(str(_STOCKDATA / "*.json")):
        try:
            d = json.loads(Path(f).read_text())
        except Exception:  # noqa: BLE001
            continue
        if not isinstance(d, dict):
            continue
        lad, tech, conv = _g(d, "ladder"), _g(d, "tech"), _g(d, "conviction")
        direction, px = lad.get("dir"), tech.get("price")
        if direction in ("up", "down", "caution") and px:
            out.append({"ticker": Path(f).stem.upper(), "dir": direction,
                        "score": lad.get("score"), "band": conv.get("band"), "price": px})
    return out


# ─────────────────────────────────────────────────────────────────────────────
# price panel + vectorized, leakage-free labeler
# ─────────────────────────────────────────────────────────────────────────────
def _load_panel():
    """Cached daily-close panel (deep + delisted, survivorship-safe) as {TICKER: Series}."""
    global _panel, _panel_tried
    if _panel_tried:
        return _panel
    _panel_tried = True
    try:
        import pandas as pd
        frames = []
        for name in ("_closes_deep", "_closes_delisted"):
            fp = _BREADTH / f"{name}.parquet"
            if fp.exists():
                frames.append(pd.read_parquet(fp))
        if not frames:
            _panel = None
            return None
        df = pd.concat(frames, axis=1)
        df = df.loc[:, ~df.columns.duplicated()]      # dedup any overlap (deep wins)
        df.index = pd.to_datetime(df.index)
        df = df.sort_index()
        _panel = {str(c).upper(): df[c].dropna() for c in df.columns}
    except Exception:  # noqa: BLE001
        _panel = None
    return _panel


def _spy_series():
    global _spy, _spy_tried
    if _spy_tried:
        return _spy
    _spy_tried = True
    try:
        import pandas as pd
        from engine import equity_alloc as ea
        s = ea.index_close("SPY")
        s.index = pd.to_datetime(s.index)
        _spy = s[s > 0].sort_index()
    except Exception:  # noqa: BLE001
        _spy = None
    return _spy


def _label(panel: dict, spy, ticker: str, entry_iso: str, horizon: int, asof_iso: str):
    """rel_return (subject − SPY over `horizon` trading days from entry), or None if unresolved.

    Look-ahead-safe: only common trading dates ≤ asof are used; both baselines anchor to the SAME
    last common close ≤ entry; resolves only once `horizon` trading days have actually elapsed."""
    try:
        import pandas as pd
        s = panel.get((ticker or "").upper())
        if s is None or spy is None:
            return None
        asof_ts, entry_ts = pd.Timestamp(asof_iso), pd.Timestamp(entry_iso)
        s = s[(s.index <= asof_ts) & (s > 0)]
        sp = spy[(spy.index <= asof_ts) & (spy > 0)]
        common = s.index.intersection(sp.index)
        if len(common) < 2:
            return None
        pre = common[common <= entry_ts]
        if len(pre) == 0:
            return None
        anchor = pre[-1]
        post = common[common >= anchor]
        if len(post) <= horizon:                       # horizon not fully elapsed → unresolved
            return None
        exit_ts = post[horizon]
        p0s, p0v = float(s[anchor]), float(sp[anchor])
        if p0s <= 0 or p0v <= 0:
            return None
        rel = (float(s[exit_ts]) / p0s - 1.0) - (float(sp[exit_ts]) / p0v - 1.0)
        return round(rel, 4)
    except Exception:  # noqa: BLE001
        return None


# ─────────────────────────────────────────────────────────────────────────────
# isolated prediction ledger
# ─────────────────────────────────────────────────────────────────────────────
def _load_ledger() -> list:
    try:
        return [json.loads(l) for l in _LEDGER.read_text().splitlines() if l.strip()]
    except Exception:  # noqa: BLE001
        return []


def _save_ledger(rows: list) -> None:
    # soft cap: keep all open + the most-recent resolved
    openr = [r for r in rows if r.get("status") == "open"]
    resr = [r for r in rows if r.get("status") != "open"]
    if len(resr) > _MAX_RESOLVED:
        resr = sorted(resr, key=lambda r: r.get("resolved_on") or "")[-_MAX_RESOLVED:]
    try:
        _PRED_DIR.mkdir(parents=True, exist_ok=True)
        _LEDGER.write_text("".join(json.dumps(r, default=str) + "\n" for r in (openr + resr)))
    except Exception:  # noqa: BLE001
        pass


def record(asof: str) -> dict:
    """Open one prediction per name the engine has an opinion on (deduped while open, like the prod
    ledger), then label every open prediction forward and resolve the matured ones. Returns coverage.
    Best-effort; never raises."""
    asof_iso = str(asof)[:10]
    ledger = _load_ledger()
    open_subj = {r["ticker"] for r in ledger if r.get("status") == "open"}
    for u in universe():
        tk = u["ticker"]
        if tk in open_subj:
            continue
        ledger.append({"id": f"{asof_iso}-{tk}-pred", "ticker": tk, "asof": asof_iso,
                       "dir": u["dir"], "score": u["score"], "band": u["band"],
                       "prob": _prob(u["score"]), "entry_px": u["price"], "horizon_d": _HORIZON,
                       "status": "open", "realized": None, "resolved_on": None})
        open_subj.add(tk)
    panel, spy = _load_panel(), _spy_series()
    if panel and spy is not None:
        for r in ledger:
            if r.get("status") != "open":
                continue
            rr = _label(panel, spy, r["ticker"], r["asof"], int(r.get("horizon_d") or _HORIZON), asof_iso)
            if rr is not None:
                r["status"], r["realized"], r["resolved_on"] = "resolved", rr, asof_iso
    _save_ledger(ledger)
    return coverage(ledger)


# ─────────────────────────────────────────────────────────────────────────────
# coverage + date-clustered cross-sectional scoring
# ─────────────────────────────────────────────────────────────────────────────
def coverage(ledger: list | None = None) -> dict:
    ledger = ledger if ledger is not None else _load_ledger()
    resolved = [r for r in ledger if r.get("status") == "resolved"]
    by_dir = defaultdict(int)
    for r in ledger:
        by_dir[r.get("dir")] += 1
    dates = sorted({r.get("asof") for r in ledger if r.get("asof")})
    res_dates = sorted({r.get("asof") for r in resolved})
    return {"n_total": len(ledger), "n_open": len(ledger) - len(resolved), "n_resolved": len(resolved),
            "by_dir": dict(by_dir), "n_entry_dates": len(dates), "n_resolved_dates": len(res_dates),
            "first_date": dates[0] if dates else None, "last_date": dates[-1] if dates else None}


def _ci(mean, se, z=1.96):
    if mean is None or se is None:
        return None
    try:
        return [round(mean - z * se, 4), round(mean + z * se, 4)]
    except Exception:  # noqa: BLE001
        return None


def _thin_independent(pairs: list) -> list:
    """Keep ONE observation per ~horizon window so the kept series is (approximately) independent.

    Predictions are entered near-daily but each is graded over a 21-business-day forward window, so
    adjacent entry-dates' per-date stats OVERLAP and are serially correlated — pooling them with a
    naive CI is overconfident (the exact bug the review caught). Thinning to ≥horizon-spaced dates
    makes each retained observation a genuinely independent time-cluster, so the pooled CI is honest
    and `effective_n` reflects the real number of independent clusters (the true bound on power —
    breadth makes each cluster precise but cannot manufacture independent clusters). `pairs` is a
    list of (date_iso, value); returns the kept values in date order. Greedy + deterministic."""
    import numpy as np
    import pandas as pd
    kept, last = [], None
    for d, v in sorted(pairs, key=lambda x: x[0]):
        try:
            ts = pd.Timestamp(d)
        except Exception:  # noqa: BLE001
            continue
        # non-overlapping ⇔ ≥ _HORIZON BUSINESS days since the last kept entry (windows are bday-based)
        if last is None or int(np.busday_count(last.date(), ts.date())) >= _HORIZON:
            kept.append(v)
            last = ts
    return kept


def score(asof: str | None = None) -> dict:
    """Cross-sectional, date-clustered scorecard over the resolved prediction log.

    - IC: per entry-date rank-IC of ladder.score vs realized rel-return, pooled with a HAC t-stat.
    - Directional: pooled hit-rate (dir matches sign of rel-return) + Brier of the stated prob,
      with a date-clustered CI.
    - up_edge: mean rel-return of 'up' calls per date, HAC t (does 'up' actually outperform?).
    Every metric reports effective-n = # of independent entry-date clusters, not raw prediction count.
    Returns an honest empty-but-valid block while it's still building."""
    out = {"status": "building", "ic": {}, "directional": {}, "up_edge": {},
           "effective_n": 0, "n_resolved": 0,
           "note": f"clustered to independent ~{_HORIZON}-business-day windows"}
    try:
        import numpy as np
        import pandas as pd
        from engine.validation import rank_ic, newey_west_tstat, brier_reliability

        ledger = _load_ledger()
        res = [r for r in ledger if r.get("status") == "resolved" and r.get("realized") is not None]
        out["n_resolved"] = len(res)
        if not res:
            return out

        # ── per-date rank-IC, then THIN to non-overlapping (independent) windows ──
        by_date = defaultdict(list)
        for r in res:
            if r.get("score") is not None:
                by_date[r["asof"]].append(r)
        ic_pairs = []
        for d in sorted(by_date):
            rows = by_date[d]
            if len(rows) >= _MIN_NAMES_PER_DATE:
                ic = rank_ic(pd.Series([x["score"] for x in rows]),
                             pd.Series([x["realized"] for x in rows]))
                if ic == ic:                          # not NaN
                    ic_pairs.append((d, ic))
        ics = _thin_independent(ic_pairs)
        out["effective_n"] = len(ics)
        if len(ics) >= _MIN_DATES:
            s = pd.Series(ics)
            mean_ic = float(s.mean())
            nw = newey_west_tstat(s, lags=_HAC_LAGS)
            se, t, p = nw.get("se"), nw.get("t"), nw.get("p")
            ci = _ci(mean_ic, se)
            ic_ir = round(mean_ic / float(s.std(ddof=1)), 3) if len(ics) > 1 and s.std(ddof=1) else None
            out["ic"] = {"mean_ic": round(mean_ic, 4),
                         "t_hac": round(t, 2) if t is not None else None,
                         "p_hac": round(p, 4) if p is not None else None,
                         "ic_ir": ic_ir, "ci95": ci, "n_dates": len(ics),
                         "significant": bool(p is not None and p < 0.05 and ci and ci[0] > 0)}
        else:
            out["ic"] = {"n_dates": len(ics), "note": "building"}

        # ── directional hit-rate (thinned per-date hit means) + Brier (point score) ──
        dirrows = [r for r in res if r.get("dir") in ("up", "down")]
        if dirrows:
            probs = np.array([r["prob"] for r in dirrows], dtype=float)
            outs = np.array([1 if ((r["dir"] == "up" and r["realized"] > 0) or
                                   (r["dir"] == "down" and r["realized"] < 0)) else 0
                             for r in dirrows], dtype=float)
            hbd = defaultdict(list)
            for r, o in zip(dirrows, outs):
                hbd[r["asof"]].append(o)
            hits = _thin_independent([(d, float(np.mean(v))) for d, v in hbd.items() if v])
            nw = newey_west_tstat(pd.Series(hits), lags=_HAC_LAGS) if len(hits) >= _MIN_DATES else {}
            br = brier_reliability(probs, outs) if len(probs) >= 30 else {}
            out["directional"] = {
                "hit_rate": round(float(outs.mean()), 3), "n": len(dirrows), "n_dates": len(hits),
                "hit_ci95": _ci(nw.get("mean"), nw.get("se")) if nw else None,
                "brier": round(br["brier"], 4) if br.get("brier") is not None else None,
                "brier_skill": round(br["skill_score"], 4) if br.get("skill_score") is not None else None,
                "beats_coin": bool(nw.get("mean") is not None and nw.get("se") is not None
                                   and (nw["mean"] - 1.96 * nw["se"]) > 0.5),
            }

        # ── does 'up' actually outperform? (thinned per-date mean rel-return, HAC t) ──
        ubd = defaultdict(list)
        for r in res:
            if r.get("dir") == "up":
                ubd[r["asof"]].append(r["realized"])
        ups = _thin_independent([(d, float(np.mean(v))) for d, v in ubd.items() if v])
        if len(ups) >= _MIN_DATES:
            s = pd.Series(ups)
            nw = newey_west_tstat(s, lags=_HAC_LAGS)
            ci = _ci(nw.get("mean"), nw.get("se"))
            out["up_edge"] = {"mean_rel": round(nw.get("mean"), 4) if nw.get("mean") is not None else None,
                              "t_hac": round(nw.get("t"), 2) if nw.get("t") is not None else None,
                              "p_hac": round(nw.get("p"), 4) if nw.get("p") is not None else None,
                              "ci95": ci, "n_dates": len(ups),
                              "significant": bool(nw.get("p") is not None and nw.get("p") < 0.05
                                                  and ci and ci[0] > 0)}

        out["status"] = "scoring" if out["effective_n"] >= _MIN_DATES else "building"
    except Exception as exc:  # noqa: BLE001
        out["error"] = str(exc)
    return out


def summary(asof: str | None = None) -> dict:
    """Coverage + cross-sectional scorecard for /api/predictions."""
    return {"coverage": coverage(), "scorecard": score(asof),
            "horizon_d": _HORIZON, "min_dates": _MIN_DATES}
