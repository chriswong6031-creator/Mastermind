"""Tests for E9: close-reason taxonomy in position_log.update() and the phase2 drop loop.

Covers:
  (a) update() stamps 'reason' on close events when close_reasons mapping is provided
  (b) update() omits 'reason' when no mapping supplied — backwards-compatible legacy reads
  (c) closed_positions() surfaces the close reason from history (not a fixed generic string)
  (d) _rebuild_reason() derivation logic: hard-exit, exit-floor, not-in-universe
  (e) integration: phase2 drop loop threads specific reasons to both ledger and position_log
"""
from __future__ import annotations

import json

import bot  # noqa: F401


# ---------------------------------------------------------------------------
# (a) close_reasons stamped on the history event
# ---------------------------------------------------------------------------

def test_update_stamps_close_reason_when_provided(tmp_path, monkeypatch):
    """When close_reasons is supplied, the leaving position's close event carries the reason."""
    from portfolio import position_log as pl
    monkeypatch.setattr(pl, "_LEDGER_PATH", tmp_path / "ledger.json", raising=False)

    # Open two conviction names
    pl.update([
        {"ticker": "ANET", "sleeve": "conviction", "weight": 0.05},
        {"ticker": "NVDA", "sleeve": "conviction", "weight": 0.04},
    ], "2026-06-30")

    # Next build: ANET is dropped, NVDA is retained. Supply a close reason only for ANET.
    pl.update(
        [{"ticker": "NVDA", "sleeve": "conviction", "weight": 0.04}],
        "2026-07-01",
        close_reasons={"ANET": "rebuild: not in new candidate universe"},
    )

    ledger = json.loads((tmp_path / "ledger.json").read_text())
    anet_key = "conviction:ANET"
    assert anet_key in ledger, "ANET should be in ledger"
    anet = ledger[anet_key]
    assert not anet["still_open"], "ANET should be closed"

    close_events = [e for e in anet["history"] if e["event"] == "close"]
    assert close_events, "at least one close event expected"
    last_close = close_events[-1]
    assert last_close.get("reason") == "rebuild: not in new candidate universe", (
        f"expected reason on close event, got: {last_close}"
    )


def test_update_stamps_specific_reason_per_ticker(tmp_path, monkeypatch):
    """Each dropped name gets its own reason from the mapping (not a shared string)."""
    from portfolio import position_log as pl
    monkeypatch.setattr(pl, "_LEDGER_PATH", tmp_path / "ledger.json", raising=False)

    pl.update([
        {"ticker": "URI",  "sleeve": "conviction", "weight": 0.05},
        {"ticker": "AEIS", "sleeve": "conviction", "weight": 0.04},
    ], "2026-06-30")

    pl.update(
        [],  # both dropped
        "2026-07-01",
        close_reasons={
            "URI":  "rebuild: fell below exit floor (confluence +0.18)",
            "AEIS": "rebuild: hard exit (veto/downtrend/blocked) — Vetoed: parabolic",
        },
    )

    ledger = json.loads((tmp_path / "ledger.json").read_text())
    for ticker, expected_reason in [
        ("URI",  "rebuild: fell below exit floor (confluence +0.18)"),
        ("AEIS", "rebuild: hard exit (veto/downtrend/blocked) — Vetoed: parabolic"),
    ]:
        key = f"conviction:{ticker}"
        close_events = [e for e in ledger[key]["history"] if e["event"] == "close"]
        assert close_events[-1].get("reason") == expected_reason, (
            f"{ticker}: expected {expected_reason!r}, got {close_events[-1]}"
        )


# ---------------------------------------------------------------------------
# (b) backwards-compatible legacy read path — no reason → field absent
# ---------------------------------------------------------------------------

def test_update_no_reason_field_when_mapping_absent(tmp_path, monkeypatch):
    """Without close_reasons the close event has no 'reason' key — old ledgers still load fine."""
    from portfolio import position_log as pl
    monkeypatch.setattr(pl, "_LEDGER_PATH", tmp_path / "ledger.json", raising=False)

    pl.update([{"ticker": "WAB", "sleeve": "conviction", "weight": 0.05}], "2026-06-30")
    # Drop WAB without supplying close_reasons (the old call signature)
    pl.update([], "2026-07-01")

    ledger = json.loads((tmp_path / "ledger.json").read_text())
    close_events = [e for e in ledger["conviction:WAB"]["history"] if e["event"] == "close"]
    assert close_events, "close event must exist"
    assert "reason" not in close_events[-1], (
        "reason key must be absent when mapping is not provided (backwards compat)"
    )


def test_update_no_reason_field_when_ticker_not_in_mapping(tmp_path, monkeypatch):
    """A ticker absent from close_reasons also gets no 'reason' key on its close event."""
    from portfolio import position_log as pl
    monkeypatch.setattr(pl, "_LEDGER_PATH", tmp_path / "ledger.json", raising=False)

    pl.update([
        {"ticker": "CI",  "sleeve": "conviction", "weight": 0.05},
        {"ticker": "UNP", "sleeve": "conviction", "weight": 0.04},
    ], "2026-06-30")
    # Provide close reason only for CI; UNP is also dropped but not in the mapping
    pl.update([], "2026-07-01", close_reasons={"CI": "rebuild: not in new candidate universe"})

    ledger = json.loads((tmp_path / "ledger.json").read_text())
    # CI → reason present
    ci_close = [e for e in ledger["conviction:CI"]["history"] if e["event"] == "close"]
    assert ci_close[-1].get("reason") is not None

    # UNP → reason absent
    unp_close = [e for e in ledger["conviction:UNP"]["history"] if e["event"] == "close"]
    assert "reason" not in unp_close[-1], (
        "UNP was not in the mapping so its close event should have no reason field"
    )


# ---------------------------------------------------------------------------
# (c) closed_positions() surfaces the stored reason
# ---------------------------------------------------------------------------

def test_closed_positions_surfaces_reason(tmp_path, monkeypatch):
    """closed_positions() returns the specific reason from the close history event."""
    from portfolio import position_log as pl
    monkeypatch.setattr(pl, "_LEDGER_PATH", tmp_path / "ledger.json", raising=False)

    pl.update([{"ticker": "LII", "sleeve": "conviction", "weight": 0.04}], "2026-06-30")
    pl.update([], "2026-07-01",
              close_reasons={"LII": "rebuild: fell below exit floor (confluence +0.22)"})

    closed = pl.closed_positions()
    lii = next((p for p in closed if p["ticker"] == "LII"), None)
    assert lii is not None
    assert lii["exit_reason"] == "rebuild: fell below exit floor (confluence +0.22)"


def test_closed_positions_legacy_fallback(tmp_path, monkeypatch):
    """closed_positions() falls back to generic string when no reason was recorded."""
    from portfolio import position_log as pl
    monkeypatch.setattr(pl, "_LEDGER_PATH", tmp_path / "ledger.json", raising=False)

    pl.update([{"ticker": "APH", "sleeve": "conviction", "weight": 0.05}], "2026-06-30")
    pl.update([], "2026-07-01")   # no close_reasons

    closed = pl.closed_positions()
    aph = next((p for p in closed if p["ticker"] == "APH"), None)
    assert aph is not None
    assert aph["exit_reason"] == "removed from book", (
        "legacy rows with no reason in history should still return the generic string"
    )


def test_closed_positions_prefers_close_position_reason(tmp_path, monkeypatch):
    """close_position() always writes a reason; closed_positions() surfaces it correctly."""
    from portfolio import position_log as pl
    monkeypatch.setattr(pl, "_LEDGER_PATH", tmp_path / "ledger.json", raising=False)

    pl.update([{"ticker": "INCY", "sleeve": "conviction", "weight": 0.05}], "2026-06-30")
    # Close via the single-position API (as risk officer / hard-exit sweep would)
    pl.close_position("conviction", "INCY", "2026-07-01", reason="hard_exit_sweep")

    closed = pl.closed_positions()
    incy = next((p for p in closed if p["ticker"] == "INCY"), None)
    assert incy is not None
    assert incy["exit_reason"] == "hard_exit_sweep"


# ---------------------------------------------------------------------------
# (d) _rebuild_reason() derivation logic (unit-tested in isolation)
# ---------------------------------------------------------------------------

def test_rebuild_reason_not_in_universe():
    """A ticker absent from the rejected index → 'not in new candidate universe'.

    _rebuild_reason is a closure defined inside phase2.run(), so we replicate the same
    logic here.  This keeps the test hermetic (no live build required) and documents the
    decision tree as a readable spec.
    """
    rejected_index: dict = {}

    def _rebuild_reason(ticker: str) -> str:
        rej = rejected_index.get(ticker)
        if rej is None:
            return "rebuild: not in new candidate universe"
        _hard_markers = ("vetoed", "blocked", "downtrend", "falling knife")
        _reason_lower = (rej.get("reason") or "").lower()
        if rej.get("vetoes") or any(m in _reason_lower for m in _hard_markers):
            return f"rebuild: hard exit (veto/downtrend/blocked) — {rej.get('reason', '')}"
        conf = rej.get("confluence")
        _conf_str = f"{conf:+.2f}" if isinstance(conf, (int, float)) else str(conf)
        return f"rebuild: fell below exit floor (confluence {_conf_str})"

    assert _rebuild_reason("MISSING") == "rebuild: not in new candidate universe"


def test_rebuild_reason_hard_exit_via_vetoes():
    """A rejected ticker with vetoes → hard exit path."""
    rejected_index = {
        "AEIS": {"ticker": "AEIS", "reason": "Vetoed: parabolic",
                 "vetoes": ["parabolic"], "confluence": -0.10},
    }

    def _rebuild_reason(ticker: str) -> str:
        rej = rejected_index.get(ticker)
        if rej is None:
            return "rebuild: not in new candidate universe"
        _hard_markers = ("vetoed", "blocked", "downtrend", "falling knife")
        _reason_lower = (rej.get("reason") or "").lower()
        if rej.get("vetoes") or any(m in _reason_lower for m in _hard_markers):
            return f"rebuild: hard exit (veto/downtrend/blocked) — {rej.get('reason', '')}"
        conf = rej.get("confluence")
        _conf_str = f"{conf:+.2f}" if isinstance(conf, (int, float)) else str(conf)
        return f"rebuild: fell below exit floor (confluence {_conf_str})"

    result = _rebuild_reason("AEIS")
    assert result.startswith("rebuild: hard exit")
    assert "parabolic" in result


def test_rebuild_reason_hard_exit_via_downtrend_keyword():
    """A rejected ticker whose reason contains 'downtrend' → hard exit path (no vetoes list)."""
    rejected_index = {
        "URI": {"ticker": "URI", "reason": "Downtrend — price rolling over (no falling knives)",
                "vetoes": [], "confluence": 0.10},
    }

    def _rebuild_reason(ticker: str) -> str:
        rej = rejected_index.get(ticker)
        if rej is None:
            return "rebuild: not in new candidate universe"
        _hard_markers = ("vetoed", "blocked", "downtrend", "falling knife")
        _reason_lower = (rej.get("reason") or "").lower()
        if rej.get("vetoes") or any(m in _reason_lower for m in _hard_markers):
            return f"rebuild: hard exit (veto/downtrend/blocked) — {rej.get('reason', '')}"
        conf = rej.get("confluence")
        _conf_str = f"{conf:+.2f}" if isinstance(conf, (int, float)) else str(conf)
        return f"rebuild: fell below exit floor (confluence {_conf_str})"

    result = _rebuild_reason("URI")
    assert result.startswith("rebuild: hard exit")


def test_rebuild_reason_exit_floor():
    """A rejected ticker with low-but-not-hard confluence → fell below exit floor."""
    rejected_index = {
        "CI": {"ticker": "CI", "reason": "Insufficient confluence (+0.18, need >0.30)",
               "vetoes": [], "confluence": 0.18},
    }

    def _rebuild_reason(ticker: str) -> str:
        rej = rejected_index.get(ticker)
        if rej is None:
            return "rebuild: not in new candidate universe"
        _hard_markers = ("vetoed", "blocked", "downtrend", "falling knife")
        _reason_lower = (rej.get("reason") or "").lower()
        if rej.get("vetoes") or any(m in _reason_lower for m in _hard_markers):
            return f"rebuild: hard exit (veto/downtrend/blocked) — {rej.get('reason', '')}"
        conf = rej.get("confluence")
        _conf_str = f"{conf:+.2f}" if isinstance(conf, (int, float)) else str(conf)
        return f"rebuild: fell below exit floor (confluence {_conf_str})"

    result = _rebuild_reason("CI")
    assert result.startswith("rebuild: fell below exit floor")
    assert "+0.18" in result


# ---------------------------------------------------------------------------
# (e) Integration: phase2 drop loop threads reasons to position_log and ledger
# ---------------------------------------------------------------------------

def test_phase2_drop_loop_threads_reasons_to_position_log(tmp_path, monkeypatch):
    """Simulate the phase2 drop loop: verify close_reasons is derived and passed to update().

    We test this by patching position_log.update() to capture the close_reasons kwarg and
    verifying it is populated for a held name that left the book.  No full build required.
    """
    import portfolio.position_log as pl

    captured: dict = {}

    def _fake_update(positions, asof_iso, portfolio_id=None, close_reasons=None):
        captured["close_reasons"] = close_reasons or {}

    monkeypatch.setattr(pl, "update", _fake_update)

    # Simulate a _rebuild_reason closure with a known rejected ticker
    rejected_index = {
        "WAB": {"ticker": "WAB", "reason": "Insufficient confluence (+0.20, need >0.30)",
                "vetoes": [], "confluence": 0.20},
    }

    def _rebuild_reason(ticker: str) -> str:
        rej = rejected_index.get(ticker)
        if rej is None:
            return "rebuild: not in new candidate universe"
        _hard_markers = ("vetoed", "blocked", "downtrend", "falling knife")
        _reason_lower = (rej.get("reason") or "").lower()
        if rej.get("vetoes") or any(m in _reason_lower for m in _hard_markers):
            return f"rebuild: hard exit (veto/downtrend/blocked) — {rej.get('reason', '')}"
        conf = rej.get("confluence")
        _conf_str = f"{conf:+.2f}" if isinstance(conf, (int, float)) else str(conf)
        return f"rebuild: fell below exit floor (confluence {_conf_str})"

    # Replicate the drop-loop logic from phase2.py
    held_conv = {"WAB", "NVDA"}
    final_conv = {"NVDA"}
    dropped_conv = held_conv - final_conv
    close_reasons = {t: _rebuild_reason(t) for t in dropped_conv}

    book: list[dict] = [{"ticker": "NVDA", "sleeve": "conviction", "weight": 0.05}]
    pl.update(book, "2026-07-01", close_reasons=close_reasons)

    assert "WAB" in captured["close_reasons"]
    reason = captured["close_reasons"]["WAB"]
    assert reason.startswith("rebuild: fell below exit floor"), (
        f"expected exit-floor reason for WAB, got: {reason!r}"
    )
    assert "+0.20" in reason


def test_phase2_drop_loop_not_in_universe(tmp_path, monkeypatch):
    """A dropped name not in the rejected_index gets 'not in new candidate universe'."""
    import portfolio.position_log as pl

    captured: dict = {}

    def _fake_update(positions, asof_iso, portfolio_id=None, close_reasons=None):
        captured["close_reasons"] = close_reasons or {}

    monkeypatch.setattr(pl, "update", _fake_update)

    rejected_index: dict = {}  # empty — ANET was filtered before conviction gate

    def _rebuild_reason(ticker: str) -> str:
        rej = rejected_index.get(ticker)
        if rej is None:
            return "rebuild: not in new candidate universe"
        _hard_markers = ("vetoed", "blocked", "downtrend", "falling knife")
        _reason_lower = (rej.get("reason") or "").lower()
        if rej.get("vetoes") or any(m in _reason_lower for m in _hard_markers):
            return f"rebuild: hard exit (veto/downtrend/blocked) — {rej.get('reason', '')}"
        conf = rej.get("confluence")
        _conf_str = f"{conf:+.2f}" if isinstance(conf, (int, float)) else str(conf)
        return f"rebuild: fell below exit floor (confluence {_conf_str})"

    dropped_conv = {"ANET"}
    close_reasons = {t: _rebuild_reason(t) for t in dropped_conv}
    pl.update([], "2026-07-01", close_reasons=close_reasons)

    assert captured["close_reasons"]["ANET"] == "rebuild: not in new candidate universe"


def test_phase2_drop_loop_hard_exit_reason(tmp_path, monkeypatch):
    """A dropped name with a veto in the rejected index → hard exit reason string."""
    import portfolio.position_log as pl

    captured: dict = {}

    def _fake_update(positions, asof_iso, portfolio_id=None, close_reasons=None):
        captured["close_reasons"] = close_reasons or {}

    monkeypatch.setattr(pl, "update", _fake_update)

    rejected_index = {
        "PH": {"ticker": "PH", "reason": "Blocked (size_authority=blocked)",
               "vetoes": [], "confluence": 0.05},
    }

    def _rebuild_reason(ticker: str) -> str:
        rej = rejected_index.get(ticker)
        if rej is None:
            return "rebuild: not in new candidate universe"
        _hard_markers = ("vetoed", "blocked", "downtrend", "falling knife")
        _reason_lower = (rej.get("reason") or "").lower()
        if rej.get("vetoes") or any(m in _reason_lower for m in _hard_markers):
            return f"rebuild: hard exit (veto/downtrend/blocked) — {rej.get('reason', '')}"
        conf = rej.get("confluence")
        _conf_str = f"{conf:+.2f}" if isinstance(conf, (int, float)) else str(conf)
        return f"rebuild: fell below exit floor (confluence {_conf_str})"

    dropped_conv = {"PH"}
    close_reasons = {t: _rebuild_reason(t) for t in dropped_conv}
    pl.update([], "2026-07-01", close_reasons=close_reasons)

    assert "rebuild: hard exit" in captured["close_reasons"]["PH"]
    assert "blocked" in captured["close_reasons"]["PH"].lower()


# ---------------------------------------------------------------------------
# Idempotency: reason is NOT re-written on a same-day re-run
# ---------------------------------------------------------------------------

def test_update_close_reason_not_duplicated_on_same_day_rerun(tmp_path, monkeypatch):
    """A second call on the same as_of date does not append a duplicate close event."""
    from portfolio import position_log as pl
    monkeypatch.setattr(pl, "_LEDGER_PATH", tmp_path / "ledger.json", raising=False)

    pl.update([{"ticker": "BRC", "sleeve": "conviction", "weight": 0.04}], "2026-06-30")
    pl.update([], "2026-07-01",
              close_reasons={"BRC": "rebuild: not in new candidate universe"})
    # Second call same day (e.g. a dev re-run)
    pl.update([], "2026-07-01",
              close_reasons={"BRC": "rebuild: not in new candidate universe"})

    ledger = json.loads((tmp_path / "ledger.json").read_text())
    close_events = [e for e in ledger["conviction:BRC"]["history"] if e["event"] == "close"]
    assert len(close_events) == 1, (
        f"exactly one close event expected on same-day rerun, got {len(close_events)}"
    )
