"""The all-China Brain book — calendar, FX-aware pricing, intake funnel, desk MCP, builder.

The China analogue of test_autonomous_portfolio: it exercises the new venue-aware pieces
(Asia calendar, CNY/HKD→USD conversion, the China candidate funnel, the china desk tools) and
the run_china build offline + with a simulated multi-venue submission, all isolated to a tmp
store so no real book is touched.
"""
from __future__ import annotations

import asyncio
import json
from datetime import date, datetime

import pytest

from portfolio import paper_account, registry


@pytest.fixture
def iso(tmp_path, monkeypatch):
    """Isolate book state and make execution-time assumptions deterministic.

    The run-level tests assert rebalance results, not the wall-clock market-hours
    gate (which has its own calendar/settlement tests). Without this seam the
    same test queues or fills depending on what time pytest happens to run.
    """
    monkeypatch.setattr(registry, "_ROOT", tmp_path, raising=False)
    from bot import settle
    monkeypatch.setattr(settle, "is_open", lambda portfolio_id: True)
    # A bare CI worktree intentionally has no vendored live snapshot. Seed only
    # the health probe seam so normal run tests exercise the book; outage tests
    # explicitly arm a failed live adapter and still take precedence.
    from data_layer import terminal_prices
    monkeypatch.setattr(
        terminal_prices,
        "price_local",
        lambda ticker: 1.0 if ticker == "600519.SS" else None,
    )
    return tmp_path


# --------------------------------------------------------------------------- #
# registry + benchmark
# --------------------------------------------------------------------------- #
def test_registry_registers_china():
    assert "china" in registry.ids()
    meta = registry.get("china")
    assert meta["kind"] == "china_brain" and meta["manager"] == "brain"
    assert registry.starting_nav("china") == 1_000_000.0


def test_benchmark_resolution():
    assert registry.benchmark("china") == "000300.SS"
    assert registry.benchmark("hk") == "^HSI"
    assert registry.benchmark_name("china") == "CSI 300"
    assert registry.benchmark_name("hk") == "Hang Seng"
    # the US books stay on SPY (back-compat) — unknown / None too
    for pid in ("flagship", "autonomous", "heavyweight", None, "nope"):
        assert registry.benchmark(pid) == "SPY"
    assert paper_account._benchmark_for("china") == "000300.SS"
    assert paper_account._benchmark_for("flagship") == "SPY"


# --------------------------------------------------------------------------- #
# China market calendar
# --------------------------------------------------------------------------- #
def test_china_calendar_trading_days():
    from portfolio import china_calendar as cc
    assert cc.is_trading_day(date(2026, 6, 22)) is True      # a Monday, not a holiday
    assert cc.is_trading_day(date(2026, 6, 20)) is False     # Saturday
    assert cc.is_trading_day(date(2026, 6, 21)) is False     # Sunday
    assert cc.is_trading_day(date(2026, 10, 1)) is False     # National Day holiday
    assert cc.is_holiday(date(2026, 2, 17)) is True          # Spring Festival


def test_china_calendar_sessions():
    from portfolio import china_calendar as cc
    CST = cc.CST
    mon = date(2026, 6, 22)
    assert cc.is_open(datetime(2026, 6, 22, 10, 0, tzinfo=CST)) is True    # morning session
    assert cc.is_open(datetime(2026, 6, 22, 12, 0, tzinfo=CST)) is False   # lunch break
    assert cc.is_open(datetime(2026, 6, 22, 14, 0, tzinfo=CST)) is True    # afternoon session
    assert cc.is_open(datetime(2026, 6, 22, 16, 0, tzinfo=CST)) is False   # after close
    assert cc.is_open(datetime(2026, 6, 20, 10, 0, tzinfo=CST)) is False   # weekend
    # next_open from a Saturday lands on the next trading Monday's 09:30
    nxt = cc.next_open(datetime(2026, 6, 20, 10, 0, tzinfo=CST))
    assert nxt.date() == date(2026, 6, 22) and nxt.hour == 9 and nxt.minute == 30
    st = cc.status(datetime(2026, 6, 22, 10, 0, tzinfo=CST))
    assert st["open"] is True and st["venue"].startswith("A-share")


# --------------------------------------------------------------------------- #
# FX — multi-currency → USD
# --------------------------------------------------------------------------- #
def test_fx_currency_and_market():
    from portfolio import fx
    assert fx.currency_of("600519.SS") == "CNY" and fx.market_of("600519.SS") == "A"
    assert fx.currency_of("300750.SZ") == "CNY"
    assert fx.currency_of("0700.HK") == "HKD" and fx.market_of("0700.HK") == "HK"
    assert fx.currency_of("BABA") == "USD" and fx.market_of("BABA") == "US"


def test_fx_to_usd(monkeypatch):
    from portfolio import fx
    monkeypatch.setattr(fx, "rate_per_usd", lambda cur: {"CNY": 7.0, "HKD": 7.8}.get(cur, 1.0))
    assert fx.to_usd(70.0, "600519.SS") == pytest.approx(10.0)    # CNY 70 / 7.0
    assert fx.to_usd(78.0, "0700.HK") == pytest.approx(10.0)      # HKD 78 / 7.8
    assert fx.to_usd(10.0, "BABA") == pytest.approx(10.0)         # already USD
    assert fx.to_usd(None, "BABA") is None
    assert fx.to_usd(0, "600519.SS") is None                      # non-positive → None


def test_fx_to_cny(monkeypatch):
    """The China book's base currency is CNY: A-shares native, HK (HKD) and ADR (USD) converted."""
    from portfolio import fx
    monkeypatch.setattr(fx, "rate_per_usd", lambda cur: {"CNY": 7.0, "HKD": 7.8}.get(cur, 1.0))
    assert fx.to_cny(70.0, "600519.SS") == pytest.approx(70.0)    # A-share: already CNY
    assert fx.to_cny(78.0, "0700.HK") == pytest.approx(70.0)      # HKD 78 * (7.0/7.8) = CNY 70
    assert fx.to_cny(10.0, "BABA") == pytest.approx(70.0)         # USD 10 * 7.0 = CNY 70
    # usd_to_cny: the shared store returns USD → multiply by CNY-per-USD
    assert fx.usd_to_cny(10.0) == pytest.approx(70.0)
    assert fx.usd_to_cny(None) is None
    # round-trip an A-share: native CNY → USD (shared store) → CNY base is loss-free
    assert fx.usd_to_cny(fx.to_usd(700.0, "600519.SS")) == pytest.approx(700.0)


def test_fx_rate_fallback(monkeypatch):
    """With no live source, rate_per_usd falls back to the static peg/recent constants."""
    from portfolio import fx
    fx.clear_cache()
    monkeypatch.setattr(fx, "_from_yahoo_store", lambda s: None)
    monkeypatch.setattr(fx, "_from_forex_snapshot", lambda k: None)
    assert fx.rate_per_usd("USD") == 1.0
    assert fx.rate_per_usd("CNY") == fx._FALLBACK["CNY"]
    assert fx.rate_per_usd("HKD") == fx._FALLBACK["HKD"]
    fx.clear_cache()


# --------------------------------------------------------------------------- #
# paper_account pricing dispatch (hermetic fixtures under a tmp vendor tree)
# --------------------------------------------------------------------------- #
def test_live_price_dispatch_and_fx(tmp_path, monkeypatch):
    from portfolio import fx
    site = tmp_path / "vendor" / "macro" / "site"
    for sub, tk, px in (("chinastockdata", "600519.SS", 700.0),
                        ("hkstockdata", "0700.HK", 390.0),
                        ("stockdata", "BABA", 105.0)):
        d = site / sub
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{tk}.json").write_text(json.dumps({"tech": {"price": px}}))
    monkeypatch.setattr(paper_account, "_ROOT", tmp_path, raising=False)
    monkeypatch.setattr(fx, "rate_per_usd", lambda cur: {"CNY": 7.0, "HKD": 7.8}.get(cur, 1.0))
    assert paper_account._live_price("600519.SS") == pytest.approx(100.0)   # 700 CNY / 7.0
    assert paper_account._live_price("0700.HK") == pytest.approx(50.0)      # 390 HKD / 7.8
    assert paper_account._live_price("BABA") == pytest.approx(105.0)        # USD as-is
    assert paper_account._live_price("UNKNOWN.SS") is None


def test_tushare_ticker_mapping():
    from data_layer import tushare_feed
    assert tushare_feed._to_ts_code("600519.SS") == "600519.SH"   # Shanghai .SS → .SH
    assert tushare_feed._to_ts_code("000001.SZ") == "000001.SZ"   # Shenzhen unchanged
    assert tushare_feed._to_ts_code("0700.HK") is None            # HK marks via Yahoo, not Tushare
    assert tushare_feed._to_ts_code("BABA") is None               # ADR not covered


def test_tushare_timeout_is_configurable_and_bounded(monkeypatch):
    from data_layer import tushare_feed
    monkeypatch.setenv("TUSHARE_TIMEOUT_SEC", "5")
    assert tushare_feed._timeout() == 5.0
    monkeypatch.setenv("TUSHARE_TIMEOUT_SEC", "0")
    assert tushare_feed._timeout() == 1.0
    monkeypatch.setenv("TUSHARE_TIMEOUT_SEC", "bad")
    assert tushare_feed._timeout() == 30.0


def test_tushare_price_local_and_degrade(monkeypatch):
    from data_layer import tushare_feed
    tushare_feed.clear_cache()
    monkeypatch.setattr(tushare_feed, "_token", lambda: "tok")
    monkeypatch.setattr(tushare_feed, "_call", lambda api, params, fields:
                        {"items": [["688411.SH", 286.98], ["000001.SZ", 12.3]]}
                        if params.get("trade_date") == "20260622" else {"items": []})
    assert tushare_feed.price_local("688411.SS", asof="2026-06-22") == pytest.approx(286.98)
    assert tushare_feed.price_local("000001.SZ", asof="2026-06-22") == pytest.approx(12.3)
    assert tushare_feed.price_local("0700.HK", asof="2026-06-22") is None    # HK is not an A-share (marks via Yahoo)
    # no token → degrade to None (paper_account falls back to the snapshot)
    tushare_feed.clear_cache()
    monkeypatch.setattr(tushare_feed, "_token", lambda: None)
    assert tushare_feed.price_local("688411.SS", asof="2026-06-22") is None
    tushare_feed.clear_cache()


def test_yahoo_feed_hk_warm_and_cache(monkeypatch):
    """The Yahoo HK feed batches the whole basket into ONE yf.download and serves the latest close
    per symbol from cache (Yahoo's HK symbol == the book's, no code mapping)."""
    import sys, types
    import pandas as pd
    from data_layer import yahoo_feed
    yahoo_feed.clear_cache()
    calls = {"n": 0}

    def fake_download(tickers, **kw):
        calls["n"] += 1
        idx = pd.to_datetime(["2026-06-18", "2026-06-22"])
        cols = pd.MultiIndex.from_product([["Close"], list(tickers)])
        data = {("Close", t): [1.0, {"0700.HK": 433.0, "3988.HK": 5.1}.get(t, 9.0)] for t in tickers}
        return pd.DataFrame(data, index=idx, columns=cols)

    fake_yf = types.ModuleType("yfinance")
    fake_yf.download = fake_download
    monkeypatch.setitem(sys.modules, "yfinance", fake_yf)
    yahoo_feed.warm(["0700.HK", "3988.HK"])
    assert yahoo_feed.price_local("0700.HK") == pytest.approx(433.0)   # latest (06-22) close
    assert yahoo_feed.price_local("3988.HK") == pytest.approx(5.1)
    assert calls["n"] == 1                                             # one batched call for both names
    yahoo_feed.clear_cache()


def test_yahoo_feed_native_index_history_is_cached(monkeypatch):
    import sys, types
    import pandas as pd
    from data_layer import yahoo_feed

    yahoo_feed.clear_cache()
    calls = {"n": 0}

    def fake_download(ticker, **kw):
        calls["n"] += 1
        idx = pd.to_datetime(["2026-06-18", "2026-06-19", "2026-06-22"])
        cols = pd.MultiIndex.from_product([["Close"], [ticker]])
        return pd.DataFrame({("Close", ticker): [4_000.0, 4_050.0, 4_100.0]},
                            index=idx, columns=cols)

    fake_yf = types.ModuleType("yfinance")
    fake_yf.download = fake_download
    monkeypatch.setitem(sys.modules, "yfinance", fake_yf)

    first = yahoo_feed.history_local("000300.SS")
    second = yahoo_feed.history_local("000300.SS")
    assert list(first.astype(float)) == [4_000.0, 4_050.0, 4_100.0]
    assert list(second.astype(float)) == list(first.astype(float))
    assert calls["n"] == 1
    yahoo_feed.clear_cache()


def test_live_price_prefers_tushare_else_snapshot(tmp_path, monkeypatch):
    """A-shares mark to the live Tushare CNY close; HK marks via Yahoo and falls back to the
    vendored snapshot when Yahoo returns nothing (here: yfinance is stubbed off in tests)."""
    from data_layer import tushare_feed
    from portfolio import fx
    monkeypatch.setattr(fx, "rate_per_usd", lambda cur: {"CNY": 7.0, "HKD": 7.8}.get(cur, 1.0))
    monkeypatch.setattr(tushare_feed, "price_local",
                        lambda t, asof=None: 700.0 if t == "600519.SS" else None)
    # seed a vendored HK snapshot fixture for the fallback path
    site = tmp_path / "vendor" / "macro" / "site" / "hkstockdata"
    site.mkdir(parents=True, exist_ok=True)
    (site / "0700.HK.json").write_text(json.dumps({"tech": {"price": 390.0}}))
    monkeypatch.setattr(paper_account, "_ROOT", tmp_path, raising=False)
    assert paper_account._live_price("600519.SS") == pytest.approx(100.0)   # Tushare 700 CNY / 7.0
    assert paper_account._live_price("0700.HK") == pytest.approx(50.0)      # snapshot 390 HKD / 7.8


def test_live_price_hk_prefers_yahoo(monkeypatch):
    """An HK name marks to the live Yahoo HKD close (converted to USD via portfolio.fx)."""
    from data_layer import yahoo_feed
    from portfolio import fx
    monkeypatch.setattr(fx, "rate_per_usd", lambda cur: {"HKD": 7.8}.get(cur, 1.0))
    monkeypatch.setattr(yahoo_feed, "price_local", lambda t, asof=None: 433.0 if t == "0700.HK" else None)
    assert paper_account._live_price("0700.HK") == pytest.approx(433.0 / 7.8)   # live Yahoo 433 HKD / 7.8


def test_mark_uses_per_book_benchmark(iso, monkeypatch):
    """A China mark initializes the benchmark shares from CSI 300 (not SPY)."""
    monkeypatch.setattr(paper_account, "_current_price", lambda t: {"000300.SS": 4_000.0}.get(t))
    paper_account.mark({"000300.SS": 4_000.0}, "2026-06-22", portfolio_id="china")
    rows = [json.loads(l) for l in
            (registry.data_dir("china") / "nav_history.jsonl").read_text().splitlines() if l.strip()]
    assert rows[-1]["spy_nav"] == pytest.approx(1_000_000.0)
    assert rows[-1]["benchmark"] == "000300.SS"
    acct = json.loads((registry.data_dir("china") / "account.json").read_text())
    assert acct["spy_inception_price"] == 4_000.0
    assert acct["benchmark_symbol"] == "000300.SS"


# --------------------------------------------------------------------------- #
# China intake funnel
# --------------------------------------------------------------------------- #
def test_china_intake_seed_when_empty(monkeypatch):
    from brain import china_intake
    monkeypatch.setattr(china_intake, "_read", lambda rel: None)   # no boards built
    r = china_intake.build(20)
    assert r["candidates"], "seed fallback should never leave the queue empty"
    assert r["candidates"][0]["sources"] == ["seed"]
    venues = {c["venue"] for c in r["candidates"]}
    assert {"A-share", "HK", "ADR"} <= venues                     # the seed spans all three venues


def test_china_intake_ranks_and_handles_conviction_dict(monkeypatch):
    from brain import china_intake

    def fake_read(rel):
        if rel.endswith("china_standouts.json"):
            return {"as_of": "2026-06-18", "buy": [
                {"ticker": "600519.SS", "label": "BUY ZONE", "dir": "up",
                 "conviction": {"score": 80, "band": "constructive"}},   # conviction is a DICT
                {"ticker": "000001.SZ", "label": "AVOID", "dir": "down",
                 "conviction": {"score": 20}},
            ]}
        if rel.endswith("hk_standouts.json"):
            return {"buy": [{"ticker": "0700.HK", "label": "UPTREND", "dir": "up",
                             "conviction": {"score": 70}}]}
        if rel.endswith("china_alpha.json"):
            return {"top": [{"ticker": "600519.SS", "alpha": 2.4, "entry": "intact"}]}
        if rel.endswith("china_regime/latest.json"):
            return {"date": "2026-06-18", "quad": "Q3", "quad_name": "Stagflation"}
        return None

    monkeypatch.setattr(china_intake, "_read", fake_read)
    r = china_intake.build(20)
    by = {c["ticker"]: c for c in r["candidates"]}
    # 600519 is corroborated (standouts + alpha) → ranks first, lean up, n_sources >= 2
    assert r["candidates"][0]["ticker"] == "600519.SS"
    assert by["600519.SS"]["n_sources"] >= 2 and by["600519.SS"]["lean"] == 1
    assert by["000001.SZ"]["lean"] == -1                          # AVOID/down
    assert by["0700.HK"]["venue"] == "HK"
    assert r["macro_context"]["quad_name"] == "Stagflation"


# --------------------------------------------------------------------------- #
# China desk MCP
# --------------------------------------------------------------------------- #
def test_submit_book_scales_and_tags_venue(iso):
    res = asyncio.run(china_submit({
        "holdings": [
            {"ticker": "600519.SS", "weight": 0.7, "rationale": "moat"},
            {"ticker": "300750.SZ", "weight": 0.7, "rationale": "battery leader"},
            {"ticker": "0700.HK", "weight": 0.3, "rationale": "off-venue HK"},   # REJECTED: A-share book only
            {"ticker": "BABA", "weight": 0.2, "rationale": ""},                  # dropped: no rationale
        ],
        "summary": "A-share barbell",
    }))
    from brain import china_mcp
    sub = china_mcp.read_submission()
    # only the two A-shares survive — HK is off-venue, BABA has no rationale
    assert {h["ticker"] for h in sub["holdings"]} == {"600519.SS", "300750.SZ"}
    # gross 1.4 > 1 → scaled back to no-leverage (1.0)
    assert sub["scaled_to_no_leverage"] is True and sub["gross"] == pytest.approx(1.0)
    venues = {h["ticker"]: h["venue"] for h in sub["holdings"]}
    assert venues["600519.SS"] == "A-share" and venues["300750.SZ"] == "A-share"
    # the off-venue name is reported as rejected in the tool result
    note = res["content"][0]["text"]
    assert "0700.HK" in note and "REJECTED" in note


def test_get_quote_reports_cny_and_venue(iso, monkeypatch):
    from brain import china_mcp
    from portfolio import fx
    monkeypatch.setattr(paper_account, "_current_price", lambda t: {"0700.HK": 50.0}.get(t))  # USD
    monkeypatch.setattr(fx, "rate_per_usd", lambda cur: {"CNY": 7.0, "HKD": 7.8}.get(cur, 1.0))
    out = asyncio.run(china_mcp.get_quote.handler({"ticker": "0700.HK"}))
    payload = json.loads(out["content"][0]["text"])
    assert payload["venue"] == "HK" and payload["currency"] == "HKD"
    assert payload["priceable"] is True
    assert payload["price_local"] == pytest.approx(390.0)         # 50 USD * 7.8 = native HKD
    assert payload["base_currency"] == "CNY"
    assert payload["price_base"] == pytest.approx(350.0)          # 50 USD * 7.0 = CNY the book marks at
    miss = json.loads(asyncio.run(china_mcp.get_quote.handler({"ticker": "9999.HK"}))["content"][0]["text"])
    assert miss["priceable"] is False


# --------------------------------------------------------------------------- #
# run_china builder — offline + simulated multi-venue submission
# --------------------------------------------------------------------------- #
def test_run_china_offline_inaugural(iso, monkeypatch):
    monkeypatch.setattr(paper_account, "_current_price", lambda t: {"000300.SS": 4_000.0}.get(t))
    from bot import china
    out = china.run_china(asof="2026-06-22", armed=False)
    assert out["inaugural"] is True and out["decided"] is False
    assert out["nav"] == 1_000_000.0
    latest = json.loads((registry.data_dir("china") / "latest.json").read_text())
    assert latest["portfolio_id"] == "china" and latest["schema"] == "portfolio.v1"
    assert latest["benchmark"] == "000300.SS" and latest["currency"] == "CNY"


def test_run_china_rejects_offvenue_and_executes_a_shares(iso, monkeypatch):
    # two A-shares (one priceable, one not) + an off-venue HK name the A-share book must reject
    prices = {"600519.SS": 100.0, "300750.SZ": 200.0, "000300.SS": 4_000.0}  # 688981.SS unpriceable
    monkeypatch.setattr(paper_account, "_current_price", lambda t: prices.get(t))
    from bot import china
    from brain import china_mcp

    def fake_brain(asof, inaugural):
        china_mcp.submission_path().parent.mkdir(parents=True, exist_ok=True)
        china_mcp.submission_path().write_text(json.dumps({
            "holdings": [
                {"ticker": "600519.SS", "weight": 0.4, "rationale": "A-share moat", "venue": "A-share"},
                {"ticker": "300750.SZ", "weight": 0.3, "rationale": "battery leader", "venue": "A-share"},
                {"ticker": "0700.HK", "weight": 0.2, "rationale": "off-venue HK", "venue": "HK"},
                {"ticker": "688981.SS", "weight": 0.1, "rationale": "unpriceable A-share", "venue": "A-share"},
            ],
            "summary": "A-share barbell", "gross": 1.0,
        }))
        return {"ok": True, "text": "x", "cost_usd": 0.0, "model": "claude-opus-4-8"}

    monkeypatch.setattr(china, "_run_brain", fake_brain)
    out = china.run_china(asof="2026-06-22", armed=True)
    assert out["decided"] is True
    # the HK name is rejected in the trusted layer (off-venue) — NOT merely unpriceable
    assert "0700.HK" in out.get("rejected_offvenue", [])
    assert "0700.HK" not in out["skipped_unpriceable"]
    # the A-share with no live price is honestly skipped
    assert "688981.SS" in out["skipped_unpriceable"]
    sides = {(t["ticker"], t["side"]) for t in out["executed"]}
    assert {("600519.SS", "buy"), ("300750.SZ", "buy")} <= sides
    assert not any(tk == "0700.HK" for tk, _ in sides)          # off-venue never entered the book
    # NAV stays ~¥1M, invested across the two priceable A-shares
    assert out["nav"] == pytest.approx(1_000_000.0, rel=1e-6)
    decs = china.load_decisions()
    assert decs and decs[0]["summary"] == "A-share barbell"
    latest = json.loads((registry.data_dir("china") / "latest.json").read_text())
    venues = {p["ticker"]: p.get("venue") for p in latest["positions"]}
    assert venues.get("600519.SS") == "A-share" and venues.get("300750.SZ") == "A-share"
    assert "0700.HK" not in venues


def test_run_china_marks_in_cny(iso, monkeypatch):
    """The book is denominated in CNY: the USD-normalised shared-store mark is booked at
    usd * CNY-per-USD, NAV stays ¥1M. (A-shares quote CNY natively, but the shared price store
    returns USD — so the book still runs every mark through the usd→CNY conversion.)"""
    from bot import china
    from brain import china_mcp
    from portfolio import fx
    monkeypatch.setattr(fx, "rate_per_usd", lambda cur: {"CNY": 7.0, "HKD": 7.8}.get(cur, 1.0))
    monkeypatch.setattr(paper_account, "_current_price",
                        lambda t: {"600519.SS": 100.0, "000300.SS": 4_000.0}.get(t))  # USD

    def fake_brain(asof, inaugural):
        china_mcp.submission_path().parent.mkdir(parents=True, exist_ok=True)
        china_mcp.submission_path().write_text(json.dumps({
            "holdings": [{"ticker": "600519.SS", "weight": 0.5, "rationale": "a-share moat"}],
            "summary": "a-share core", "gross": 0.5}))
        return {"ok": True, "model": "m"}

    monkeypatch.setattr(china, "_run_brain", fake_brain)
    out = china.run_china(asof="2026-06-22", armed=True)
    latest = json.loads((registry.data_dir("china") / "latest.json").read_text())
    assert latest["currency"] == "CNY"
    pos = next(p for p in latest["positions"] if p["ticker"] == "600519.SS")
    assert pos["cost_basis"] == pytest.approx(700.0)            # USD 100 * 7.0 = CNY 700
    assert out["nav"] == pytest.approx(1_000_000.0, rel=1e-6)    # ¥1M, self-consistent


def test_display_name_by_venue(monkeypatch):
    """A-shares show the Chinese name; HK and ADRs show the English name; missing → ticker."""
    from brain import china_intake
    china_intake.clear_name_cache()
    monkeypatch.setattr(china_intake, "_read", lambda rel: {
        "chinastockdata/600519.SS.json": {"name": "Kweichow Moutai Co., Ltd. / 贵州茅台"},
        "hkstockdata/0700.HK.json": {"name": "Tencent"},
        "stockdata/BABA.json": {"name": "Alibaba Group (ADR)"},
    }.get(rel))
    assert china_intake.display_name("600519.SS") == "贵州茅台"          # A-share → Chinese half
    assert china_intake.display_name("0700.HK") == "Tencent"             # HK → English
    assert china_intake.display_name("BABA") == "Alibaba Group (ADR)"    # ADR → English
    assert china_intake.display_name("9999.HK") == "9999.HK"             # no name → ticker fallback
    china_intake.clear_name_cache()


def test_us_display_name_recovers_etf_and_placeholder_company_names(monkeypatch):
    """US rows resolve when Macro omits an ETF snapshot or publishes a ticker placeholder."""
    from brain import china_intake
    china_intake.clear_name_cache()

    def fake_read(rel):
        if rel == "stockdata/GATX.json":
            return {
                "name": "GATX",
                "profile": {
                    "description": "GATX Corporation is a railcar lessor operating worldwide.",
                },
            }
        return None

    monkeypatch.setattr(china_intake, "_read", fake_read)
    assert china_intake.display_name("SGOV") == "iShares 0-3 Month Treasury Bond ETF"
    assert china_intake.display_name("KRE") == "SPDR S&P Regional Banking ETF"
    assert china_intake.display_name("GATX") == "GATX Corporation"
    china_intake.clear_name_cache()


def test_hk_display_name_has_english_and_native_chinese_variants(monkeypatch):
    """HK names resolve from the bilingual market universe without machine translation."""
    from brain import china_intake
    china_intake.clear_name_cache()

    def fake_read(rel):
        if rel == "hkstockdata/0700.HK.json":
            return {"name": "Tencent"}
        if rel == "marketdata/hk_heatmap.json":
            return {"tiles": [
                {"t": "0700.HK", "name": "Tencent", "name_zh": "腾讯控股"},
                {"t": "0941.HK", "name": "China Mobile", "name_zh": "中国移动"},
            ]}
        return None

    monkeypatch.setattr(china_intake, "_read", fake_read)
    assert china_intake.display_name("0700.HK") == "Tencent"
    assert china_intake.display_name_zh("0700.HK") == "腾讯控股"
    assert china_intake.display_name("0941.HK") == "China Mobile"
    assert china_intake.display_name_zh("0941.HK") == "中国移动"
    china_intake.clear_name_cache()


def test_a_share_display_name_falls_back_to_complete_heatmap(monkeypatch):
    """A-share holdings resolve natively when optional per-name snapshots are absent."""
    from brain import china_intake
    china_intake.clear_name_cache()

    def fake_read(rel):
        if rel == "marketdata/china_heatmap.json":
            return {"tiles": [
                {"t": "600882.SS", "name": "Milkground Food", "name_zh": "妙可蓝多"},
                {"t": "002020.SZ", "name": "Jingxin Pharma", "name_zh": "京新药业"},
            ]}
        return None

    monkeypatch.setattr(china_intake, "_read", fake_read)
    assert china_intake.display_name("600882.SS") == "妙可蓝多"
    assert china_intake.display_name("002020.SZ") == "京新药业"
    china_intake.clear_name_cache()


def test_display_name_falls_back_to_board(monkeypatch):
    """A freshly surfaced name with NO per-name `chinastockdata/<T>.json` snapshot still resolves via
    the desk boards (every buy-board / alpha-leader row carries a `name`). Regression for 603301
    Zhende Medical showing as a bare '603301.SS' on the book after the 2026-06-22 feed-recovery rerun."""
    from brain import china_intake
    china_intake.clear_name_cache()

    def fake_read(rel):
        # no per-name snapshot for either name; the name lives only on the boards
        if rel == "factordata/china_standouts.json":
            return {"buy": [{"ticker": "603301.SS", "name": "Zhende Medical Co., Ltd. / 振德医疗"}]}
        if rel == "factordata/china_alpha.json":
            return {"top": [{"ticker": "603301.SS", "name": "Zhende Medical Co., Ltd. / 振德医疗"}]}
        if rel == "factordata/hk_standouts.json":
            return {"buy": [{"ticker": "9999.HK", "name": "Acme Holdings"}]}
        return None

    monkeypatch.setattr(china_intake, "_read", fake_read)
    assert china_intake.display_name("603301.SS") == "振德医疗"      # A-share → 中文 half from the board
    assert china_intake.display_name("9999.HK") == "Acme Holdings"   # HK → English from the board
    assert china_intake.display_name("000001.SZ") == "000001.SZ"     # on no board → ticker fallback
    china_intake.clear_name_cache()


def test_run_china_attaches_names_and_delegates_translation(iso, monkeypatch):
    """Every holding carries a display name, and the report is auto-translated via the Haiku tier."""
    from bot import china
    from brain import china_intake, china_mcp, translate
    monkeypatch.setattr(paper_account, "_current_price",
                        lambda t: {"600519.SS": 100.0, "000300.SS": 4_000.0}.get(t))
    monkeypatch.setattr(china_intake, "display_name", lambda t: {"600519.SS": "贵州茅台"}.get(t, t))
    captured: dict = {}
    monkeypatch.setattr(translate, "translate_and_cache",
                        lambda texts: (captured.update(texts=list(texts)), {})[1])

    def fake_brain(asof, inaugural):
        china_mcp.submission_path().parent.mkdir(parents=True, exist_ok=True)
        china_mcp.submission_path().write_text(json.dumps({
            "holdings": [{"ticker": "600519.SS", "weight": 0.5, "rationale": "A-share moat"}],
            "summary": "a-share core", "gross": 0.5}))
        return {"ok": True, "text": "closing note", "model": "m"}

    monkeypatch.setattr(china, "_run_brain", fake_brain)
    out = china.run_china(asof="2026-06-22", armed=True)
    assert out.get("translated") is True
    latest = json.loads((registry.data_dir("china") / "latest.json").read_text())
    pos = next(p for p in latest["positions"] if p["ticker"] == "600519.SS")
    assert pos["name"] == "贵州茅台"                                      # name on the position
    # the Haiku translation got the summary + rationale + the Brain's closing note
    assert {"a-share core", "A-share moat", "closing note"} <= set(captured.get("texts") or [])


def test_api_trades_attaches_security_names_for_every_region(iso, monkeypatch):
    """Trade History rows carry the same canonical display names as Positions."""
    from app import web
    from brain import china_intake
    monkeypatch.setattr(china_intake, "display_name",
                        lambda t: {"600519.SS": "贵州茅台", "0700.HK": "Tencent",
                                   "AAPL": "Apple Inc"}.get(t, t))
    monkeypatch.setattr(china_intake, "display_name_zh",
                        lambda t: {"0700.HK": "腾讯控股"}.get(t, t))

    for pid, ticker, want in (("china", "600519.SS", "贵州茅台"), ("hk", "0700.HK", "Tencent")):
        cdir = registry.data_dir(pid)
        cdir.mkdir(parents=True, exist_ok=True)
        (cdir / "fills.jsonl").write_text(
            json.dumps({"date": "2026-06-20", "ticker": ticker, "side": "buy",
                        "shares": 10, "price": 100.0, "value": 1000.0}) + "\n")
        data = json.loads(web.api_trades(portfolio=pid).body)
        assert data["history"], f"{pid}: expected a blotter row"
        assert data["history"][0]["name"] == want                       # name on the blotter row
        if pid == "hk":
            assert data["history"][0]["name_zh"] == "腾讯控股"

    # Flagship's blotter resolves through the test-isolated legacy _FILLS_PATH.
    import portfolio.trade_history as th
    th._FILLS_PATH.write_text(
        json.dumps({"date": "2026-06-20", "ticker": "AAPL", "side": "buy",
                    "shares": 10, "price": 100.0, "value": 1000.0}) + "\n")
    us = json.loads(web.api_trades(portfolio="flagship").body)
    assert us["history"][0]["name"] == "Apple Inc"


def test_trade_history_uses_account_to_hide_stale_fifo_residue(iso):
    """Historical fractional sizing can leave a tiny fill-derived remainder even after
    the authoritative account has exited the name. It must not appear as an open trade."""
    from portfolio import trade_history

    cdir = registry.data_dir("hk")
    cdir.mkdir(parents=True, exist_ok=True)
    (cdir / "fills.jsonl").write_text(
        json.dumps({"date": "2026-06-20", "ticker": "3993.HK", "side": "buy",
                    "shares": 100.0, "price": 7.0, "value": 700.0}) + "\n" +
        json.dumps({"date": "2026-06-22", "ticker": "3993.HK", "side": "sell",
                    "shares": 99.999, "price": 7.5, "value": 749.9925}) + "\n")
    (cdir / "account.json").write_text(json.dumps({
        "inception_date": "2026-06-20",
        "starting_nav": 1_000_000.0,
        "cash": 1_000_000.0,
        "positions": {},
    }))

    buy = next(row for row in trade_history.history(
        live_prices={"3993.HK": 8.0}, portfolio_id="hk"
    ) if row["action"] == "buy")
    assert buy["still_open"] is False
    assert buy["open_shares"] is None
    assert buy["unrealized_pnl"] is None


def test_api_decisions_attaches_security_names_for_every_region(iso, monkeypatch):
    """Daily Decision Log trades and holdings backfill names for every Brain book."""
    from app import web
    from brain import china_intake
    monkeypatch.setattr(china_intake, "display_name",
                        lambda t: {"600519.SS": "贵州茅台", "0700.HK": "Tencent",
                                   "AAPL": "Apple Inc"}.get(t, t))
    monkeypatch.setattr(china_intake, "display_name_zh",
                        lambda t: {"0700.HK": "腾讯控股"}.get(t, t))

    for pid, ticker, want in (("china", "600519.SS", "贵州茅台"), ("hk", "0700.HK", "Tencent")):
        cdir = registry.data_dir(pid)
        cdir.mkdir(parents=True, exist_ok=True)
        # the historical shape: NO name baked into the executed trade or the holding
        (cdir / "decisions.jsonl").write_text(json.dumps({
            "asof": "2026-06-22", "ts": "2026-06-22T00:00:00+00:00",
            "executed": [{"ticker": ticker, "side": "buy", "shares": 10, "value": 1000.0}],
            "holdings": [{"ticker": ticker, "weight": 0.5, "rationale": "core"}]}) + "\n")
        d = json.loads(web.api_decisions(portfolio=pid).body)["decisions"][0]
        assert d["executed"][0]["name"] == want                          # name on the buy/sell chip
        assert d["holdings"][0]["name"] == want                          # name on the holding row
        if pid == "hk":
            assert d["executed"][0]["name_zh"] == "腾讯控股"
            assert d["holdings"][0]["name_zh"] == "腾讯控股"

    adir = registry.data_dir("autonomous")
    adir.mkdir(parents=True, exist_ok=True)
    (adir / "decisions.jsonl").write_text(json.dumps({
        "asof": "2026-06-22", "ts": "2026-06-22T00:00:00+00:00",
        "executed": [{"ticker": "AAPL", "side": "buy", "shares": 10, "value": 1000.0}],
        "holdings": [{"ticker": "AAPL", "weight": 0.5, "rationale": "core"}]}) + "\n")
    ud = json.loads(web.api_decisions(portfolio="autonomous").body)["decisions"][0]
    assert ud["executed"][0]["name"] == "Apple Inc"
    assert ud["holdings"][0]["name"] == "Apple Inc"


def test_api_decisions_sell_chips_show_pct_and_realized_pnl(iso, monkeypatch):
    """A SELL chip in the Daily Decision Log carries the fraction of the position trimmed AND
    the realized P&L (+%), derived from the FIFO blotter so it agrees with Trade History. A
    historical entry that stored only ticker/side/value backfills on read; BUYs stay bare."""
    from app import web
    from portfolio import trade_history
    cdir = registry.data_dir("hk")
    cdir.mkdir(parents=True, exist_ok=True)
    # bought 100 @ HK$100 on day 1, trimmed 40 @ HK$110 on day 2 → sold 40% for +HK$400 (+10%)
    (cdir / "fills.jsonl").write_text(
        json.dumps({"date": "2026-06-21", "ticker": "0700.HK", "side": "buy",
                    "shares": 100, "price": 100.0, "value": 10000.0}) + "\n" +
        json.dumps({"date": "2026-06-22", "ticker": "0700.HK", "side": "sell",
                    "shares": 40, "price": 110.0, "value": 4400.0}) + "\n")
    (cdir / "decisions.jsonl").write_text(json.dumps({
        "asof": "2026-06-22", "ts": "2026-06-22T00:00:00+00:00",
        "executed": [{"ticker": "0700.HK", "side": "sell", "shares": 40, "value": 4400.0}],
        "holdings": []}) + "\n")

    sell = json.loads(web.api_decisions(portfolio="hk").body)["decisions"][0]["executed"][0]
    assert sell["pct_of_position"] == 0.4                                 # trimmed 40% of the line
    assert sell["realized_pnl"] == 400.0                                  # 40 · (110 − 100)
    assert sell["realized_pct"] == 10.0
    # agrees with the Trade History blotter to the cent (same FIFO source)
    blot = next(r for r in trade_history.history(portfolio_id="hk")
                if r["ticker"] == "0700.HK" and r["action"] == "sell")
    assert sell["realized_pnl"] == blot["realized_pnl"]


def test_api_portfolio_backfills_hk_bilingual_names(iso, monkeypatch):
    """An existing HK book gets both language variants without waiting for a republish."""
    from app import web
    from brain import china_intake

    monkeypatch.setattr(china_intake, "display_name", lambda t: "Tencent")
    monkeypatch.setattr(china_intake, "display_name_zh", lambda t: "腾讯控股")
    cdir = registry.data_dir("hk")
    cdir.mkdir(parents=True, exist_ok=True)
    (cdir / "latest.json").write_text(json.dumps({
        "portfolio_id": "hk",
        "as_of": "2026-06-23",
        "kind": "hk_brain",
        "summary": "Hold the core.",
        "positions": [{"ticker": "0700.HK", "name": "Tencent", "weight": 0.1}],
    }))

    position = json.loads(web.api_portfolio(portfolio="hk").body)["positions"][0]
    assert position["name"] == "Tencent"
    assert position["name_zh"] == "腾讯控股"


def test_api_portfolio_repairs_stale_china_ticker_names(iso, monkeypatch):
    """An existing China book replaces a raw-ticker name from an earlier degraded publish."""
    from app import web
    from brain import china_intake

    monkeypatch.setattr(
        china_intake, "display_name",
        lambda t: {"600882.SS": "妙可蓝多"}.get(t, t),
    )
    cdir = registry.data_dir("china")
    cdir.mkdir(parents=True, exist_ok=True)
    (cdir / "latest.json").write_text(json.dumps({
        "portfolio_id": "china",
        "as_of": "2026-07-30",
        "kind": "china_brain",
        "summary": "Hold the core.",
        "positions": [{"ticker": "600882.SS", "name": "600882.SS", "weight": 0.1}],
    }))

    position = json.loads(web.api_portfolio(portfolio="china").body)["positions"][0]
    assert position["name"] == "妙可蓝多"


def test_api_portfolio_backfills_us_security_names(iso, monkeypatch):
    """Existing US books get canonical names without waiting for a republish."""
    from app import web
    from brain import china_intake

    monkeypatch.setattr(
        china_intake, "display_name",
        lambda t: {"LNG": "Cheniere Energy"}.get(t, t),
    )
    pdir = registry.data_dir("autonomous")
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / "latest.json").write_text(json.dumps({
        "portfolio_id": "autonomous",
        "as_of": "2026-07-30",
        "kind": "autonomous",
        "positions": [{"ticker": "LNG", "weight": 0.15}],
    }))

    position = json.loads(web.api_portfolio(portfolio="autonomous").body)["positions"][0]
    assert position["name"] == "Cheniere Energy"


def test_api_self_directed_backfills_us_security_names(monkeypatch):
    """Self-Directed positions and history use the same security-name contract."""
    from app import web
    from brain import china_intake
    from portfolio import self_directed

    monkeypatch.setattr(china_intake, "display_name", lambda t: "Apple Inc" if t == "AAPL" else t)
    monkeypatch.setattr(self_directed, "_load_account", lambda: {"positions": {"AAPL": {}}})
    monkeypatch.setattr(self_directed, "_load_pending", lambda: [])
    monkeypatch.setattr(web, "_live_prices", lambda tickers: {})
    monkeypatch.setattr(
        self_directed, "book",
        lambda **kwargs: {"positions": [{"ticker": "AAPL"}], "pending": []},
    )
    book = json.loads(web.api_self_directed().body)
    assert book["positions"][0]["name"] == "Apple Inc"

    monkeypatch.setattr(
        self_directed, "history",
        lambda **kwargs: {"history": [{"ticker": "AAPL"}], "pending": []},
    )
    history = json.loads(web.api_self_directed_history().body)
    assert history["history"][0]["name"] == "Apple Inc"


def test_api_portfolio_banner_summary_falls_back_to_decision_log(iso):
    """The top banner summary is published from the live submission, which is CLEARED at the start
    of every run — so a skipped/feed-gated run nulls latest.json's summary AND appends a
    summary-less decision. /api/portfolio must surface the most recent decision that DOES carry a
    summary (the still-current book) rather than blank the banner. Gated/self-directed books, which
    have no Brain decision log, get no such fallback."""
    from app import web
    cdir = registry.data_dir("hk")
    cdir.mkdir(parents=True, exist_ok=True)
    # what a carried-unchanged run leaves on disk: a nulled banner summary ...
    (cdir / "latest.json").write_text(json.dumps({
        "portfolio_id": "hk", "as_of": "2026-06-23", "kind": "hk_brain",
        "summary": None, "positions": []}))
    # ... and a NEWEST decision with no summary on top of the prior one that has the real rationale
    (cdir / "decisions.jsonl").write_text(
        json.dumps({"asof": "2026-06-22", "ts": "2026-06-22T00:00:00+00:00",
                    "summary": "Stagflation barbell — hold the thesis.",
                    "sold_note": "exited CCB", "holdings": [], "executed": []}) + "\n" +
        json.dumps({"asof": "2026-06-23", "ts": "2026-06-23T00:00:00+00:00",
                    "summary": None, "holdings": [], "executed": []}) + "\n")

    b = json.loads(web.api_portfolio(portfolio="hk").body)
    assert b["summary"] == "Stagflation barbell — hold the thesis."     # walked back past empty newest
    assert b.get("sold_note") == "exited CCB"

    # only the free-form Brain books carry a decision log to fall back on
    assert web._brain_book_module("hk") is not None
    assert web._brain_book_module("flagship") is None
    assert web._brain_book_module("self_directed") is None


# --------------------------------------------------------------------------- #
# regression guards for the adversarial-review findings
# --------------------------------------------------------------------------- #
def test_run_china_carries_unpriceable_held_position(iso, monkeypatch):
    """CRITICAL guard: a held name the Brain RE-SUBMITS but that is unpriceable this run must be
    CARRIED, not liquidated to cash (the rebalance must see the full target, not the priceable subset)."""
    from bot import china
    from brain import china_mcp

    def _submit(holdings, summary):
        china_mcp.submission_path().parent.mkdir(parents=True, exist_ok=True)
        china_mcp.submission_path().write_text(json.dumps({"holdings": holdings, "summary": summary}))

    book = [{"ticker": "600519.SS", "weight": 0.4, "rationale": "a"},
            {"ticker": "300750.SZ", "weight": 0.3, "rationale": "b"},
            {"ticker": "601318.SS", "weight": 0.2, "rationale": "c"}]
    # Day 1 — everything priceable, book gets built
    monkeypatch.setattr(paper_account, "_current_price",
                        lambda t: {"600519.SS": 100.0, "300750.SZ": 50.0,
                                   "601318.SS": 105.0, "000300.SS": 4_000.0}.get(t))
    monkeypatch.setattr(china, "_run_brain",
                        lambda a, i: (_submit(book, "init"), {"ok": True, "model": "m"})[1])
    china.run_china(asof="2026-06-22", armed=True)
    assert "300750.SZ" in json.loads((registry.data_dir("china") / "account.json").read_text())["positions"]

    # Day 2 — 300750.SZ is UNPRICEABLE this run but STILL in the submission → must be carried
    monkeypatch.setattr(paper_account, "_current_price",
                        lambda t: {"600519.SS": 100.0, "601318.SS": 105.0,
                                   "000300.SS": 4_000.0}.get(t))   # no 300750.SZ
    monkeypatch.setattr(china, "_run_brain",
                        lambda a, i: (_submit(book, "hold"), {"ok": True, "model": "m"})[1])
    out = china.run_china(asof="2026-06-23", armed=True)
    acct = json.loads((registry.data_dir("china") / "account.json").read_text())
    assert "300750.SZ" in acct["positions"], "unpriceable-but-resubmitted name was wrongly liquidated"
    assert "300750.SZ" in out["skipped_unpriceable"]
    assert not any(t["ticker"] == "300750.SZ" and t["side"] == "sell" for t in out["executed"])


def test_china_research_tools_return_valid_json_at_default():
    """get_china_intake / get_china_standouts must return VALID JSON at their default limits
    (the raw boards overflow the tool's 8000-char serialization cap → truncated/invalid JSON)."""
    from brain import china_mcp
    intake = json.loads(asyncio.run(china_mcp.get_china_intake.handler({}))["content"][0]["text"])
    assert isinstance(intake.get("candidates"), list)
    standouts = json.loads(asyncio.run(china_mcp.get_china_standouts.handler({}))["content"][0]["text"])
    assert isinstance(standouts.get("a_share_buy"), list)
    # the slim projection must have dropped the heavy spark_svg blob
    if standouts["a_share_buy"]:
        assert "spark_svg" not in standouts["a_share_buy"][0]


def test_fx_cache_is_date_keyed(monkeypatch):
    """The FX memo refreshes when the calendar day rolls (so a long-lived server doesn't freeze
    the first rate forever) but is stable within a day."""
    import datetime as _dt
    from portfolio import fx
    fx.clear_cache()
    live = {"rate": 7.0}
    monkeypatch.setattr(fx, "_from_yahoo_store", lambda s: live["rate"])
    monkeypatch.setattr(fx, "_from_forex_snapshot", lambda k: None)
    monkeypatch.setattr(fx, "date", type("D", (), {"today": staticmethod(lambda: _dt.date(2026, 6, 22))}))
    assert fx.rate_per_usd("CNY") == 7.0
    live["rate"] = 6.5
    assert fx.rate_per_usd("CNY") == 7.0                       # same day → cached
    monkeypatch.setattr(fx, "date", type("D", (), {"today": staticmethod(lambda: _dt.date(2026, 6, 23))}))
    assert fx.rate_per_usd("CNY") == 6.5                       # new day → refreshed
    fx.clear_cache()


def test_current_price_series_fallback_converts_foreign(monkeypatch):
    """The series fallback (yahoo/breadth) must also FX-convert a China/HK name to USD, not leak a
    raw CNY/HKD mark into NAV."""
    import pandas as pd
    from portfolio import fx
    monkeypatch.setattr(paper_account, "_live_price", lambda t: None)   # force the series fallback
    monkeypatch.setattr(fx, "rate_per_usd", lambda cur: {"HKD": 7.8, "CNY": 7.0}.get(cur, 1.0))
    monkeypatch.setattr(paper_account, "_fetch_price_series",
                        lambda t: pd.Series([390.0]) if t == "9999.HK" else None)
    assert paper_account._current_price("9999.HK") == pytest.approx(50.0)   # 390 HKD / 7.8
    monkeypatch.setattr(paper_account, "_fetch_price_series",
                        lambda t: pd.Series([105.0]) if t == "BABA" else None)
    assert paper_account._current_price("BABA") == pytest.approx(105.0)     # USD passthrough


def test_intake_survives_malformed_row(monkeypatch):
    """One non-dict row in a board must be skipped, not collapse the whole desk."""
    from brain import china_intake
    monkeypatch.setattr(china_intake, "_read", lambda rel: (
        {"buy": ["GARBAGE", {"ticker": "600519.SS", "dir": "up", "conviction": {"score": 80}}]}
        if rel.endswith("china_standouts.json") else None))
    r = china_intake.build(20)
    assert "600519.SS" in {c["ticker"] for c in r["candidates"]}


def test_intake_entry_gate_demotes_avoid(monkeypatch):
    """A 'good company, bad entry' name (conviction.size.bucket=='avoid') must lose its BUY lean and
    rank BELOW a clean setup, even with a higher raw conviction score."""
    from brain import china_intake
    monkeypatch.setattr(china_intake, "_read", lambda rel: ({"buy": [
        {"ticker": "AAA.SS", "dir": "up", "label": "BUY ZONE", "conviction": {"score": 90}},
        {"ticker": "BBB.SS", "dir": "up", "label": "BOTTOMING",
         "conviction": {"score": 98, "size": {"bucket": "avoid", "note": "cycle blocks"},
                        "verdict": "Extended — don't chase"}},
    ]} if rel.endswith("china_standouts.json") else None))
    r = china_intake.build(20)
    by = {c["ticker"]: c for c in r["candidates"]}
    assert by["BBB.SS"]["lean"] == 0                            # blocked entry → no buy lean
    assert by["AAA.SS"]["score"] > by["BBB.SS"]["score"]        # clean setup outranks it
    assert r["candidates"][0]["ticker"] == "AAA.SS"


# --------------------------------------------------------------------------- #
# W6 T4 — CHINA_FUNNEL_PROFILE: edge-led mode
# --------------------------------------------------------------------------- #
class TestChinaFunnelProfile:
    """Guards for the CHINA_FUNNEL_PROFILE flag (W6 T4).

    Key invariants:
    1. default profile → byte-identical to pre-W6 behaviour (no score change).
    2. edge-led → reversal scores are higher (REVERSAL_BOOST) and the cap is raised.
    3. edge-led → alpha 'intact' names receive a score penalty.
    4. Both profiles surface a 'funnel_profile' field in the build() output.
    5. Profile can be set via env OR via the profile= kwarg.
    6. Unknown env value falls back to 'default'.
    """

    def _reversal_data(self, rev_z=3.0) -> dict:
        return {"watch": [{"ticker": "REV.SS", "rev_z": rev_z, "ret_3m": -12.0}]}

    def _alpha_data(self, intact=True) -> dict:
        return {"top": [{"ticker": "MOM.SS", "alpha": 2.4,
                         "entry": "intact" if intact else "waning"}]}

    def test_default_profile_is_byte_identical(self, monkeypatch):
        """build() with profile='default' must produce the same scores as pre-W6."""
        from brain import china_intake
        monkeypatch.setattr(china_intake, "_read", lambda rel: (
            self._reversal_data() if "china_reversal" in rel else None))
        r_default = china_intake.build(20, profile="default")
        by = {c["ticker"]: c for c in r_default["candidates"]}
        # score = min(3.0/4.0, 0.5) = 0.5  (old cap of 0.5)
        assert by["REV.SS"]["score"] == pytest.approx(0.5, abs=1e-4)
        assert r_default["funnel_profile"] == "default"

    def test_edge_led_boosts_reversal_score(self, monkeypatch):
        """edge-led mode multiplies reversal scores by _REVERSAL_BOOST and raises the cap."""
        from brain import china_intake
        monkeypatch.setattr(china_intake, "_read", lambda rel: (
            self._reversal_data(rev_z=2.0) if "china_reversal" in rel else None))
        # default: score = min(2.0/4.0, 0.5) = 0.5
        r_default = china_intake.build(20, profile="default")
        by_def = {c["ticker"]: c for c in r_default["candidates"]}
        # edge-led: raw = 2.0/4.0 * BOOST; with BOOST=1.6 and cap=0.75 → min(0.8, 0.75)=0.75
        r_edge = china_intake.build(20, profile="edge-led")
        by_edge = {c["ticker"]: c for c in r_edge["candidates"]}
        assert by_edge["REV.SS"]["score"] > by_def["REV.SS"]["score"]
        assert r_edge["funnel_profile"] == "edge-led"

    def test_edge_led_raises_reversal_cap(self, monkeypatch):
        """Reversal cap must be > 0.5 in edge-led mode (0.75 configured)."""
        from brain import china_intake
        # rev_z=4.0: default score = min(1.0, 0.5) = 0.5; edge-led = min(1.0*1.6, 0.75) = 0.75
        monkeypatch.setattr(china_intake, "_read", lambda rel: (
            self._reversal_data(rev_z=4.0) if "china_reversal" in rel else None))
        r = china_intake.build(20, profile="edge-led")
        by = {c["ticker"]: c for c in r["candidates"]}
        assert by["REV.SS"]["score"] == pytest.approx(china_intake._REVERSAL_CAP_EDGE, abs=1e-4)
        assert by["REV.SS"]["score"] > 0.5   # strictly above the default cap

    def test_edge_led_penalises_alpha_intact_names(self, monkeypatch):
        """Alpha 'intact' names must receive a score haircut in edge-led mode."""
        from brain import china_intake
        monkeypatch.setattr(china_intake, "_read", lambda rel: (
            self._alpha_data(intact=True) if "china_alpha" in rel else None))
        # default: score = min(2.4/3.0, 1.0) = 0.8
        r_default = china_intake.build(20, profile="default")
        by_def = {c["ticker"]: c for c in r_default["candidates"]}
        # edge-led: score *= _MOMENTUM_PENALTY
        r_edge = china_intake.build(20, profile="edge-led")
        by_edge = {c["ticker"]: c for c in r_edge["candidates"]}
        assert by_edge["MOM.SS"]["score"] < by_def["MOM.SS"]["score"]
        assert by_edge["MOM.SS"]["score"] == pytest.approx(
            by_def["MOM.SS"]["score"] * china_intake._MOMENTUM_PENALTY, abs=1e-4)

    def test_edge_led_does_not_penalise_non_intact_alpha(self, monkeypatch):
        """Alpha names with entry != 'intact' must NOT be penalised in edge-led mode."""
        from brain import china_intake
        monkeypatch.setattr(china_intake, "_read", lambda rel: (
            self._alpha_data(intact=False) if "china_alpha" in rel else None))
        r_default = china_intake.build(20, profile="default")
        r_edge = china_intake.build(20, profile="edge-led")
        by_def = {c["ticker"]: c for c in r_default["candidates"]}
        by_edge = {c["ticker"]: c for c in r_edge["candidates"]}
        # waning entry → no penalty in either mode
        assert by_edge["MOM.SS"]["score"] == pytest.approx(by_def["MOM.SS"]["score"], abs=1e-4)

    def test_env_var_controls_profile(self, monkeypatch):
        """CHINA_FUNNEL_PROFILE env var selects the profile when no kwarg is passed."""
        import os
        from brain import china_intake
        monkeypatch.setattr(china_intake, "_read", lambda rel: (
            self._reversal_data() if "china_reversal" in rel else None))
        monkeypatch.setenv("CHINA_FUNNEL_PROFILE", "edge-led")
        r = china_intake.build(20)
        assert r["funnel_profile"] == "edge-led"
        monkeypatch.setenv("CHINA_FUNNEL_PROFILE", "default")
        r2 = china_intake.build(20)
        assert r2["funnel_profile"] == "default"

    def test_unknown_env_value_falls_back_to_default(self, monkeypatch):
        """An unknown CHINA_FUNNEL_PROFILE value must degrade to 'default', never raise."""
        from brain import china_intake
        monkeypatch.setenv("CHINA_FUNNEL_PROFILE", "experimental-2.0")
        monkeypatch.setattr(china_intake, "_read", lambda rel: None)
        r = china_intake.build(10)
        assert r["funnel_profile"] == "default"

    def test_kwarg_overrides_env(self, monkeypatch):
        """profile= kwarg overrides the env var (for one-off calls without restarting)."""
        from brain import china_intake
        monkeypatch.setenv("CHINA_FUNNEL_PROFILE", "default")
        monkeypatch.setattr(china_intake, "_read", lambda rel: (
            self._reversal_data() if "china_reversal" in rel else None))
        r = china_intake.build(20, profile="edge-led")
        assert r["funnel_profile"] == "edge-led"

    def test_edge_led_does_not_break_degrade_path(self, monkeypatch):
        """edge-led mode must degrade as gracefully as default when no boards are built."""
        from brain import china_intake
        monkeypatch.setattr(china_intake, "_read", lambda rel: None)
        r = china_intake.build(10, profile="edge-led")
        assert r["candidates"], "seed fallback must fire even in edge-led mode"
        assert r["funnel_profile"] == "edge-led"


def test_tushare_feed_healthy_tristate(monkeypatch):
    """The A-share feed-health probe is TRI-STATE: up (bulk market non-empty), down (token present
    but every walked-back day empty → an outage), None (no token → live feed not deployed; the
    book runs on the snapshot by design, not an outage — and it must NOT hit the network)."""
    from data_layer import tushare_feed
    tushare_feed.clear_cache()
    monkeypatch.setattr(tushare_feed, "_token", lambda: "tok")
    # a non-empty whole-market response for the trade date → feed is UP
    monkeypatch.setattr(tushare_feed, "_call", lambda api, params, fields:
                        {"items": [["600519.SH", 1500.0]]} if params.get("trade_date") == "20260622"
                        else {"items": []})
    assert tushare_feed.feed_healthy("2026-06-22") is True
    # token present but every bulk call comes back empty → OUTAGE (False), never a per-name gap
    tushare_feed.clear_cache()
    monkeypatch.setattr(tushare_feed, "_call", lambda api, params, fields: {"items": []})
    assert tushare_feed.feed_healthy("2026-06-22") is False
    # no token → not deployed (None) and the probe must short-circuit WITHOUT calling the API
    tushare_feed.clear_cache()
    monkeypatch.setattr(tushare_feed, "_token", lambda: None)
    monkeypatch.setattr(tushare_feed, "_call",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not call _call")))
    assert tushare_feed.feed_healthy("2026-06-22") is None
    tushare_feed.clear_cache()


def test_feed_health_status_dispatch(monkeypatch):
    """A-shares use live Yahoo when Tushare is unavailable; HK uses Yahoo."""
    from data_layer import feed_health, tushare_feed, yahoo_feed
    monkeypatch.setattr(tushare_feed, "feed_healthy", lambda asof=None: False)
    monkeypatch.setattr(yahoo_feed, "feed_healthy", lambda *a, **k: True)
    assert feed_health.status("A-share", "2026-06-22") == {
        "venue": "A-share", "feed": "yahoo", "status": "up", "asof": "2026-06-22"}
    assert feed_health.status("HK")["status"] == "up"
    assert feed_health.status(None)["status"] == "snapshot"       # US / unrestricted → no gate
    assert feed_health.is_down("A-share") is False and feed_health.is_down("HK") is False


def test_feed_health_missing_live_adapter_requires_real_snapshot(monkeypatch):
    """Missing yfinance is safe snapshot mode only when a usable HK snapshot exists."""
    from data_layer import feed_health, terminal_prices, yahoo_feed

    monkeypatch.setattr(yahoo_feed, "feed_healthy", lambda *a, **k: None)
    monkeypatch.setattr(terminal_prices, "price_local", lambda ticker: None)
    assert feed_health.status("HK")["status"] == "down"

    monkeypatch.setattr(
        terminal_prices, "price_local",
        lambda ticker: 471.8 if ticker == "0700.HK" else None,
    )
    assert feed_health.status("HK")["status"] == "snapshot"


def test_run_china_aborts_on_tushare_feed_outage(iso, monkeypatch):
    """CRITICAL guard: when the live A-share feed (Tushare ``daily``) is DOWN — a token is
    configured but the bulk call returns an empty market — the turn must ABORT rather than let the
    Brain transact on an ASYMMETRIC priceable map (held names price off the stale snapshot while
    fresh candidates return priceable=false). Regression for the 2026-06-22 corruption where the
    Brain parked ~48% cash citing a (false) 'no investable candidates' constraint."""
    from bot import china
    from brain import china_mcp
    from data_layer import tushare_feed

    tushare_feed.clear_cache()
    monkeypatch.setattr(tushare_feed, "_token", lambda: "tok")
    monkeypatch.setattr(tushare_feed, "_call", lambda api, params, fields: {"items": []})  # OUTAGE
    monkeypatch.setattr(paper_account, "_current_price", lambda t: {"000300.SS": 4_000.0}.get(t))

    ran = {"brain": False}

    def fake_brain(asof, inaugural):
        ran["brain"] = True                                       # must NOT be reached on an outage
        china_mcp.submission_path().parent.mkdir(parents=True, exist_ok=True)
        china_mcp.submission_path().write_text(json.dumps({
            "holdings": [{"ticker": "600519.SS", "weight": 0.5, "rationale": "should not run"}],
            "summary": "should not run"}))
        return {"ok": True, "model": "m"}

    monkeypatch.setattr(china, "_run_brain", fake_brain)
    out = china.run_china(asof="2026-06-22", armed=True)

    assert out["feed_health"]["status"] == "down" and out["feed_health"]["feed"] == "tushare"
    assert out.get("feed_aborted") is True
    assert ran["brain"] is False, "the Brain must not run while the A-share feed is down"
    assert out["decided"] is False
    assert not out["executed"]                                    # nothing traded
    # the outage is recorded in the decision log so the dashboard can show WHY nothing happened
    decs = china.load_decisions()
    assert decs and decs[0]["feed_health"]["status"] == "down"
    tushare_feed.clear_cache()


def test_run_china_force_overrides_feed_gate(iso, monkeypatch):
    """``force=True`` is the operator escape hatch: it bypasses the feed gate and runs the turn even
    when the A-share feed is down (e.g. to mark/carry on a known-degraded feed)."""
    from bot import china
    from brain import china_mcp
    from data_layer import tushare_feed
    tushare_feed.clear_cache()
    monkeypatch.setattr(tushare_feed, "_token", lambda: "tok")
    monkeypatch.setattr(tushare_feed, "_call", lambda api, params, fields: {"items": []})  # down
    monkeypatch.setattr(paper_account, "_current_price",
                        lambda t: {"600519.SS": 100.0, "000300.SS": 4_000.0}.get(t))

    def fake_brain(asof, inaugural):
        china_mcp.submission_path().parent.mkdir(parents=True, exist_ok=True)
        china_mcp.submission_path().write_text(json.dumps({
            "holdings": [{"ticker": "600519.SS", "weight": 0.5, "rationale": "moat"}],
            "summary": "forced"}))
        return {"ok": True, "model": "m"}

    monkeypatch.setattr(china, "_run_brain", fake_brain)
    out = china.run_china(asof="2026-06-22", armed=True, force=True)
    assert out["feed_health"]["status"] == "down"
    assert out.get("feed_aborted") is None                        # gate bypassed
    assert out["decided"] is True
    tushare_feed.clear_cache()


def test_china_calendar_next_open_lunch_break():
    from portfolio import china_calendar as cc
    nxt = cc.next_open(datetime(2026, 6, 22, 12, 0, tzinfo=cc.CST))   # during the 11:30–13:00 break
    assert nxt.date() == date(2026, 6, 22) and nxt.hour == 13 and nxt.minute == 0


def test_china_allowlist_is_only_typed_read_desk_web():
    """Leak-guard (parity with the autonomous book's leak-fix): the China Brain may use ONLY its
    typed mcp__china__* tools + web — no raw Read/Grep/Glob, no flagship get_portfolio, no gated
    execute_trade, no mcp__bot__* — and its server map is isolated to the 'china' server."""
    from brain import bot_mcp, china_mcp
    allowed = set(china_mcp.allowed_tools())
    assert allowed == {f"mcp__china__{t.name}" for t in china_mcp._ALL_TOOLS} | set(bot_mcp.WEB_TOOLS)
    assert not (allowed & {"Read", "Grep", "Glob"})
    assert not any(a.startswith("mcp__bot__") for a in allowed)
    assert "mcp__china__execute_trade" not in allowed
    assert set(china_mcp.build_servers().keys()) == {"china"}


# --- helper: invoke the SdkMcpTool's async handler directly ---------------------
def china_submit(args):
    from brain import china_mcp
    return china_mcp.submit_book.handler(args)
