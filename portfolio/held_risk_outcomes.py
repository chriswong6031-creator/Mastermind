"""Outcome ledger for portfolio held-risk alerts (W3, masterplan §9).

Nightly appender: for each alert in alerts.jsonl older than 5 (and 21) sessions
without an outcome row, computes forward return from alert-day close to t+5/t+21
and appends to outcomes.jsonl.

Descriptive only (PRD-R10). No signals, no thresholds tuned from this data.
"""
from __future__ import annotations

import json
import logging
from datetime import date, timedelta
from pathlib import Path

log = logging.getLogger("mastermind.pfolio.outcomes")

_REPO_ROOT = Path(__file__).resolve().parent.parent
_ALERTS_PATH = _REPO_ROOT / "data" / "portfolio_watch" / "alerts.jsonl"
_OUTCOMES_PATH = _REPO_ROOT / "data" / "portfolio_watch" / "outcomes.jsonl"
_HORIZONS = [5, 21]  # sessions


def _trading_days_between(start: date, end: date) -> int:
    """Approximate count of NYSE trading sessions in (start, end] exclusive of start.

    Uses simple Mon-Fri approximation for speed; good enough for 5/21-day horizons.
    """
    count = 0
    cur = start + timedelta(days=1)
    while cur <= end:
        if cur.weekday() < 5:
            count += 1
        cur += timedelta(days=1)
    return count


def _load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except Exception:
                pass
    return out


def _get_ohlcv_series(ticker: str, vendor_root: Path | None = None):
    """Return (idx_strs, closes_values) tuple from OHLCV, or (None, None) on failure.

    idx_strs: list of date strings 'YYYY-MM-DD' in index order
    closes_values: list of float close prices in matching order
    """
    try:
        from portfolio.held_risk import _load_ohlcv, VENDOR_DEFAULT
        vr = vendor_root or VENDOR_DEFAULT
        df = _load_ohlcv(ticker, vr)
        if df is None or df.empty:
            return None, None
        closes = df["close"].dropna()
        if closes.empty:
            return None, None
        try:
            idx_strs = [str(i.date()) if hasattr(i, "date") else str(i)[:10] for i in closes.index]
        except Exception:
            idx_strs = [str(i)[:10] for i in closes.index]
        return idx_strs, list(closes.values)
    except Exception:
        return None, None


def _get_price_at(ticker: str, as_of: date, vendor_root: Path | None = None) -> float | None:
    """Get close price for ticker on or before as_of date (for ref_close lookups only).

    Used only for the reference bar lookup. Forward prices are computed by
    bar-index offset via _grade_outcome() to avoid calendar-date approximation.
    """
    idx_strs, values = _get_ohlcv_series(ticker, vendor_root)
    if idx_strs is None:
        return None
    as_of_str = str(as_of)
    valid_pairs = [(s, v) for s, v in zip(idx_strs, values) if s <= as_of_str]
    if not valid_pairs:
        return None
    return float(valid_pairs[-1][1])


def _grade_outcome(
    ticker: str,
    alert_date: date,
    horizon: int,
    vendor_root: Path | None = None,
) -> tuple[float | None, float | None, bool]:
    """Compute (ref_close, fwd_close, deferred) using bar-count semantics.

    Steps:
      1. Find ref_idx = last bar index at-or-before alert_date
      2. fwd_idx = ref_idx + horizon (exact bar count, no calendar arithmetic)
      3. If fwd_idx >= len(series) → deferred=True (bar not yet available; never skip by back-filling)
      4. Return (ref_close, fwd_close, False) if both bars exist; (None, None, True) if deferred

    Returns:
        (ref_close, fwd_close, deferred) where deferred=True means the forward bar
        does not yet exist and this outcome should be skipped until a later run.
    """
    idx_strs, values = _get_ohlcv_series(ticker, vendor_root)
    if idx_strs is None or not values:
        return None, None, False  # missing data, not a deferral

    as_of_str = str(alert_date)
    # Find the last bar at-or-before alert_date
    ref_idx = None
    for i, s in enumerate(idx_strs):
        if s <= as_of_str:
            ref_idx = i
        else:
            break

    if ref_idx is None:
        return None, None, False  # no ref bar available

    fwd_idx = ref_idx + horizon
    if fwd_idx >= len(idx_strs):
        # Forward bar does not yet exist → defer (do not back-fill)
        return None, None, True

    ref_close = float(values[ref_idx])
    fwd_close = float(values[fwd_idx])
    return ref_close, fwd_close, False


def append_outcomes(today: date | None = None, vendor_root: Path | None = None) -> int:
    """Compute and append forward return outcomes for matured alerts.

    Returns number of new outcome rows appended.
    """
    today_d = today or date.today()

    alerts = _load_jsonl(_ALERTS_PATH)
    if not alerts:
        return 0

    existing_outcomes = _load_jsonl(_OUTCOMES_PATH)
    graded_ids = {
        (r["alert_id"], r["horizon"])
        for r in existing_outcomes
        if "alert_id" in r and "horizon" in r
    }

    new_rows = 0

    for alert in alerts:
        alert_id = alert.get("alert_id")
        ticker = (alert.get("ticker") or "").upper()
        ts = alert.get("ts") or alert.get("alert_date")
        if not alert_id or not ticker or not ts:
            continue

        try:
            alert_date = date.fromisoformat(str(ts)[:10])
        except ValueError:
            continue

        for horizon in _HORIZONS:
            if (alert_id, horizon) in graded_ids:
                continue

            sessions_elapsed = _trading_days_between(alert_date, today_d)
            if sessions_elapsed < horizon:
                continue  # not yet matured per calendar approximation

            # Bar-count grading: forward close is the bar EXACTLY `horizon` positions
            # after the ref bar in the OHLCV index.  If that bar doesn't exist yet,
            # deferred=True → skip this run; it will grade on the next nightly run
            # once the bar is present.  Never back-fill the forward leg.
            ref_close, fwd_close, deferred = _grade_outcome(ticker, alert_date, horizon, vendor_root)
            if deferred:
                continue  # forward bar not yet in index; will grade later
            if ref_close is None or fwd_close is None:
                continue  # missing data; skip without writing a fabricated row
            if ref_close <= 0:
                continue

            fwd_return_pct = (fwd_close - ref_close) / ref_close * 100

            row = {
                "alert_id": alert_id,
                "ticker": ticker,
                "role": alert.get("type"),
                "alert_date": str(alert_date),
                "horizon": horizon,
                "fwd_return_pct": round(fwd_return_pct, 4),
                "ref_close": round(ref_close, 4),
                "fwd_close": round(fwd_close, 4),
                "graded_at": str(today_d),
            }

            try:
                _OUTCOMES_PATH.parent.mkdir(parents=True, exist_ok=True)
                with open(_OUTCOMES_PATH, "a") as f:
                    f.write(json.dumps(row, default=str) + "\n")
                new_rows += 1
                graded_ids.add((alert_id, horizon))
            except Exception as exc:
                log.warning("outcomes append failed: %s", exc)

    if new_rows:
        log.info("outcome ledger: %d new rows appended", new_rows)
    return new_rows
