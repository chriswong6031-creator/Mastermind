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
    never touch the real books. registry.data_dir() derives every path off registry._ROOT."""
    monkeypatch.setattr(registry, "_ROOT", tmp_path, raising=False)
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
    prices = {"NVDA": 210.0, "AVGO": 300.0, "SPY": 740.0}
    monkeypatch.setattr(paper_account, "_current_price", lambda t: prices.get(t))
    _seed_flagship(["NVDA", "AVGO"])
    monkeypatch.setattr(hw, "_run_brain", _fake_brain([
        {"ticker": "NVDA", "weight": 0.7, "rationale": "press the winner"},   # clamp → 0.50
        {"ticker": "AVGO", "weight": 0.3, "rationale": "high conviction"},
        {"ticker": "TSLA", "weight": 0.2, "rationale": "not held by flagship"},  # dropped
    ]))
    out = hw.run_heavyweight(asof="2026-06-21", armed=True)
    assert out["decided"] is True and out["held_prior_book"] is False
    assert "TSLA" in out["enforcement"]["out_of_universe"]
    assert any(c["ticker"] == "NVDA" for c in out["enforcement"]["clamped"])
    sides = {(t["ticker"], t["side"]) for t in out["executed"]}
    assert ("NVDA", "buy") in sides and ("AVGO", "buy") in sides
    assert "TSLA" not in {t["ticker"] for t in out["executed"]}
    latest = json.loads((registry.data_dir("heavyweight") / "latest.json").read_text())
    nvda = next(p for p in latest["positions"] if p["ticker"] == "NVDA")
    assert nvda["weight"] == pytest.approx(0.50, abs=0.02)     # clamped, not 0.70


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
