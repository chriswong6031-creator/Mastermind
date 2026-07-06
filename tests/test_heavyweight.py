"""Heavyweight portfolio — the concentrated book that presses Flagship's best ideas.

Covers the genuinely new logic vs the autonomous clone: the deterministic universe constraint
(Flagship's current holdings only), the 5–50% concentration rails (clamp / drop-nibble / top-N /
no-leverage), the never-liquidate guard, state isolation, the Flagship-visibility MCP tools, and
the dashboard decision-log dispatch.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from portfolio import paper_account, registry

from bot import heavyweight as hw


@pytest.fixture
def iso(tmp_path, monkeypatch):
    """Isolate all per-id portfolio state (incl. the legacy flagship dir) to a tmp root, so tests
    never touch the real books. registry.data_dir() derives every path off registry._ROOT.

    These integration tests assert the UNIVERSE + SIZING-RAILS weights in isolation; the flagship
    latest.json they seed is the universe SOURCE, but it is ALSO a firm peer, so the W3 B1 firm-cap
    clamp would (correctly) trim a 0.50 name to the 0.10 firm name cap and mask the rails behaviour
    under test. Disable the flag-gated firm clamp here (default-ON in prod); the firm clamp's own
    wiring is covered by test_run_firm_cap_clamps_below_rails below and by tests/test_firm_exposure.py."""
    monkeypatch.setattr(registry, "_ROOT", tmp_path, raising=False)
    monkeypatch.setenv("MASTERMIND_FIRM_CAPS", "0")
    return tmp_path


def _seed_flagship(positions: list[str], pending: list[str] | None = None) -> None:
    """Write a fake Flagship latest.json (the universe Heavyweight constrains against)."""
    fdir = registry.data_dir("flagship")           # tmp/data/portfolio under iso
    fdir.mkdir(parents=True, exist_ok=True)
    (fdir / "latest.json").write_text(json.dumps({
        "as_of": "2026-06-21", "portfolio_id": "flagship",
        "positions": [{"ticker": t, "sleeve": "conviction", "weight": 0.02,
                       "thesis_full": {"summary": f"{t} thesis"}} for t in positions],
        "pending_orders": [{"ticker": t, "weight": 0.02, "side": "buy", "status": "pending"}
                           for t in (pending or [])],
    }))


def _submit(holdings: list[dict], summary: str = "concentrate") -> None:
    from brain import heavyweight_mcp
    p = heavyweight_mcp.submission_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"holdings": holdings, "summary": summary}))


def _fake_brain(holdings, summary="concentrate"):
    def brain(asof, inaugural):
        _submit(holdings, summary)
        return {"ok": True, "text": "x", "cost_usd": 0.0, "model": "claude-opus-4-8"}
    return brain


# ── the deterministic universe + sizing rails (pure function) ─────────────────────────────

def test_enforce_drops_out_of_universe():
    final, _, notes = hw._enforce([
        {"ticker": "AAA", "weight": 0.2, "rationale": "x"},
        {"ticker": "ZZZ", "weight": 0.2, "rationale": "x"},   # not a Flagship holding
    ], {"AAA", "BBB"})
    assert "AAA" in final and "ZZZ" not in final
    assert "ZZZ" in notes["out_of_universe"]


def test_enforce_clamps_above_max():
    final, _, notes = hw._enforce([{"ticker": "AAA", "weight": 0.7, "rationale": "x"}], {"AAA"})
    assert final["AAA"] == hw.MAX_W                            # 0.70 → 0.50
    assert notes["clamped"][0]["ticker"] == "AAA"


def test_enforce_drops_sub_floor_nibbles():
    final, _, notes = hw._enforce([
        {"ticker": "AAA", "weight": 0.03, "rationale": "x"},   # nibble → dropped
        {"ticker": "BBB", "weight": 0.2, "rationale": "x"},
    ], {"AAA", "BBB"})
    assert "AAA" not in final and "BBB" in final
    assert any(d["ticker"] == "AAA" for d in notes["dropped_below_floor"])


def test_enforce_caps_name_count_to_top_n():
    allowed = {f"T{i}" for i in range(10)}
    holds = [{"ticker": f"T{i}", "weight": 0.05 + i * 0.001, "rationale": "x"} for i in range(10)]
    final, _, notes = hw._enforce(holds, allowed)
    assert len(final) == hw.MAX_NAMES
    assert len(notes["dropped_overflow"]) == 10 - hw.MAX_NAMES
    assert "T9" in final and "T0" not in final                # highest-weight names survive


def test_enforce_renormalizes_to_no_leverage():
    allowed = {"A", "B", "C", "D"}
    holds = [{"ticker": t, "weight": 0.6, "rationale": "x"} for t in allowed]  # 4×0.5 = 2.0 gross
    final, _, notes = hw._enforce(holds, allowed)
    assert sum(final.values()) == pytest.approx(1.0, abs=1e-3)
    assert notes.get("renormalized_from_gross") == pytest.approx(2.0, abs=1e-3)


# ── universe sourcing ─────────────────────────────────────────────────────────────────────

def test_universe_includes_positions_and_pending(iso):
    _seed_flagship(positions=["AAA"], pending=["BBB"])
    assert hw._flagship_universe() == {"AAA", "BBB"}


def test_universe_empty_when_no_flagship_book(iso):
    assert hw._flagship_universe() == set()


# ── run_heavyweight integration ───────────────────────────────────────────────────────────

def test_run_offline_inaugural(iso, monkeypatch):
    monkeypatch.setattr(paper_account, "_current_price", lambda t: {"SPY": 740.0}.get(t))
    _seed_flagship(["NVDA", "AVGO"])
    out = hw.run_heavyweight(asof="2026-06-21", armed=False)
    assert out["inaugural"] is True and out["decided"] is False
    assert out["nav"] == 1_000_000.0 and out["flagship_universe_size"] == 2
    latest = json.loads((registry.data_dir("heavyweight") / "latest.json").read_text())
    assert latest["portfolio_id"] == "heavyweight" and latest["kind"] == "heavyweight"


def test_run_enforces_universe_and_sizing(iso, monkeypatch):
    # NVDA (semis_ai) + XLE (commodity_inflation) are DIFFERENT clusters, so both survive one-per-cluster
    # and this test isolates the universe-drop + clamp rails (the cluster collapse is covered separately).
    prices = {"NVDA": 210.0, "XLE": 90.0, "SPY": 740.0}
    monkeypatch.setattr(paper_account, "_current_price", lambda t: prices.get(t))
    _seed_flagship(["NVDA", "XLE"])
    monkeypatch.setattr(hw, "_run_brain", _fake_brain([
        {"ticker": "NVDA", "weight": 0.7, "rationale": "press the winner"},   # clamp → 0.50
        {"ticker": "XLE", "weight": 0.3, "rationale": "high conviction"},
        {"ticker": "TSLA", "weight": 0.2, "rationale": "not held by any book"},  # dropped
    ]))
    out = hw.run_heavyweight(asof="2026-06-21", armed=True)
    assert out["decided"] is True and out["held_prior_book"] is False
    assert "TSLA" in out["enforcement"]["out_of_universe"]
    assert any(c["ticker"] == "NVDA" for c in out["enforcement"]["clamped"])
    sides = {(t["ticker"], t["side"]) for t in out["executed"]}
    assert ("NVDA", "buy") in sides and ("XLE", "buy") in sides
    assert "TSLA" not in {t["ticker"] for t in out["executed"]}
    latest = json.loads((registry.data_dir("heavyweight") / "latest.json").read_text())
    nvda = next(p for p in latest["positions"] if p["ticker"] == "NVDA")
    assert nvda["weight"] == pytest.approx(0.50, abs=0.02)     # clamped, not 0.70


def test_run_firm_cap_clamps_below_rails(iso, monkeypatch):
    """W3 B1 wiring: with the firm clamp ARMED (default-on in prod), Heavyweight's contribution to a
    firm name is clamped BELOW its own 5-50% rails. Flagship (a peer) already holds NVDA at 0.06; the
    firm NAME cap is 0.10, so Heavyweight's NVDA is trimmed to the 0.04 remaining firm headroom even
    though the concentration rails alone would allow up to 0.50."""
    monkeypatch.setenv("MASTERMIND_FIRM_CAPS", "1")               # re-arm (iso disables it)
    prices = {"NVDA": 210.0, "AVGO": 300.0, "SPY": 740.0}
    monkeypatch.setattr(paper_account, "_current_price", lambda t: prices.get(t))
    # NVDA is in Flagship's universe AND Flagship holds NVDA at 0.06 (a firm peer weight on the name).
    fdir = registry.data_dir("flagship")
    fdir.mkdir(parents=True, exist_ok=True)
    (fdir / "latest.json").write_text(json.dumps({
        "as_of": "2026-06-21", "portfolio_id": "flagship",
        "positions": [{"ticker": "NVDA", "sleeve": "conviction", "weight": 0.06},
                      {"ticker": "AVGO", "sleeve": "conviction", "weight": 0.02}]}))
    monkeypatch.setattr(hw, "_run_brain", _fake_brain([
        {"ticker": "NVDA", "weight": 0.5, "rationale": "press it"},   # rails allow 0.50; firm caps 0.04
        {"ticker": "AVGO", "weight": 0.3, "rationale": "diversify"}]))
    out = hw.run_heavyweight(asof="2026-06-21", armed=True)
    # the firm clamp bound (surfaced on the run output)
    assert out.get("firm_clamp") and out["firm_clamp"]["freed"] > 0
    assert any(c["kind"] == "name" and c["key"] == "NVDA" for c in out["firm_clamp"]["clamped"])
    latest = json.loads((registry.data_dir("heavyweight") / "latest.json").read_text())
    nvda = next(p for p in latest["positions"] if p["ticker"] == "NVDA")
    # NVDA firm name room = 0.10 - 0.06 = 0.04; the 0.50 rails allowance is clamped down to ~0.04.
    assert nvda["weight"] == pytest.approx(0.04, abs=0.01)


def test_fail_closed_when_universe_empty(iso, monkeypatch):
    monkeypatch.setattr(paper_account, "_current_price", lambda t: {"SPY": 740.0}.get(t))
    # NO flagship book seeded → empty universe
    monkeypatch.setattr(hw, "_run_brain", _fake_brain([{"ticker": "NVDA", "weight": 0.4, "rationale": "x"}]))
    out = hw.run_heavyweight(asof="2026-06-21", armed=True)
    assert out.get("universe_empty") is True
    assert out["held_prior_book"] is True and out["executed"] == []


def test_never_liquidates_when_rails_strip_all(iso, monkeypatch):
    prices = {"NVDA": 210.0, "SPY": 740.0}
    monkeypatch.setattr(paper_account, "_current_price", lambda t: prices.get(t))
    _seed_flagship(["NVDA"])
    # run 1 — establish a real book (NVDA 0.4)
    monkeypatch.setattr(hw, "_run_brain", _fake_brain([{"ticker": "NVDA", "weight": 0.4, "rationale": "x"}]))
    hw.run_heavyweight(asof="2026-06-21", armed=True)
    acct = json.loads((registry.data_dir("heavyweight") / "account.json").read_text())
    assert "NVDA" in acct["positions"]
    # run 2 — Brain submits ONLY an off-universe name → all stripped → HOLD prior, never liquidate
    monkeypatch.setattr(hw, "_run_brain", _fake_brain([{"ticker": "TSLA", "weight": 0.5, "rationale": "x"}]))
    out = hw.run_heavyweight(asof="2026-06-22", armed=True)
    assert out["held_prior_book"] is True and out["executed"] == []
    acct2 = json.loads((registry.data_dir("heavyweight") / "account.json").read_text())
    assert "NVDA" in acct2["positions"]                       # not blown to cash


def test_state_isolation(iso, monkeypatch):
    monkeypatch.setattr(paper_account, "_current_price", lambda t: {"NVDA": 210.0, "SPY": 740.0}.get(t))
    _seed_flagship(["NVDA"])
    flagship_before = (registry.data_dir("flagship") / "latest.json").read_text()
    monkeypatch.setattr(hw, "_run_brain", _fake_brain([{"ticker": "NVDA", "weight": 0.3, "rationale": "x"}]))
    hw.run_heavyweight(asof="2026-06-21", armed=True)
    # only the heavyweight dir was written; flagship book is byte-unchanged; autonomous never created
    assert (registry.data_dir("heavyweight") / "account.json").exists()
    assert (registry.data_dir("flagship") / "latest.json").read_text() == flagship_before
    assert not (registry.data_dir("autonomous") / "account.json").exists()


# ── MCP surface ───────────────────────────────────────────────────────────────────────────

def test_submit_book_scoped_to_heavyweight(iso):
    from brain import heavyweight_mcp
    asyncio.run(heavyweight_mcp.submit_book.handler({
        "holdings": [{"ticker": "nvda", "weight": 0.3, "rationale": "x"},
                     {"ticker": "NVDA", "weight": 0.1, "rationale": "dup"}],   # dedup
        "summary": "s"}))
    sub = heavyweight_mcp.read_submission()
    assert [h["ticker"] for h in sub["holdings"]] == ["NVDA"]
    assert heavyweight_mcp.submission_path().parent == registry.data_dir("heavyweight")


def test_get_flagship_book_returns_universe(iso):
    _seed_flagship(["NVDA", "AVGO"], pending=["MU"])
    from brain import heavyweight_mcp
    res = asyncio.run(heavyweight_mcp.get_flagship_book.handler({}))
    d = json.loads(res["content"][0]["text"])
    assert set(d["universe"]) == {"NVDA", "AVGO", "MU"}
    assert d["n_positions"] == 2


def test_allowed_tools_match_servers_and_no_raw_fs():
    from brain import heavyweight_mcp
    allowed = set(heavyweight_mcp.allowed_tools())
    for t in heavyweight_mcp._DESK_TOOLS:
        assert f"mcp__heavydesk__{t.name}" in allowed
    assert not (allowed & {"Read", "Grep", "Glob"})           # no raw filesystem
    # the 4 Flagship-visibility tools are present
    for name in ("get_flagship_book", "get_flagship_trades", "get_flagship_research", "get_flagship_thinking"):
        assert f"mcp__heavydesk__{name}" in allowed


# ── persona / prompt / dashboard ──────────────────────────────────────────────────────────

def test_persona_states_universe_and_sizing():
    assert "Flagship" in hw._PERSONA
    assert "5%" in hw._PERSONA and "50%" in hw._PERSONA


def test_prompt_injects_flagship_universe(iso):
    _seed_flagship(["NVDA", "AVGO"])
    p = hw._build_prompt("2026-06-21", inaugural=True)
    assert "NVDA" in p and "AVGO" in p and "ONLY hold names" in p


def test_web_decisions_dispatch(iso, monkeypatch):
    monkeypatch.setattr(paper_account, "_current_price", lambda t: {"SPY": 740.0}.get(t))
    _seed_flagship(["NVDA"])
    hw.run_heavyweight(asof="2026-06-21", armed=False)        # seeds the heavyweight decision log
    from fastapi.testclient import TestClient
    from app.main import app
    c = TestClient(app, raise_server_exceptions=True)
    ids = {p["id"] for p in c.get("/api/portfolios").json()["portfolios"]}
    assert "heavyweight" in ids
    assert isinstance(c.get("/api/decisions?portfolio=heavyweight").json()["decisions"], list)
    assert c.get("/api/decisions?portfolio=flagship").json()["decisions"] == []


# ═══════════════════════════════════════════════════════════════════════════════════════════════
# W6 T1 — Heavyweight mandate v2: firm best-ideas union, not a Flagship mirror
# ═══════════════════════════════════════════════════════════════════════════════════════════════

def _seed_book(pid: str, positions: list[str], pending: list[str] | None = None) -> None:
    """Write a fake published latest.json for ANY book (autonomous/etf/self_directed) — a firm-union
    universe source."""
    bdir = registry.data_dir(pid)
    bdir.mkdir(parents=True, exist_ok=True)
    (bdir / "latest.json").write_text(json.dumps({
        "as_of": "2026-06-21", "portfolio_id": pid,
        "positions": [{"ticker": t, "weight": 0.05} for t in positions],
        "pending_orders": [{"ticker": t, "weight": 0.05, "side": "buy", "status": "pending"}
                           for t in (pending or [])],
    }))


# ── union assembly ───────────────────────────────────────────────────────────────────────────

def test_firm_universe_is_union_of_published_books(iso):
    _seed_flagship(["NVDA", "AVGO"])
    _seed_book("autonomous", ["XLE", "TSLA"])
    _seed_book("etf", ["SPY"], pending=["GLD"])
    allowed, meta = hw._firm_universe()
    assert allowed == {"NVDA", "AVGO", "XLE", "TSLA", "SPY", "GLD"}
    assert meta["source"] == "firm_union" and meta["mirror_fallback"] is False
    # post-R1: self_directed is NOT in _FIRM_UNION_BOOKS → no per_book key for it
    assert meta["per_book"] == {"flagship": 2, "autonomous": 2, "etf": 2}
    assert "self_directed" not in meta["per_book"]


def test_self_directed_unique_tickers_excluded_from_firm_universe(iso):
    """R1 ruling: a ticker that appears ONLY in self_directed must never enter _firm_universe().
    A ticker held by another published book remains eligible (the ban is on sourcing, not tickers)."""
    _seed_flagship(["NVDA", "AVGO", "MSFT"])
    _seed_book("autonomous", ["XLE"])
    # XLU/XLV are ONLY in self_directed — they must be absent from the universe.
    # XLE is also in autonomous — it must remain eligible.
    _seed_book("self_directed", ["XLU", "XLV", "XLE"])
    allowed, meta = hw._firm_universe()
    assert "XLU" not in allowed, "XLU appears only in self_directed — must not seed Heavyweight (R1)"
    assert "XLV" not in allowed, "XLV appears only in self_directed — must not seed Heavyweight (R1)"
    assert "XLE" in allowed, "XLE is also in autonomous — remains eligible after R1"
    assert "self_directed" not in meta.get("per_book", {})


def test_firm_universe_ignores_china_hk(iso):
    _seed_flagship(["NVDA", "AVGO", "MSFT"])
    _seed_book("china", ["600519.SS"])                  # non-USD disjoint venue
    _seed_book("hk", ["0700.HK"])
    allowed, _meta = hw._firm_universe()
    assert "600519.SS" not in allowed and "0700.HK" not in allowed


# ── one-name-per-cluster ─────────────────────────────────────────────────────────────────────

def test_one_per_cluster_keeps_highest_conviction(iso):
    # NVDA/AVGO/MU all resolve to the semis_ai cluster; only the top-conviction one survives.
    kept, notes = hw._one_per_cluster([
        {"ticker": "NVDA", "weight": 0.2, "conviction": 7},
        {"ticker": "AVGO", "weight": 0.4, "conviction": 9},   # highest conviction → kept
        {"ticker": "MU", "weight": 0.3, "conviction": 4},
        {"ticker": "XLE", "weight": 0.2, "conviction": 5},    # different cluster → survives
    ])
    kept_tk = {h["ticker"] for h in kept}
    assert kept_tk == {"AVGO", "XLE"}
    dropped = {d["ticker"] for d in notes["dropped_same_cluster"]}
    assert dropped == {"NVDA", "MU"}
    assert all(d["kept"] == "AVGO" for d in notes["dropped_same_cluster"])


def test_one_per_cluster_ties_break_on_weight_then_order(iso):
    # equal conviction → higher weight wins; equal weight → earlier submission order wins.
    kept, _notes = hw._one_per_cluster([
        {"ticker": "NVDA", "weight": 0.3, "conviction": 5},
        {"ticker": "AVGO", "weight": 0.4, "conviction": 5},   # same conv, more weight → kept
    ])
    assert [h["ticker"] for h in kept] == ["AVGO"]


def test_one_per_cluster_distinct_singletons_not_collapsed(iso):
    # two un-clustered names (name:<T> singletons) must NEVER be collapsed together.
    kept, notes = hw._one_per_cluster([
        {"ticker": "ZZZ", "weight": 0.3, "conviction": 5},
        {"ticker": "YYY", "weight": 0.2, "conviction": 5},
    ])
    assert {h["ticker"] for h in kept} == {"ZZZ", "YYY"}
    assert notes["dropped_same_cluster"] == []


# ── <4-fundable mirror fallback ──────────────────────────────────────────────────────────────

def test_thin_union_falls_back_to_flagship_mirror(iso, monkeypatch):
    monkeypatch.setenv("MASTERMIND_HW_MIN_FUNDABLE", "4")
    # only flagship publishes, and it holds <4 names → union too thin → mirror fallback
    _seed_flagship(["NVDA", "AVGO"])
    allowed, meta = hw._firm_universe()
    assert allowed == {"NVDA", "AVGO"}                  # exactly the flagship mirror
    assert meta["source"] == "mirror" and meta["mirror_fallback"] is True


def test_union_at_min_fundable_uses_firm_union(iso, monkeypatch):
    monkeypatch.setenv("MASTERMIND_HW_MIN_FUNDABLE", "4")
    _seed_flagship(["NVDA", "AVGO"])
    _seed_book("autonomous", ["XLE", "TSLA"])           # union now 4 == min → firm_union
    _allowed, meta = hw._firm_universe()
    assert meta["source"] == "firm_union" and meta["mirror_fallback"] is False


# ── rails intact under the union ─────────────────────────────────────────────────────────────

def test_run_union_universe_enforces_cluster_and_rails(iso, monkeypatch):
    prices = {"NVDA": 210.0, "AVGO": 300.0, "XLE": 90.0, "TSLA": 250.0, "SPY": 740.0}
    monkeypatch.setattr(paper_account, "_current_price", lambda t: prices.get(t))
    _seed_flagship(["NVDA", "AVGO"])
    _seed_book("autonomous", ["XLE", "TSLA"])
    # Brain submits two semis_ai names (NVDA+AVGO) — one-per-cluster keeps the top-conviction; XLE is a
    # separate cluster; TSLA is in-universe (autonomous). NVDA over the 0.50 rail is clamped.
    monkeypatch.setattr(hw, "_run_brain", _fake_brain([
        {"ticker": "NVDA", "weight": 0.7, "conviction": 9, "rationale": "press semis"},
        {"ticker": "AVGO", "weight": 0.3, "conviction": 4, "rationale": "also semis"},  # dropped (cluster)
        {"ticker": "XLE", "weight": 0.3, "conviction": 8, "rationale": "energy"},
        {"ticker": "TSLA", "weight": 0.2, "conviction": 6, "rationale": "autonomous name"}]))
    out = hw.run_heavyweight(asof="2026-06-21", armed=True)
    assert out["universe"]["source"] == "firm_union"
    tickers = {t["ticker"] for t in out["executed"]}
    assert "AVGO" not in tickers                        # collapsed into NVDA (higher conviction)
    assert {"NVDA", "XLE", "TSLA"} <= tickers
    latest = json.loads((registry.data_dir("heavyweight") / "latest.json").read_text())
    nvda = next(p for p in latest["positions"] if p["ticker"] == "NVDA")
    assert nvda["weight"] == pytest.approx(0.50, abs=0.02)   # rail clamp still binds
    assert any(d["ticker"] == "AVGO" for d in out["enforcement"]["dropped_same_cluster"])


# ── flag OFF → byte-identical to the old flagship-only gate ───────────────────────────────────

def test_flag_off_is_flagship_mirror(iso, monkeypatch):
    monkeypatch.setenv("MASTERMIND_HW_FIRM_UNIVERSE", "0")
    _seed_flagship(["NVDA", "AVGO"])
    _seed_book("autonomous", ["XLE", "TSLA"])           # would enter the union if the flag were ON
    allowed, meta = hw._firm_universe()
    assert allowed == {"NVDA", "AVGO"}                  # ONLY flagship — the union is ignored
    assert meta["source"] == "mirror" and meta["mirror_fallback"] is False
    assert "XLE" not in allowed and "TSLA" not in allowed


def test_flag_off_run_matches_old_universe_gate(iso, monkeypatch):
    monkeypatch.setenv("MASTERMIND_HW_FIRM_UNIVERSE", "0")
    prices = {"NVDA": 210.0, "XLE": 90.0, "SPY": 740.0}
    monkeypatch.setattr(paper_account, "_current_price", lambda t: prices.get(t))
    _seed_flagship(["NVDA"])
    _seed_book("autonomous", ["XLE"])
    # XLE is NOT in Flagship's book → with the flag OFF it is out-of-universe (old behaviour)
    monkeypatch.setattr(hw, "_run_brain", _fake_brain([
        {"ticker": "NVDA", "weight": 0.3, "conviction": 8, "rationale": "x"},
        {"ticker": "XLE", "weight": 0.3, "conviction": 8, "rationale": "not flagship"}]))
    out = hw.run_heavyweight(asof="2026-06-21", armed=True)
    assert "XLE" in out["enforcement"]["out_of_universe"]
    assert "XLE" not in {t["ticker"] for t in out["executed"]}


# ── the mirror-shadow A/B arm ─────────────────────────────────────────────────────────────────

def test_mirror_shadow_ab_measures_cluster_overlap(iso):
    _seed_flagship(["NVDA", "AVGO"])                    # Flagship = the semis_ai cluster
    _seed_book("autonomous", ["XLE", "TSLA"])          # firm universe adds commodity_inflation + TSLA
    from portfolio import heavyweight_shadow
    submission = {"holdings": [
        {"ticker": "XLE", "weight": 0.4, "conviction": 9},   # commodity_inflation — NOT a Flagship cluster
        {"ticker": "TSLA", "weight": 0.3, "conviction": 8},  # singleton — NOT a Flagship cluster
    ], "summary": "orthogonal"}
    row = heavyweight_shadow.compare(submission, asof="2026-06-21")
    # the firm-universe (live) arm can hold XLE/TSLA (0% cluster-overlap with Flagship's semis_ai);
    # the mirror arm drops both (not in Flagship's book) → empty → 0 overlap too, but the live arm is
    # the one that actually EXPRESSES the orthogonal names.
    assert "XLE" in row["live"]["tickers"] and "TSLA" in row["live"]["tickers"]
    assert row["live"]["overlap_frac"] == 0.0           # neither name shares Flagship's clusters
    assert row["mirror"]["tickers"] == []               # mirror can't hold them (out of flagship universe)


def test_mirror_shadow_ab_flag_favored_when_more_orthogonal(iso):
    _seed_flagship(["NVDA", "AVGO", "MU"])              # semis_ai
    _seed_book("autonomous", ["XLE", "XOM"])            # commodity_inflation (orthogonal)
    from portfolio import heavyweight_shadow
    # Brain proposes a semis name (overlaps Flagship) AND an energy name (orthogonal).
    submission = {"holdings": [
        {"ticker": "NVDA", "weight": 0.4, "conviction": 9},  # semis — mirror keeps it (Flagship holds it)
        {"ticker": "XLE", "weight": 0.3, "conviction": 8},   # energy — only the firm arm can hold it
    ], "summary": "x"}
    row = heavyweight_shadow.compare(submission, asof="2026-06-21")
    # live arm holds NVDA(semis, overlaps) + XLE(energy, orthogonal) → overlap_frac = 1/2 = 0.5
    # mirror arm holds NVDA only → overlap_frac = 1/1 = 1.0. The firm universe is MORE orthogonal.
    assert row["live"]["overlap_frac"] == pytest.approx(0.5, abs=1e-6)
    assert row["mirror"]["overlap_frac"] == pytest.approx(1.0, abs=1e-6)
    assert row["overlap_delta"] < 0 and row["flag_favored"] is True


def test_mirror_shadow_record_and_window_verdict(iso):
    _seed_flagship(["NVDA", "AVGO", "MU"])
    _seed_book("autonomous", ["XLE", "XOM"])
    from portfolio import heavyweight_shadow
    sub = {"holdings": [{"ticker": "NVDA", "weight": 0.4, "conviction": 9},
                        {"ticker": "XLE", "weight": 0.3, "conviction": 8}], "summary": "x"}
    for d in range(6):                                   # 6 rows > the n>=5 evidence floor
        heavyweight_shadow.record(sub, asof=f"2026-06-{21 + d:02d}")
    v = heavyweight_shadow.window_verdict()
    assert v["n"] == 6
    assert v["mean_live_overlap"] < v["mean_mirror_overlap"]   # firm universe more orthogonal
    assert v["flag_favored"] is True and v["kill_flag"] is False   # flag EARNS its keep → do not kill


def test_registry_has_hw_firm_universe_ab():
    from brain import experiment_registry as er
    exp = er.get("hw-firm-universe-ab")
    assert exp is not None
    assert exp["owner"] == "fable-review" and exp["comeback_date"] == "2026-07-24"
    assert "overlap" in exp["gate"].lower()


# ── persona golden ───────────────────────────────────────────────────────────────────────────

def test_persona_states_firm_universe_and_one_per_cluster():
    assert "FIRM'S BEST-IDEAS CONCENTRATOR" in hw._PERSONA
    assert "EVERY PUBLISHED BOOK" in hw._PERSONA
    assert "ONE EXPRESSION PER CLUSTER" in hw._PERSONA
    assert "vs Flagship" in hw._PERSONA or "vs Flagship itself" in hw._PERSONA
    assert "5%" in hw._PERSONA and "50%" in hw._PERSONA         # rails preserved
