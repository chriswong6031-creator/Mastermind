"""INCIDENT REPLAY BATTERY — 2026-07-02 Semis/AI Breakdown.

This file is the permanent executable memory of the incident.  Every assertion here
corresponds to a concrete failure mode from the post-mortem (INCIDENT_REPORT.md §3
counterfactual) and passes on the CURRENT W0-W4 stack.  A regression in any of these
tests means the stack has drifted back toward the incident's failure modes.

FIVE CANONICAL ASSERTIONS (W-I Task 4 spec):
  (1) 07-01 CAUTION->RISK_ON flip is blocked by the dwell machine.
  (2) sev-2 tripwire + gross 0.90 => eff_cap 0.70 cuts the heavyweight book.
  (3) Autonomous SMH rebuy on 07-02 is rejected by firm name-cap (peer pile-up > cap).
  (4) XLK late_cycle blocks a NEW semis seed; XLV and XLU are entry_favored.
  (5) budget() < 0.50 on the 07-01 regime file (conf=0.327, STABLE, flip_margin=0.05).

Structure: fixtures are loaded from the sibling fixtures/2026-07-02-semis-breakdown/
directory.  No live files are touched.  All side-effects are monkeypatched via the
conftest autouse fixtures (store._DB / position_log / runlog all isolated).
"""
from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest

import bot  # noqa: F401  -> vendor/macro onto sys.path

_FIX = Path(__file__).resolve().parent / "fixtures" / "2026-07-02-semis-breakdown"

# ── fixture loaders ─────────────────────────────────────────────────────────────────────────────

def _state_json(day: str) -> dict:
    """The recorded macro_risk state.json for a replay day."""
    p = _FIX / day / "state.json"
    content = json.loads(p.read_text())
    # state.json may be wrapped in {"agent":..., "state":{...}} or be the inner dict directly.
    return content.get("state", content)


def _regime() -> dict:
    """The vendored regime/latest.json snapshot consumed by the bot on 07-01."""
    return json.loads((_FIX / "regime_latest.json").read_text())


def _sector_cycles() -> dict:
    """The sector_cycles.json snapshot as of 07-02 (fresh, age 0 trading days)."""
    return json.loads((_FIX / "sector_cycles.json").read_text())


def _peer_books() -> dict:
    """The synthetic peer-book holdings at the incident date."""
    return json.loads((_FIX / "peer_books.json").read_text())


def _derisk_sev(day: str) -> int:
    """Max tripwire severity recorded for any book on ``day``."""
    worst = 0
    day_dir = _FIX / day
    if not day_dir.exists():
        return 0
    for f in day_dir.glob("derisk_*.json"):
        try:
            j = json.loads(f.read_text())
            sev = ((j or {}).get("tripwire") or {}).get("severity")
            worst = max(worst, int(sev or 0))
        except (TypeError, ValueError, OSError):
            pass
    return worst


# ── shared helpers ────────────────────────────────────────────────────────────────────────────────

class _MemStore:
    """In-memory dwell-state stand-in (mirrors the pattern from test_macro_risk_dwell.py)."""

    def __init__(self, seed=None):
        self.rec = seed

    def load(self):
        return self.rec

    def save(self, j):
        self.rec = j


def _patch_axes(monkeypatch, day: str) -> None:
    """Force the five axis scorers to emit the recorded per-day axis fragilities."""
    from brain import macro_risk as MR
    ax = _state_json(day)["axes"]
    monkeypatch.setattr(MR, "_collect", lambda regime: {
        "regime": {}, "sector_rs": [], "crowded_baskets": [], "transition_flags": {}
    })
    monkeypatch.setattr(MR, "_axis_volatility",   lambda s: (ax["volatility"]["fragility"],   "replay"))
    monkeypatch.setattr(MR, "_axis_credit_usd",   lambda s: (ax["credit_usd"]["fragility"],   "replay"))
    monkeypatch.setattr(MR, "_axis_liquidity",    lambda s: (ax["liquidity"]["fragility"],     "replay"))
    monkeypatch.setattr(MR, "_axis_crowding",     lambda s: (ax["crowding"]["fragility"], [], "replay"))
    monkeypatch.setattr(MR, "_axis_dealer_gamma", lambda s: (ax["dealer_gamma"]["fragility"], "replay"))


# ═══════════════════════════════════════════════════════════════════════════════════════════════════
# ASSERTION 1 — 07-01 CAUTION->RISK_ON flip is blocked (dwell machine)
# ═══════════════════════════════════════════════════════════════════════════════════════════════════

def test_dwell_blocks_0701_caution_to_risk_on_flip(monkeypatch):
    """The dwell machine holds CAUTION on 07-01 even though the stateless scorer read risk_on.

    Replay: escalate on 06-26 (caution, frag 0.552), hold through 06-29/06-30 (caution,
    frag 0.516 / 0.4685), then run 07-01 with raw=risk_on (frag 0.121) + recorded sev-2
    tripwire.  The state must stay CAUTION and the gross_cap must be < 1.0.
    """
    from brain import macro_risk as MR

    store = _MemStore(seed=None)   # cold start
    seq: dict[str, dict] = {}

    # Roll the machine forward through the pre-crash session sequence.
    for day in ("2026-06-26", "2026-06-29", "2026-06-30", "2026-07-01"):
        _patch_axes(monkeypatch, day)
        st = MR.risk_state(day, {}, dwell=True,
                           state_loader=store.load, state_saver=store.save,
                           tripwire_sev=_derisk_sev(day))
        seq[day] = st

    # Sanity: the raw read for 07-01 IS risk_on (the stateless bug)
    assert seq["2026-07-01"]["raw_state"] == "risk_on", (
        "test setup: 07-01 raw scorer should read risk_on (the crash collapsed the crowding axis)"
    )

    crash = seq["2026-07-01"]

    # THE FIX: dwell state stays CAUTION
    assert crash["state"] == "caution", (
        "07-01 must NOT flip to risk_on — the dwell machine must hold the prior CAUTION state"
    )
    assert crash["gross_cap"] < 1.0, (
        "gross_cap must be < 1.0 on 07-01 (the un-cap to 1.0 was the bug)"
    )
    assert crash["gross_cap"] <= MR.gross_cap("caution") + 1e-9, (
        "gross_cap must be caution-grade, not looser than the caution ceiling"
    )
    # The clamp must cite the severity-2 tripwire (it's what blocks the flip)
    assert crash["clamp_reason"] and "tripwire" in crash["clamp_reason"], (
        "clamp_reason must reference the tripwire that blocked de-escalation"
    )


# ═══════════════════════════════════════════════════════════════════════════════════════════════════
# ASSERTION 2 — sev-2 eff_cap cuts a heavyweight-style 0.90-gross book to 0.70
# ═══════════════════════════════════════════════════════════════════════════════════════════════════

def test_sev2_eff_cap_cuts_0p90_gross_book(monkeypatch):
    """eff_cap = min(state_cap=1.0, sev_cap=0.70) = 0.70 must cut a 0.90-gross book.

    Before W0-W2 fix (BUG-A): the code took ONLY state_cap (1.0 for risk_on), so a
    correctly-fired severity-2 tripwire did nothing to a book under 1.0.  The fix:
    eff_cap = min(state_cap, severity_cap) so the cut ALWAYS bites at sev>=2.

    This test reconstructs the heavyweight-style scenario from counterfactual.md §5:
    state=risk_on, gross_cap=1.0, severity=2, book gross=0.90.  eff_cap must be 0.70
    and the book must be cut.
    """
    from bot import derisk as D

    # Patch the macro state: risk_on / cap=1.0 (the STABLE/Goldilocks label)
    from brain import macro_risk as real_mr
    monkeypatch.setattr(real_mr, "risk_state",
                        lambda asof, regime: {
                            "state": "risk_on", "gross_cap": 1.0, "drivers": [],
                            "allow_adds": True
                        }, raising=True)

    # Calm tape / no GEX / no credit / no theme (severity comes from the tripwire arg only)
    ov_stub = types.ModuleType("data_layer.overnight")
    ov_stub.tape = lambda force=False: {"risk": {"state": "calm"}}
    ov_stub._fetch_changes = lambda syms: {}
    import data_layer as _dl
    monkeypatch.setattr(_dl, "overnight", ov_stub, raising=False)
    monkeypatch.setitem(sys.modules, "data_layer.overnight", ov_stub)

    monkeypatch.setattr(D, "_gex_flip",    lambda: (False, ""))
    monkeypatch.setattr(D, "_credit_gap",  lambda: (False, ""))
    monkeypatch.setattr(D, "_theme_drop",  lambda drivers: (True, "theme day: SOXX -6.4% (≤ -4%)"))
    # theme_drop alone → severity=2, trigger=True

    # Heavyweight-style book: 9 positions, gross ≈ 0.90.
    # Weights mirror the incident counterfactual (counterfactual.md §4): heavyweight gross=0.8984.
    # SMH+XLK are leadership legs; others are conviction.  Sum = 0.8984 ≈ 0.90.
    positions = [
        {"ticker": "SMH",   "sleeve": "leadership", "current_weight": 0.1499, "entry_price": 600.0},
        {"ticker": "XLK",   "sleeve": "leadership", "current_weight": 0.1300, "entry_price": 180.0},
        {"ticker": "NVDA",  "sleeve": "conviction",  "current_weight": 0.0800, "entry_price": 120.0},
        {"ticker": "MSFT",  "sleeve": "conviction",  "current_weight": 0.0800, "entry_price": 380.0},
        {"ticker": "AAPL",  "sleeve": "conviction",  "current_weight": 0.0800, "entry_price": 190.0},
        {"ticker": "AMZN",  "sleeve": "conviction",  "current_weight": 0.0800, "entry_price": 200.0},
        {"ticker": "GOOGL", "sleeve": "conviction",  "current_weight": 0.0800, "entry_price": 170.0},
        {"ticker": "META",  "sleeve": "conviction",  "current_weight": 0.0800, "entry_price": 600.0},
        {"ticker": "TSLA",  "sleeve": "conviction",  "current_weight": 0.0800, "entry_price": 250.0},
        {"ticker": "MTUM",  "sleeve": "conviction",  "current_weight": 0.0385, "entry_price": 260.0},
    ]
    gross_before = round(sum(p["current_weight"] for p in positions), 4)
    assert 0.87 <= gross_before <= 0.92, f"test setup: gross={gross_before}"

    # Wire the position subsystem stubs
    import portfolio as _pf_pkg
    import brain as _brain_pkg
    from portfolio import fragility_chain as _real_fc
    from brain import risk_officer as _real_ro

    pl = types.ModuleType("portfolio.position_log")
    pl.open_positions = lambda portfolio_id=None: list(positions)
    closed: list[str] = []
    pl.close_position = lambda sleeve, t, asof, reason="x", portfolio_id=None: closed.append(t) or True

    pa = types.ModuleType("portfolio.paper_account")
    filled: list[str] = []
    pa.execute_fill = lambda t, side, asof=None, **k: filled.append(t) or {"ok": True}
    pa._current_price = lambda t: 100.0
    pa.load_pending_target = lambda pid=None: None

    lg = types.ModuleType("brain.ledger")
    lg.close = lambda t, note="": None

    monkeypatch.setattr(_pf_pkg, "position_log", pl, raising=False)
    monkeypatch.setattr(_pf_pkg, "paper_account", pa, raising=False)
    monkeypatch.setattr(_pf_pkg, "fragility_chain", _real_fc, raising=False)
    monkeypatch.setattr(_brain_pkg, "risk_officer", _real_ro, raising=False)
    monkeypatch.setattr(_brain_pkg, "ledger", lg, raising=False)
    monkeypatch.setitem(sys.modules, "portfolio.position_log", pl)
    monkeypatch.setitem(sys.modules, "portfolio.paper_account", pa)
    monkeypatch.setitem(sys.modules, "brain.ledger", lg)

    res = D.derisk_flagship("2026-07-01", regime={}, force=True)

    tw = res.get("tripwire") or {}
    assert tw.get("trigger") is True, "tripwire must fire (theme-day sev-2)"
    assert tw.get("severity") == 2, "severity must be 2"

    eff_cap = res.get("eff_cap")
    assert eff_cap is not None, "eff_cap must be present in the result"
    assert abs(eff_cap - 0.70) < 1e-6, f"eff_cap must be 0.70 (sev-2 cap), got {eff_cap}"

    # Exits must have been queued (book was over 0.70)
    assert res.get("action") != "hold", (
        f"gross {gross_before} > eff_cap {eff_cap}: must exit names, not hold. action={res.get('action')}"
    )


# ═══════════════════════════════════════════════════════════════════════════════════════════════════
# ASSERTION 3 — 07-02 SMH rebuy rejected by firm name cap
# ═══════════════════════════════════════════════════════════════════════════════════════════════════

def test_smh_rebuy_rejected_by_firm_cap(monkeypatch, tmp_path):
    """clamp_book() zeros the autonomous SMH add when peer pile-up already saturates firm name cap.

    Firm SMH name pile-up from counterfactual.md §4b:
      etf 0.0196 + heavyweight 0.1499 = 0.2695 (already over firm_name_cap=0.10 from peers alone).
    A new autonomous SMH weight of 0.1621 (the actual rebuy size) must be clamped to 0.

    clamp_book() is PURE and DI-friendly: we inject a fake _peer_exposure() that returns the
    incident peer weights, bypassing the live latest.json reads.
    """
    from portfolio import firm_exposure as FE

    peer_data = _peer_books()

    # Build the by_name / by_cluster peer aggregation from fixture data
    # mirroring FE._peer_exposure() output structure
    def _cluster_id(ticker: str) -> str:
        """Minimal cluster mapping for the test: SMH/XLK/NVDA/MSFT/AAPL/AMZN/GOOGL/META → semis_ai."""
        SEMIS_AI = {"SMH", "XLK", "NVDA", "AMD", "AMAT", "LRCX", "KLAC", "MU", "IREN",
                    "MSFT", "AAPL", "AMZN", "GOOGL", "META", "TSLA"}
        t = ticker.upper()
        return "semis_ai" if t in SEMIS_AI else t

    # Aggregate peer exposure (ETF + heavyweight — autonomous is excluded as 'self').
    # Skip comment keys (start with '_') and the requesting book's own entry.
    by_name: dict[str, float] = {}
    by_cluster: dict[str, float] = {}
    for pid, book in peer_data.items():
        if pid.startswith("_") or not isinstance(book, dict):
            continue   # skip metadata comment keys
        if pid == "autonomous":
            continue   # autonomous is the requesting book — excluded from peers
        for pos in book.get("positions", []):
            tk = pos["ticker"].upper()
            w = float(pos["weight"])
            by_name[tk] = by_name.get(tk, 0.0) + w
            cid = _cluster_id(tk)
            by_cluster[cid] = by_cluster.get(cid, 0.0) + w

    # Peer SMH: etf(0.0196) + heavyweight(0.1499) = 0.2695 — already >> firm name cap 0.10
    peer_smh = by_name.get("SMH", 0.0)
    assert peer_smh > 0.10, f"test setup: peer SMH {peer_smh:.4f} must exceed firm name cap"

    # Inject the fake peer_exposure into clamp_book's internal helper
    monkeypatch.setattr(FE, "_peer_exposure",
                        lambda book_id: {"by_name": by_name, "by_cluster": by_cluster},
                        raising=False)
    # Also patch cluster_id to our minimal version so the cluster pass works consistently
    monkeypatch.setattr(FE, "_cluster_id", _cluster_id, raising=False)

    # Autonomous target: adds SMH 0.1621 (the crash-day rebuy from the incident)
    autonomous_target = [
        {"ticker": "SMH",  "weight": 0.1621},  # the $24.8k rebuy that must be rejected
        {"ticker": "EME",  "weight": 0.0935},
        {"ticker": "URI",  "weight": 0.0852},
        {"ticker": "APH",  "weight": 0.0846},
        {"ticker": "HWM",  "weight": 0.0793},
    ]

    result = FE.clamp_book(autonomous_target, "autonomous")

    assert result["bound"] is True, "clamp_book must bind (firm cap exceeded)"
    # Find the SMH row after clamping
    out_positions = result["positions"]
    smh_after = next((p["weight"] for p in out_positions
                      if p.get("ticker", "").upper() == "SMH"), None)

    # The peer pile-up (0.2695) already exceeds firm_name_cap (0.10), so headroom is 0
    assert smh_after is not None, "SMH must appear in clamped positions"
    assert smh_after < 1e-6, (
        f"SMH must be clamped to ~0 (peer pile-up {peer_smh:.4f} >= firm cap 0.10), "
        f"got {smh_after:.4f}"
    )


# ═══════════════════════════════════════════════════════════════════════════════════════════════════
# ASSERTION 4 — XLK late_cycle blocks new semis seed; XLV/XLU are entry_favored
# ═══════════════════════════════════════════════════════════════════════════════════════════════════

def test_cycles_xlk_late_cycle_blocks_semis_seed(monkeypatch):
    """regime_frame.cycles() with the incident sector_cycles fixture returns:
      - XLK: late_cycle=True (phase=Peak, pos=80.8, osc_slope=-18.4)
      - XLV: entry_favored=True (phase=Expansion)
      - XLU: entry_favored=True (phase=Trough)

    A new SMH leadership leg maps to XLK's sector row; the extension brake halves or blocks it.
    XLV/XLU being entry_favored means the DEF_SLEEVE can seed there.
    """
    from brain import regime_frame as RF

    # Inject the incident sector_cycles fixture via the module-level path
    cycles_data = _sector_cycles()
    monkeypatch.setattr(RF, "_CYCLES_PATH",
                        _FIX / "sector_cycles.json", raising=False)
    # Also patch _trading_days_since so the freshness gate sees age=0 (today's file)
    monkeypatch.setattr(RF, "_trading_days_since", lambda asof: 0, raising=False)

    cy = RF.cycles()

    assert cy, "cycles() must return a non-empty dict with the incident fixture (asOf=2026-07-02)"

    # XLK (Technology sector): phase=Peak, pos=80.8, osc_slope=-18.4 → late_cycle=True
    xlk = cy.get("XLK")
    assert xlk is not None, "XLK must be in cycles() output"
    assert xlk["late_cycle"] is True, (
        f"XLK must be late_cycle (Peak/pos≥70/osc_slope<0) — got {xlk}"
    )
    assert xlk["entry_favored"] is False, "XLK late_cycle must NOT be entry_favored"

    # XLV (Healthcare): phase=Expansion → entry_favored=True
    xlv = cy.get("XLV")
    assert xlv is not None, "XLV must be in cycles() output"
    assert xlv["entry_favored"] is True, (
        f"XLV must be entry_favored (Expansion phase) — got {xlv}"
    )

    # XLU (Utilities): phase=Trough → entry_favored=True
    xlu = cy.get("XLU")
    assert xlu is not None, "XLU must be in cycles() output"
    assert xlu["entry_favored"] is True, (
        f"XLU must be entry_favored (Trough phase) — got {xlu}"
    )

    # Validate the late_cycle brake would halve a new SMH leg
    # SMH maps to XLK sector; late_cycle_mult = 0.5 (doctrine default)
    from portfolio.sleeves import apply_leadership_caps

    new_smh_leg = [{"ticker": "SMH", "sleeve": "leadership", "weight": 0.10,
                    "verdict": "new"}]  # not retained → not held → cycle brake fires
    result = apply_leadership_caps(
        new_smh_leg,
        cycles=cy,
        trend_fn=lambda t: {"pct_vs_200d": 10.0},  # not over-extended → only cycle brake fires
        held=set(),  # SMH not in held set → it IS a new leg
    )

    assert result["freed_to_cash"] > 0, "late_cycle brake must free weight to cash for new SMH leg"
    smh_after = new_smh_leg[0]["weight"]
    assert smh_after < 0.10, "SMH new leg must be halved by late_cycle brake"
    # The brake should approximately halve it (late_cycle_mult=0.5 → weight 0.05)
    assert smh_after <= 0.051, f"SMH new leg weight after brake should be ~0.05, got {smh_after:.4f}"


# ═══════════════════════════════════════════════════════════════════════════════════════════════════
# ASSERTION 5 — budget() < 0.50 on the 07-01 regime file
# ═══════════════════════════════════════════════════════════════════════════════════════════════════

def test_budget_below_0p50_on_0701_regime(monkeypatch, tmp_path):
    """budget() on the 07-01 regime fixture (conf=0.327, STABLE, flip_margin=0.05) must be < 0.50.

    The ONE equation:
      lead_budget = clamp(0.40 + 0.20 · 0.327 · T · F, 0.40, 0.60)
      T = 1.0 (STABLE → calm-tape, no shrink from transition term)
      F = 0.75 (flip_margin=0.05 < flip_margin_min=0.15 → fragility damp fires)
      raw = 0.40 + 0.20 · 0.327 · 1.0 · 0.75 = 0.40 + 0.04905 = 0.44905

    Before W2 the budget was HARDWIRED 0.50.  Today it must flex to 0.449.
    """
    from brain import regime_frame as RF

    # Write the regime fixture into a tmp file and inject the path
    regime_file = tmp_path / "regime_latest.json"
    regime_file.write_text((_FIX / "regime_latest.json").read_text())

    orig_paths = dict(RF._REGION_PATHS)
    monkeypatch.setattr(RF, "_REGION_PATHS",
                        {**orig_paths, "us": regime_file}, raising=False)

    result = RF.budget("us")
    lb = result["lead_budget"]
    inputs = result["inputs"]

    assert inputs["confidence"] == pytest.approx(0.327, abs=1e-6), (
        f"confidence must be 0.327 (from fixture), got {inputs['confidence']}"
    )
    assert inputs["transition_state"] == "STABLE", (
        f"transition_state must be STABLE (from fixture), got {inputs['transition_state']}"
    )
    assert inputs["T"] == pytest.approx(1.0, abs=1e-6), (
        "T must be 1.0 for STABLE (no transition multiplier)"
    )
    assert inputs["F"] == pytest.approx(0.75, abs=1e-6), (
        "F must be 0.75 (flip_margin=0.05 < flip_margin_min=0.15 → fragility damp)"
    )
    assert lb < 0.50, (
        f"budget must be < 0.50 (the old hardwired value), got {lb:.5f}"
    )
    assert lb == pytest.approx(0.44905, abs=1e-4), (
        f"budget must be ~0.449 (0.40 + 0.20·0.327·1.0·0.75), got {lb:.5f}"
    )


# ═══════════════════════════════════════════════════════════════════════════════════════════════════
# BONUS — confirm fixture integrity (guards against fixture rot)
# ═══════════════════════════════════════════════════════════════════════════════════════════════════

def test_fixture_integrity():
    """Sanity-check the fixture files so future changes to the fixture schema break loudly here."""
    # state.json per day
    for day, expected_state in [
        ("2026-06-26", "caution"),
        ("2026-06-29", "caution"),
        ("2026-06-30", "caution"),
        ("2026-07-01", "risk_on"),  # raw stateless — dwell holds CAUTION
        ("2026-07-02", "risk_on"),  # raw stateless — dwell holds CAUTION (dwell=1 in fixture)
    ]:
        s = _state_json(day)
        assert s["state"] == expected_state, (
            f"fixture {day}/state.json: expected state={expected_state}, got {s['state']}"
        )
        assert "axes" in s and "fragility" in s, (
            f"fixture {day}/state.json: must have axes and fragility keys"
        )

    # regime fixture
    r = _regime()
    assert r["quad"] == "Q1" and r["quad_name"] == "Goldilocks"
    assert r["transition_state"] == "STABLE"
    assert abs(r["confidence"] - 0.327) < 1e-6
    assert r["flip_condition"]["margin"] == pytest.approx(0.05, abs=1e-6)

    # sector_cycles fixture
    cy_raw = _sector_cycles()
    assert cy_raw["meta"]["asOf"] == "2026-07-02"
    tickers = {s["ticker"] for s in cy_raw.get("sectors", [])}
    for required in ("XLK", "XLV", "XLU"):
        assert required in tickers, f"sector_cycles fixture must contain {required}"

    # peer books fixture
    pb = _peer_books()
    for pid in ("etf", "heavyweight"):
        assert pid in pb, f"peer_books fixture must contain {pid}"
        smh_w = next((p["weight"] for p in pb[pid]["positions"] if p["ticker"] == "SMH"), 0)
        assert smh_w > 0, f"peer {pid} must have a non-zero SMH position in the fixture"

    # etf_closes fixture
    closes = json.loads((_FIX / "etf_closes.json").read_text())
    for tk in ("SMH", "XLV", "XLK", "XLU", "SPY"):
        assert tk in closes, f"etf_closes fixture must contain {tk}"
        # SMH should show a loss from its incident-window peak (06-22) to the end of the window
        # (07-01): 668.91 -> 620.46 = -7.2%.  The fixture includes 06-22 as the first date.
        if tk == "SMH":
            dates = sorted(closes[tk].keys())
            peak_close = closes[tk]["2026-06-22"]   # the pre-breakdown high
            last_close = closes[tk]["2026-07-01"]   # end of the incident window
            assert last_close < peak_close, (
                f"SMH should have fallen from its 06-22 peak to 07-01 "
                f"(peak={peak_close:.2f} last={last_close:.2f})"
            )
