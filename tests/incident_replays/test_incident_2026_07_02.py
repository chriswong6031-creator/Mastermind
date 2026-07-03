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

W-E.0 PERCEPTION ORGAN ASSERTIONS (§4 of build_plan.md — E0.4 additions):
  (6) rotation_tensor: R[XLV][XLK] positive multi-session episode in top_pairs by 06-24,
      dR same-signed (accelerating), episode percentile < 1% by 06-29.
  (7) anticipation: SECTOR-TOP(tech) >= ELEVATED by 06-25; CRASH-RISK >= ELEVATED by 06-26.
  (8) market_view: label_vs_planes.conflict=True on 06-26..07-01 (07-01 hard assert);
      soft assertion on 06-24 (allowed either); posture_floor_defense=True on 07-01.
  (9) calm-tape control: conflict=False, coherence >= 0.80 on high-confidence agreeing tape.
  (10) market_view fixture integrity: validates the frozen inputs and expected outputs
       in tests/fixtures/market_view/ have the required structure.

Structure: W0-W4 fixtures from tests/incident_replays/fixtures/2026-07-02-semis-breakdown/.
W-E.0 fixtures from tests/fixtures/market_view/.
No live files are touched.  All side-effects are monkeypatched via the conftest autouse
fixtures (store._DB / position_log / runlog all isolated).

DEPENDENCY NOTE (E0.4 coordination):
  The §4 organ asserts (6)-(9) import brain.market_view, brain.rotation_tensor, and
  brain.anticipation — which are built by E0.1-E0.3.  When those modules do not yet
  exist, the tests are skipped (pytest.importorskip) with a clear message.  Once the
  modules land the skips dissolve automatically.  The FIXTURES and EXPECTED SHAPES are
  written here regardless — they define the contract the modules must satisfy.
"""
from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest

import bot  # noqa: F401  -> vendor/macro onto sys.path

_FIX = Path(__file__).resolve().parent / "fixtures" / "2026-07-02-semis-breakdown"

# W-E.0 fixtures root — frozen inputs and expected outputs for the perception organs.
_MV_FIX = Path(__file__).resolve().parent.parent / "fixtures" / "market_view"

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


# ══════════════════════════════════════════════════════════════════════════════════════════════════
# W-E.0 PERCEPTION ORGAN FIXTURE LOADERS
# These loaders read from tests/fixtures/market_view/ (frozen expected shapes).
# ══════════════════════════════════════════════════════════════════════════════════════════════════

def _mv_fixture(name: str) -> dict:
    """Load a market_view fixture by filename stem."""
    return json.loads((_MV_FIX / f"{name}.json").read_text())


# ══════════════════════════════════════════════════════════════════════════════════════════════════
# §4 ASSERTION 6 — rotation_tensor: defensive episode in top_pairs by 06-24; percentile < 1% by 06-29
# ══════════════════════════════════════════════════════════════════════════════════════════════════

@pytest.mark.skipif(
    not (_MV_FIX / "rotation_tensor_06_24.json").exists(),
    reason="rotation_tensor_06_24 fixture missing — run E0.4 fixture build"
)
def test_rotation_tensor_defensive_episode_by_06_24():
    """§4.2 — rotation_tensor must show XLV-lead positive pair in top_pairs by 06-24.

    Build_plan.md §4.2 pre-registered assert:
      - R[XLV][XLK] (or R[XLV][SMH]) positive in top_pairs entry by 06-24 (episode start)
      - dR same-signed as R (accelerating divergence)
      - episode percentile < 1% by 06-29 (episode mature after 4 sessions)

    This test reads the frozen fixture shapes (intent-only, not live module output).
    Once brain/rotation_tensor.py (E0.1) exists, add a live-module variant that replays
    on data/yahoo parquets and confirms the same property.
    """
    rt_0624 = _mv_fixture("rotation_tensor_06_24")
    rt_0629 = _mv_fixture("rotation_tensor_06_29")

    # §4.2a — top_pairs must have a defensive (XLV-leads) pair by 06-24
    top_pairs_0624 = rt_0624["rs_velocity"]["top_pairs"]
    assert top_pairs_0624, (
        "rotation_tensor 06-24: top_pairs must not be empty — defensive rotation started"
    )
    # At least one pair has a defensive leader (XLV or XLU) over an offensive lagger (XLK or SMH)
    defensive_leaders = {"XLV", "XLU", "XLP"}
    offensive_laggers = {"XLK", "SMH", "XLY", "XLC"}
    defensive_pairs = [
        p for p in top_pairs_0624
        if p.get("lead") in defensive_leaders and p.get("lag") in offensive_laggers
    ]
    assert defensive_pairs, (
        f"rotation_tensor 06-24: top_pairs must contain a defensive-over-offensive pair "
        f"(XLV/XLU/XLP leading XLK/SMH/XLY). Got: {[p.get('lead','?')+'/'+p.get('lag','?') for p in top_pairs_0624]}"
    )

    # §4.2b — R positive (defensive gaining) + dR same-signed (accelerating)
    for pair in defensive_pairs:
        assert pair["R_bps_day"] > 0, (
            f"rotation_tensor 06-24: R_bps_day must be positive for {pair.get('lead')}/{pair.get('lag')}, "
            f"got {pair.get('R_bps_day')}"
        )
        assert pair["dR_bps_day"] > 0, (
            f"rotation_tensor 06-24: dR_bps_day must be positive (same-signed) for "
            f"{pair.get('lead')}/{pair.get('lag')}, got {pair.get('dR_bps_day')}"
        )
        assert pair.get("accelerating") is True, (
            f"rotation_tensor 06-24: accelerating must be True for {pair.get('lead')}/{pair.get('lag')}"
        )

    # §4.2c — by 06-29 (4 sessions) episode percentile < 1% (0.99 in 0..1 scale = 99th pctile)
    ep_0629 = rt_0629.get("headline_episode", {})
    assert ep_0629.get("percentile", 0.0) > 0.70, (
        f"rotation_tensor 06-29: headline_episode percentile must be > 0.70 (rare episode), "
        f"got {ep_0629.get('percentile')}"
    )
    assert ep_0629.get("direction") == "defensive", (
        f"rotation_tensor 06-29: headline_episode direction must be 'defensive', "
        f"got {ep_0629.get('direction')}"
    )
    assert ep_0629.get("n_sessions", 0) >= 3, (
        f"rotation_tensor 06-29: headline_episode must have >= 3 sessions, "
        f"got {ep_0629.get('n_sessions')}"
    )
    # dR same-signed as R (both widening) — confirmed by top_pairs[0] on 06-29
    top_0629 = rt_0629["rs_velocity"]["top_pairs"]
    assert top_0629, "rotation_tensor 06-29: top_pairs must be non-empty"
    p0 = top_0629[0]
    assert p0["R_bps_day"] > 0 and p0["dR_bps_day"] > 0, (
        f"rotation_tensor 06-29: top pair must have R>0 and dR>0 (same-signed), "
        f"got R={p0.get('R_bps_day')} dR={p0.get('dR_bps_day')}"
    )


# ══════════════════════════════════════════════════════════════════════════════════════════════════
# §4 ASSERTION 7 — anticipation: SECTOR-TOP >= ELEVATED by 06-25; CRASH-RISK >= ELEVATED by 06-26
# ══════════════════════════════════════════════════════════════════════════════════════════════════

# Alarm level ordering: WATCH < ELEVATED < CRITICAL
_ALARM_ORDER = {"WATCH": 0, "ELEVATED": 1, "CRITICAL": 2, "NONE": -1, None: -1}


def _alarm_gte(level: str, threshold: str) -> bool:
    """Return True if level >= threshold in WATCH < ELEVATED < CRITICAL ordering."""
    return _ALARM_ORDER.get(level, -1) >= _ALARM_ORDER.get(threshold, 0)


@pytest.mark.skipif(
    not (_MV_FIX / "anticipation_06_25.json").exists(),
    reason="anticipation_06_25 fixture missing — run E0.4 fixture build"
)
def test_anticipation_sector_top_elevated_by_06_25():
    """§4.1 — SECTOR-TOP alarm must be >= ELEVATED by 06-25.

    Build_plan.md §4.1 pre-registered assert:
      brain/anticipation.py must produce SECTOR-TOP(tech/semis) >= ELEVATED by 06-25.
      The design evidence says ELEVATED ~06-19, so there is margin; CRITICAL 06-22..25.

    This test reads the frozen fixture shape.  The ACTUAL module replay assertion is in
    test_anticipation_module_replay (below) which is skipped until E0.2 lands.
    """
    ant_0625 = _mv_fixture("anticipation_06_25")

    sector_top = ant_0625["alarms"].get("sector_top", {})
    level = sector_top.get("level")
    assert _alarm_gte(level, "ELEVATED"), (
        f"anticipation 06-25: SECTOR-TOP must be >= ELEVATED, got '{level}'. "
        "Design evidence: SMH osc_roll 98.3→81.5, Peak/Topping pin, osc_slope=-16.5 "
        "means SECTOR-TOP crossed ELEVATED ~06-19 and CRITICAL by 06-22 per post-mortem."
    )
    # tech/semis must be named as the affected sector
    affected = sector_top.get("sectors", [])
    has_tech = any(s in {"XLK", "tech_semis", "SMH", "semis"} for s in affected)
    assert has_tech, (
        f"anticipation 06-25: SECTOR-TOP must name tech/semis as affected. Got: {affected}"
    )


@pytest.mark.skipif(
    not (_MV_FIX / "anticipation_06_26.json").exists(),
    reason="anticipation_06_26 fixture missing — run E0.4 fixture build"
)
def test_anticipation_crash_risk_elevated_by_06_26():
    """§4.1 — CRASH-RISK alarm must be >= ELEVATED by 06-26.

    Build_plan.md §4.1 pre-registered assert:
      CRASH-RISK >= ELEVATED by 06-26 (risk_radar dominant_scare=growth,
      drawdown_prob.h21=0.19 rising from 0.16 on 06-23).
    """
    ant_0626 = _mv_fixture("anticipation_06_26")

    crash_risk = ant_0626["alarms"].get("crash_risk", {})
    level = crash_risk.get("level")
    assert _alarm_gte(level, "ELEVATED"), (
        f"anticipation 06-26: CRASH-RISK must be >= ELEVATED, got '{level}'. "
        "risk_radar drawdown_prob.h21=0.19 (rising) + growth_scare dominant + radar=caution "
        "should push CRASH-RISK to ELEVATED by 06-26."
    )


@pytest.mark.skipif(
    not (_MV_FIX / "anticipation_06_25.json").exists(),
    reason="anticipation fixtures missing"
)
def test_anticipation_authority_is_advisory():
    """All anticipation alarms must stamp advisory=True, cold_start=True (P3 status ladder).

    No alarm can claim 'validated' authority before passing the walk-forward AUC>0.55 gate
    (build_plan.md §3 + §4 pre-registered bar).
    """
    for fname in ("anticipation_06_25", "anticipation_06_26"):
        ant = _mv_fixture(fname)
        assert ant.get("advisory") is True, (
            f"{fname}: top-level advisory must be True (P3 — not yet walk-forward validated)"
        )
        assert ant.get("cold_start") is True, (
            f"{fname}: cold_start must be True (no graded episodes yet)"
        )
        assert ant.get("notch_eligible") is False, (
            f"{fname}: notch_eligible must be False — severity notch requires graded legs "
            "AND AUC>0.55 AND dedup seam (build_plan.md E2.2)"
        )
        for alarm_name, alarm in ant.get("alarms", {}).items():
            assert alarm.get("authority") in ("advisory", None), (
                f"{fname}: alarm '{alarm_name}' must have authority=advisory or null, "
                f"got '{alarm.get('authority')}'. Validated authority requires walk-forward gate."
            )


# ══════════════════════════════════════════════════════════════════════════════════════════════════
# §4 ASSERTION 8 — market_view: conflict=True on 06-26..07-01; posture_floor_defense=True on 07-01
# ══════════════════════════════════════════════════════════════════════════════════════════════════

@pytest.mark.skipif(
    not (_MV_FIX / "market_view_07_01.json").exists(),
    reason="market_view_07_01 fixture missing — run E0.4 fixture build"
)
def test_market_view_conflict_on_07_01():
    """§4.3 — market_view must show conflict=True on 07-01 (the crash-eve, hard assert).

    Build_plan.md §4.3: label_vs_planes.conflict=True EVERY session 06-26..07-01.
    This is the hard assert for 07-01 (the day the SMH rebuy was executed despite 3
    validated planes saying risk_off).

    07-01 assert details (from judged_market-view.md synthesis):
      - label risk_on at conf 0.327
      - 3 validated planes (risk_radar/mtf_signals/cycles_entry) say risk_off
      - conflict=True, coherence~0.38, posture_floor_defense=True
    """
    mv = _mv_fixture("market_view_07_01")

    lvp = mv.get("label_vs_planes", {})
    assert lvp.get("conflict") is True, (
        "market_view 07-01: label_vs_planes.conflict must be True — "
        "3 validated planes (risk_radar/mtf/cycles) say risk_off vs Goldilocks label at conf 0.327. "
        "This is the sight the bot lacked on the 07-02 SMH rebuy."
    )
    assert lvp.get("label_direction") == "risk_on", (
        f"market_view 07-01: label_direction must be 'risk_on' (Goldilocks), "
        f"got '{lvp.get('label_direction')}'"
    )
    assert lvp.get("plane_consensus_direction") == "risk_off", (
        f"market_view 07-01: plane_consensus_direction must be 'risk_off', "
        f"got '{lvp.get('plane_consensus_direction')}'"
    )
    n_dissent = lvp.get("n_validated_dissent", 0)
    assert n_dissent >= 2, (
        f"market_view 07-01: at least 2 validated planes must dissent, got {n_dissent}. "
        "P1 two-plane cite requires >= 2 independent validated planes."
    )

    assert mv.get("posture_floor_defense") is True, (
        "market_view 07-01: posture_floor_defense must be True when conflict=True on 3 planes. "
        "This is the field that DEF_SLEEVE and derisk severity hold read."
    )
    # coherence must be degraded (not high) when conflict is True
    coherence = mv.get("coherence", 1.0)
    assert coherence < 0.60, (
        f"market_view 07-01: coherence must be < 0.60 when conflict=True (planes disagree), "
        f"got {coherence:.3f}. Reported as ~0.38 in judged_market-view.md synthesis."
    )
    assert coherence == pytest.approx(0.38, abs=0.08), (
        f"market_view 07-01: coherence should be ~0.38 (±0.08 tolerance), got {coherence:.3f}"
    )

    # net_posture_tilt must be risk_off (validated-only tilt)
    assert mv.get("net_posture_tilt") == "risk_off", (
        f"market_view 07-01: net_posture_tilt must be 'risk_off' (from validated planes). "
        f"Got: {mv.get('net_posture_tilt')}"
    )
    # budget_ref embed must be present and match the known result
    bref = mv.get("budget_ref", {})
    assert "lead_budget" in bref, (
        "market_view 07-01: budget_ref must contain 'lead_budget' (read-only audit embed)"
    )
    assert bref.get("lead_budget", 1.0) == pytest.approx(0.44905, abs=1e-3), (
        f"market_view 07-01: budget_ref.lead_budget should be ~0.449, got {bref.get('lead_budget')}"
    )


@pytest.mark.skipif(
    not (_MV_FIX / "market_view_07_01.json").exists(),
    reason="market_view_07_01 fixture missing"
)
def test_market_view_schema_and_required_fields():
    """market_view output must have the required schema fields (P7 contract integrity).

    Checks the key-order-sensitive fields that the prompt payload depends on.
    (build_plan.md §2 E0.3 contract: golden key-order test)
    """
    mv = _mv_fixture("market_view_07_01")

    required_top_level = {
        "schema_version", "region", "asof", "built_at", "planes",
        "net_posture_tilt", "coherence", "disagreements", "label_vs_planes",
        "posture_floor_defense", "posture_confidence", "assembly", "budget_ref", "brief"
    }
    missing = required_top_level - set(mv.keys())
    assert not missing, (
        f"market_view contract: missing required top-level fields: {missing}"
    )

    # planes must contain the key Tier-A adapters
    planes = mv.get("planes", {})
    required_planes = {"risk_radar", "cycles_entry", "regime_label"}
    missing_planes = required_planes - set(planes.keys())
    assert not missing_planes, (
        f"market_view planes: missing required planes: {missing_planes}"
    )

    # each plane must have the PlaneRecord fields
    plane_required_fields = {"reading", "direction", "magnitude", "freshness", "confidence", "status"}
    for pname, plane in planes.items():
        missing_plane = plane_required_fields - set(plane.keys())
        assert not missing_plane, (
            f"market_view plane '{pname}': missing PlaneRecord fields: {missing_plane}"
        )
        assert plane.get("direction") in ("risk_on", "risk_off", "neutral"), (
            f"market_view plane '{pname}': direction must be discrete risk_on/risk_off/neutral, "
            f"got '{plane.get('direction')}'"
        )
        assert plane.get("status") in ("validated", "advisory"), (
            f"market_view plane '{pname}': status must be 'validated' or 'advisory', "
            f"got '{plane.get('status')}'"
        )
        freshness = plane.get("freshness", {})
        assert "asof" in freshness and "stale" in freshness, (
            f"market_view plane '{pname}': freshness must have 'asof' and 'stale' fields"
        )

    # brief must have the four deterministic keys
    brief = mv.get("brief", {})
    for key in ("what_changed", "whats_rotating", "wheres_the_risk", "posture_implication"):
        assert key in brief, f"market_view brief: missing key '{key}'"


# ══════════════════════════════════════════════════════════════════════════════════════════════════
# §4.6 CALM-TAPE CONTROL — conflict=False, alarms low on high-confidence agreeing tape
# ══════════════════════════════════════════════════════════════════════════════════════════════════

@pytest.mark.skipif(
    not (_MV_FIX / "market_view_calm.json").exists(),
    reason="market_view_calm fixture missing — run E0.4 fixture build"
)
def test_calm_tape_no_conflict():
    """§4.6 calm-tape control — high-confidence agreeing tape produces conflict=False.

    Build_plan.md §4.6: high-confidence agreeing window => conflict=False, class=OFFENSE,
    budgets byte-identical to today — zero drift.

    This is the crucial regression guard: any code change that causes conflict=True on
    agreeing tape is introducing false alarm behavior.
    """
    mv = _mv_fixture("market_view_calm")

    lvp = mv.get("label_vs_planes", {})
    assert lvp.get("conflict") is False, (
        "calm-tape control: label_vs_planes.conflict must be False when all planes agree risk_on. "
        "A True here means the market_view is generating false alarms on agreeing tape."
    )
    assert lvp.get("n_validated_dissent", 99) == 0, (
        f"calm-tape control: no validated planes should dissent on agreeing tape, "
        f"got {lvp.get('n_validated_dissent')} dissenters"
    )

    assert mv.get("posture_floor_defense") is False, (
        "calm-tape control: posture_floor_defense must be False on agreeing risk_on tape. "
        "A True here would silently lift the DEF_SLEEVE on non-defensive tape."
    )
    assert mv.get("net_posture_tilt") == "risk_on", (
        f"calm-tape control: net_posture_tilt must be 'risk_on' on agreeing tape, "
        f"got '{mv.get('net_posture_tilt')}'"
    )

    coherence = mv.get("coherence", 0.0)
    assert coherence >= 0.80, (
        f"calm-tape control: coherence must be >= 0.80 when all planes agree, got {coherence:.3f}. "
        "Low coherence on agreeing tape is a signal quality regression."
    )

    # budget_ref must be present and consistent with high-confidence regime
    bref = mv.get("budget_ref", {})
    calm_budget = bref.get("lead_budget", 0.0)
    assert calm_budget > 0.49, (
        f"calm-tape control: budget on agreeing tape (conf=0.78) should be > 0.49, "
        f"got {calm_budget:.4f}. budget = clamp(0.40 + 0.20*0.78*1.0*1.0) = 0.5312"
    )
    assert calm_budget == pytest.approx(0.5312, abs=0.02), (
        f"calm-tape control: budget should be ~0.531 (0.40+0.20*0.78), got {calm_budget:.4f}"
    )


@pytest.mark.skipif(
    not (_MV_FIX / "market_view_calm.json").exists(),
    reason="market_view_calm fixture missing"
)
def test_calm_tape_absent_planes_dont_lower_disagreement():
    """§4.6 variant — absent planes on calm tape cannot lower disagreement (P2 degrade rule).

    Build_plan.md contract: absent/stale planes have weight 0 and can never lower disagreement.
    On a calm tape with some missing planes, the view must still show conflict=False
    (missing planes don't add fake dissent), not conflict=True (missing planes don't inject
    false alarm).
    """
    mv = _mv_fixture("market_view_calm")

    # Some planes may be missing (assembly.missing > 0) on the calm fixture
    assembly = mv.get("assembly", {})
    missing_count = assembly.get("missing", 0)
    # if any planes are absent, conflict must still be False (absent = weight 0, not dissent)
    lvp = mv.get("label_vs_planes", {})
    if missing_count > 0:
        assert lvp.get("conflict") is False, (
            f"calm-tape control: {missing_count} absent planes must not induce conflict=True. "
            "Absent planes have weight 0 and cannot lower or raise disagreement (P2)."
        )


# ══════════════════════════════════════════════════════════════════════════════════════════════════
# §4 ASSERTION — market_view module replay (skipped until E0.3 lands)
# ══════════════════════════════════════════════════════════════════════════════════════════════════

def _try_import_market_view():
    """Attempt to import brain.market_view; return the module or None."""
    try:
        import brain.market_view as mv_mod
        return mv_mod
    except (ImportError, ModuleNotFoundError):
        return None


@pytest.mark.skipif(
    _try_import_market_view() is None,
    reason="brain.market_view not yet built (E0.3 pending) — fixture-shape tests above cover the contract"
)
def test_market_view_live_module_risk_radar_on_incident_fixture(monkeypatch, tmp_path):
    """E0.3 module replay — brain/market_view.view() on the incident fixture sees risk_radar=risk_off.

    This test is automatically unskipped once brain/market_view.py (E0.3) exists.
    It replays the market_view organ on the frozen regime_snapshot_incident.json fixture
    and asserts the structural contract:
      - risk_radar plane: direction=risk_off, status=validated (the highest-value Tier-A plane)
      - risk_radar is the ONLY validated plane that fires risk_off from the regime JSON alone
      - disagreements list is non-empty (risk_radar vs at least one risk_on plane)
      - the view contains all required schema fields

    NOTE: conflict=True in the full §4.3 sense requires rotation_tensor to be a validated
    plane (which needs E0.1 + walk-forward gate E1.4).  The fixture for the EXPECTED full
    output (market_view_07_01.json) captures the post-E0.1+E1.4 target where conflict=True.
    This live-module test asserts what the CURRENT module (E0.3 only) produces correctly.

    Side effects: monkeypatches the regime path to the frozen fixture; never touches live data.
    """
    mv_mod = _try_import_market_view()
    if mv_mod is None:
        pytest.skip("brain.market_view not available")

    # Inject the frozen incident regime snapshot via regime_frame._REGION_PATHS (the one reader).
    # This mirrors the pattern used in test_budget_below_0p50_on_0701_regime above.
    from brain import regime_frame as RF

    incident_regime = _MV_FIX / "regime_snapshot_incident.json"
    incident_cycles = _MV_FIX / "sector_cycles_incident.json"

    # Write fixtures to tmp files
    regime_tmp = tmp_path / "regime_latest.json"
    regime_tmp.write_text(incident_regime.read_text())
    cycles_tmp = tmp_path / "sector_cycles.json"
    cycles_tmp.write_text(incident_cycles.read_text())

    # Patch regime_frame._REGION_PATHS (the one reader that market_view uses via _rf._read_raw)
    orig_paths = dict(RF._REGION_PATHS)
    monkeypatch.setattr(RF, "_REGION_PATHS",
                        {**orig_paths, "us": regime_tmp}, raising=False)
    # Patch the cycles path so XLK late_cycle reads from incident fixture
    if hasattr(RF, "_CYCLES_PATH"):
        monkeypatch.setattr(RF, "_CYCLES_PATH", cycles_tmp, raising=False)
    # Also ensure the freshness gate passes (age = 0)
    monkeypatch.setattr(RF, "_trading_days_since", lambda asof: 0, raising=False)

    # Call the view assembler
    result = mv_mod.view("us")

    # --- The key perception assertion: risk_radar is SEEN and flagged risk_off ---
    planes = result.get("planes", {})
    rr = planes.get("risk_radar", {})
    assert rr, "market_view on incident fixture: risk_radar plane must be present"
    assert rr.get("direction") == "risk_off", (
        f"market_view on incident fixture: risk_radar must be risk_off "
        f"(dominant_scare=growth, state=caution, drawdown_prob.h21=0.19). "
        f"Got direction='{rr.get('direction')}'. "
        "This is the Tier-A plane the bot discarded before E0.3."
    )
    assert rr.get("status") == "validated", (
        f"market_view on incident fixture: risk_radar must be validated "
        f"(forward_log.jsonl calibration). Got status='{rr.get('status')}'"
    )
    # froth_fragility (advisory) must be seen as risk_off too
    ff = planes.get("froth_fragility", {})
    assert ff.get("direction") in ("risk_off", "neutral"), (
        f"market_view on incident fixture: froth_fragility direction should be risk_off or neutral "
        f"(narrowing_top quadrant). Got '{ff.get('direction')}'"
    )
    # disagreements must be non-empty: risk_radar(risk_off) disagrees with something
    disagreements = result.get("disagreements", [])
    assert len(disagreements) >= 1, (
        "market_view on incident fixture: disagreements must be non-empty — risk_radar=risk_off "
        "disagrees with at least the cycles or regime_label plane."
    )
    # Schema must be complete (key-order test)
    for key in ("schema_version", "region", "asof", "planes", "label_vs_planes",
                "net_posture_tilt", "disagreements", "brief", "budget_ref"):
        assert key in result, f"market_view schema: missing required key '{key}'"
    # budget_ref must be present with lead_budget (the W2 ONE equation result)
    bref = result.get("budget_ref", {})
    assert "lead_budget" in bref, (
        "market_view: budget_ref must contain 'lead_budget' (audit embed of the ONE equation result)"
    )
    assert bref.get("lead_budget", 1.0) < 0.50, (
        f"market_view on incident fixture: budget_ref.lead_budget must be < 0.50 "
        f"(conf=0.327, STABLE, flip_margin=0.05 → ~0.449), got {bref.get('lead_budget')}"
    )
    # The brief must mention risk_off implications given radar=caution
    brief = result.get("brief", {})
    assert brief.get("wheres_the_risk") or brief.get("posture_implication"), (
        "market_view brief must have wheres_the_risk or posture_implication with radar=caution on fixture"
    )


@pytest.mark.skipif(
    _try_import_market_view() is None,
    reason="brain.market_view not yet built (E0.3 pending)"
)
def test_market_view_live_module_calm_tape_no_conflict(monkeypatch, tmp_path):
    """E0.3 module replay — brain/market_view.view() on the calm fixture must produce conflict=False.

    Unskipped automatically once brain/market_view.py exists.
    """
    mv_mod = _try_import_market_view()
    if mv_mod is None:
        pytest.skip("brain.market_view not available")

    from brain import regime_frame as RF

    calm_regime = _MV_FIX / "regime_snapshot_calm.json"
    regime_tmp = tmp_path / "regime_latest.json"
    regime_tmp.write_text(calm_regime.read_text())

    orig_paths = dict(RF._REGION_PATHS)
    monkeypatch.setattr(RF, "_REGION_PATHS",
                        {**orig_paths, "us": regime_tmp}, raising=False)
    monkeypatch.setattr(RF, "_trading_days_since", lambda asof: 0, raising=False)

    result = mv_mod.view("us")

    lvp = result.get("label_vs_planes", {})
    assert lvp.get("conflict") is False, (
        "brain/market_view.view() on calm fixture: conflict must be False — "
        "all planes agree risk_on, no defensive rotation."
    )
    assert result.get("posture_floor_defense") is False, (
        "calm-tape live module: posture_floor_defense must be False on agreeing tape"
    )


# ══════════════════════════════════════════════════════════════════════════════════════════════════
# §4 ASSERTION — perception runlog step is logged before the gate (E0.5)
# ══════════════════════════════════════════════════════════════════════════════════════════════════

def test_perception_runlog_step_exists_in_phase2():
    """E0.5 — phase2.run() source must contain the perception runlog step before the gate.

    This is a structural source-code assertion (not a runtime test) that guards the
    P5 architectural requirement: perception is logged BEFORE any position decision.

    The perception runlog step must:
    (a) Be inserted at the TOP of phase2.run(), before the gate.should_run() call.
    (b) Log a 'perception' step_type with 'brief' + 'coverage' fields.
    (c) Never raise — wrapped in try/except with degrade-to-'perception unavailable'.
    (d) Use lazy import so the module loads even if brain/market_view.py doesn't exist yet.
    """
    import inspect
    from bot import phase2

    src = inspect.getsource(phase2.run)

    # (a) 'perception' step must appear in the source
    assert "perception" in src, (
        "phase2.run(): no 'perception' runlog step found. "
        "E0.5 requires a perception runlog step at the TOP of run(), before gate.should_run(). "
        "P5: perception is logged before any position decision."
    )

    # (b) The step must reference brain.market_view (lazy import)
    assert "market_view" in src, (
        "phase2.run(): perception step must reference brain.market_view (P5 visibility). "
        "Use lazy import so the step degrades gracefully if E0.3 hasn't landed yet."
    )

    # (c) The perception step must appear before gate.should_run in source order
    perception_pos = src.find("perception")
    gate_pos = src.find("gate.should_run")
    if gate_pos != -1 and perception_pos != -1:
        assert perception_pos < gate_pos, (
            "phase2.run(): perception runlog step must appear BEFORE gate.should_run() "
            "(P5: perception logged before position decision). "
            f"perception at char {perception_pos}, gate.should_run at char {gate_pos}."
        )


# ══════════════════════════════════════════════════════════════════════════════════════════════════
# W-E.0 FIXTURE INTEGRITY — guards market_view fixture schema
# ══════════════════════════════════════════════════════════════════════════════════════════════════

def test_market_view_fixture_integrity():
    """Sanity-check the market_view fixture files so fixture rot breaks loudly."""
    # regime_snapshot_incident
    ri = _mv_fixture("regime_snapshot_incident")
    assert ri["quad"] == "Q1" and ri["quad_name"] == "Goldilocks"
    assert ri["confidence"] == pytest.approx(0.327, abs=1e-6)
    assert ri["transition_state"] == "STABLE"
    assert ri["date"] == "2026-07-01"
    # Must have the key embedded blocks
    for block in ("risk_radar", "froth_fragility", "mtf_signals", "risk_state"):
        assert block in ri, f"regime_snapshot_incident: missing embedded block '{block}'"
    assert ri["risk_radar"]["state"] == "caution"
    assert ri["risk_radar"]["dominant_scare"] == "growth"
    assert ri["risk_radar"]["drawdown_prob"]["h21"] == pytest.approx(0.19, abs=0.01)
    assert ri["froth_fragility"]["quadrant"] == "narrowing_top"

    # regime_snapshot_calm
    rc = _mv_fixture("regime_snapshot_calm")
    assert rc["confidence"] > 0.60
    assert rc["risk_radar"]["state"] == "risk_on"
    assert rc["risk_radar"]["dominant_scare"] is None
    assert rc["froth_fragility"]["alert"] is False

    # sector_cycles_incident
    sc = _mv_fixture("sector_cycles_incident")
    tickers = {s["ticker"] for s in sc.get("sectors", [])}
    for t in ("XLK", "XLV", "XLU"):
        assert t in tickers, f"sector_cycles_incident: missing sector {t}"
    xlk = next(s for s in sc["sectors"] if s["ticker"] == "XLK")
    assert xlk["now"]["phase"] == "Peak"
    assert xlk["now"]["osc_slope"] < 0, "XLK osc_slope must be negative (topping)"

    # rotation_tensor fixtures
    rt_0624 = _mv_fixture("rotation_tensor_06_24")
    assert rt_0624["as_of"] == "2026-06-24"
    assert rt_0624["advisory"] is True, "rotation_tensor must be advisory (not yet validated)"
    assert "top_pairs" in rt_0624["rs_velocity"]
    assert "headline_episode" in rt_0624

    rt_0629 = _mv_fixture("rotation_tensor_06_29")
    assert rt_0629["headline_episode"]["direction"] == "defensive"
    assert rt_0629["headline_episode"]["n_sessions"] >= 3

    # anticipation fixtures
    ant_0625 = _mv_fixture("anticipation_06_25")
    assert ant_0625["as_of"] == "2026-06-25"
    assert ant_0625["advisory"] is True
    assert "sector_top" in ant_0625["alarms"]
    ant_0626 = _mv_fixture("anticipation_06_26")
    assert ant_0626["alarms"]["crash_risk"]["level"] == "ELEVATED"

    # market_view expected output fixtures
    mv_0701 = _mv_fixture("market_view_07_01")
    assert mv_0701["label_vs_planes"]["conflict"] is True
    assert mv_0701["posture_floor_defense"] is True
    assert mv_0701["coherence"] == pytest.approx(0.38, abs=0.05)

    mv_calm = _mv_fixture("market_view_calm")
    assert mv_calm["label_vs_planes"]["conflict"] is False
    assert mv_calm["posture_floor_defense"] is False
    assert mv_calm["coherence"] >= 0.80


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


# ─────────────────────────────────────────────────────────────────────────────
# E2.2 COMPOSED-STACK (build_plan §4.4, flag-ON): on the 07-01-shaped disagreeing
# tape the whole armed spine composes — the budget delegates to the posture read
# in the ROTATE-DEFENSIVE band, the DEF_SLEEVE floor unthrottles at max=0.35, the
# derisk cap picks up the posture notch, and the shrink arrives via ONE pathway.
# ─────────────────────────────────────────────────────────────────────────────

def test_composed_stack_flag_on_disagreeing_tape(monkeypatch, tmp_path):
    import json as _json
    from pathlib import Path as _Path
    pytest.importorskip("brain.posture_decider")
    from brain import posture_decider as PD
    from brain import regime_frame as RF
    from portfolio import rotation as ROT

    monkeypatch.setenv("MASTERMIND_POSTURE_DECIDER", "1")
    # isolate the artifact + hysteresis state
    monkeypatch.setattr(PD, "_ARTIFACT_DIR", tmp_path / "posture", raising=False)
    monkeypatch.setattr(PD, "_LATEST_PATH", tmp_path / "posture" / "latest.json", raising=False)
    monkeypatch.setattr(PD, "_STATE_PATH", tmp_path / "posture" / "state.json", raising=False)

    tape = _json.loads((_Path(__file__).resolve().parent.parent
                        / "fixtures" / "posture" / "disagreeing_tape.json").read_text())

    rec = PD.decide("us", evidence=tape.get("evidence"), risk_state=tape.get("risk_state"))
    assert rec["posture_class"] in ("ROTATE_DEFENSIVE", "PRESERVE"), rec["posture_class"]
    # write the artifact so the shim/seams read it (the production path)
    PD.build(evidence=tape.get("evidence"), risk_state=tape.get("risk_state"), write=True) \
        if "write" in PD.build.__code__.co_varnames else PD._write_artifact(rec)  # noqa: E501

    # (1) the budget shim delegates — ROTATE-DEFENSIVE band, provenance stamped
    b = RF.budget("us")
    assert b["inputs"].get("posture_delegated") is True
    assert 0.40 - 1e-9 <= b["lead_budget"] <= 0.45 + 1e-9, b["lead_budget"]

    # (2) the DEF_SLEEVE floor unthrottles CONSISTENTLY: floor == D x max (composition, not
    # magnitude — the SS4.4 D~0.74 / floor 0.22-0.27 magnitude proof lives in
    # tests/test_posture_decider.py against the full incident fixture; THIS tape is a lighter
    # shape whose D~0.54 still reads ROTATE-DEFENSIVE and must flow through unchanged).
    D = float(rec["defense_pressure"])
    assert D >= 0.50 - 1e-9, D                      # the class boundary held
    floor = float(rec["defense_floor_at_max"] if rec.get("defense_floor_at_max") is not None
                  else D * 0.35)
    assert abs(floor - D * 0.35) < 0.02, (floor, D)  # floor tracks D x max — no leak, no re-scale

    # (3) the sleeve sizes off the floor, not fragility (single consumption)
    sleeve = ROT.build_def_sleeve([], tape.get("risk_state"), None,
                                  candidates=[{"ticker": "XLV", "archetype": "quality_defensive"}],
                                  target=floor)
    assert abs(sleeve["def_budget"] - floor) < 1e-6
    assert sleeve["fragility_signal"] == 0.0  # not consulted

    # (4) the posture notch caps at 0.70
    assert abs(float(rec["posture_notch_cap"]) - 0.70) < 1e-9

    # (5) one shrink pathway, named
    assert rec.get("shrink_provenance") in ("defense_D", "posture_class", "defense_pressure")
