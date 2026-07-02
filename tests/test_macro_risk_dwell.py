"""Guards for the FRAGILITY DWELL STATE MACHINE (brain/macro_risk — W1, kills problem #9).

The stateless scorer flipped CAUTION -> RISK_ON on 2026-07-01 — the very day SOXX fell -6.4% — because
a crash COLLAPSES the crowded/complacent read that drove fragility, so the raw read fell to 0.121 after
two straight CAUTION sessions (0.516, 0.469). The old code un-capped the book 0.7 -> 1.0 into a live
unwind and a severity-2 tripwire fired into an empty cap.

The acceptance falsifier (``test_replay_soxx_crash_day_stays_caution``) replays the REAL recorded raw
fragility sequence + derisk tripwire artifacts for 06-29/06-30/07-01 (copied into
tests/fixtures/macro_risk_replay/) and asserts 07-01 holds CAUTION (cap 0.7). BOTH independent blocks
are then shown to fire alone: the 3-session de-escalation rule (test_...blocked_by_streak) and the
severity-2 tripwire clamp (test_...blocked_by_tripwire). We also prove escalation is instant, a clean
3-session decay DOES de-escalate, and a corrupt/missing state file degrades to the stateless read.

The dwell machine is a PURE transition (``_advance_dwell``) wrapped by ``risk_state``; we drive it with
injected state loader/saver + tripwire severity (no live state file touched) and, for the full replay,
monkeypatch the five axis scorers to emit the recorded per-day axis fragilities — so ``risk_state``
recomputes byte-identical raw fragility and the ONLY new behaviour under test is the dwell wrapper."""
from __future__ import annotations

import json
from pathlib import Path

import bot  # noqa: F401  -> vendor/macro onto sys.path
from brain import macro_risk as MR

_FIX = Path(__file__).resolve().parent / "fixtures" / "macro_risk_replay"
_DAYS = ("2026-06-29", "2026-06-30", "2026-07-01")

# the dwell thresholds under test (mirror config/doctrine.yml risk_state:; passed explicitly so the test
# is hermetic and does not depend on the live doctrine.yml being loadable).
_CFG = dict(MR._DWELL_DEFAULTS)


# ── fixture readers ──────────────────────────────────────────────────────────────────────────────
def _recorded(day: str) -> dict:
    """The recorded raw state.json for a replay day (state + fragility + axes)."""
    return json.loads((_FIX / day / "state.json").read_text())["state"]


def _recorded_tripwire_sev(day: str) -> int:
    """The MAX derisk tripwire severity recorded across all books that day (what the machine reads to
    decide the clamp). Mirrors ``_recent_tripwire_severity`` but points at the FIXTURE dir."""
    worst = 0
    for f in (_FIX / day).glob("derisk_*.json"):
        sev = ((json.loads(f.read_text()) or {}).get("tripwire") or {}).get("severity") or 0
        try:
            worst = max(worst, int(sev))
        except (TypeError, ValueError):
            pass
    return worst


def _fake_collect_for(day: str):
    """A ``_collect`` stand-in whose axis reads, once scored, reproduce the recorded fragility for
    ``day`` EXACTLY. We don't reconstruct raw signals — we monkeypatch the axis scorers instead (below);
    this returns a benign dict so the non-axis reads (regime growth/infl, drivers) don't blow up."""
    return {"regime": {}, "sector_rs": [], "crowded_baskets": [], "transition_flags": {}}


def _patch_axes(monkeypatch, day: str) -> None:
    """Force the five axis scorers to emit the recorded per-day axis fragilities, so risk_state's
    weighted sum == the recorded raw fragility. Faithful replay with no vendor data touched."""
    ax = _recorded(day)["axes"]
    monkeypatch.setattr(MR, "_collect", lambda regime: _fake_collect_for(day))
    monkeypatch.setattr(MR, "_axis_volatility", lambda s: (ax["volatility"]["fragility"], "replay"))
    monkeypatch.setattr(MR, "_axis_credit_usd", lambda s: (ax["credit_usd"]["fragility"], "replay"))
    monkeypatch.setattr(MR, "_axis_liquidity", lambda s: (ax["liquidity"]["fragility"], "replay"))
    monkeypatch.setattr(MR, "_axis_crowding",
                        lambda s: (ax["crowding"]["fragility"], [], "replay"))
    monkeypatch.setattr(MR, "_axis_dealer_gamma", lambda s: (ax["dealer_gamma"]["fragility"], "replay"))


class _MemStore:
    """An in-memory stand-in for the persistent state_machine.json (the injected loader/saver)."""

    def __init__(self, seed=None):
        self.rec = seed

    def load(self):
        return self.rec

    def save(self, j):
        self.rec = j


# ══ THE ACCEPTANCE FALSIFIER ═══════════════════════════════════════════════════════════════════
def test_replay_soxx_crash_day_stays_caution(monkeypatch):
    """Replay the real 06-29 -> 06-30 -> 07-01 raw fragility + tripwire sequence through risk_state and
    assert 07-01 stays CAUTION (cap 0.7) instead of the historical stateless flip to RISK_ON (cap 1.0)."""
    store = _MemStore(seed=None)  # cold start (degrades to raw on day 1)
    seq = {}
    for day in _DAYS:
        _patch_axes(monkeypatch, day)
        st = MR.risk_state(day, {}, dwell=True,
                           state_loader=store.load, state_saver=store.save,
                           tripwire_sev=_recorded_tripwire_sev(day))
        seq[day] = st

    # sanity: the raw reads reproduce the recorded stateless sequence (CAUTION, CAUTION, RISK_ON)
    assert seq["2026-06-29"]["raw_state"] == "caution"
    assert seq["2026-06-30"]["raw_state"] == "caution"
    assert seq["2026-07-01"]["raw_state"] == "risk_on"     # the stateless flip that caused the bug
    assert abs(seq["2026-07-01"]["fragility"] - 0.121) < 1e-6

    # THE FIX: the DWELL state holds CAUTION on the crash day, so the cap stays CAUTION-grade (a real
    # cash floor) instead of the stateless un-cap to 1.0 — that un-cap into a live -6.4% unwind is
    # exactly the bug. (The precise 0.7 the desk recorded came from that day's defensive-tilt cash_floor
    # of 0.30; the tilt reads live vendor data patched away here, so we assert the invariant the dwell
    # machine actually governs: a bounded caution cap, materially below 1.0.)
    crash = seq["2026-07-01"]
    assert crash["state"] == "caution", "07-01 must NOT flip to risk_on — the whole point of the machine"
    assert crash["gross_cap"] < 1.0, "the crash-day book must NOT be un-capped to 1.0"
    assert crash["gross_cap"] <= MR.gross_cap("caution") + 1e-9, "cap must be caution-grade, not looser"
    assert crash["allow_adds"] is True                     # caution allows adds (risk_off would block)
    # the clamp names WHY it held (the severity-2 tripwire that fired that day)
    assert crash["clamp_reason"] and "tripwire" in crash["clamp_reason"]


def test_replay_both_blocks_fire_independently(monkeypatch):
    """The masterplan requires the crash-day hold to be OVER-DETERMINED: BOTH the 3-session rule and the
    tripwire clamp must independently block de-escalation. Replay to 07-01, then flip each block off in
    turn and confirm the OTHER still holds CAUTION."""
    # roll the machine forward to the end of 06-30 (two CAUTION sessions, below_streak=0)
    store = _MemStore(seed=None)
    for day in ("2026-06-29", "2026-06-30"):
        _patch_axes(monkeypatch, day)
        MR.risk_state(day, {}, dwell=True, state_loader=store.load, state_saver=store.save,
                      tripwire_sev=_recorded_tripwire_sev(day))
    prior_before_crash = dict(store.rec)

    # (A) tripwire clamp OFF (sev=0), streak rule still in force → below_streak was 0, so 1 sub-deadband
    #     session is far short of the required 3 → STILL CAUTION.
    _patch_axes(monkeypatch, "2026-07-01")
    store.rec = dict(prior_before_crash)
    st_a = MR.risk_state("2026-07-01", {}, dwell=True, state_loader=store.load,
                         state_saver=store.save, tripwire_sev=0)
    assert st_a["state"] == "caution", "3-session rule alone must hold CAUTION on the crash day"
    assert st_a["clamp_reason"] is None                    # no clamp — it's the streak that held it
    assert st_a["deescalation_progress"] == "1/3"          # only 1 of 3 sub-deadband sessions accrued

    # (B) streak rule satisfied (pretend 3 prior sub-deadband sessions + cooldown elapsed), tripwire
    #     clamp still in force (recorded sev-2) → STILL CAUTION.
    _patch_axes(monkeypatch, "2026-07-01")
    store.rec = {**prior_before_crash, "below_streak": 3, "sessions_since_escalation": 5}
    st_b = MR.risk_state("2026-07-01", {}, dwell=True, state_loader=store.load,
                         state_saver=store.save, tripwire_sev=_recorded_tripwire_sev("2026-07-01"))
    assert st_b["state"] == "caution", "the sev-2 tripwire clamp alone must hold CAUTION"
    assert st_b["clamp_reason"] and "tripwire" in st_b["clamp_reason"]


# ══ THE FOUR REQUIRED PROPERTY TESTS ═════════════════════════════════════════════════════════════
def test_escalation_is_instant():
    """A single run whose raw read outranks the held state jumps up SAME-run (no dwell required)."""
    # held risk_on, raw read risk_off → instant risk_off
    rec = MR._advance_dwell({"v": 1, "state": "risk_on", "asof": "2026-06-20", "dwell": 9,
                             "below_streak": 0, "sessions_since_escalation": 999,
                             "last_escalation": None, "clamp_reason": None},
                            "risk_off", 0.72, "2026-06-21", tripwire_sev=0, cfg=_CFG)
    assert rec["state"] == "risk_off"
    assert rec["sessions_since_escalation"] == 0           # cooldown re-armed
    assert rec["last_escalation"] == "2026-06-21"
    # and one-step escalation caution->risk_off is equally instant
    rec2 = MR._advance_dwell({"v": 1, "state": "caution", "asof": "2026-06-20", "dwell": 3,
                              "below_streak": 2, "sessions_since_escalation": 4,
                              "last_escalation": "2026-06-16", "clamp_reason": None},
                             "risk_off", 0.65, "2026-06-21", tripwire_sev=0, cfg=_CFG)
    assert rec2["state"] == "risk_off" and rec2["below_streak"] == 0


def test_clean_three_session_decay_deescalates():
    """With NO hot tripwire and the cooldown elapsed, 3 consecutive sub-deadband sessions step the state
    down exactly one rank (risk_off -> caution -> risk_on over enough clean sessions)."""
    # seed: in CAUTION, cooldown already elapsed, no clamp. Feed 3 clean sub-0.28 sessions.
    rec = {"v": 1, "state": "caution", "asof": "2026-06-20", "dwell": 4,
           "below_streak": 0, "sessions_since_escalation": 9, "last_escalation": None,
           "clamp_reason": None}
    days = ["2026-06-21", "2026-06-22", "2026-06-23"]
    for i, day in enumerate(days):
        rec = MR._advance_dwell(rec, "risk_on", 0.10, day, tripwire_sev=0, cfg=_CFG)
        if i < 2:
            assert rec["state"] == "caution", f"must not step down before the 3rd clean session ({day})"
    assert rec["state"] == "risk_on", "3 clean sub-deadband sessions + cooldown must de-escalate"


def test_deescalation_needs_cooldown_since_escalation():
    """Even with a full 3-session sub-deadband streak, de-escalation waits out the post-escalation
    cooldown (a state can't escalate then immediately decay on the next quiet reads)."""
    # just escalated to caution (since_esc=0); feed clean sessions — streak builds but cooldown blocks.
    rec = {"v": 1, "state": "caution", "asof": "2026-06-20", "dwell": 1,
           "below_streak": 0, "sessions_since_escalation": 0, "last_escalation": "2026-06-20",
           "clamp_reason": None}
    rec = MR._advance_dwell(rec, "risk_on", 0.10, "2026-06-21", tripwire_sev=0, cfg=_CFG)  # since_esc=1
    rec = MR._advance_dwell(rec, "risk_on", 0.10, "2026-06-22", tripwire_sev=0, cfg=_CFG)  # since_esc=2
    # streak now 2 (<3) so still caution; keep going until streak hits 3 with cooldown satisfied
    rec = MR._advance_dwell(rec, "risk_on", 0.10, "2026-06-23", tripwire_sev=0, cfg=_CFG)  # streak=3,cd=3
    assert rec["state"] == "risk_on"


def test_max_dwell_auto_release():
    """A state stuck longer than max_dwell_sessions, currently below its floor, releases one step even
    if the strict streak isn't met — the anti-stuck-forever valve (still tripwire-clampable)."""
    rec = {"v": 1, "state": "caution", "asof": "2026-06-20", "dwell": 16,  # > 15 max_dwell
           "below_streak": 1, "sessions_since_escalation": 30, "last_escalation": None,
           "clamp_reason": None}
    rec = MR._advance_dwell(rec, "risk_on", 0.10, "2026-06-21", tripwire_sev=0, cfg=_CFG)
    assert rec["state"] == "risk_on", "over-max-dwell + below-floor must auto-release one step"
    # but a hot tripwire STILL clamps even the max-dwell release
    rec2 = {"v": 1, "state": "caution", "asof": "2026-06-20", "dwell": 16,
            "below_streak": 1, "sessions_since_escalation": 30, "last_escalation": None,
            "clamp_reason": None}
    rec2 = MR._advance_dwell(rec2, "risk_on", 0.10, "2026-06-21", tripwire_sev=2, cfg=_CFG)
    assert rec2["state"] == "caution" and rec2["clamp_reason"]


# ── degrade-to-stateless (the invariant) ──────────────────────────────────────────────────────────
def test_corrupt_state_file_degrades_to_stateless(monkeypatch, tmp_path):
    """A corrupt/unreadable state file must NOT raise and must fall back to the stateless raw read —
    the state machine may never make the read looser than today's behaviour on bad data."""
    bad = tmp_path / "state_machine.json"
    bad.write_text("{ this is not json ::::")
    monkeypatch.setattr(MR, "_STATE_MACHINE", bad, raising=False)
    # a caution-grade raw read: with a corrupt file the loader returns None -> cold-start seeds at raw,
    # so state == raw_state (stateless). No exception, and the cap matches the raw state's cap.
    _patch_axes(monkeypatch, "2026-06-29")
    st = MR.risk_state("2026-06-29", {}, dwell=True, tripwire_sev=0)   # real loader/saver, corrupt file
    assert st["raw_state"] == "caution"
    assert st["state"] == "caution"                        # degrades to the stateless read, not looser
    assert st["gross_cap"] == 0.7


def test_missing_state_file_cold_start_is_stateless(monkeypatch, tmp_path):
    """First-ever run (no state file) seeds the machine at today's raw read — behaviour unchanged."""
    monkeypatch.setattr(MR, "_STATE_MACHINE", tmp_path / "nope.json", raising=False)
    _patch_axes(monkeypatch, "2026-07-01")
    st = MR.risk_state("2026-07-01", {}, dwell=True, tripwire_sev=0)
    assert st["state"] == st["raw_state"] == "risk_on"     # cold start == stateless
    assert st["gross_cap"] == 1.0


def test_dwell_flag_off_reverts_to_stateless(monkeypatch):
    """MASTERMIND_DWELL=0 defeats the machine byte-for-byte (the invariant's required escape hatch): the
    crash-day raw RISK_ON read is honoured, exactly as pre-W1."""
    monkeypatch.setenv("MASTERMIND_DWELL", "0")
    _patch_axes(monkeypatch, "2026-07-01")
    st = MR.risk_state("2026-07-01", {})                   # no injection: uses the env flag
    assert st["state"] == "risk_on" and st["gross_cap"] == 1.0


def test_advance_dwell_never_raises_on_garbage():
    """The pure transition tolerates a garbage prior record (defensive; degrade-to-stateless)."""
    rec = MR._advance_dwell({"state": "bogus", "dwell": "x"}, "caution", 0.4, "2026-06-29",
                            tripwire_sev=0, cfg=_CFG)
    assert rec["state"] in MR._STATES
