"""Price-read hardening: contaminated sub-$1 ticks (proven present in the breadth caches by the
single-name factor experiment) must not poison the falling-knife / commodity price reads."""
from __future__ import annotations

import bot  # noqa: F401
import portfolio.lenses as L


def test_clean_closes_drops_sub_dollar_and_nonpositive():
    import pandas as pd
    s = pd.Series([100.0, 0.5, 0.0, 50.0, -1.0, 0.005])
    cleaned = L._clean_closes(s)
    assert list(cleaned) == [100.0, 50.0], "only real (>= $1) prices survive"


def test_clean_closes_no_op_on_clean_series():
    import pandas as pd
    s = pd.Series([100.0, 101.0, 99.0])
    assert list(L._clean_closes(s)) == [100.0, 101.0, 99.0]


def _seed_breadth(monkeypatch, col):
    import pandas as pd
    idx = pd.to_datetime([f"2026-06-{d:02d}" for d in range(1, len(col) + 1)])
    fr = pd.DataFrame({"BBB": pd.Series(col, index=idx)})
    monkeypatch.setattr(L, "_BREADTH_LOADED", True, raising=False)
    monkeypatch.setattr(L, "_BREADTH_FRAME", fr, raising=False)
    L._SERIES_CACHE.clear()


def test_closes_returns_cleaned_series(monkeypatch):
    _seed_breadth(monkeypatch, [100.0, 0.005, 102.0, 0.0, 104.0])
    s = L._closes("BBB")
    assert s is not None and (s >= 1.0).all() and len(s) == 3   # 0.005 and 0.0 dropped


def test_recent_return_robust_to_contaminated_base(monkeypatch):
    # a sub-cent tick sits where the 5-session base would land; WITHOUT cleaning that yields a
    # +30,000x garbage return that wrongly drives the falling-knife veto. With cleaning it is sane.
    _seed_breadth(monkeypatch, [100.0, 0.003, 101.0, 102.0, 101.0, 100.0, 100.0])
    r = L._recent_return("BBB", 5)
    assert r is not None and abs(r) < 1.0, f"contaminated base must not produce a garbage return; got {r}"
