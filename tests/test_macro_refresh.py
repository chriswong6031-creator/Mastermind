"""The vendored-macro freshness guard: staleness math + the trip-wire (no network)."""
import datetime

import pytest

from data_layer import macro_refresh as mr


def test_is_stale_thresholds(monkeypatch):
    monkeypatch.setattr(mr, "asof", lambda: "2026-06-22")
    # boundary: > max_age_days is stale, == is not
    assert mr.is_stale(max_age_days=2, today=datetime.date(2026, 6, 23)) is False   # 1d old
    assert mr.is_stale(max_age_days=2, today=datetime.date(2026, 6, 24)) is False   # 2d == threshold
    assert mr.is_stale(max_age_days=2, today=datetime.date(2026, 6, 25)) is True    # 3d old -> stale
    # unknown date -> None (never assert stale on an unreadable date)
    monkeypatch.setattr(mr, "asof", lambda: None)
    assert mr.is_stale() is None


def test_check_and_warn_warns_and_blocks(monkeypatch):
    monkeypatch.setattr(mr, "asof", lambda: "2026-01-01")
    monkeypatch.setattr(mr, "is_stale", lambda *a, **k: True)
    msgs: list[str] = []
    info = mr.check_and_warn(block=False, log=msgs.append)
    assert info["stale"] is True and msgs and "STALE" in msgs[0]          # warns, does not raise
    with pytest.raises(RuntimeError):                                     # block -> refuse
        mr.check_and_warn(block=True, log=lambda *_: None)
    # fresh data never blocks even with block=True
    monkeypatch.setattr(mr, "is_stale", lambda *a, **k: False)
    assert mr.check_and_warn(block=True, log=lambda *_: None)["stale"] is False
