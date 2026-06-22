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
    """Isolate per-id portfolio state to a tmp root (registry.data_dir derives off _ROOT)."""
    monkeypatch.setattr(registry, "_ROOT", tmp_path, raising=False)
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
    assert registry.benchmark("china") == "FXI"
    # the US books stay on SPY (back-compat) — unknown / None too
    for pid in ("flagship", "autonomous", "heavyweight", None, "nope"):
        assert registry.benchmark(pid) == "SPY"
    assert paper_account._benchmark_for("china") == "FXI"
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


def test_mark_uses_per_book_benchmark(iso, monkeypatch):
    """A china mark initialises the benchmark shares from FXI (not SPY); spy_nav tracks FXI."""
    monkeypatch.setattr(paper_account, "_current_price", lambda t: {"FXI": 30.0}.get(t))
    paper_account.mark({"FXI": 30.0}, "2026-06-22", portfolio_id="china")
    rows = [json.loads(l) for l in
            (registry.data_dir("china") / "nav_history.jsonl").read_text().splitlines() if l.strip()]
    assert rows[-1]["spy_nav"] == pytest.approx(1_000_000.0)   # FXI normalised to $1M at inception
    acct = json.loads((registry.data_dir("china") / "account.json").read_text())
    assert acct["spy_inception_price"] == 30.0                 # the FXI mark, in the benchmark slot


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
            {"ticker": "0700.HK", "weight": 0.7, "rationale": "platform"},
            {"ticker": "BABA", "weight": 0.2, "rationale": ""},      # dropped: no rationale
        ],
        "summary": "all-China barbell",
    }))
    from brain import china_mcp
    sub = china_mcp.read_submission()
    assert {h["ticker"] for h in sub["holdings"]} == {"600519.SS", "0700.HK"}
    assert sub["scaled_to_no_leverage"] is True and sub["gross"] == pytest.approx(1.0)
    venues = {h["ticker"]: h["venue"] for h in sub["holdings"]}
    assert venues["600519.SS"] == "A-share" and venues["0700.HK"] == "HK"


def test_get_quote_reports_usd_and_venue(iso, monkeypatch):
    from brain import china_mcp
    from portfolio import fx
    monkeypatch.setattr(paper_account, "_current_price", lambda t: {"0700.HK": 50.0}.get(t))
    monkeypatch.setattr(fx, "rate_per_usd", lambda cur: {"HKD": 7.8}.get(cur, 1.0))
    out = asyncio.run(china_mcp.get_quote.handler({"ticker": "0700.HK"}))
    payload = json.loads(out["content"][0]["text"])
    assert payload["venue"] == "HK" and payload["currency"] == "HKD"
    assert payload["priceable"] is True
    assert payload["price_usd"] == pytest.approx(50.0)
    assert payload["price_local"] == pytest.approx(390.0)         # 50 USD * 7.8
    miss = json.loads(asyncio.run(china_mcp.get_quote.handler({"ticker": "9999.HK"}))["content"][0]["text"])
    assert miss["priceable"] is False


# --------------------------------------------------------------------------- #
# run_china builder — offline + simulated multi-venue submission
# --------------------------------------------------------------------------- #
def test_run_china_offline_inaugural(iso, monkeypatch):
    monkeypatch.setattr(paper_account, "_current_price", lambda t: {"FXI": 30.0}.get(t))
    from bot import china
    out = china.run_china(asof="2026-06-22", armed=False)
    assert out["inaugural"] is True and out["decided"] is False
    assert out["nav"] == 1_000_000.0
    latest = json.loads((registry.data_dir("china") / "latest.json").read_text())
    assert latest["portfolio_id"] == "china" and latest["schema"] == "portfolio.v1"
    assert latest["benchmark"] == "FXI" and latest["currency"] == "USD"


def test_run_china_executes_multivenue_submission(iso, monkeypatch):
    # an A-share, an HK name, an ADR (all priceable, in USD) + an unpriceable HK name
    prices = {"600519.SS": 100.0, "0700.HK": 50.0, "BABA": 105.0, "FXI": 30.0}
    monkeypatch.setattr(paper_account, "_current_price", lambda t: prices.get(t))
    from bot import china
    from brain import china_mcp

    def fake_brain(asof, inaugural):
        china_mcp.submission_path().parent.mkdir(parents=True, exist_ok=True)
        china_mcp.submission_path().write_text(json.dumps({
            "holdings": [
                {"ticker": "600519.SS", "weight": 0.4, "rationale": "A-share moat", "venue": "A-share"},
                {"ticker": "0700.HK", "weight": 0.3, "rationale": "HK platform", "venue": "HK"},
                {"ticker": "BABA", "weight": 0.2, "rationale": "ADR value", "venue": "ADR"},
                {"ticker": "9999.HK", "weight": 0.1, "rationale": "unpriceable", "venue": "HK"},
            ],
            "summary": "all-China barbell", "gross": 1.0,
        }))
        return {"ok": True, "text": "x", "cost_usd": 0.0, "model": "claude-opus-4-8"}

    monkeypatch.setattr(china, "_run_brain", fake_brain)
    out = china.run_china(asof="2026-06-22", armed=True)
    assert out["decided"] is True
    assert "9999.HK" in out["skipped_unpriceable"]
    sides = {(t["ticker"], t["side"]) for t in out["executed"]}
    assert {("600519.SS", "buy"), ("0700.HK", "buy"), ("BABA", "buy")} <= sides
    # NAV stays ~$1M (USD), invested across the three priceable venues
    assert out["nav"] == pytest.approx(1_000_000.0, rel=1e-6)
    decs = china.load_decisions()
    assert decs and decs[0]["summary"] == "all-China barbell"
    latest = json.loads((registry.data_dir("china") / "latest.json").read_text())
    venues = {p["ticker"]: p.get("venue") for p in latest["positions"]}
    assert venues.get("600519.SS") == "A-share" and venues.get("0700.HK") == "HK"


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
            {"ticker": "0700.HK", "weight": 0.3, "rationale": "b"},
            {"ticker": "BABA", "weight": 0.2, "rationale": "c"}]
    # Day 1 — everything priceable, book gets built
    monkeypatch.setattr(paper_account, "_current_price",
                        lambda t: {"600519.SS": 100.0, "0700.HK": 50.0, "BABA": 105.0, "FXI": 30.0}.get(t))
    monkeypatch.setattr(china, "_run_brain",
                        lambda a, i: (_submit(book, "init"), {"ok": True, "model": "m"})[1])
    china.run_china(asof="2026-06-22", armed=True)
    assert "0700.HK" in json.loads((registry.data_dir("china") / "account.json").read_text())["positions"]

    # Day 2 — 0700.HK is UNPRICEABLE this run but STILL in the submission → must be carried
    monkeypatch.setattr(paper_account, "_current_price",
                        lambda t: {"600519.SS": 100.0, "BABA": 105.0, "FXI": 30.0}.get(t))   # no 0700.HK
    monkeypatch.setattr(china, "_run_brain",
                        lambda a, i: (_submit(book, "hold"), {"ok": True, "model": "m"})[1])
    out = china.run_china(asof="2026-06-23", armed=True)
    acct = json.loads((registry.data_dir("china") / "account.json").read_text())
    assert "0700.HK" in acct["positions"], "unpriceable-but-resubmitted name was wrongly liquidated"
    assert "0700.HK" in out["skipped_unpriceable"]
    assert not any(t["ticker"] == "0700.HK" and t["side"] == "sell" for t in out["executed"])


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
