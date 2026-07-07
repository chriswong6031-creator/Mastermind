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


def _get_price_at(ticker: str, as_of: date, vendor_root: Path | None = None) -> float | None:
    """Get close price for ticker on or before as_of date from available sources."""
    try:
        from portfolio.held_risk import _load_ohlcv, VENDOR_DEFAULT
        vr = vendor_root or VENDOR_DEFAULT
        df = _load_ohlcv(ticker, vr)
        if df is None or df.empty:
            return None
        closes = df["close"].dropna()
        if closes.empty:
            return None
        # Normalize index to date strings for comparison
        try:
            idx_strs = [str(i.date()) if hasattr(i, "date") else str(i)[:10] for i in closes.index]
        except Exception:
            idx_strs = [str(i)[:10] for i in closes.index]
        as_of_str = str(as_of)
        # Find the last close on or before as_of
        valid_pairs = [(s, v) for s, v in zip(idx_strs, closes.values) if s <= as_of_str]
        if not valid_pairs:
            return None
        return float(valid_pairs[-1][1])
    except Exception:
        return None


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
                continue  # not yet matured

            ref_close = _get_price_at(ticker, alert_date, vendor_root)
            if ref_close is None or ref_close <= 0:
                continue

            # Estimate forward date: horizon * ~1.4 calendar days per session
            fwd_date = alert_date + timedelta(days=int(horizon * 1.4) + 1)
            # Advance to a trading day
            while fwd_date.weekday() >= 5:
                fwd_date += timedelta(days=1)
            # Don't go past today
            if fwd_date > today_d:
                fwd_date = today_d

            fwd_close = _get_price_at(ticker, fwd_date, vendor_root)
            if fwd_close is None or fwd_close <= 0:
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
