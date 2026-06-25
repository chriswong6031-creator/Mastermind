"""The fast statistical STUDENT (#3) — a daily-retrained CatBoost over the universe prediction log.

The LLM is frozen and a poor streaming belief-updater; gradient-boosted trees beat LLMs on tabular
numeric prediction. So the desk's fast-adapting numeric core should be a small tree model that retrains
NIGHTLY and feeds the (slow, frozen) Opus layer a calibrated cross-sectional read — and is later a
distillation target for Opus's repeated decisions. This module is that student:

  * TRAINING SET — the resolved universe prediction log (portfolio.predictions): per (ticker, asof) the
    engine's directional score/band + POINT-IN-TIME technical features derived from the historical price
    panel (trailing returns, realized vol, RS vs SPY, distance from MAs — all computed ONLY from prices
    <= asof, leakage-free) → target = realized forward rel-return vs SPY.
  * VALIDATION — WALK-FORWARD by entry date (train on earlier dates, score on strictly later ones); it
    reports OUT-OF-SAMPLE rank-IC + hit-rate, never an in-sample fit (the backtest-overfitting trap).
  * OUTPUT — a persisted model + metrics, a per-name predicted-edge ranking, and a compact prompt_line
    the Brain books can show Opus so it reasons OVER a fast calibrated belief instead of updating poorly.

Pure numeric (NO LLM — it never spends Opus). Flag-gated injection (MASTERMIND_STUDENT) is OFF by
default; nightly training is degrade-safe — if CatBoost isn't installed, or too few rows have resolved,
it returns status 'unavailable'/'building' and NEVER raises. Honestly 'building' until the universe log
accrues resolved rows (the short-horizon tiers from #1 mature this in weeks, not months).
"""
from __future__ import annotations

import json
import os
from datetime import date
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_DIR = _ROOT / "data" / "brain" / "student"
_MODEL = _DIR / "model.cbm"
_METRICS = _DIR / "metrics.json"

_HORIZON = 21            # train on the canonical 21-bday tier (the deepest, most economically meaningful)
_MIN_SAMPLES = 150       # below this many resolved feature rows, don't train (cold-start safety)
_MIN_TEST = 40           # need a real OOS block before reporting metrics
_CAT_FEATURES = ["band", "dir"]   # CatBoost's native categorical handling (the reason to pick CatBoost)
_NUM_FEATURES = ["score", "ret_21", "ret_63", "vol_21", "dist_50", "dist_200", "rs_63"]
_FEATURES = _NUM_FEATURES + _CAT_FEATURES


def _on() -> bool:
    return os.environ.get("MASTERMIND_STUDENT", "0").strip().lower() in ("1", "true", "yes", "on")


def _catboost():
    """Lazy import — returns the CatBoostRegressor class or None (degrade-safe; no hard dep)."""
    try:
        from catboost import CatBoostRegressor
        return CatBoostRegressor
    except Exception:  # noqa: BLE001 — catboost not installed → student stays 'unavailable'
        return None


# ─────────────────────────────────────────────────────────────────────────────
# point-in-time features from the historical price panel (leakage-free)
# ─────────────────────────────────────────────────────────────────────────────
def _pit_features(panel, spy, ticker: str, asof_iso: str) -> dict | None:
    """Technical features for one (ticker, asof) computed ONLY from closes <= asof (no look-ahead).
    Returns None if the name lacks enough history. Reuses portfolio.predictions' cached panel/SPY."""
    try:
        import pandas as pd
        s = panel.get((ticker or "").upper())
        if s is None or spy is None:
            return None
        ts = pd.Timestamp(asof_iso)
        s = s[(s.index <= ts) & (s > 0)]
        sp = spy[(spy.index <= ts) & (spy > 0)]
        if len(s) < 210 or len(sp) < 65:
            return None
        px = float(s.iloc[-1])
        r = s.pct_change().dropna()
        ret_21 = px / float(s.iloc[-22]) - 1.0
        ret_63 = px / float(s.iloc[-64]) - 1.0
        vol_21 = float(r.iloc[-21:].std()) * (252 ** 0.5)
        dist_50 = px / float(s.iloc[-50:].mean()) - 1.0
        dist_200 = px / float(s.iloc[-200:].mean()) - 1.0
        spy_ret_63 = float(sp.iloc[-1]) / float(sp.iloc[-64]) - 1.0
        rs_63 = ret_63 - spy_ret_63
        return {"ret_21": round(ret_21, 4), "ret_63": round(ret_63, 4), "vol_21": round(vol_21, 4),
                "dist_50": round(dist_50, 4), "dist_200": round(dist_200, 4), "rs_63": round(rs_63, 4)}
    except Exception:  # noqa: BLE001
        return None


def _dataset(asof: str | None = None):
    """Assemble the training frame from the resolved prediction log joined to PIT panel features.
    Returns (rows, panel, spy) where rows = list of {asof, ticker, **features, score, band, dir, y}.
    Best-effort: ([], None, None) when prices/ledger unavailable."""
    try:
        import pandas as pd  # noqa: F401
        from portfolio import predictions as P
        panel, spy = P._load_panel(), P._spy_series()
        if not panel or spy is None:
            return [], panel, spy
        out = []
        for r in P._load_ledger():
            if (r.get("status") != "resolved" or r.get("realized") is None
                    or int(r.get("horizon_d") or _HORIZON) != _HORIZON):
                continue
            feats = _pit_features(panel, spy, r.get("ticker", ""), str(r.get("asof"))[:10])
            if feats is None or r.get("score") is None:
                continue
            out.append({"asof": str(r.get("asof"))[:10], "ticker": (r.get("ticker") or "").upper(),
                        "score": float(r["score"]), "band": str(r.get("band") or "na"),
                        "dir": str(r.get("dir") or "na"), "y": float(r["realized"]), **feats})
        return out, panel, spy
    except Exception:  # noqa: BLE001
        return [], None, None


# ─────────────────────────────────────────────────────────────────────────────
# train (walk-forward OOS) + persist
# ─────────────────────────────────────────────────────────────────────────────
def train(asof: str | date | None = None) -> dict:
    """Retrain the student on the resolved log and persist model + metrics. Walk-forward: train on the
    earlier 70% of ENTRY DATES, validate on the strictly-later 30% (leakage-free, honest OOS). Reports
    OOS rank-IC + directional hit-rate + feature importances. Degrade-safe: 'unavailable' (no CatBoost)
    / 'building' (too few rows) and NEVER raises. Cheap + LLM-free → safe to run nightly."""
    out = {"status": "building", "n": 0, "trained_at": (str(asof)[:10] if asof else None),
           "oos_rank_ic": None, "oos_hit_rate": None, "n_train": 0, "n_test": 0, "importances": {}}
    CB = _catboost()
    if CB is None:
        out["status"] = "unavailable"
        out["note"] = "catboost not installed — `pip install catboost` to enable the student"
        _persist_metrics(out)
        return out
    rows, _, _ = _dataset(asof)
    out["n"] = len(rows)
    if len(rows) < _MIN_SAMPLES:
        _persist_metrics(out)
        return out
    try:
        import numpy as np
        import pandas as pd
        from engine.validation import rank_ic
        df = pd.DataFrame(rows).sort_values("asof").reset_index(drop=True)
        dates = sorted(df["asof"].unique())
        cut = dates[int(len(dates) * 0.7)] if len(dates) > 3 else dates[-1]
        tr, te = df[df["asof"] < cut], df[df["asof"] >= cut]
        if len(tr) < _MIN_SAMPLES - _MIN_TEST or len(te) < _MIN_TEST:
            _persist_metrics(out)
            return out
        cat_idx = [_FEATURES.index(c) for c in _CAT_FEATURES]
        model = CB(iterations=400, depth=5, learning_rate=0.03, loss_function="RMSE",
                   l2_leaf_reg=6.0, random_seed=12345, verbose=False)
        model.fit(tr[_FEATURES], tr["y"], cat_features=cat_idx)
        pred = model.predict(te[_FEATURES])
        ic = rank_ic(pd.Series(pred), pd.Series(te["y"].values))
        hit = float(((pred > 0) == (te["y"].values > 0)).mean())
        imp = {f: round(float(v), 2) for f, v in zip(_FEATURES, model.get_feature_importance())}
        _DIR.mkdir(parents=True, exist_ok=True)
        model.save_model(str(_MODEL))
        out.update({"status": "scoring", "oos_rank_ic": round(float(ic), 4) if ic == ic else None,
                    "oos_hit_rate": round(hit, 3), "n_train": len(tr), "n_test": len(te),
                    "importances": dict(sorted(imp.items(), key=lambda x: -x[1]))})
    except Exception as exc:  # noqa: BLE001
        out["status"] = "building"
        out["error"] = str(exc)[:160]
    _persist_metrics(out)
    return out


def _persist_metrics(m: dict) -> None:
    try:
        _DIR.mkdir(parents=True, exist_ok=True)
        _METRICS.write_text(json.dumps(m, indent=2, default=str))
    except Exception:  # noqa: BLE001
        pass


# ─────────────────────────────────────────────────────────────────────────────
# predict the live universe + LLM-facing read
# ─────────────────────────────────────────────────────────────────────────────
def predict(asof: str | date | None = None, top: int = 12) -> list[dict]:
    """Score the engine's current universe with the persisted model → per-name predicted forward edge,
    ranked. Leakage-free (PIT features <= asof). [] if no model / no CatBoost. Never raises."""
    CB = _catboost()
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
            feats = _pit_features(panel, spy, u["ticker"], asof_iso)
            if feats is None or u.get("score") is None:
                continue
            recs.append({"ticker": u["ticker"], "score": float(u["score"]),
                         "band": str(u.get("band") or "na"), "dir": str(u.get("dir") or "na"), **feats})
        if not recs:
            return []
        model = CB()
        model.load_model(str(_MODEL))
        X = pd.DataFrame(recs)[_FEATURES]
        preds = model.predict(X)
        for rec, p in zip(recs, preds):
            rec["pred_edge"] = round(float(p), 4)
        recs.sort(key=lambda r: -r["pred_edge"])
        return [{"ticker": r["ticker"], "pred_edge": r["pred_edge"], "dir": r["dir"]} for r in recs[:top]]
    except Exception:  # noqa: BLE001
        return []


def summary() -> dict:
    try:
        return json.loads(_METRICS.read_text())
    except Exception:  # noqa: BLE001
        return {"status": "building", "n": 0}


def prompt_line(asof: str | date | None = None) -> str:
    """A one-line student read for the Brain prompt (so Opus reasons OVER a fast calibrated belief).
    Empty unless the model is scoring AND its OOS edge is real. Never raises. Flag-gated by the caller."""
    try:
        m = summary()
        if m.get("status") != "scoring" or m.get("oos_rank_ic") is None:
            return ""
        top = predict(asof, top=6)
        names = ", ".join(f"{t['ticker']}({t['pred_edge']:+.1%})" for t in top) if top else "n/a"
        imp = ", ".join(list((m.get("importances") or {}).keys())[:3])
        return (f"Student model (CatBoost, n={m.get('n')}, OOS rank-IC {m['oos_rank_ic']:+.3f}, "
                f"hit {m.get('oos_hit_rate', 0) * 100:.0f}%): top predicted edge — {names}. "
                f"Most predictive features: {imp}. (A fast numeric prior; weigh it, don't obey it.)")
    except Exception:  # noqa: BLE001
        return ""


def inject(prompt: str, asof: str | date | None = None) -> str:
    """Append the student read to a Brain prompt, gated by MASTERMIND_STUDENT. Returns `prompt`
    UNCHANGED when the flag is off or the read is empty (byte-identical default)."""
    if not _on():
        return prompt
    line = prompt_line(asof)
    return (prompt + "\n\n--- STATISTICAL STUDENT (fast numeric prior) ---\n" + line) if line else prompt
