"""Labeled backfill for the benchmark series store and the benchmark ledger history.

RULING R8: recorded series only; labeled derived/backfilled; forward-only for authority.

This script is run MANUALLY by the orchestrator in production when the benchmark series store
(data/benchmark/_series.json) is thin because the live-collect path had not yet started running
(books live since 2026-06-18; the store only accumulates from the first time the scheduler ran).

Two backfill actions, both idempotent and labeled:

  (A) PRICE SERIES BACKFILL — fills data/benchmark/_series.json with historical close prices
      for SPY + the defensive basket (XLU/XLV/XLF/XLP), sourced from the vendored Yahoo parquet
      files. Native CSI 300 / Hang Seng history is hydrated by the scheduler's cached Yahoo seam
      because those indexes are not in the parquet store. Only dates between
      ``INCEPTION`` and yesterday are backfilled; any date already present in the store is SKIPPED
      (the live-collected row is never overwritten — R8).  Each new row is tagged in a parallel
      ``data/benchmark/_series_meta.json`` file: {ticker: {date: source}} where source is
      "backfill-yahoo-parquet".

  (B) BOOK ACTIVE RETURN DERIVATION — derives per-review increment rows from each book's
      nav_history.jsonl and writes them to data/benchmark/_book_returns.jsonl.  Each row carries
      source: "derived-from-nav-history" so downstream consumers know this is first-order-
      approximate.  Idempotent: rows with an existing date+book_id are skipped.

After backfill the posture_governor.gap_series() will see real effective_n (it reads the persisted
benchmark ledger files, which are built from the series store on each nightly run).  The backfill
ONLY feeds the series store; the actual ledger build remains the daily_mark scheduler job.

Usage (production — vendor present):
    python scripts/backfill_benchmark.py [--inception YYYY-MM-DD] [--dry-run]

Tests inject a parquet fixture instead of the vendor path (see tests/test_backfill_benchmark.py).
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

# ── paths ──────────────────────────────────────────────────────────────────
_ROOT = Path(__file__).resolve().parent.parent
_BENCH_DIR = _ROOT / "data" / "benchmark"
_SERIES_PATH = _BENCH_DIR / "_series.json"
_SERIES_META_PATH = _BENCH_DIR / "_series_meta.json"
_BOOK_RETURNS_PATH = _BENCH_DIR / "_book_returns.jsonl"
_PORTFOLIO_DIR = _ROOT / "data" / "portfolios"

# The default inception date matches the program start (charter P7 — the book that beat us is a
# named input from the program start).  Pass --inception to override.
INCEPTION_DEFAULT = "2026-06-18"

# Parquet-backed tickers. Native regional indexes are hydrated by app.scheduler from Yahoo.
_SERIES_TICKERS = ["SPY", "XLU", "XLV", "XLF", "XLP"]

# Per-book benchmark ticker (mirrors paper_account._benchmark_for).
_BOOK_BENCHMARK = {
    "flagship":    "SPY",
    "autonomous":  "SPY",
    "heavyweight": "SPY",
    "etf":         "SPY",
    "china":       "000300.SS",
    "hk":          "^HSI",
}

SOURCE_YAHOO = "backfill-yahoo-parquet"
SOURCE_NAV   = "derived-from-nav-history"


# ── yahoo parquet reader ───────────────────────────────────────────────────

def _yahoo_parquet_dir(override: Path | None = None) -> Path:
    if override is not None:
        return override
    return _ROOT / "vendor" / "macro" / "data" / "yahoo"


def _read_yahoo_parquet(ticker: str, parquet_dir: Path) -> dict:
    """{date: close_px} from the yahoo parquet for `ticker`.  {} when absent/unreadable."""
    candidates = [
        parquet_dir / f"{ticker}.parquet",
        parquet_dir / f"{ticker.upper()}.parquet",
        parquet_dir / f"{ticker.lower()}.parquet",
    ]
    for path in candidates:
        if path.exists():
            try:
                import pandas as pd
                df = pd.read_parquet(path)
                # normalize the date column
                if "date" in df.columns:
                    df = df.set_index("date")
                elif df.index.name in ("date", "Date"):
                    pass
                else:
                    # try reset for MultiIndex or integer index
                    df.index = pd.to_datetime(df.index)
                # prefer 'close' column; fall back to first numeric column
                close_col = None
                for col in ("close", "Close", "Adj Close", "adj_close"):
                    if col in df.columns:
                        close_col = col
                        break
                if close_col is None:
                    num_cols = [c for c in df.columns if df[c].dtype.kind in ("f", "i")]
                    if num_cols:
                        close_col = num_cols[0]
                if close_col is None:
                    continue
                out: dict = {}
                for idx, px in df[close_col].items():
                    try:
                        d = str(idx)[:10]
                        v = float(px)
                        if v > 0:
                            out[d] = round(v, 6)
                    except Exception:  # noqa: BLE001
                        continue
                return out
            except Exception:  # noqa: BLE001
                continue
    return {}


# ── series store helpers ───────────────────────────────────────────────────

def _load_series() -> dict:
    try:
        d = json.loads(_SERIES_PATH.read_text()) if _SERIES_PATH.exists() else {}
        return {k: v for k, v in d.items() if isinstance(v, dict)}
    except Exception:  # noqa: BLE001
        return {}


def _load_meta() -> dict:
    try:
        d = json.loads(_SERIES_META_PATH.read_text()) if _SERIES_META_PATH.exists() else {}
        return {k: v for k, v in d.items() if isinstance(v, dict)}
    except Exception:  # noqa: BLE001
        return {}


def _save_series(series: dict, dry_run: bool = False) -> None:
    if dry_run:
        return
    _BENCH_DIR.mkdir(parents=True, exist_ok=True)
    _SERIES_PATH.write_text(json.dumps(series, indent=2, sort_keys=True))


def _save_meta(meta: dict, dry_run: bool = False) -> None:
    if dry_run:
        return
    _BENCH_DIR.mkdir(parents=True, exist_ok=True)
    _SERIES_META_PATH.write_text(json.dumps(meta, indent=2, sort_keys=True))


# ── nav_history reader ─────────────────────────────────────────────────────

def _nav_history(book_id: str, portfolio_dir: Path | None = None) -> list[dict]:
    """Read nav_history.jsonl for `book_id`, oldest-first.  [] when absent/unreadable."""
    base = (portfolio_dir or _PORTFOLIO_DIR) / book_id
    path = base / "nav_history.jsonl"
    if not path.exists():
        return []
    rows: list[dict] = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except Exception:  # noqa: BLE001
            continue
    rows.sort(key=lambda r: str(r.get("date") or ""))
    return rows


def _derive_active_returns(book_id: str, portfolio_dir: Path | None = None) -> list[dict]:
    """Derive per-date growth-of-$1 rows from nav_history.jsonl for `book_id`.

    Each row: {date, book_id, book_nav_norm, benchmark_nav_norm, active_return, source}.
    The NAV and spy_nav columns are renormed to $1 at the FIRST row with both values present.
    `active_return` = book_nav_norm − benchmark_nav_norm (R8: first-order approximation, labeled).
    Only rows where BOTH nav AND spy_nav are present are included (P2 — no fabricated observations)."""
    rows = _nav_history(book_id, portfolio_dir)
    if not rows:
        return []
    benchmark = _BOOK_BENCHMARK.get(book_id, "SPY")

    def _matches_benchmark(row: dict) -> bool:
        row_benchmark = row.get("benchmark")
        # Unlabelled history is legacy SPY only. Regional rows without a symbol are old FXI proxy
        # observations and must never be backfilled under a native-index label.
        return row_benchmark == benchmark or (row_benchmark is None and benchmark == "SPY")

    rows = [row for row in rows if _matches_benchmark(row)]
    if not rows:
        return []
    # find inception row (first row with both nav and spy_nav)
    inc_nav: Optional[float] = None
    inc_bench: Optional[float] = None
    for r in rows:
        n = r.get("nav")
        s = r.get("spy_nav")
        if n and s and float(n) > 0 and float(s) > 0:
            inc_nav = float(n)
            inc_bench = float(s)
            break
    if inc_nav is None or inc_bench is None:
        return []

    out: list[dict] = []
    for r in rows:
        d = str(r.get("date") or "")[:10]
        n = r.get("nav")
        s = r.get("spy_nav")
        if not d or n is None or s is None:
            continue
        try:
            nv = float(n)
            sv = float(s)
            if nv <= 0 or sv <= 0:
                continue
            book_norm = round(nv / inc_nav, 6)
            bench_norm = round(sv / inc_bench, 6)
            active = round(book_norm - bench_norm, 6)
        except (TypeError, ValueError):
            continue
        out.append({
            "date": d,
            "book_id": book_id,
            "book_nav_norm": book_norm,
            "benchmark_nav_norm": bench_norm,
            "benchmark": benchmark,
            "active_return": active,
            "source": SOURCE_NAV,
        })
    return out


# ── book returns store helpers ─────────────────────────────────────────────

def _load_book_returns() -> dict:
    """{(date, book_id): row} from _book_returns.jsonl (for idempotency check)."""
    if not _BOOK_RETURNS_PATH.exists():
        return {}
    out: dict = {}
    for line in _BOOK_RETURNS_PATH.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
            key = (str(r.get("date") or "")[:10], str(r.get("book_id") or ""))
            out[key] = r
        except Exception:  # noqa: BLE001
            continue
    return out


def _append_book_returns(rows: list[dict], dry_run: bool = False) -> None:
    if dry_run or not rows:
        return
    _BENCH_DIR.mkdir(parents=True, exist_ok=True)
    with _BOOK_RETURNS_PATH.open("a") as fh:
        for r in rows:
            fh.write(json.dumps(r, default=str) + "\n")


# ── the two backfill actions ───────────────────────────────────────────────

def backfill_series(
    *,
    inception: str = INCEPTION_DEFAULT,
    parquet_dir: Path | None = None,
    dry_run: bool = False,
) -> dict:
    """Action (A): backfill data/benchmark/_series.json from the yahoo parquet store.

    Returns {tickers_attempted, rows_added, rows_skipped, rows_missing_in_parquet, dry_run}.
    Never raises."""
    pdir = _yahoo_parquet_dir(parquet_dir)
    series = _load_series()
    meta = _load_meta()
    yesterday = (date.today() - timedelta(days=1)).isoformat()

    rows_added = 0
    rows_skipped = 0
    rows_missing: list[str] = []

    for ticker in _SERIES_TICKERS:
        hist = _read_yahoo_parquet(ticker, pdir)
        if not hist:
            rows_missing.append(ticker)
            continue
        existing = series.get(ticker) or {}
        existing_meta = meta.get(ticker) or {}
        new_dates = {d: px for d, px in hist.items()
                     if inception <= d <= yesterday and d not in existing}
        skipped = sum(1 for d in hist if inception <= d <= yesterday and d in existing)
        rows_skipped += skipped
        rows_added += len(new_dates)
        if new_dates:
            existing.update(new_dates)
            series[ticker] = existing
            for d in new_dates:
                existing_meta[d] = SOURCE_YAHOO
            meta[ticker] = existing_meta

    _save_series(series, dry_run)
    _save_meta(meta, dry_run)
    return {
        "tickers_attempted": len(_SERIES_TICKERS),
        "rows_added": rows_added,
        "rows_skipped": rows_skipped,
        "rows_missing_in_parquet": rows_missing,
        "dry_run": dry_run,
    }


def backfill_book_returns(
    *,
    book_ids: list[str] | None = None,
    portfolio_dir: Path | None = None,
    dry_run: bool = False,
) -> dict:
    """Action (B): derive active returns from nav_history.jsonl and append to _book_returns.jsonl.

    Returns {books_attempted, rows_added, rows_skipped, dry_run}.  Never raises."""
    books = book_ids if book_ids is not None else list(_BOOK_BENCHMARK.keys())
    existing = _load_book_returns()
    all_new: list[dict] = []
    rows_skipped = 0
    books_attempted = 0

    for book_id in books:
        books_attempted += 1
        derived = _derive_active_returns(book_id, portfolio_dir)
        for row in derived:
            key = (row["date"], row["book_id"])
            if key in existing:
                rows_skipped += 1
            else:
                all_new.append(row)
                existing[key] = row  # prevent dups within this run

    _append_book_returns(all_new, dry_run)
    return {
        "books_attempted": books_attempted,
        "rows_added": len(all_new),
        "rows_skipped": rows_skipped,
        "dry_run": dry_run,
    }


# ── governor gap_series check (report-only) ────────────────────────────────

def _report_governor_effective_n() -> dict:
    """Read the posture_governor's effective_n AFTER backfill (report; never modifies).

    The governor reads the persisted benchmark ledger *.json files (not _series.json directly).
    The backfill feeds _series.json; the ledger files are built by the nightly daily_mark job
    from _series.json.  So effective_n visible to the governor AFTER backfill = the count of
    existing *.json ledger files in data/benchmark/ (the backfill does NOT build new ledger files
    — that is the scheduler's job).

    This function reports what the governor WOULD see on the next nightly run (once the scheduler
    has rebuilt the ledger from the now-fuller _series.json)."""
    try:
        files = [f for f in _BENCH_DIR.glob("20*.json")]
        effective_n_live = len(files)
    except Exception:  # noqa: BLE001
        effective_n_live = 0
    # also report the number of series data points (what the nightly will use)
    series = _load_series()
    spy_dates = len(series.get("SPY") or {})
    return {
        "current_ledger_files": effective_n_live,
        "spy_series_points_after_backfill": spy_dates,
        "note": ("The governor reads ledger *.json files, not _series.json directly. "
                 "Run the nightly daily_mark job (or build() manually) to convert the "
                 "backfilled series into new ledger files and raise effective_n."),
    }


# ── CLI entry point ────────────────────────────────────────────────────────

def run(
    inception: str = INCEPTION_DEFAULT,
    parquet_dir: Path | None = None,
    portfolio_dir: Path | None = None,
    dry_run: bool = False,
) -> None:
    """Full backfill: (A) price series, (B) book active returns.  Prints a summary."""
    print("=== backfill_benchmark ===")
    print(f"  inception   : {inception}")
    print(f"  dry_run     : {dry_run}")
    print(f"  parquet_dir : {parquet_dir or _yahoo_parquet_dir()}")
    print()

    # (A) price series
    res_a = backfill_series(inception=inception, parquet_dir=parquet_dir, dry_run=dry_run)
    print("(A) price series backfill:")
    print(f"    tickers attempted  : {res_a['tickers_attempted']}")
    print(f"    rows added         : {res_a['rows_added']}")
    print(f"    rows skipped       : {res_a['rows_skipped']} (live rows — not overwritten, R8)")
    if res_a["rows_missing_in_parquet"]:
        print(f"    MISSING in parquet : {res_a['rows_missing_in_parquet']}")
    print()

    # (B) book active returns
    res_b = backfill_book_returns(portfolio_dir=portfolio_dir, dry_run=dry_run)
    print("(B) book active returns derivation:")
    print(f"    books attempted    : {res_b['books_attempted']}")
    print(f"    rows added         : {res_b['rows_added']}")
    print(f"    rows skipped       : {res_b['rows_skipped']} (already present — not overwritten, R8)")
    print()

    # governor report
    gov = _report_governor_effective_n()
    print("(C) posture governor effective_n report:")
    print(f"    current ledger files   : {gov['current_ledger_files']}")
    print(f"    SPY series points now  : {gov['spy_series_points_after_backfill']}")
    print(f"    note: {gov['note']}")
    print()

    if dry_run:
        print("[DRY RUN — no files written]")
    else:
        print("[DONE — files updated]")
        print(f"  _series.json     : {_SERIES_PATH}")
        print(f"  _series_meta.json: {_SERIES_META_PATH}")
        print(f"  _book_returns.jsonl: {_BOOK_RETURNS_PATH}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inception", default=INCEPTION_DEFAULT,
                        help="Earliest date to backfill (YYYY-MM-DD; default: %(default)s)")
    parser.add_argument("--parquet-dir", default=None,
                        help="Override the vendor/macro/data/yahoo path for tests/one-off runs")
    parser.add_argument("--portfolio-dir", default=None,
                        help="Override the data/portfolios path for tests/one-off runs")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print what would be written without writing")
    args = parser.parse_args()
    run(
        inception=args.inception,
        parquet_dir=Path(args.parquet_dir) if args.parquet_dir else None,
        portfolio_dir=Path(args.portfolio_dir) if args.portfolio_dir else None,
        dry_run=args.dry_run,
    )
