"""The fast statistical STUDENT (brain.student, #3) — CatBoost over the resolved universe log.

Pins the leakage-free point-in-time features, the ledger→features join (canonical-horizon only), the
degrade-safe train paths (no catboost / too few rows), the flag-gated inject (byte-identical OFF), and —
when catboost is installed — a real walk-forward train + predict. CatBoost is OPTIONAL: only the heavy
train test is importorskip'd; everything else runs without it.
"""
from __future__ import annotations

import pytest

from brain import student as S


def _panel(n: int = 260, start: str = "2025-01-01"):
    import numpy as np
    import pandas as pd
    idx = pd.bdate_range(start, periods=n)
    aaa = pd.Series(np.linspace(100, 150, n) * (1 + 0.01 * np.sin(np.arange(n) / 5.0)), index=idx)
    bbb = pd.Series(np.linspace(50, 40, n), index=idx)            # a downtrend
    spy = pd.Series(np.linspace(400, 440, n), index=idx)
    return {"AAA": aaa, "BBB": bbb}, spy


def test_pit_features_shape_and_history_floor():
    panel, spy = _panel()
    asof = panel["AAA"].index[-1].strftime("%Y-%m-%d")
    f = S._pit_features(panel, spy, "AAA", asof)
    assert f is not None and set(f) == {"ret_21", "ret_63", "vol_21", "dist_50", "dist_200", "rs_63"}
    # too little history (<210 closes ≤ asof) → None (no half-baked features)
    early = panel["AAA"].index[50].strftime("%Y-%m-%d")
    assert S._pit_features(panel, spy, "AAA", early) is None
    assert S._pit_features(panel, spy, "ZZZ", asof) is None        # absent name


def test_pit_features_are_leakage_free():
    # features at `asof` must be identical whether the panel carries future bars or is truncated at asof
    panel, spy = _panel()
    asof_ts = panel["AAA"].index[-30]
    asof = asof_ts.strftime("%Y-%m-%d")
    trunc = {k: v[v.index <= asof_ts] for k, v in panel.items()}
    assert S._pit_features(panel, spy, "AAA", asof) == S._pit_features(trunc, spy[spy.index <= asof_ts], "AAA", asof)


def test_dataset_joins_and_filters_to_canonical_horizon(monkeypatch):
    panel, spy = _panel()
    asof = panel["AAA"].index[-1].strftime("%Y-%m-%d")
    ledger = [
        {"ticker": "AAA", "asof": asof, "score": 40, "band": "high", "dir": "up",
         "horizon_d": 21, "status": "resolved", "realized": 0.05},
        {"ticker": "BBB", "asof": asof, "score": -30, "band": "low", "dir": "down",
         "horizon_d": 21, "status": "resolved", "realized": -0.03},
        {"ticker": "AAA", "asof": asof, "score": 40, "band": "high", "dir": "up",
         "horizon_d": 3, "status": "resolved", "realized": 0.01},     # wrong horizon → excluded
        {"ticker": "AAA", "asof": asof, "score": 40, "band": "high", "dir": "up",
         "horizon_d": 21, "status": "open", "realized": None},        # not resolved → excluded
    ]
    from portfolio import predictions as P
    monkeypatch.setattr(P, "_load_panel", lambda: panel)
    monkeypatch.setattr(P, "_spy_series", lambda: spy)
    monkeypatch.setattr(P, "_load_ledger", lambda: ledger)
    rows, _, _ = S._dataset(asof)
    assert len(rows) == 2 and {r["ticker"] for r in rows} == {"AAA", "BBB"}
    assert all("ret_63" in r and "y" in r and "band" in r for r in rows)


def test_train_unavailable_without_catboost(monkeypatch, tmp_path):
    monkeypatch.setattr(S, "_catboost", lambda: None)
    monkeypatch.setattr(S, "_DIR", tmp_path)
    monkeypatch.setattr(S, "_METRICS", tmp_path / "metrics.json")
    out = S.train("2026-06-25")
    assert out["status"] == "unavailable"


def test_train_building_below_min_samples(monkeypatch, tmp_path):
    monkeypatch.setattr(S, "_catboost", lambda: object)              # non-None so we pass the import gate
    monkeypatch.setattr(S, "_dataset", lambda asof=None: ([], None, None))
    monkeypatch.setattr(S, "_DIR", tmp_path)
    monkeypatch.setattr(S, "_METRICS", tmp_path / "metrics.json")
    out = S.train("2026-06-25")
    assert out["status"] == "building" and out["n"] == 0


def test_inject_is_flag_gated_and_identity_when_empty(monkeypatch):
    monkeypatch.delenv("MASTERMIND_STUDENT", raising=False)
    p = "PROMPT"
    assert S.inject(p) is p                                          # disarmed → identity
    monkeypatch.setenv("MASTERMIND_STUDENT", "1")
    monkeypatch.setattr(S, "prompt_line", lambda asof=None: "")      # building → empty read
    assert S.inject(p) is p                                          # empty → unchanged
    monkeypatch.setattr(S, "prompt_line", lambda asof=None: "edge: AAA")
    assert "edge: AAA" in S.inject(p) and S.inject(p) != p           # armed + non-empty → appended


def test_train_and_predict_with_real_catboost(monkeypatch, tmp_path):
    pytest.importorskip("catboost")
    import numpy as np
    panel, spy = _panel(n=320)
    idx = panel["AAA"].index
    dates = [idx[i].strftime("%Y-%m-%d") for i in range(210, 310)]   # 100 dates, all ≥210 history
    rng = np.random.default_rng(0)
    ledger = []
    for d in dates:
        for tk, sc in (("AAA", 40), ("BBB", -30)):
            f = S._pit_features(panel, spy, tk, d)
            if not f:
                continue
            y = 0.5 * f["rs_63"] + 0.001 * sc + float(rng.normal(0, 0.01))   # a real, learnable signal
            ledger.append({"ticker": tk, "asof": d, "score": sc, "band": "high", "dir": "up",
                           "horizon_d": 21, "status": "resolved", "realized": round(y, 4)})
    from portfolio import predictions as P
    monkeypatch.setattr(P, "_load_panel", lambda: panel)
    monkeypatch.setattr(P, "_spy_series", lambda: spy)
    monkeypatch.setattr(P, "_load_ledger", lambda: ledger)
    monkeypatch.setattr(P, "universe", lambda: [
        {"ticker": "AAA", "dir": "up", "score": 40, "band": "high", "price": 150.0},
        {"ticker": "BBB", "dir": "down", "score": -30, "band": "low", "price": 40.0}])
    monkeypatch.setattr(S, "_DIR", tmp_path)
    monkeypatch.setattr(S, "_MODEL", tmp_path / "model.cbm")
    monkeypatch.setattr(S, "_METRICS", tmp_path / "metrics.json")
    out = S.train("2026-06-25")
    assert out["status"] == "scoring" and out["n"] >= 150
    assert out["oos_rank_ic"] is not None and "importances" in out
    preds = S.predict("2026-06-25")
    assert isinstance(preds, list) and (not preds or "pred_edge" in preds[0])
