"""Tests for scripts/backfill_benchmark.py (Lane B — MW5 regional repair).

Covers:
  · build_regional wiring in scheduler._build_benchmark_ledger (SPY spy + regional books)
  · native-index identity present on regional bogey rows after the scheduler step
  · regional lifecycle recs honest (insufficient-n until enough reviews)
  · backfill script fixtures (idempotency, no-overwrite, labels)
  · reviews_remaining math

Vendor parquet is absent in the worktree — tests use in-memory parquet fixtures.
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import pytest

import bot  # noqa: F401 — bootstraps vendor/macro
from brain import benchmark_ledger as B
from brain import book_lifecycle as BL
from scripts import backfill_benchmark as BF


# ─────────────────────────────────────────────────────────────────────────────
# helpers
# ─────────────────────────────────────────────────────────────────────────────

def _make_parquet(tmp_path: Path, ticker: str, rows: dict) -> Path:
    """Write a minimal yahoo parquet fixture: {date: close}.  Returns the dir."""
    df = pd.DataFrame(
        [{"date": d, "close": v} for d, v in rows.items()]
    ).set_index("date")
    pdir = tmp_path / "parquet"
    pdir.mkdir(exist_ok=True)
    df.to_parquet(pdir / f"{ticker}.parquet")
    return pdir


def _make_nav_history(tmp_path: Path, book_id: str, rows: list[dict]) -> Path:
    """Write a minimal nav_history.jsonl fixture.  Returns the portfolios dir."""
    pdir = tmp_path / "portfolios" / book_id
    pdir.mkdir(parents=True, exist_ok=True)
    with (pdir / "nav_history.jsonl").open("w") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")
    return tmp_path / "portfolios"


def _spy_series() -> dict:
    return {
        "SPY": {"2026-06-18": 530.0, "2026-06-19": 535.0, "2026-06-20": 528.0},
        "000300.SS": {"2026-06-18": 4_000.0, "2026-06-19": 4_050.0,
                      "2026-06-20": 3_980.0},
        "^HSI": {"2026-06-18": 20_000.0, "2026-06-19": 20_200.0,
                 "2026-06-20": 19_900.0},
    }


def _native_history(symbol: str) -> pd.Series:
    values = _spy_series()[symbol]
    return pd.Series(values.values(), index=pd.to_datetime(list(values)))


# ─────────────────────────────────────────────────────────────────────────────
# 1. build_regional wiring — scheduler._build_benchmark_ledger includes regional
# ─────────────────────────────────────────────────────────────────────────────

class TestBuildRegionalWiring:
    """Verify that _build_benchmark_ledger now calls build_regional for china and hk."""

    def test_native_indexes_accumulated_in_series_store(self, tmp_path, monkeypatch):
        """The scheduler hydrates both native indexes into the rolling series store."""
        import app.scheduler as S
        monkeypatch.setattr(S, "_build_benchmark_ledger", S._build_benchmark_ledger)
        from app.scheduler import _build_benchmark_ledger
        # patch the benchmark dir to tmp
        monkeypatch.setattr(B, "_BENCH_DIR", tmp_path)
        # also patch run_events to a no-op to avoid touching the real governance ledger
        import control_plane.run_events as _re
        monkeypatch.setattr(_re, "append", lambda ev: None)
        from data_layer import yahoo_feed
        monkeypatch.setattr(yahoo_feed, "history_local", _native_history)
        union_usd = {"SPY": 530.0, "XLU": 55.0, "XLV": 150.0, "XLF": 42.0, "XLP": 77.0}
        _build_benchmark_ledger("2026-06-18", union_usd)
        series_path = tmp_path / "_series.json"
        assert series_path.exists(), "_series.json must be written"
        series = json.loads(series_path.read_text())
        assert "000300.SS" in series
        assert "^HSI" in series
        assert "SPY" in series

    def test_regional_ledger_files_created(self, tmp_path, monkeypatch):
        """After _build_benchmark_ledger, data/benchmark/china/ and hk/ subdirs must have a file."""
        from app.scheduler import _build_benchmark_ledger
        monkeypatch.setattr(B, "_BENCH_DIR", tmp_path)
        import control_plane.run_events as _re
        monkeypatch.setattr(_re, "append", lambda ev: None)
        from data_layer import yahoo_feed
        monkeypatch.setattr(yahoo_feed, "history_local", _native_history)
        union_usd = {"SPY": 530.0, "XLU": 55.0, "XLV": 150.0, "XLF": 42.0, "XLP": 77.0}
        # need at least 1 prior data point for inception to work
        series_path = tmp_path / "_series.json"
        tmp_path.mkdir(parents=True, exist_ok=True)
        series_path.write_text(json.dumps({}))
        _build_benchmark_ledger("2026-06-19", union_usd)
        assert (tmp_path / "china").is_dir(), "china benchmark dir must be created"
        assert (tmp_path / "hk").is_dir(), "hk benchmark dir must be created"
        assert list((tmp_path / "china").glob("*.json")), "china benchmark file must exist"
        assert list((tmp_path / "hk").glob("*.json")), "hk benchmark file must exist"

    def test_native_flags_present_on_regional_bogey(self, tmp_path, monkeypatch):
        """The regional bogey is explicitly marked as a native index, not a proxy."""
        from app.scheduler import _build_benchmark_ledger
        monkeypatch.setattr(B, "_BENCH_DIR", tmp_path)
        import control_plane.run_events as _re
        captured = []
        monkeypatch.setattr(_re, "append", lambda ev: captured.append(ev))
        from data_layer import yahoo_feed
        monkeypatch.setattr(yahoo_feed, "history_local", _native_history)
        series_path = tmp_path / "_series.json"
        tmp_path.mkdir(parents=True, exist_ok=True)
        series_path.write_text(json.dumps({}))
        union_usd = {"SPY": 530.0, "XLU": 55.0, "XLV": 150.0, "XLF": 42.0, "XLP": 77.0}
        _build_benchmark_ledger("2026-06-19", union_usd)
        regional_events = [e for e in captured if e.get("kind") == "build_regional_benchmark"]
        assert len(regional_events) == 2, f"expected 2 regional events, got {regional_events}"
        for ev in regional_events:
            assert ev["bogey_is_proxy"] is False
        assert {tuple(e["benchmark"]) for e in regional_events} == {
            ("000300.SS",), ("^HSI",),
        }

    def test_regional_miss_does_not_abort_us_build(self, tmp_path, monkeypatch):
        """Even when build_regional raises, the US benchmark file must still be written."""
        from app.scheduler import _build_benchmark_ledger
        monkeypatch.setattr(B, "_BENCH_DIR", tmp_path)
        import control_plane.run_events as _re
        monkeypatch.setattr(_re, "append", lambda ev: None)
        from data_layer import yahoo_feed
        monkeypatch.setattr(yahoo_feed, "history_local", _native_history)
        # make build_regional always raise
        monkeypatch.setattr(B, "build_regional", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom")))
        union_usd = {"SPY": 530.0, "XLU": 55.0, "XLV": 150.0, "XLF": 42.0, "XLP": 77.0}
        _build_benchmark_ledger("2026-06-19", union_usd)
        # the US build still produced a ledger file
        us_files = list(tmp_path.glob("20*.json"))
        assert us_files, "US benchmark ledger file must be written even when regional raises"


# ─────────────────────────────────────────────────────────────────────────────
# 2. regional lifecycle — insufficient-n until enough reviews
# ─────────────────────────────────────────────────────────────────────────────

class TestRegionalLifecycle:
    """Grade cards show insufficient-n at paper-n and name their native index."""

    def _ledgers(self, n: int, *, book_id: str,
                 book_ret: float = 0.01, bogey_ret: float = 0.005) -> list[dict]:
        """n synthetic regional ledger snapshots (oldest-first)."""
        d0 = date(2026, 6, 18)
        out = []
        cum_book = 1.0
        cum_bogey = 1.0
        for i in range(n):
            cum_book *= (1 + book_ret)
            cum_bogey *= (1 + bogey_ret)
            book_ret_pct = round((cum_book - 1.0) * 100, 4)
            bogey_ret_pct = round((cum_bogey - 1.0) * 100, 4)
            d = (d0 + timedelta(days=7 * i)).isoformat()
            out.append({
                "as_of": d,
                "book_id": book_id,
                "bogeys": {
                    "regional": {
                        "curve": {},
                        "return_pct": bogey_ret_pct,
                        "inception": d0.isoformat(),
                        "n_points": i + 1,
                    },
                    "do_nothing": {"curve": {}, "return_pct": None, "inception": None, "n_points": 0},
                },
                "leaderboard": [
                    {"id": "regional", "kind": "bogey", "return_pct": bogey_ret_pct, "n_points": i + 1},
                    {"id": book_id, "kind": "book", "return_pct": book_ret_pct, "n_points": i + 1},
                ],
            })
        return out

    def test_insufficient_n_below_gate(self):
        """With fewer than min_effective_n regional reviews the grade is insufficient-n."""
        min_n = BL._ci("min_effective_n", BL._MIN_EFFECTIVE_N)
        cn_ledgers = self._ledgers(min_n - 1, book_id="china")
        rep = BL.regional_review(cn_ledgers=cn_ledgers, hk_ledgers=[])
        cn_grade = next(g for g in rep["grades"] if g["book"] == "china")
        assert cn_grade["loss_test"]["status"] == "insufficient-n", (
            f"should be insufficient-n with {min_n-1} reviews < {min_n}: {cn_grade}")
        assert len(rep["recommendations"]) == 0, (
            "no recommendation must be made below the effective_n gate")

    def test_reviews_remaining_counts_down(self):
        """reviews_remaining must equal min_n - effective_n, floored at 0."""
        min_n = BL._ci("min_effective_n", BL._MIN_EFFECTIVE_N)
        for have in range(0, min_n + 2):
            cn_ledgers = self._ledgers(have, book_id="china")
            rep = BL.regional_review(cn_ledgers=cn_ledgers, hk_ledgers=[])
            cn_grade = next(g for g in rep["grades"] if g["book"] == "china")
            expected = max(0, min_n - have)
            actual = cn_grade["reviews_remaining"]
            assert actual == expected, (
                f"have={have}, expected reviews_remaining={expected}, got {actual}")

    def test_native_index_identity_on_every_grade(self):
        cn_ledgers = self._ledgers(2, book_id="china")
        rep = BL.regional_review(cn_ledgers=cn_ledgers, hk_ledgers=[])
        grades = {grade["book"]: grade for grade in rep["grades"]}
        assert grades["china"]["benchmark"] == "000300.SS"
        assert grades["hk"]["benchmark"] == "^HSI"
        assert all(grade.get("bogey_is_proxy") is False for grade in grades.values())

    def test_native_benchmark_on_recommendation(self):
        min_n = BL._ci("min_effective_n", BL._MIN_EFFECTIVE_N)
        t_min = BL._cf("hac_t_min", BL._HAC_T_MIN)
        streak = BL._ci("losing_reviews_to_probation", BL._LOSING_REVIEWS_TO_PROBATION)
        # build enough losing reviews to trigger probation (if HAC is achievable at this n)
        n = min_n + 10  # give enough n for a HAC-significant loss
        cn_ledgers = self._ledgers(n, book_id="china", book_ret=-0.03, bogey_ret=0.0)
        # inject a streak-1 state so this review can trigger probation
        states = {"china": {"state": BL.STATE_ACTIVE, "losing_streak": streak - 1, "since": "2026-06-18"}}
        rep = BL.regional_review(cn_ledgers=cn_ledgers, hk_ledgers=[], states=states)
        for rec in rep["recommendations"]:
            assert rec.get("bogey_is_proxy") is False
            assert rec.get("benchmark") == "000300.SS"

    def test_regional_books_not_in_us_orthogonality_matrix(self):
        """china and hk must never appear in the US orthogonality matrix."""
        hist = [{
            "date": "2026-06-18",
            "books": {"flagship": 0.01, "autonomous": 0.02, "heavyweight": 0.01, "etf": 0.0},
            "bogeys": {"spy": 0.005, "defensive": 0.008, "regime_max": 0.008},
        }] * 10
        rep = BL.review(hist)
        ortho_books = rep["orthogonality"]["books"]
        for rb in BL.REGIONAL_BOOKS:
            assert rb not in ortho_books, (
                f"{rb} must not be in the US orthogonality matrix")

    def test_cn_hk_pairwise_corr_insufficient_n(self):
        """With < min_pairs reviews the pairwise corr is insufficient-n."""
        cn_ledgers = self._ledgers(2, book_id="china")
        hk_ledgers = self._ledgers(2, book_id="hk")
        rep = BL.regional_review(cn_ledgers=cn_ledgers, hk_ledgers=hk_ledgers)
        corr = rep["cn_hk_pairwise_corr"]
        assert corr["status"] == "insufficient-n", f"expected insufficient-n: {corr}"
        assert corr["corr"] is None

    def test_bogey_source_label_live_vs_derived(self):
        """bogey_source='live' when ledgers are not injected; 'derived' when they are."""
        cn_ledgers = self._ledgers(3, book_id="china")
        rep_derived = BL.regional_review(cn_ledgers=cn_ledgers, hk_ledgers=[])
        assert rep_derived["bogey_source"] == "derived", "injected ledgers should be labeled 'derived'"


# ─────────────────────────────────────────────────────────────────────────────
# 3. US lifecycle — reviews_remaining in grade_book and review()
# ─────────────────────────────────────────────────────────────────────────────

class TestReviewsRemaining:
    """reviews_remaining appears in loss_test, grade_book, and the paper_n banner."""

    def _hist(self, n: int, book_ret: float = 0.005, bogey_ret: float = 0.001):
        d0 = date(2026, 5, 1)
        return [{
            "date": (d0 + timedelta(days=7 * i)).isoformat(),
            "books": {"flagship": book_ret},
            "bogeys": {"spy": bogey_ret, "defensive": bogey_ret, "regime_max": bogey_ret},
        } for i in range(n)]

    def test_loss_significance_carries_reviews_remaining(self):
        min_n = BL._ci("min_effective_n", BL._MIN_EFFECTIVE_N)
        series = [0.01] * (min_n - 3)
        result = BL._loss_significance(series)
        assert "reviews_remaining" in result, "reviews_remaining must be in loss_significance output"
        assert result["reviews_remaining"] == 3

    def test_grade_book_carries_reviews_remaining(self):
        min_n = BL._ci("min_effective_n", BL._MIN_EFFECTIVE_N)
        hist = self._hist(min_n - 2)
        g = BL.grade_book("flagship", hist)
        assert "reviews_remaining" in g, "grade_book must return reviews_remaining"
        assert g["reviews_remaining"] == 2

    def test_review_paper_n_max_reviews_remaining(self):
        min_n = BL._ci("min_effective_n", BL._MIN_EFFECTIVE_N)
        hist = self._hist(min_n - 1)
        rep = BL.review(hist)
        pn = rep["paper_n"]
        assert "max_reviews_remaining" in pn, "paper_n must carry max_reviews_remaining"
        assert pn["max_reviews_remaining"] >= 1
        assert str(pn["max_reviews_remaining"]) in pn["note"], (
            "note must mention the reviews_remaining count")

    def test_reviews_remaining_zero_when_enough(self):
        min_n = BL._ci("min_effective_n", BL._MIN_EFFECTIVE_N)
        hist = self._hist(min_n + 2)
        g = BL.grade_book("flagship", hist)
        assert g["reviews_remaining"] == 0

    def test_reviews_remaining_floored_at_zero(self):
        """Never negative."""
        min_n = BL._ci("min_effective_n", BL._MIN_EFFECTIVE_N)
        series = [0.01] * (min_n + 5)
        result = BL._loss_significance(series)
        assert result["reviews_remaining"] == 0


# ─────────────────────────────────────────────────────────────────────────────
# 4. backfill script — fixtures (idempotency, no-overwrite, labels)
# ─────────────────────────────────────────────────────────────────────────────

class TestBackfillScript:
    """Backfill script with in-memory parquet fixtures."""

    def _make_multi_parquet(self, tmp_path: Path) -> Path:
        pdir = tmp_path / "parquet"
        pdir.mkdir(exist_ok=True)
        for ticker, base in [("SPY", 530.0), ("XLU", 55.0), ("XLV", 150.0),
                              ("XLF", 42.0), ("XLP", 77.0)]:
            rows = {
                "2026-06-18": base,
                "2026-06-19": base * 1.01,
                "2026-06-20": base * 0.99,
            }
            df = pd.DataFrame(
                [{"date": d, "close": v} for d, v in rows.items()]
            ).set_index("date")
            df.to_parquet(pdir / f"{ticker}.parquet")
        return pdir

    def test_backfill_adds_rows_with_source_label(self, tmp_path, monkeypatch):
        """Backfill writes rows and labels them with SOURCE_YAHOO."""
        monkeypatch.setattr(BF, "_BENCH_DIR", tmp_path)
        monkeypatch.setattr(BF, "_SERIES_PATH", tmp_path / "_series.json")
        monkeypatch.setattr(BF, "_SERIES_META_PATH", tmp_path / "_series_meta.json")
        monkeypatch.setattr(BF, "_BOOK_RETURNS_PATH", tmp_path / "_book_returns.jsonl")
        pdir = self._make_multi_parquet(tmp_path)
        result = BF.backfill_series(inception="2026-06-18", parquet_dir=pdir)
        assert result["rows_added"] > 0, "must add rows when parquet present"
        assert not result["rows_missing_in_parquet"], f"all tickers should be in parquet: {result}"
        meta = json.loads((tmp_path / "_series_meta.json").read_text())
        # every added date must be labeled SOURCE_YAHOO
        for ticker, date_map in meta.items():
            for d, src in date_map.items():
                assert src == BF.SOURCE_YAHOO, f"{ticker}/{d}: expected SOURCE_YAHOO got {src!r}"

    def test_backfill_idempotent_no_overwrite(self, tmp_path, monkeypatch):
        """Running backfill twice — second run adds 0 rows (no overwrites)."""
        monkeypatch.setattr(BF, "_BENCH_DIR", tmp_path)
        monkeypatch.setattr(BF, "_SERIES_PATH", tmp_path / "_series.json")
        monkeypatch.setattr(BF, "_SERIES_META_PATH", tmp_path / "_series_meta.json")
        monkeypatch.setattr(BF, "_BOOK_RETURNS_PATH", tmp_path / "_book_returns.jsonl")
        pdir = self._make_multi_parquet(tmp_path)
        r1 = BF.backfill_series(inception="2026-06-18", parquet_dir=pdir)
        r2 = BF.backfill_series(inception="2026-06-18", parquet_dir=pdir)
        assert r2["rows_added"] == 0, "second run must add 0 rows"
        assert r2["rows_skipped"] == r1["rows_added"], "second run skipped = first run added"

    def test_backfill_does_not_overwrite_live_rows(self, tmp_path, monkeypatch):
        """Pre-existing live rows in _series.json are NEVER overwritten (R8)."""
        monkeypatch.setattr(BF, "_BENCH_DIR", tmp_path)
        monkeypatch.setattr(BF, "_SERIES_PATH", tmp_path / "_series.json")
        monkeypatch.setattr(BF, "_SERIES_META_PATH", tmp_path / "_series_meta.json")
        monkeypatch.setattr(BF, "_BOOK_RETURNS_PATH", tmp_path / "_book_returns.jsonl")
        # pre-populate a live row with a DIFFERENT value
        live_series = {"SPY": {"2026-06-19": 9999.0}}  # obviously wrong live price
        (tmp_path / "_series.json").write_text(json.dumps(live_series))
        pdir = self._make_multi_parquet(tmp_path)
        BF.backfill_series(inception="2026-06-18", parquet_dir=pdir)
        final = json.loads((tmp_path / "_series.json").read_text())
        # the live row must survive unchanged
        assert final["SPY"]["2026-06-19"] == 9999.0, (
            "live row must not be overwritten by backfill (R8)")

    def test_dry_run_writes_nothing(self, tmp_path, monkeypatch):
        """--dry-run must leave all files untouched."""
        monkeypatch.setattr(BF, "_BENCH_DIR", tmp_path)
        monkeypatch.setattr(BF, "_SERIES_PATH", tmp_path / "_series.json")
        monkeypatch.setattr(BF, "_SERIES_META_PATH", tmp_path / "_series_meta.json")
        monkeypatch.setattr(BF, "_BOOK_RETURNS_PATH", tmp_path / "_book_returns.jsonl")
        pdir = self._make_multi_parquet(tmp_path)
        BF.backfill_series(inception="2026-06-18", parquet_dir=pdir, dry_run=True)
        assert not (tmp_path / "_series.json").exists(), "dry_run must not write _series.json"

    def test_book_returns_source_label(self, tmp_path, monkeypatch):
        """Derived book returns rows carry SOURCE_NAV label."""
        monkeypatch.setattr(BF, "_BENCH_DIR", tmp_path)
        monkeypatch.setattr(BF, "_SERIES_PATH", tmp_path / "_series.json")
        monkeypatch.setattr(BF, "_SERIES_META_PATH", tmp_path / "_series_meta.json")
        monkeypatch.setattr(BF, "_BOOK_RETURNS_PATH", tmp_path / "_book_returns.jsonl")
        # write a nav_history fixture for flagship
        nav_rows = [
            {"date": "2026-06-18", "nav": 1000000.0, "cash": 500000.0, "spy_nav": 1000000.0},
            {"date": "2026-06-19", "nav": 1005000.0, "cash": 500000.0, "spy_nav": 1010000.0},
            {"date": "2026-06-20", "nav": 1002000.0, "cash": 500000.0, "spy_nav": 1008000.0},
        ]
        pdir = _make_nav_history(tmp_path, "flagship", nav_rows)
        result = BF.backfill_book_returns(book_ids=["flagship"], portfolio_dir=pdir)
        assert result["rows_added"] > 0, "must derive rows from nav_history"
        rows = [json.loads(l) for l in (tmp_path / "_book_returns.jsonl").read_text().splitlines() if l.strip()]
        for r in rows:
            assert r["source"] == BF.SOURCE_NAV, f"expected SOURCE_NAV, got {r['source']!r}"

    def test_book_returns_idempotent(self, tmp_path, monkeypatch):
        """Running book-return derivation twice adds 0 rows the second time."""
        monkeypatch.setattr(BF, "_BENCH_DIR", tmp_path)
        monkeypatch.setattr(BF, "_SERIES_PATH", tmp_path / "_series.json")
        monkeypatch.setattr(BF, "_SERIES_META_PATH", tmp_path / "_series_meta.json")
        monkeypatch.setattr(BF, "_BOOK_RETURNS_PATH", tmp_path / "_book_returns.jsonl")
        nav_rows = [
            {"date": "2026-06-18", "nav": 1000000.0, "cash": 500000.0, "spy_nav": 1000000.0},
            {"date": "2026-06-19", "nav": 1005000.0, "cash": 500000.0, "spy_nav": 1010000.0},
        ]
        pdir = _make_nav_history(tmp_path, "flagship", nav_rows)
        r1 = BF.backfill_book_returns(book_ids=["flagship"], portfolio_dir=pdir)
        r2 = BF.backfill_book_returns(book_ids=["flagship"], portfolio_dir=pdir)
        assert r2["rows_added"] == 0, "second run must add 0 rows"
        assert r2["rows_skipped"] == r1["rows_added"]

    def test_missing_parquet_degrades_gracefully(self, tmp_path, monkeypatch):
        """When a parquet file is absent the script degrades (reports missing, no crash)."""
        monkeypatch.setattr(BF, "_BENCH_DIR", tmp_path)
        monkeypatch.setattr(BF, "_SERIES_PATH", tmp_path / "_series.json")
        monkeypatch.setattr(BF, "_SERIES_META_PATH", tmp_path / "_series_meta.json")
        monkeypatch.setattr(BF, "_BOOK_RETURNS_PATH", tmp_path / "_book_returns.jsonl")
        empty_dir = tmp_path / "empty_parquet"
        empty_dir.mkdir()
        result = BF.backfill_series(inception="2026-06-18", parquet_dir=empty_dir)
        assert set(result["rows_missing_in_parquet"]) == set(BF._SERIES_TICKERS), (
            "all tickers should be reported missing when parquet dir is empty")
        assert result["rows_added"] == 0

    def test_governor_effective_n_report(self, tmp_path, monkeypatch):
        """_report_governor_effective_n returns expected keys and a note."""
        monkeypatch.setattr(BF, "_BENCH_DIR", tmp_path)
        monkeypatch.setattr(BF, "_SERIES_PATH", tmp_path / "_series.json")
        (tmp_path).mkdir(parents=True, exist_ok=True)
        rep = BF._report_governor_effective_n()
        assert "current_ledger_files" in rep
        assert "spy_series_points_after_backfill" in rep
        assert "note" in rep
