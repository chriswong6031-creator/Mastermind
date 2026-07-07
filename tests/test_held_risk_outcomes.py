"""Tests for portfolio.held_risk_outcomes (W3 outcome ledger stub).

Coverage:
- append_outcomes with synthetic alerts.jsonl + mock price loader → appends rows
- Dedup: (alert_id, horizon) already in outcomes.jsonl → not re-appended
- Not-yet-matured alerts → skipped
- _trading_days_between correctness
- _get_price_at returns None gracefully on missing data
- Returns count of newly appended rows
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_alerts(path: Path, alerts: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        for a in alerts:
            f.write(json.dumps(a) + "\n")


def _write_outcomes(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def _alert(ticker: str, alert_id: str, alert_date: str) -> dict:
    return {
        "alert_id": alert_id,
        "ticker": ticker,
        "type": "monitor",
        "headline": f"{ticker} elevated",
        "ts": alert_date,
        "lanes": ["macro_sensitivity"],
    }


def _make_ohlcv_df(closes: list[float], start: str = "2025-01-02") -> pd.DataFrame:
    idx = pd.date_range(start=start, periods=len(closes), freq="B")
    return pd.DataFrame({"close": closes}, index=idx)


# ---------------------------------------------------------------------------
# _trading_days_between
# ---------------------------------------------------------------------------

def test_trading_days_between_simple():
    from portfolio.held_risk_outcomes import _trading_days_between

    # Mon to following Mon = 5 trading days
    start = date(2026, 6, 29)  # Monday
    end = date(2026, 7, 6)     # Monday
    n = _trading_days_between(start, end)
    assert n == 5, f"expected 5, got {n}"


def test_trading_days_between_includes_end():
    from portfolio.held_risk_outcomes import _trading_days_between

    # Single day: Mon → Tue = 1 trading day
    start = date(2026, 6, 29)
    end = date(2026, 6, 30)
    n = _trading_days_between(start, end)
    assert n == 1


def test_trading_days_between_across_weekend():
    from portfolio.held_risk_outcomes import _trading_days_between

    # Fri → Mon = 1 trading day (weekend days excluded)
    start = date(2026, 7, 3)   # Friday
    end = date(2026, 7, 6)     # Monday
    n = _trading_days_between(start, end)
    assert n == 1, f"expected 1, got {n}"


def test_trading_days_between_same_day():
    from portfolio.held_risk_outcomes import _trading_days_between

    start = end = date(2026, 7, 7)
    n = _trading_days_between(start, end)
    assert n == 0


def test_trading_days_between_21_approx():
    from portfolio.held_risk_outcomes import _trading_days_between

    # 4 calendar weeks Mon-Mon = 20 trading days
    start = date(2026, 6, 1)   # Monday
    end = date(2026, 6, 29)    # Monday
    n = _trading_days_between(start, end)
    # Should be close to 20 (4 weeks × 5 days)
    assert 18 <= n <= 21, f"expected ~20 trading days, got {n}"


# ---------------------------------------------------------------------------
# _get_price_at
# ---------------------------------------------------------------------------

def test_get_price_at_returns_none_gracefully():
    """_get_price_at returns None when no price data available (loader returns None)."""
    from portfolio.held_risk_outcomes import _get_price_at

    # When _load_ohlcv returns None, _get_price_at should return None without raising
    with patch("portfolio.held_risk._load_ohlcv", return_value=None):
        result = _get_price_at("NOSUCH", date(2026, 7, 7), vendor_root=Path("/nonexistent"))
        assert result is None


def test_get_price_at_with_mock_loader(tmp_path):
    """_get_price_at returns correct price when mock ohlcv available.

    _get_price_at imports _load_ohlcv from portfolio.held_risk internally;
    we patch it at its source.
    """
    closes = [100.0 + i for i in range(300)]
    df = _make_ohlcv_df(closes, start="2025-06-01")

    def mock_load_ohlcv(ticker, vendor_root, *args, **kwargs):
        if ticker == "TST":
            return df
        return None

    with patch("portfolio.held_risk._load_ohlcv", mock_load_ohlcv):
        from portfolio.held_risk_outcomes import _get_price_at
        last_date = df.index[-1].date()
        result = _get_price_at("TST", last_date, vendor_root=tmp_path)
        assert result is not None
        assert abs(result - closes[-1]) < 0.01, f"expected {closes[-1]}, got {result}"


def test_get_price_at_finds_closest_prior(tmp_path):
    """_get_price_at finds the last close on or before as_of (handles non-trading days)."""
    # Build a 10-day frame ending 2026-07-03 (Friday)
    closes = [100.0 + i for i in range(10)]
    idx = pd.date_range(start="2026-06-19", periods=10, freq="B")
    df = pd.DataFrame({"close": closes}, index=idx)

    def mock_load_ohlcv(ticker, vendor_root, *args, **kwargs):
        return df

    with patch("portfolio.held_risk._load_ohlcv", mock_load_ohlcv):
        from portfolio.held_risk_outcomes import _get_price_at
        # Ask for price on weekend (2026-07-05 = Sunday) → should get closest prior close
        result = _get_price_at("TST", date(2026, 7, 5), vendor_root=tmp_path)
        # df ends 2026-07-03, so result should be closes[-1]
        assert result is not None
        assert abs(result - closes[-1]) < 0.01


# ---------------------------------------------------------------------------
# append_outcomes
# ---------------------------------------------------------------------------

def test_append_outcomes_empty_alerts_returns_zero(tmp_path):
    """append_outcomes with no alerts.jsonl → returns 0."""
    import portfolio.held_risk_outcomes as hro

    with patch.object(hro, "_ALERTS_PATH", tmp_path / "alerts.jsonl"), \
         patch.object(hro, "_OUTCOMES_PATH", tmp_path / "outcomes.jsonl"):
        from portfolio.held_risk_outcomes import append_outcomes
        n = append_outcomes(today=date(2026, 7, 7))
        assert n == 0


def test_append_outcomes_not_matured_skipped(tmp_path):
    """Alert from today → horizon=5 not matured → 0 new rows."""
    import portfolio.held_risk_outcomes as hro

    alerts_path = tmp_path / "alerts.jsonl"
    outcomes_path = tmp_path / "outcomes.jsonl"
    _write_alerts(alerts_path, [_alert("AAPL", "alert-1", "2026-07-07")])

    with patch.object(hro, "_ALERTS_PATH", alerts_path), \
         patch.object(hro, "_OUTCOMES_PATH", outcomes_path):
        from importlib import reload
        reload(hro)
        n = hro.append_outcomes(today=date(2026, 7, 7))
        assert n == 0


def test_append_outcomes_matured_appends_rows(tmp_path):
    """Alert from 50 calendar days ago → both horizon=5 and horizon=21 matured → rows appended."""
    import portfolio.held_risk_outcomes as hro

    alerts_path = tmp_path / "alerts.jsonl"
    outcomes_path = tmp_path / "outcomes.jsonl"

    alert_date = date(2026, 7, 7) - timedelta(days=50)
    _write_alerts(alerts_path, [_alert("AAPL", "alert-old-1", str(alert_date))])

    def mock_price(ticker, as_of, vendor_root=None):
        return 100.0

    with patch.object(hro, "_ALERTS_PATH", alerts_path), \
         patch.object(hro, "_OUTCOMES_PATH", outcomes_path), \
         patch.object(hro, "_get_price_at", mock_price):
        n = hro.append_outcomes(today=date(2026, 7, 7))

    assert n >= 1, f"expected at least 1 outcome row, got {n}"
    lines = [json.loads(l) for l in outcomes_path.read_text().splitlines() if l.strip()]
    horizons = {r["horizon"] for r in lines}
    assert 5 in horizons or 21 in horizons, f"expected horizon 5 or 21, got {horizons}"


def test_append_outcomes_dedup(tmp_path):
    """Existing outcome row for (alert_id, horizon) → not re-appended."""
    import portfolio.held_risk_outcomes as hro

    alerts_path = tmp_path / "alerts.jsonl"
    outcomes_path = tmp_path / "outcomes.jsonl"

    alert_date = date(2026, 7, 7) - timedelta(days=50)
    _write_alerts(alerts_path, [_alert("AAPL", "alert-dup-1", str(alert_date))])

    # Pre-seed outcomes with horizon=5 row
    existing = [{"alert_id": "alert-dup-1", "ticker": "AAPL",
                 "horizon": 5, "fwd_return_pct": 2.5,
                 "alert_date": str(alert_date), "graded_at": "2026-07-06"}]
    _write_outcomes(outcomes_path, existing)

    def mock_price(ticker, as_of, vendor_root=None):
        return 100.0

    with patch.object(hro, "_ALERTS_PATH", alerts_path), \
         patch.object(hro, "_OUTCOMES_PATH", outcomes_path), \
         patch.object(hro, "_get_price_at", mock_price):
        n = hro.append_outcomes(today=date(2026, 7, 7))

    # horizon=5 already exists → at most 1 new row (horizon=21)
    lines = [json.loads(l) for l in outcomes_path.read_text().splitlines() if l.strip()]
    horizon5_rows = [r for r in lines if r.get("horizon") == 5]
    assert len(horizon5_rows) == 1, f"expected exactly 1 horizon=5 row (dedup), got {len(horizon5_rows)}"
    assert n <= 1, f"expected at most 1 new row, got {n}"


def test_append_outcomes_missing_ref_price_skipped(tmp_path):
    """If ref_close price is None → outcome row skipped."""
    import portfolio.held_risk_outcomes as hro

    alerts_path = tmp_path / "alerts.jsonl"
    outcomes_path = tmp_path / "outcomes.jsonl"

    alert_date = date(2026, 7, 7) - timedelta(days=50)
    _write_alerts(alerts_path, [_alert("GHOST", "alert-ghost-1", str(alert_date))])

    def mock_price(ticker, as_of, vendor_root=None):
        return None  # no price data

    with patch.object(hro, "_ALERTS_PATH", alerts_path), \
         patch.object(hro, "_OUTCOMES_PATH", outcomes_path), \
         patch.object(hro, "_get_price_at", mock_price):
        n = hro.append_outcomes(today=date(2026, 7, 7))

    assert n == 0


def test_append_outcomes_row_schema(tmp_path):
    """Outcome rows have required fields: alert_id, ticker, horizon, fwd_return_pct, graded_at."""
    import portfolio.held_risk_outcomes as hro

    alerts_path = tmp_path / "alerts.jsonl"
    outcomes_path = tmp_path / "outcomes.jsonl"

    alert_date = date(2026, 7, 7) - timedelta(days=50)
    _write_alerts(alerts_path, [_alert("AAPL", "alert-schema-1", str(alert_date))])

    call_count = [0]

    def mock_price(ticker, as_of, vendor_root=None):
        call_count[0] += 1
        return 100.0 + call_count[0]  # slightly different prices for ref vs fwd

    with patch.object(hro, "_ALERTS_PATH", alerts_path), \
         patch.object(hro, "_OUTCOMES_PATH", outcomes_path), \
         patch.object(hro, "_get_price_at", mock_price):
        n = hro.append_outcomes(today=date(2026, 7, 7))

    if n > 0:
        lines = [json.loads(l) for l in outcomes_path.read_text().splitlines() if l.strip()]
        new_rows = [r for r in lines if r.get("alert_id") == "alert-schema-1"]
        assert new_rows, "expected at least one outcome row for alert-schema-1"
        row = new_rows[0]
        for field in ("alert_id", "ticker", "horizon", "fwd_return_pct", "graded_at"):
            assert field in row, f"missing field {field!r} in outcome row"
        assert row["ticker"] == "AAPL"
        assert row["horizon"] in (5, 21)
        assert isinstance(row["fwd_return_pct"], float)
