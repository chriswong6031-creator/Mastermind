"""Tests for portfolio.mandate_packet — MW5 Lane A.

Covers:
  - packet per book (all 7 books, including self_directed)
  - breach cases (universe, sizing, currency)
  - degradation (absent book state → computed=False)
  - write_packet (persists to tmp dir)
  - emit_run_event (fires run_event on breach; silent on clean)
"""
from __future__ import annotations

import json

import pytest

from portfolio import mandate_packet as MP
from portfolio import registry


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _state(book_id: str, positions=None, gross=None, currency=None, asof="2026-07-06") -> dict:
    """Minimal book state for a given book."""
    meta = registry.get(book_id)
    return {
        "portfolio_id": book_id,
        "asof": asof,
        "positions": positions or [],
        "gross": gross if gross is not None else 0.0,
        "currency": currency or meta.get("currency") or "USD",
    }


def _pos(ticker, weight=0.10):
    return {"ticker": ticker, "weight": weight}


# ---------------------------------------------------------------------------
# base build — packet shape for every book
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("book_id", [p["id"] for p in registry.all_portfolios()])
def test_packet_has_required_keys(book_id):
    state = _state(book_id, positions=[_pos("AAPL", 0.1)])
    pkt = MP.build(book_id, state)
    assert pkt.get("computed") is True
    assert pkt["book_id"] == book_id
    assert "asof" in pkt
    assert "mandate" in pkt
    assert isinstance(pkt["mandate"], str)
    assert "breaches" in pkt
    assert isinstance(pkt["breaches"], list)
    assert "n_positions" in pkt
    assert "gross" in pkt


def test_packet_universe_ok_no_positions():
    """An empty book is universe-clean."""
    pkt = MP.build("flagship", _state("flagship", positions=[]))
    assert pkt["computed"] is True
    assert pkt["universe_ok"] is True
    assert pkt["n_positions"] == 0


# ---------------------------------------------------------------------------
# universe breach cases
# ---------------------------------------------------------------------------

def test_heavyweight_universe_breach_when_firm_union_unavailable(monkeypatch):
    """When firm union tickers are unavailable, universe_ok degrades to None (not False)."""
    monkeypatch.setattr(MP, "_firm_union_tickers", lambda: set())
    pkt = MP.build("heavyweight", _state("heavyweight", positions=[_pos("AAPL", 0.10)]))
    # empty union → degraded to None (can't confirm or deny)
    assert pkt["universe_ok"] is None
    # no breach should be added for a None (uncertain) check
    universe_breaches = [b for b in pkt["breaches"] if b.startswith("universe")]
    assert len(universe_breaches) == 0


def test_heavyweight_universe_breach_when_name_not_in_union(monkeypatch):
    """Name not in firm union → universe_ok=False → breach."""
    monkeypatch.setattr(MP, "_firm_union_tickers", lambda: {"SPY", "QQQ"})
    pkt = MP.build("heavyweight", _state("heavyweight",
                                         positions=[_pos("AAPL", 0.10), _pos("SPY", 0.20)]))
    assert pkt["universe_ok"] is False
    assert any("universe" in b for b in pkt["breaches"])
    assert "AAPL" in pkt["universe_detail"]


def test_heavyweight_universe_ok_when_all_names_in_union(monkeypatch):
    monkeypatch.setattr(MP, "_firm_union_tickers", lambda: {"AAPL", "SPY"})
    pkt = MP.build("heavyweight", _state("heavyweight",
                                         positions=[_pos("AAPL", 0.20), _pos("SPY", 0.30)]))
    assert pkt["universe_ok"] is True
    assert not any("universe" in b for b in pkt["breaches"])


def test_china_universe_breach_wrong_venue():
    """Non A-share ticker in China book → breach."""
    pkt = MP.build("china", _state("china", positions=[_pos("AAPL", 0.10)], currency="CNY"))
    assert pkt["universe_ok"] is False
    assert any("universe" in b for b in pkt["breaches"])


def test_china_universe_ok_correct_venue():
    pkt = MP.build("china", _state("china",
                                    positions=[_pos("601318.SS", 0.20), _pos("300015.SZ", 0.15)],
                                    currency="CNY"))
    assert pkt["universe_ok"] is True


def test_hk_universe_breach_wrong_venue():
    pkt = MP.build("hk", _state("hk", positions=[_pos("AAPL", 0.10)], currency="HKD"))
    assert pkt["universe_ok"] is False


def test_hk_universe_ok_correct_venue():
    pkt = MP.build("hk", _state("hk",
                                  positions=[_pos("700.HK", 0.20), _pos("9988.HK", 0.15)],
                                  currency="HKD"))
    assert pkt["universe_ok"] is True


def test_flagship_universe_unrestricted():
    """Flagship has no venue restriction — any ticker is fine."""
    pkt = MP.build("flagship", _state("flagship",
                                       positions=[_pos("NVDA", 0.15), _pos("700.HK", 0.05)]))
    assert pkt["universe_ok"] is True


# ---------------------------------------------------------------------------
# sizing rails breach cases
# ---------------------------------------------------------------------------

def test_heavyweight_sizing_min_breach():
    """Weight below 5% min → breach."""
    pkt = MP.build("heavyweight", _state("heavyweight",
                                          positions=[_pos("AAPL", 0.03)]))
    assert pkt["sizing_rails_ok"] is False
    assert any("sizing" in b for b in pkt["breaches"])


def test_heavyweight_sizing_max_breach():
    """Weight above 50% max → breach."""
    pkt = MP.build("heavyweight", _state("heavyweight",
                                          positions=[_pos("AAPL", 0.55)]))
    assert pkt["sizing_rails_ok"] is False
    assert any("sizing" in b for b in pkt["breaches"])


def test_heavyweight_sizing_max_names_breach():
    """More than 8 names → breach."""
    positions = [_pos(f"T{i}", 0.10) for i in range(9)]
    pkt = MP.build("heavyweight", _state("heavyweight", positions=positions))
    assert pkt["sizing_rails_ok"] is False
    assert any("sizing" in b for b in pkt["breaches"])


def test_heavyweight_sizing_ok():
    """6 names at 10% each — within rails."""
    positions = [_pos(f"T{i}", 0.10) for i in range(6)]
    pkt = MP.build("heavyweight", _state("heavyweight", positions=positions))
    assert pkt["sizing_rails_ok"] is True


def test_autonomous_no_per_name_cap():
    """Autonomous has no per-name cap — 80% allocation should be fine."""
    pkt = MP.build("autonomous", _state("autonomous", positions=[_pos("NVDA", 0.80)]))
    assert pkt["sizing_rails_ok"] is True


# ---------------------------------------------------------------------------
# currency breach cases
# ---------------------------------------------------------------------------

def test_currency_breach():
    """Wrong currency in book state �� breach."""
    pkt = MP.build("china", _state("china", positions=[], currency="USD"))
    assert pkt["currency_ok"] is False
    assert any("currency" in b for b in pkt["breaches"])


def test_currency_ok_hk():
    pkt = MP.build("hk", _state("hk", positions=[], currency="HKD"))
    assert pkt["currency_ok"] is True


def test_currency_missing_degrades():
    """Missing currency in state → None, not a breach."""
    state = _state("flagship")
    del state["currency"]
    pkt = MP.build("flagship", state)
    assert pkt["currency_ok"] is None
    assert not any("currency" in b for b in pkt["breaches"])


# ---------------------------------------------------------------------------
# degradation — absent state
# ---------------------------------------------------------------------------

def test_build_none_state():
    pkt = MP.build("flagship", None)
    assert pkt["computed"] is False
    assert "reason" in pkt


def test_build_empty_state():
    pkt = MP.build("flagship", {})
    assert pkt["computed"] is False


def test_build_never_raises_on_garbage():
    """build() must never raise."""
    result = MP.build("flagship", {"positions": "not a list", "gross": "bad"})
    assert isinstance(result, dict)


# ---------------------------------------------------------------------------
# gross and n_positions computed when missing
# ---------------------------------------------------------------------------

def test_gross_computed_from_positions():
    state = {
        "asof": "2026-07-06",
        "positions": [_pos("AAPL", 0.20), _pos("NVDA", 0.15)],
        "currency": "USD",
    }
    pkt = MP.build("flagship", state)
    assert pkt["n_positions"] == 2
    assert abs(pkt["gross"] - 0.35) < 1e-6


# ---------------------------------------------------------------------------
# write_packet
# ---------------------------------------------------------------------------

def test_write_packet(tmp_path, monkeypatch):
    import portfolio.registry as _reg

    def _mock_data_dir(pid=None):
        d = tmp_path / (pid or "flagship")
        d.mkdir(parents=True, exist_ok=True)
        return d

    monkeypatch.setattr(_reg, "data_dir", _mock_data_dir)
    pkt = MP.build("flagship", _state("flagship", positions=[_pos("AAPL", 0.20)]))
    path = MP.write_packet(pkt, "flagship")
    assert path is not None
    assert path.exists()
    on_disk = json.loads(path.read_text())
    assert on_disk["book_id"] == "flagship"
    assert on_disk["computed"] is True


def test_write_packet_never_raises():
    """write_packet must never raise — even on a bad path."""
    bad_pkt: dict = {"book_id": "\x00invalid\x00path/\x00", "computed": True}
    result = MP.write_packet(bad_pkt, "\x00")
    assert result is None


# ---------------------------------------------------------------------------
# emit_run_event
# ---------------------------------------------------------------------------

def test_emit_run_event_fires_on_breach(tmp_path, monkeypatch):
    events: list[dict] = []
    import control_plane.run_events as _re
    monkeypatch.setattr(_re, "append", lambda e, **kw: events.append(e))

    pkt = {
        "computed": True,
        "book_id": "hk",
        "asof": "2026-07-06",
        "breaches": ["universe: bad ticker"],
        "n_positions": 1,
        "gross": 0.10,
    }
    MP.emit_run_event(pkt, "hk", job="hk_daily")
    assert len(events) == 1
    assert events[0]["kind"] == "mandate_breach"
    assert events[0]["book"] == "hk"


def test_emit_run_event_silent_on_clean(monkeypatch):
    events: list[dict] = []
    import control_plane.run_events as _re
    monkeypatch.setattr(_re, "append", lambda e, **kw: events.append(e))

    pkt = {"computed": True, "book_id": "flagship", "breaches": []}
    MP.emit_run_event(pkt, "flagship")
    assert len(events) == 0


def test_emit_run_event_silent_on_uncomputed(monkeypatch):
    events: list[dict] = []
    import control_plane.run_events as _re
    monkeypatch.setattr(_re, "append", lambda e, **kw: events.append(e))

    pkt = {"computed": False, "reason": "no state"}
    MP.emit_run_event(pkt, "flagship")
    assert len(events) == 0


# ---------------------------------------------------------------------------
# advisory_only flag is present and the gate-hook placeholder is in the code
# ---------------------------------------------------------------------------

def test_gate_hook_placeholder_in_source():
    """Ratchet: the GATE-HOOK PLACEHOLDER comment must exist in mandate_packet.py so future
    promoters can find it without archaeology."""
    import inspect
    src = inspect.getsource(MP)
    assert "GATE-HOOK PLACEHOLDER" in src
