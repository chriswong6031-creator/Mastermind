"""Guards for TASK #8 — the free-form Brain books' grading overlay + self-mirror injection.

Offline only (no vendor/macro engine, no network, no real LLM): we never touch real price data —
we monkeypatch `calibration._label_name` to return canned forward labels, and patch the brain
PACKAGE ATTRIBUTES (not sys.modules alone) per the combined-run hang lesson.

We prove:
  * `_book_reliability` builds a multiplier from synthetic holdings + canned outcomes (de-confidence
    only: it shrinks toward realized hit-rate, clamps to [FLOOR, 1.0]), grades each book vs its OWN
    benchmark (SPY for US/autonomous + heavyweight, FXI for china + hk),
  * the four books are registered in `calibration.compute()`,
  * `self_mirror.inject` is the IDENTITY when MASTERMIND_SELF_MIRROR is OFF (byte-identical persona),
  * `self_mirror.inject` APPENDS the book's digest when the flag is ON and the book is `scoring`.
"""
from __future__ import annotations

import json
from datetime import date

import pytest

from brain import calibration as C
from brain import self_mirror as SM

_ASOF = date(2026, 6, 23)
_BOOKS = [("autonomous", "SPY"), ("heavyweight", "SPY"), ("china", "FXI"), ("hk", "FXI")]


def _write_decisions(portfolios_dir, portfolio_id, rows):
    d = portfolios_dir / portfolio_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "decisions.jsonl").write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")


def _decision_rows():
    # one decision dated well before _ASOF so the 21d window has fully elapsed (_elapsed True).
    # 8 winners + 4 losers → reliability 0.667; mean conf high (0.9) → multiplier < 1.0 (shrinks).
    holdings = ([{"ticker": f"W{i}", "weight": 0.1, "conviction": "high"} for i in range(8)]
                + [{"ticker": f"L{i}", "weight": 0.1, "conviction": "high"} for i in range(4)])
    return [{"asof": "2026-01-02", "holdings": holdings}]


def _fake_label(ticker, asof_iso, asof=None, horizon=21, vs="SPY"):
    """Winners (W*) beat the benchmark; losers (L*) trail it. Always resolved."""
    rel = 0.05 if str(ticker).upper().startswith("W") else -0.05
    return {"resolved": True, "rel_return": rel}


# ───────────────────────── grading (calibration._book_reliability) ─────────────────────────

def test_book_reliability_produces_multiplier(monkeypatch, tmp_path):
    monkeypatch.setattr(C, "_PORTFOLIOS", tmp_path)
    monkeypatch.setattr(C, "_label_name", _fake_label)
    for pid, _bench in _BOOKS:
        _write_decisions(tmp_path, pid, _decision_rows())

    for pid, bench in _BOOKS:
        block = C._book_reliability(_ASOF, pid, bench)
        assert block["n"] == 12, (pid, block)            # all 12 holdings resolved
        assert block["reliability"] == pytest.approx(8 / 12, abs=1e-3)
        assert block["status"] == "scoring"              # n >= MIN_N
        # de-confidence only: reliability 0.667 / mean_conf 0.9 < 1.0, clamped to [FLOOR, 1.0]
        assert C.FLOOR <= block["multiplier"] <= 1.0
        assert block["multiplier"] < 1.0


def test_book_reliability_grades_vs_correct_benchmark(monkeypatch, tmp_path):
    """The benchmark passed through reaches _label_name's `vs` arg (SPY for US, FXI for CN/HK)."""
    monkeypatch.setattr(C, "_PORTFOLIOS", tmp_path)
    seen = []

    def _spy_capture(ticker, asof_iso, asof=None, horizon=21, vs="SPY"):
        seen.append((str(ticker), vs))
        return _fake_label(ticker, asof_iso, asof, horizon, vs)

    monkeypatch.setattr(C, "_label_name", _spy_capture)
    _write_decisions(tmp_path, "china", _decision_rows())
    C._book_reliability(_ASOF, "china", "FXI")
    assert seen and all(v == "FXI" for _t, v in seen)


def test_book_reliability_cold_start_inert(monkeypatch, tmp_path):
    """Below MIN_N resolved holdings the multiplier stays 1.0 (cold-start safety)."""
    monkeypatch.setattr(C, "_PORTFOLIOS", tmp_path)
    monkeypatch.setattr(C, "_label_name", _fake_label)
    _write_decisions(tmp_path, "autonomous", [
        {"asof": "2026-01-02", "holdings": [{"ticker": "W0", "weight": 1.0}]}])
    block = C._book_reliability(_ASOF, "autonomous", "SPY")
    assert block["n"] == 1 and block["multiplier"] == 1.0 and block["status"] == "building"


def test_book_reliability_missing_book_empty(monkeypatch, tmp_path):
    monkeypatch.setattr(C, "_PORTFOLIOS", tmp_path)
    block = C._book_reliability(_ASOF, "autonomous", "SPY")
    assert block["n"] == 0 and block["multiplier"] == 1.0


def test_books_registered_in_compute(monkeypatch, tmp_path):
    """All four books appear under compute()['agents'] (never raises; empty when no data)."""
    monkeypatch.setattr(C, "_PORTFOLIOS", tmp_path)
    monkeypatch.setattr(C, "_label_name", _fake_label)
    block = C.compute(_ASOF)
    for pid, _bench in _BOOKS:
        assert pid in block["agents"], pid


# ───────────────────────── self-mirror injection (flag gate) ─────────────────────────

_PERSONAS = {
    "autonomous": "You are the AUTONOMOUS PM.",
    "heavyweight": "You are the HEAVYWEIGHT PM.",
    "china": "You are the CHINA PM.",
    "hk": "You are the HK PM.",
}


def test_inject_off_is_identity(monkeypatch):
    """Flag OFF → inject returns the persona UNCHANGED (byte-identical) for every brain."""
    monkeypatch.delenv("MASTERMIND_SELF_MIRROR", raising=False)
    for book, persona in _PERSONAS.items():
        out = SM.inject(persona, book, _ASOF)
        assert out == persona and out is persona, book


def test_inject_on_appends_digest(monkeypatch, tmp_path):
    """Flag ON + book is `scoring` → inject APPENDS the book's track-record digest."""
    monkeypatch.setenv("MASTERMIND_SELF_MIRROR", "1")
    monkeypatch.setattr(C, "_PORTFOLIOS", tmp_path)
    monkeypatch.setattr(C, "_label_name", _fake_label)

    # a calibration.json that marks every book as scoring (n >= MIN_N) so digest() speaks
    calib = {"agents": {pid: {"status": "scoring", "reliability": 0.667,
                              "multiplier": 0.74, "n": 12} for pid, _b in _BOOKS}}
    monkeypatch.setattr(C, "load", lambda: calib)
    # self_mirror reads calibration via its `_calib` alias — same object, but be explicit
    monkeypatch.setattr(SM._calib, "load", lambda: calib, raising=False)

    for pid, bench in _BOOKS:
        _write_decisions(tmp_path, pid, _decision_rows())

    for book, persona in _PERSONAS.items():
        out = SM.inject(persona, book, _ASOF)
        assert out != persona, book
        assert out.startswith(persona + "\n\n"), book
        assert "YOUR TRACK RECORD" in out, book

    # china/hk digests quote their FXI benchmark in the miss lines, not SPY
    cn = SM.inject(_PERSONAS["china"], "china", _ASOF)
    assert "vs FXI" in cn and "vs SPY" not in cn
