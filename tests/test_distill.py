"""Opus-distillation model (brain.distill, #3 v2) — a CatBoost classifier that mimics Opus's buys.

Pins the rank-AUC helper, the Opus-buys/universe join + labels, the degrade-safe train paths, and (with
catboost) a real walk-forward train where Opus 'buys' high-momentum names so the model must learn them.
CatBoost optional: the heavy test is importorskip'd.
"""
from __future__ import annotations

import pytest

from brain import distill as D


def _panel(n: int = 460, ntick: int = 30, start: str = "2024-05-01"):
    import numpy as np
    import pandas as pd
    idx = pd.bdate_range(start, periods=n)
    cols = {}
    for i in range(ntick):
        slope = 60 + 4.0 * i                                   # higher i → stronger trend (higher ret_252)
        cols[f"T{i:02d}"] = pd.Series(np.linspace(100, 100 + slope, n), index=idx)
    spy = pd.Series(np.linspace(400, 440, n), index=idx)
    return cols, spy


def test_auc_rank_separation():
    assert D._auc([1, 1, 0, 0], [0.9, 0.8, 0.2, 0.1]) == 1.0    # perfect ranking
    assert D._auc([1, 1, 0, 0], [0.1, 0.2, 0.8, 0.9]) == 0.0    # reversed
    assert D._auc([1, 1], [0.5, 0.5]) is None                   # one class only → undefined


def test_opus_buys_are_fresh_entries_not_holdings(monkeypatch):
    # `holdings` is the full daily book; a buy is the DELTA. AAA is held both days → it's a buy ONCE.
    from brain import calibration
    monkeypatch.setattr(calibration, "_book_decisions", lambda book: iter([
        {"asof": "2026-06-21", "holdings": [{"ticker": "AAA"}, {"ticker": "bbb"}]},
        {"asof": "2026-06-22", "holdings": [{"ticker": "AAA"}, {"ticker": "CCC"}]},   # AAA held, CCC new
        {"asof": "", "holdings": [{"ticker": "X"}]},            # no date → dropped
    ]))
    buys = D._opus_buys()
    assert buys == {"2026-06-21": {"AAA", "BBB"}, "2026-06-22": {"CCC"}}   # AAA NOT re-counted on 06-22


def test_dataset_labels_positives_and_negatives(monkeypatch):
    panel, spy = _panel()
    asof = panel["T00"].index[-1].strftime("%Y-%m-%d")
    from portfolio import predictions as P
    from brain import calibration
    monkeypatch.setattr(P, "_load_panel", lambda: panel)
    monkeypatch.setattr(P, "_spy_series", lambda: spy)
    monkeypatch.setattr(P, "_load_ledger", lambda: [{"asof": asof, "ticker": t} for t in panel])
    monkeypatch.setattr(calibration, "_book_decisions",
                        lambda book: iter([{"asof": asof, "holdings": [{"ticker": "T29"}, {"ticker": "T28"}]}]))
    rows, _, _ = D._dataset(asof)
    by_tk = {r["ticker"]: r for r in rows}
    assert by_tk["T29"]["y"] == 1 and by_tk["T28"]["y"] == 1    # bought → positive
    assert by_tk["T00"]["y"] == 0                               # not bought → negative
    assert all("ret_252" in r for r in rows)                   # carries the PIT feature vector


def test_train_unavailable_without_catboost(monkeypatch, tmp_path):
    monkeypatch.setattr(D, "_classifier", lambda: None)
    monkeypatch.setattr(D, "_DIR", tmp_path)
    monkeypatch.setattr(D, "_METRICS", tmp_path / "m.json")
    assert D.train("2026-06-25")["status"] == "unavailable"


def test_train_building_below_min_positives(monkeypatch, tmp_path):
    monkeypatch.setattr(D, "_classifier", lambda: object)
    monkeypatch.setattr(D, "_dataset", lambda asof=None: ([{"asof": "2026-06-21", "y": 1}], None, None))
    monkeypatch.setattr(D, "_DIR", tmp_path)
    monkeypatch.setattr(D, "_METRICS", tmp_path / "m.json")
    out = D.train("2026-06-25")
    assert out["status"] == "building" and out["n_pos"] == 1


def test_train_real_catboost_learns_opus(monkeypatch, tmp_path):
    pytest.importorskip("catboost")
    import numpy as np
    import pandas as pd
    n = 520
    idx = pd.bdate_range("2024-05-01", periods=n)
    cols = {f"L{i:02d}": pd.Series(np.linspace(100, 101, n), index=idx) for i in range(30)}      # flat lows
    cols.update({f"H{i:02d}": pd.Series(np.linspace(100, 140 + i, n), index=idx) for i in range(70)})  # rising
    spy = pd.Series(np.linspace(400, 440, n), index=idx)
    lows = [f"L{i:02d}" for i in range(30)]
    dates = [idx[300 + 10 * k].strftime("%Y-%m-%d") for k in range(14)]   # 14 dates, all ≥260 history
    # Opus INITIATES 5 NEW high-momentum names each date (book accumulates) → 70 fresh buys, all high-trend
    holdings = [[f"H{j:02d}" for j in range(5 * (k + 1))] for k in range(14)]
    fresh = lambda k: [f"H{j:02d}" for j in range(5 * k, 5 * k + 5)]
    from portfolio import predictions as P
    from brain import calibration
    monkeypatch.setattr(P, "_load_panel", lambda: cols)
    monkeypatch.setattr(P, "_spy_series", lambda: spy)
    monkeypatch.setattr(P, "_load_ledger", lambda: [   # per-date universe = that day's fresh-5 high + all lows
        {"asof": dates[k], "ticker": t} for k in range(14) for t in (fresh(k) + lows)])
    monkeypatch.setattr(P, "universe", lambda: [{"ticker": t, "dir": "up", "score": 0, "band": "na",
                                                 "price": 100.0} for t in cols])
    monkeypatch.setattr(calibration, "_book_decisions", lambda book: iter([
        {"asof": dates[k], "holdings": [{"ticker": t} for t in holdings[k]]} for k in range(14)]))
    monkeypatch.setattr(D, "_DIR", tmp_path)
    monkeypatch.setattr(D, "_MODEL", tmp_path / "model.cbm")
    monkeypatch.setattr(D, "_METRICS", tmp_path / "m.json")
    out = D.train("2026-06-25")
    assert out["status"] == "scoring" and out["n_pos"] >= D._MIN_POS    # 5 fresh × 14 = 70 positives
    assert out["oos_auc"] is not None and out["oos_auc"] > 0.6          # learns Opus initiates high-momentum
    assert isinstance(D.predict("2026-06-25"), list)
