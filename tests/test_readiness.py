"""Guards for the forward-proof readiness watcher (portfolio.readiness).

The contract: each threshold pings EXACTLY ONCE when it crosses (not every day after), alerts
persist, and nothing raises on missing data.
"""
from __future__ import annotations

import pytest

import bot  # noqa: F401
from portfolio import readiness as R


def test_fires_once_per_crossing(tmp_path, monkeypatch):
    monkeypatch.setattr(R, "_STATE", tmp_path / "state.json")
    monkeypatch.setattr(R, "_ALERTS", tmp_path / "alerts.jsonl")

    def _set(**flags):
        monkeypatch.setattr(R, "status", lambda: {"flags": flags, "detail": {}})

    _set(calibration_forge=False, prediction_xsec=False)
    assert R.check_and_record("2026-06-21")["new"] == []          # nothing ready yet

    _set(calibration_forge=True, prediction_xsec=False)
    assert R.check_and_record("2026-07-01")["new"] == ["calibration_forge"]   # crosses → ping
    assert R.check_and_record("2026-07-02")["new"] == []          # still true next day → NO repeat

    _set(calibration_forge=True, prediction_xsec=True)
    assert R.check_and_record("2027-02-01")["new"] == ["prediction_xsec"]     # later crossing → ping

    al = R.alerts()
    assert len(al) == 2 and {a["flag"] for a in al} == {"calibration_forge", "prediction_xsec"}
    assert all(a.get("message") for a in al)                      # each alert carries a human message


def test_status_never_raises_on_missing_data(monkeypatch, tmp_path):
    # point readiness at empty dirs → status must still return a flags dict, all False
    monkeypatch.setattr(R, "_ROOT", tmp_path)
    s = R.status()
    assert "flags" in s and isinstance(s["flags"], dict)


def test_alerts_empty_when_none(tmp_path, monkeypatch):
    monkeypatch.setattr(R, "_ALERTS", tmp_path / "none.jsonl")
    assert R.alerts() == []
