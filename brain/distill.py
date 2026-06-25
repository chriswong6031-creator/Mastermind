"""DISTILLATION (#3 v2) — a cheap CatBoost model that learns to MIMIC Opus's buy decisions.

The principle the user set: don't waste Opus on easy work. A cheap model that predicts P(Opus buys this
name) is how you operationalize that — once trained on enough of Opus's history it can (a) pre-screen the
obvious names so Opus is reserved for the genuinely hard calls, and (b) run a near-free shadow of the
Opus book. Training set: per Opus decision date, the candidate universe (the engine's directional names
that day, from the prediction log) labeled 1 if Opus INITIATED a position that day (a FRESH entry =
held(d) minus held(prev) — the genuine buy ACTION, not mere incumbency; a held-but-not-fresh name is
EXCLUDED, since you can't 'buy' what you already hold), with POINT-IN-TIME panel features (reuses
brain.student._pit_features — closes <= asof, leakage-free). WALK-FORWARD OOS AUC (train earlier
dates, score strictly later). Pure numeric — distilling Opus spends NO Opus. Degrade-safe (never raises).

HONESTLY data-starved: the Brain books began ~2026-06-21, so until months of Opus decisions accrue this
returns status 'building'. It is the forward-accruing foundation, not a usable model today. Read via
/api/distill; NOT injected back into Opus's prompt (predicting Opus to Opus is circular). The candidate
pool is the engine universe (a proxy for the free-form Brain's consideration set) — an approximation
noted here, refined in v3 once a true per-day consideration set is logged.
"""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import date
from pathlib import Path

from brain import student as _student

_ROOT = Path(__file__).resolve().parent.parent
_DIR = _ROOT / "data" / "brain" / "distill"
_MODEL = _DIR / "model.cbm"
_METRICS = _DIR / "metrics.json"

_BOOK = "autonomous"        # the US free-form Brain book (SPY benchmark; US universe ↔ the prediction log)
_MIN_POS = 60               # need this many BOUGHT examples before training (cold-start safety)
_MIN_DATES = 8              # and this many distinct decision dates for a real walk-forward split
_NEG_PER_POS = 6            # deterministic negative sampling cap per positive (class imbalance)
_FEATURES = _student._NUM_FEATURES + _student._CAT_FEATURES   # SAME PIT features as the return-student


def _auc(y, p) -> float | None:
    """Rank-based ROC-AUC (Mann-Whitney U), no sklearn dependency. None if one class is empty."""
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
        return round(float((ranks[y == 1].sum() - n_pos * (n_pos + 1) / 2.0) / (n_pos * n_neg)), 3)
    except Exception:  # noqa: BLE001
        return None


def _classifier():
    """Lazy CatBoostClassifier (None if catboost absent → degrade-safe, no hard dep)."""
    try:
        from catboost import CatBoostClassifier
        return CatBoostClassifier
    except Exception:  # noqa: BLE001
        return None


def _opus_holdings(book_id: str = _BOOK) -> list:
    """[(asof, set(tickers held))] in date order from the book's decision log. `holdings` is the COMPLETE
    submitted target book each day (per bot.autonomous + calibration._book_reliability), so this is the
    book STATE, not the buy ACTION — the fresh-entry delta is taken in _opus_buys / _dataset."""
    by_d: dict = {}
    try:
        from brain import calibration
        for row in calibration._book_decisions(book_id):
            d = str(row.get("asof") or "")[:10]
            if not d:
                continue
            by_d[d] = {(h.get("ticker") or "").upper().strip()
                       for h in (row.get("holdings") or []) if h.get("ticker")}
    except Exception:  # noqa: BLE001
        pass
    return sorted(by_d.items())


def _opus_buys(book_id: str = _BOOK) -> dict:
    """{asof: set(FRESH entries)} = held(d) − held(prior decision date) — the genuine BUY ACTION (a name
    held continuously is a buy ONCE, on its entry date; the first date's whole book is the inaugural buy).
    NOT the full holdings (that would learn incumbency/persistence, not the buy decision)."""
    out: dict = {}
    prev: set = set()
    for d, held in _opus_holdings(book_id):
        fresh = held - prev
        if fresh:
            out[d] = fresh
        prev = held
    return out


def _dataset(asof: str | None = None):
    """Build (PIT features → did Opus INITIATE a position here? 0/1) rows. For each decision date d:
    positives = FRESH entries (held(d) − held(prev)); negatives = a deterministic capped sample of the
    same-day engine universe that is NOT currently held (a held-but-not-fresh name is neither — you can't
    'buy' what you already hold — so it is excluded). Features + label are both as-of d (leakage-free).
    Returns (rows, panel, spy). Best-effort ([], None, None)."""
    try:
        from portfolio import predictions as P
        panel, spy = P._load_panel(), P._spy_series()
        if not panel or spy is None:
            return [], panel, spy
        seq = _opus_holdings()
        if not seq:
            return [], panel, spy
        uni_by_date: dict = defaultdict(set)
        for r in P._load_ledger():
            d = str(r.get("asof") or "")[:10]
            tk = (r.get("ticker") or "").upper().strip()
            if d and tk:
                uni_by_date[d].add(tk)
        rows = []
        prev: set = set()
        for d, held in seq:
            fresh = held - prev
            prev = held
            if not fresh:                       # no buy decision to learn on a no-change day
                continue
            pool = uni_by_date.get(d) or set()
            negs = sorted(pool - held)[: max(_NEG_PER_POS * len(fresh), 20)]   # not held, not bought
            for tk in sorted(set(fresh) | set(negs)):
                f = _student._pit_features(panel, spy, tk, d)
                if f is None:
                    continue
                rows.append({"asof": d, "ticker": tk, "score": 0.0, "band": "na", "dir": "na",
                             **f, "y": 1 if tk in fresh else 0})
        return rows, panel, spy
    except Exception:  # noqa: BLE001
        return [], None, None


def train(asof: str | date | None = None) -> dict:
    """Retrain the distilled-Opus classifier + persist. Walk-forward by date (train earlier, score
    strictly later); reports OOS ROC-AUC + accuracy + feature importances. Degrade-safe: 'unavailable'
    (no catboost) / 'building' (too few Opus buys). NEVER raises. LLM-free → safe to run nightly."""
    out = {"status": "building", "n": 0, "n_pos": 0, "trained_at": (str(asof)[:10] if asof else None),
           "oos_auc": None, "oos_acc": None, "n_train": 0, "n_test": 0, "importances": {}}
    CB = _classifier()
    if CB is None:
        out["status"] = "unavailable"
        out["note"] = "catboost not installed — `pip install catboost` to enable distillation"
        _persist(out)
        return out
    rows, _, _ = _dataset(asof)
    out["n"] = len(rows)
    out["n_pos"] = sum(r["y"] for r in rows)
    n_dates = len({r["asof"] for r in rows})
    if out["n_pos"] < _MIN_POS or n_dates < _MIN_DATES:
        _persist(out)
        return out
    try:
        import pandas as pd
        df = pd.DataFrame(rows).sort_values("asof").reset_index(drop=True)
        dates = sorted(df["asof"].unique())
        cut = dates[int(len(dates) * 0.7)]
        tr, te = df[df["asof"] < cut], df[df["asof"] >= cut]
        if tr["y"].nunique() < 2 or te["y"].nunique() < 2 or len(te) < 20:
            _persist(out)
            return out
        cat_idx = [_FEATURES.index(c) for c in _student._CAT_FEATURES]
        model = CB(iterations=400, depth=5, learning_rate=0.03, l2_leaf_reg=6.0,
                   auto_class_weights="Balanced", random_seed=12345, verbose=False)
        model.fit(tr[_FEATURES], tr["y"], cat_features=cat_idx)
        proba = model.predict_proba(te[_FEATURES])[:, 1]
        auc = _auc(te["y"].values, proba)
        acc = float(((proba > 0.5).astype(int) == te["y"].values).mean())
        imp = {f: round(float(v), 2) for f, v in zip(_FEATURES, model.get_feature_importance())}
        _DIR.mkdir(parents=True, exist_ok=True)
        model.save_model(str(_MODEL))
        out.update({"status": "scoring", "oos_auc": round(auc, 3), "oos_acc": round(acc, 3),
                    "n_train": len(tr), "n_test": len(te),
                    "importances": dict(sorted(imp.items(), key=lambda x: -x[1]))})
    except Exception as exc:  # noqa: BLE001
        out["status"] = "building"
        out["error"] = str(exc)[:160]
    _persist(out)
    return out


def _persist(m: dict) -> None:
    try:
        _DIR.mkdir(parents=True, exist_ok=True)
        _METRICS.write_text(json.dumps(m, indent=2, default=str))
    except Exception:  # noqa: BLE001
        pass


def predict(asof: str | date | None = None, top: int = 12) -> list[dict]:
    """Score the current universe with the distilled model → P(Opus would buy), ranked. [] if no model
    / no catboost. Leakage-free (PIT features <= asof). Never raises."""
    CB = _classifier()
    if CB is None or not _MODEL.exists():
        return []
    try:
        import pandas as pd
        from portfolio import predictions as P
        panel, spy = P._load_panel(), P._spy_series()
        if not panel or spy is None:
            return []
        asof_iso = str(asof or date.today())[:10]
        recs = []
        for u in P.universe():
            f = _student._pit_features(panel, spy, u["ticker"], asof_iso)
            if f is None:
                continue
            recs.append({"ticker": u["ticker"], "score": 0.0, "band": "na", "dir": "na", **f})
        if not recs:
            return []
        model = CB()
        model.load_model(str(_MODEL))
        proba = model.predict_proba(pd.DataFrame(recs)[_FEATURES])[:, 1]
        ranked = sorted(({"ticker": r["ticker"], "p_opus_buy": round(float(p), 3)}
                         for r, p in zip(recs, proba)), key=lambda r: -r["p_opus_buy"])
        return ranked[:top]
    except Exception:  # noqa: BLE001
        return []


def summary() -> dict:
    try:
        return json.loads(_METRICS.read_text())
    except Exception:  # noqa: BLE001
        return {"status": "building", "n": 0, "n_pos": 0}
