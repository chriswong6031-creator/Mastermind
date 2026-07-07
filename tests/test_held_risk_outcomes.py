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


# ---------------------------------------------------------------------------
# MAJOR-3: Bar-count outcome grading (not calendar-date approximation)
# ---------------------------------------------------------------------------

class TestBarCountGrading:
    """_grade_outcome must use bar-index semantics (ref_idx + horizon), never
    calendar-date arithmetic.  Key invariants:
      1. Forward bar is exactly index[ref_idx + horizon], not a calendar estimate.
      2. Insufficient history (fwd bar not yet in index) → deferred=True, no row.
      3. Gaps/holidays in the series do not fabricate prices (no at-or-before fallback
         for the forward leg).
    """

    def _make_series(self, n: int, start: str = "2024-01-02") -> tuple[list[str], list[float]]:
        """Build (idx_strs, values) with n business-day bars starting at start."""
        import pandas as pd
        idx = pd.date_range(start=start, periods=n, freq="B")
        idx_strs = [str(d.date()) for d in idx]
        values = [100.0 + i * 0.5 for i in range(n)]
        return idx_strs, values

    def _patch_series(self, monkeypatch, ticker: str, idx_strs, values):
        import portfolio.held_risk_outcomes as hro
        monkeypatch.setattr(
            hro, "_get_ohlcv_series",
            lambda t, vendor_root=None: (idx_strs, values) if t == ticker else (None, None),
        )

    def test_forward_bar_is_exact_index_offset(self, monkeypatch):
        """fwd_close must be values[ref_idx + horizon], not a calendar approximation."""
        from portfolio.held_risk_outcomes import _grade_outcome

        horizon = 5
        # 30 bars: ref bar is at index 0 (first bar), forward at index 5
        idx_strs, values = self._make_series(30)
        alert_date = date.fromisoformat(idx_strs[0])

        self._patch_series(monkeypatch, "TST", idx_strs, values)

        ref_close, fwd_close, deferred = _grade_outcome("TST", alert_date, horizon)

        assert not deferred, "should not be deferred — enough bars exist"
        assert ref_close is not None and fwd_close is not None
        # ref_idx = 0 (alert_date == first bar), fwd_idx = 5
        assert abs(ref_close - values[0]) < 1e-9, f"ref_close mismatch: {ref_close} vs {values[0]}"
        assert abs(fwd_close - values[5]) < 1e-9, f"fwd_close mismatch: {fwd_close} vs {values[5]}"

    def test_insufficient_history_defers(self, monkeypatch):
        """When fwd_idx >= len(index), _grade_outcome must return deferred=True.

        No row should be written; grading resumes on a future run when the bar
        appears in the OHLCV index.
        """
        from portfolio.held_risk_outcomes import _grade_outcome

        horizon = 5
        # Only 3 bars total; ref=index[0], fwd would be index[5] → out of bounds
        idx_strs, values = self._make_series(3)
        alert_date = date.fromisoformat(idx_strs[0])

        self._patch_series(monkeypatch, "TST2", idx_strs, values)

        ref_close, fwd_close, deferred = _grade_outcome("TST2", alert_date, horizon)

        assert deferred is True, (
            f"expected deferred=True when only {len(idx_strs)} bars exist "
            f"for horizon={horizon}, got deferred={deferred}"
        )
        assert ref_close is None and fwd_close is None

    def test_gap_day_uses_correct_bar_not_calendar_approx(self, monkeypatch):
        """A gap/holiday in the index must not shift the forward bar.

        Scenario: 30 bars with a deliberate gap between bar 5 and bar 6
        (simulating a holiday).  The forward bar for horizon=5 starting at
        bar 0 must still be bar 5 (index[0+5]), regardless of calendar dates.
        """
        from portfolio.held_risk_outcomes import _grade_outcome
        import pandas as pd

        # Build 30 consecutive business-day bars, then manually inject a gap
        idx_full = list(pd.date_range(start="2024-01-02", periods=31, freq="B"))
        # Skip the 6th element (index 5) — simulate a holiday
        idx_with_gap = idx_full[:5] + idx_full[6:]  # 30 bars, gap between [4] and [5]
        idx_strs = [str(d.date()) for d in idx_with_gap]
        values = [100.0 + i * 0.5 for i in range(len(idx_strs))]

        alert_date = date.fromisoformat(idx_strs[0])
        horizon = 5

        self._patch_series(monkeypatch, "TST3", idx_strs, values)

        ref_close, fwd_close, deferred = _grade_outcome("TST3", alert_date, horizon)

        assert not deferred
        # Forward bar is index[0 + 5] = idx_strs[5] (after the gap)
        expected_fwd = values[5]
        assert abs(fwd_close - expected_fwd) < 1e-9, (
            f"fwd_close should be values[5]={expected_fwd}, got {fwd_close}"
        )

    def test_append_outcomes_defers_when_fwd_bar_missing(self, tmp_path, monkeypatch):
        """append_outcomes must not write a row when _grade_outcome returns deferred=True."""
        import portfolio.held_risk_outcomes as hro

        alerts_path = tmp_path / "alerts.jsonl"
        outcomes_path = tmp_path / "outcomes.jsonl"

        # Alert from 8 calendar days ago → calendar says "matured for horizon=5"
        # but OHLCV only has 3 bars → bar-count check defers it
        alert_date = date(2026, 7, 7) - timedelta(days=8)
        _write_alerts(alerts_path, [_alert("AAPL", "alert-defer-1", str(alert_date))])

        # Only 3 bars in the index — fwd bar for horizon=5 does not exist yet
        import pandas as pd
        idx = pd.date_range(start=str(alert_date), periods=3, freq="B")
        idx_strs = [str(d.date()) for d in idx]
        values = [100.0, 101.0, 102.0]

        monkeypatch.setattr(
            hro, "_get_ohlcv_series",
            lambda t, vendor_root=None: (idx_strs, values) if t == "AAPL" else (None, None),
        )

        with patch.object(hro, "_ALERTS_PATH", alerts_path), \
             patch.object(hro, "_OUTCOMES_PATH", outcomes_path):
            n = hro.append_outcomes(today=date(2026, 7, 7))

        assert n == 0, (
            f"expected 0 rows when forward bar missing (deferred), got {n}"
        )
        # outcomes file either doesn't exist or is empty
        if outcomes_path.exists():
            lines = [l for l in outcomes_path.read_text().splitlines() if l.strip()]
            assert len(lines) == 0, f"expected no rows, got {len(lines)}"
