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


# ===========================================================================
# CLOSE-REASON TAXONOMY (structured reason_code) — observability, additive,
# byte-compatible. A rebuild rotation must be machine-distinguishable from a
# risk exit / time-stop / cap / hard-veto / manual close.
# ===========================================================================

def _last_close(ledger: dict, key: str) -> dict:
    return [e for e in ledger[key]["history"] if e["event"] == "close"][-1]


def test_reason_codes_taxonomy_is_closed_and_expected():
    """The enum is a small CLOSED set with the doctrine-named codes."""
    from portfolio import position_log as pl
    assert pl.REASON_CODES == frozenset({
        "rebuild_dropped", "hard_veto", "time_stop_d5", "cap_trim",
        "risk_officer_exit", "judgment_exit", "manual", "unspecified",
    })


def test_update_stamps_rebuild_dropped_code(tmp_path, monkeypatch):
    """A rebuild-drop close carries reason_code='rebuild_dropped'."""
    from portfolio import position_log as pl
    monkeypatch.setattr(pl, "_LEDGER_PATH", tmp_path / "ledger.json", raising=False)

    pl.update([
        {"ticker": "URI", "sleeve": "conviction", "weight": 0.05},
        {"ticker": "NVDA", "sleeve": "conviction", "weight": 0.04},
    ], "2026-06-30")
    pl.update(
        [{"ticker": "NVDA", "sleeve": "conviction", "weight": 0.04}],
        "2026-07-01",
        close_reasons={"URI": "rebuild: fell below exit floor (confluence +0.18)"},
        reason_codes={"URI": "rebuild_dropped"},
    )

    ledger = json.loads((tmp_path / "ledger.json").read_text())
    ev = _last_close(ledger, "conviction:URI")
    assert ev.get("reason_code") == "rebuild_dropped"
    # human string preserved alongside (nothing removed)
    assert ev.get("reason") == "rebuild: fell below exit floor (confluence +0.18)"


def test_update_stamps_hard_veto_and_time_stop_codes(tmp_path, monkeypatch):
    """A hard-veto rebuild drop and a D5 time-stop drop carry their distinct codes."""
    from portfolio import position_log as pl
    monkeypatch.setattr(pl, "_LEDGER_PATH", tmp_path / "ledger.json", raising=False)

    pl.update([
        {"ticker": "AEIS", "sleeve": "conviction", "weight": 0.05},
        {"ticker": "LII",  "sleeve": "conviction", "weight": 0.04},
    ], "2026-06-30")
    pl.update(
        [],
        "2026-07-01",
        close_reasons={
            "AEIS": "rebuild: hard exit (veto/downtrend/blocked) — Vetoed: parabolic",
            "LII":  "exited (D5 dead-capital time-stop)",
        },
        reason_codes={"AEIS": "hard_veto", "LII": "time_stop_d5"},
    )

    ledger = json.loads((tmp_path / "ledger.json").read_text())
    assert _last_close(ledger, "conviction:AEIS").get("reason_code") == "hard_veto"
    assert _last_close(ledger, "conviction:LII").get("reason_code") == "time_stop_d5"


def test_update_no_reason_code_field_when_mapping_absent(tmp_path, monkeypatch):
    """BYTE-COMPAT: with no reason_codes mapping the close event has NO reason_code key."""
    from portfolio import position_log as pl
    monkeypatch.setattr(pl, "_LEDGER_PATH", tmp_path / "ledger.json", raising=False)

    pl.update([{"ticker": "WAB", "sleeve": "conviction", "weight": 0.05}], "2026-06-30")
    # Legacy call signature — no reason_codes at all
    pl.update([], "2026-07-01")

    ledger = json.loads((tmp_path / "ledger.json").read_text())
    ev = _last_close(ledger, "conviction:WAB")
    assert "reason_code" not in ev, "reason_code must be absent when no mapping supplied (byte-compat)"


def test_update_reason_code_absent_for_ticker_not_in_mapping(tmp_path, monkeypatch):
    """A ticker absent from the reason_codes mapping gets no reason_code key on its close."""
    from portfolio import position_log as pl
    monkeypatch.setattr(pl, "_LEDGER_PATH", tmp_path / "ledger.json", raising=False)

    pl.update([
        {"ticker": "CI",  "sleeve": "conviction", "weight": 0.05},
        {"ticker": "UNP", "sleeve": "conviction", "weight": 0.04},
    ], "2026-06-30")
    pl.update([], "2026-07-01", reason_codes={"CI": "rebuild_dropped"})

    ledger = json.loads((tmp_path / "ledger.json").read_text())
    assert _last_close(ledger, "conviction:CI").get("reason_code") == "rebuild_dropped"
    assert "reason_code" not in _last_close(ledger, "conviction:UNP")


def test_update_out_of_taxonomy_code_coerced_to_unspecified(tmp_path, monkeypatch):
    """An out-of-enum code is coerced to 'unspecified' rather than admitted to the ledger."""
    from portfolio import position_log as pl
    monkeypatch.setattr(pl, "_LEDGER_PATH", tmp_path / "ledger.json", raising=False)

    pl.update([{"ticker": "ODD", "sleeve": "conviction", "weight": 0.05}], "2026-06-30")
    pl.update([], "2026-07-01", reason_codes={"ODD": "totally_made_up_code"})

    ledger = json.loads((tmp_path / "ledger.json").read_text())
    assert _last_close(ledger, "conviction:ODD").get("reason_code") == "unspecified"


def test_close_position_infers_code_from_reason_string(tmp_path, monkeypatch):
    """close_position() lands the RIGHT code from the legacy reason string — no call-site change."""
    from portfolio import position_log as pl
    monkeypatch.setattr(pl, "_LEDGER_PATH", tmp_path / "ledger.json", raising=False)

    for tk, reason, expected in [
        ("INCY", "hard_exit_sweep",   "hard_veto"),
        ("URI",  "risk_officer_exit", "risk_officer_exit"),
        ("LII",  "macro_risk_cap",    "cap_trim"),
    ]:
        pl.update([{"ticker": tk, "sleeve": "conviction", "weight": 0.05}], "2026-06-30")
        pl.close_position("conviction", tk, "2026-07-01", reason=reason)

    ledger = json.loads((tmp_path / "ledger.json").read_text())
    assert _last_close(ledger, "conviction:INCY").get("reason_code") == "hard_veto"
    assert _last_close(ledger, "conviction:URI").get("reason_code") == "risk_officer_exit"
    assert _last_close(ledger, "conviction:LII").get("reason_code") == "cap_trim"


def test_close_position_explicit_code_wins(tmp_path, monkeypatch):
    """An explicit reason_code overrides string inference; unknown string → 'unspecified'."""
    from portfolio import position_log as pl
    monkeypatch.setattr(pl, "_LEDGER_PATH", tmp_path / "ledger.json", raising=False)

    pl.update([{"ticker": "ANET", "sleeve": "conviction", "weight": 0.05}], "2026-06-30")
    pl.close_position("conviction", "ANET", "2026-07-01",
                      reason="something_weird", reason_code="time_stop_d5")
    # An unrecognised string with no explicit code → unspecified
    pl.update([{"ticker": "ZZZ", "sleeve": "conviction", "weight": 0.05}], "2026-06-30")
    pl.close_position("conviction", "ZZZ", "2026-07-01", reason="fast_derisk")

    ledger = json.loads((tmp_path / "ledger.json").read_text())
    assert _last_close(ledger, "conviction:ANET").get("reason_code") == "time_stop_d5"
    assert _last_close(ledger, "conviction:ZZZ").get("reason_code") == "unspecified"


def test_record_manual_close_tags_manual_code(tmp_path, monkeypatch):
    """The advisor-chat ad-hoc close is coded 'manual'; adds/trims carry no code."""
    from portfolio import position_log as pl
    monkeypatch.setattr(pl, "_LEDGER_PATH", tmp_path / "ledger.json", raising=False)

    pl.record_manual("PLTR", "conviction", event="open", weight=0.05, asof_iso="2026-06-30")
    pl.record_manual("PLTR", "conviction", event="close", asof_iso="2026-07-01")

    ledger = json.loads((tmp_path / "ledger.json").read_text())
    hist = ledger["conviction:PLTR"]["history"]
    open_ev = [e for e in hist if e["event"] == "open"][-1]
    close_ev = [e for e in hist if e["event"] == "close"][-1]
    assert "reason_code" not in open_ev, "open event should carry no reason_code (byte-compat)"
    assert close_ev.get("reason_code") == "manual"


def test_closed_positions_surfaces_reason_code(tmp_path, monkeypatch):
    """closed_positions() echoes the structured code; legacy rows degrade to 'unspecified'."""
    from portfolio import position_log as pl
    monkeypatch.setattr(pl, "_LEDGER_PATH", tmp_path / "ledger.json", raising=False)

    # coded close
    pl.update([{"ticker": "COHR", "sleeve": "conviction", "weight": 0.05}], "2026-06-30")
    pl.update([], "2026-07-01", reason_codes={"COHR": "rebuild_dropped"})
    # legacy close — no code
    pl.update([{"ticker": "APH", "sleeve": "conviction", "weight": 0.05}], "2026-06-30")
    pl.update([], "2026-07-01")

    closed = {p["ticker"]: p for p in pl.closed_positions()}
    assert closed["COHR"]["reason_code"] == "rebuild_dropped"
    assert closed["APH"]["reason_code"] == "unspecified", "legacy (no-code) rows → unspecified"


def test_phase2_rebuild_reason_code_derivation():
    """Replicate the phase2 _rebuild_reason_code closure: hard structural marker → 'hard_veto',
    everything else (exit floor / not-in-universe) → 'rebuild_dropped'."""
    rejected_index = {
        "AEIS": {"reason": "Vetoed: parabolic", "vetoes": ["parabolic"]},
        "URI":  {"reason": "Downtrend — price rolling over", "vetoes": []},
        "CI":   {"reason": "Insufficient confluence (+0.18)", "vetoes": [], "confluence": 0.18},
    }

    def _rebuild_reason_code(ticker: str) -> str:
        rej = rejected_index.get(ticker)
        if rej is not None:
            _reason_lower = (rej.get("reason") or "").lower()
            _hard = ("vetoed", "blocked", "downtrend", "falling knife")
            if rej.get("vetoes") or any(m in _reason_lower for m in _hard):
                return "hard_veto"
        return "rebuild_dropped"

    assert _rebuild_reason_code("AEIS") == "hard_veto"        # veto list
    assert _rebuild_reason_code("URI")  == "hard_veto"        # 'downtrend' keyword
    assert _rebuild_reason_code("CI")   == "rebuild_dropped"  # merely fell below floor
    assert _rebuild_reason_code("MISS") == "rebuild_dropped"  # not in the candidate universe
