"""brain/regime_frame.py — the single regime reader (architecture Stage 1, W1).

WHY THIS MODULE EXISTS
----------------------
Before W1 every bot that needed the macro regime had its own private ``_regime_dict()``
copy pasted from the same three-liner.  Five copies × two fields each = ten places where
the contract between the macro-dashboard JSON and the Brain prompts could silently drift.
The architecture mandates ONE reader; this is it.

INVARIANT (governs all paths here)
-----------------------------------
Missing / stale / corrupt data may coarsen identity, freeze the book, or shrink size —
it may NEVER un-cap, raise authority, or flip direction.  Concretely: every missing field
returns None (not a permissive default), every I/O error returns an empty dict, and
lens_row() degrades to {quad: None, quad_name: None, liquidity_overlay: None} rather than
inventing a regime.

PUBLIC API
----------
* ``frame(region='us')``   -> full enriched dict (quad … flag_confidence_decay)
* ``lens_row(region='us')``-> {quad, quad_name, liquidity_overlay} — byte-identical to
                              today's five _regime_dict() copies; the golden-output test
                              asserts this.
* ``freshness(region='us')``-> int | None — calendar days between the regime file's
                              ``date`` field and today; None if unknown.
* ``cycles()``             -> {ticker: {phase, phaseLabel, pos, osc_slope, late_cycle,
                              entry_favored}} — the SOLE reader of sector_cycles.json, gated
                              stale (>5 trading days by meta.asOf) -> {} (W2).
* ``budget(region='us', evidence=None)`` -> {lead_budget, inputs} — the ONE leadership budget
                              equation; missing frame fields -> 0.50 midpoint (W2).  The optional
                              ``evidence`` dict adds the shrink-only rotation-evidence damp (W-I t6).
* ``rotation_evidence(...)`` -> {n_agree, sources} — the shrink-only disagreement-agreement count
                              consumed by budget()'s damp AND rotation.fragility_signal (W-I t6).

REGION ROUTING
--------------
``region='us'``    -> vendor/macro/data/regime/latest.json
``region='china'`` -> vendor/macro/data/china_regime/latest.json
``region='hk'``    -> vendor/macro/data/china_regime/latest.json  (HK uses China's frame)

No other regions are supported yet; unknown regions degrade to empty dict (safe).

ADDING NEW CONSUMERS (W2+)
--------------------------
Read frame() for the enriched fields.  Never add a new consumer of the raw JSON path —
import this module instead.  Do NOT add new fields to lens_row() without bumping a golden
test — that dict feeds LLM prompts and key-order drift would silently change outputs.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

# The module lives at brain/regime_frame.py; the repo root is two levels up.
_ROOT: Path = Path(__file__).resolve().parent.parent

# Map region tokens to the relative JSON paths under vendor/macro/data/.
_REGION_PATHS: dict[str, Path] = {
    "us":     _ROOT / "vendor" / "macro" / "data" / "regime"       / "latest.json",
    "china":  _ROOT / "vendor" / "macro" / "data" / "china_regime" / "latest.json",
    "hk":     _ROOT / "vendor" / "macro" / "data" / "china_regime" / "latest.json",
}

# The ONLY path to the sector-cycle read.  cycles() is the sole consumer of this file
# firm-wide (architecture Stage 1); leadership brakes read cycles()'s output, never the JSON.
_CYCLES_PATH: Path = _ROOT / "vendor" / "macro" / "site" / "sectordata" / "sector_cycles.json"

# doctrine.yml lives beside the other config; loaded lazily by _doctrine() below.
_DOCTRINE_PATH: Path = _ROOT / "config" / "doctrine.yml"

# ---- cycle-phase semantics (from the sector_cycles.json contract) ----
# late_cycle (an ENTRY-size brake on NEW leadership legs, never an exit) requires ALL of:
#   phase ∈ {Peak, Downturn}  AND  osc_slope < 0  AND  pos ≥ 70.
# Any missing component → False (never guess a brake into existence).
_LATE_CYCLE_PHASES: frozenset[str] = frozenset({"Peak", "Downturn"})
_LATE_CYCLE_POS_MIN: float = 70.0
# entry_favored: the walk-forward-defensible entry tilt for NEW additions only.
_ENTRY_FAVORED_PHASES: frozenset[str] = frozenset({"Trough", "Recovery", "Expansion"})

# Budget-equation fallback constants — MUST mirror config/doctrine.yml's budget: block.
# Used only when PyYAML is absent or the block is malformed; the yml is the source of truth.
_BUDGET_FALLBACK: dict[str, Any] = {
    "base": 0.40, "slope": 0.20, "floor": 0.40, "ceil": 0.60,
    "midpoint": 0.50, "flip_margin_min": 0.15,
    "transition_mult": {"WEAKENING": 0.6, "ROLLING": 0.4, "DETERIORATING": 0.4},
}
# Fallback staleness horizon for cycles(), mirroring config/doctrine.yml's cycles: block.
_CYCLES_STALE_TD_FALLBACK: int = 5

# Rotation-evidence fallback constants — MUST mirror config/doctrine.yml's rotation_evidence: block.
# Used only when PyYAML is absent or the block is malformed; the yml is the source of truth.
# See budget()'s evidence damp + rotation.fragility_signal's unthrottle (W-I task 6).
_ROTATION_EVIDENCE_FALLBACK: dict[str, Any] = {
    "damp_2_agree": 0.90,
    "damp_3plus_agree": 0.80,
    "lift_per_source": 0.15,
    "liquidity_stress_labels": ("stress-expansion", "neutral-hollow"),
    "radar_caution_states": ("caution", "risk_off", "risk-off"),
}
# The path to the VALIDATED risk_radar forward-log — the fallback radar-caution read when
# brain/risk_prior (which lands via the in-flight merge) is not importable.  Read-only; a missing
# file degrades the radar source to None (non-agreeing) — never fabricates a caution.
_RADAR_FORWARD_LOG: Path = (
    _ROOT / "vendor" / "macro" / "data" / "risk_radar" / "forward_log.jsonl"
)

# Fields carried through to frame() unchanged from the raw JSON.
# Any field NOT in the raw dict becomes None — never a KeyError.
_FRAME_FIELDS = (
    "quad",
    "quad_name",
    "liquidity_overlay",
    "confidence",
    "transition_state",
    "contradicting",
    "flip_condition",
    "flip_margin",           # synthesised below if absent
    "flag_confidence_decay", # synthesised below if absent
    "date",
)


# ---------------------------------------------------------------------------
# internal helpers
# ---------------------------------------------------------------------------

def _read_raw(region: str) -> dict[str, Any]:
    """Read and parse the regime JSON for *region*.

    Returns an empty dict on any I/O or parse error — never raises.  The empty
    dict triggers None defaults in frame() which is the correct degraded state.
    """
    path = _REGION_PATHS.get(region)
    if path is None:
        # Unknown region: degrade silently.  Caller gets an empty frame.
        return {}
    try:
        if path.exists():
            return json.loads(path.read_text())
    except Exception:
        pass
    return {}


def _doctrine() -> dict[str, Any]:
    """Read config/doctrine.yml, or {} on any error (PyYAML absent / parse fail).

    Lazy and defensive — the budget/cycles helpers fall back to the hard-coded
    priors above when this returns {}, so a missing config never breaks a build.
    """
    try:
        import yaml  # lazy — not on the hot path; gracefully absent in bare CI
        if _DOCTRINE_PATH.exists():
            return yaml.safe_load(_DOCTRINE_PATH.read_text()) or {}
    except Exception:  # noqa: BLE001
        pass
    return {}


def _trading_days_since(as_of: str | None) -> int | None:
    """Count NYSE trading sessions strictly between *as_of* (YYYY-MM-DD) and today.

    Reuses the freshness()-style arithmetic but on a trading-day basis (the cycle
    freshness gate is specified in trading days, not calendar days).  Returns None
    if *as_of* is absent/unparseable — callers MUST treat None as "unknown age",
    which the cycles() gate handles by degrading to {} (fail-closed, never fail-open).

    A same-day or future asOf returns 0.  Weekends and NYSE holidays are excluded
    via portfolio.market_calendar.is_trading_day so a Friday read is not "stale" by
    Monday.  Falls back to a calendar-weekday count if the calendar import fails.
    """
    if not as_of:
        return None
    try:
        d0 = date.fromisoformat(str(as_of))
    except (ValueError, TypeError):
        return None
    today = datetime.now(tz=timezone.utc).date()
    if d0 >= today:
        return 0
    try:
        from portfolio import market_calendar as _mc
        _is_td = _mc.is_trading_day
    except Exception:  # noqa: BLE001 — degrade to a weekday count (Mon–Fri)
        _is_td = lambda d: d.weekday() < 5  # noqa: E731
    n = 0
    cur = d0 + timedelta(days=1)
    while cur <= today:
        if _is_td(cur):
            n += 1
        cur += timedelta(days=1)
    return n


def _flip_margin(raw: dict[str, Any]) -> float | None:
    """Extract flip_margin from the nested flip_condition block, or return None.

    The JSON schema stores flip info as:
        "flip_condition": {"margin": 0.05, ...}
    But we also accept a top-level "flip_margin" key for forward-compat.
    """
    # Top-level key takes priority if present.
    if "flip_margin" in raw:
        val = raw["flip_margin"]
        try:
            return float(val)
        except (TypeError, ValueError):
            return None
    # Fall through to the nested structure.
    fc = raw.get("flip_condition")
    if isinstance(fc, dict):
        val = fc.get("margin")
        try:
            return float(val)
        except (TypeError, ValueError):
            return None
    return None


def _flag_confidence_decay(raw: dict[str, Any]) -> bool | None:
    """Extract the confidence-decay flag from transition_flags or a top-level key.

    Returns None if absent so callers can distinguish "no data" from False.
    """
    # Top-level key (future schema or test override).
    if "flag_confidence_decay" in raw:
        return bool(raw["flag_confidence_decay"])
    # Nested in transition_flags (current production schema).
    tf = raw.get("transition_flags")
    if isinstance(tf, dict) and "flag_confidence_decay" in tf:
        return bool(tf["flag_confidence_decay"])
    return None


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------

def frame(region: str = "us") -> dict[str, Any]:
    """Return the full regime frame for *region*.

    Every key is present in the returned dict; missing raw fields map to None.
    This is the authoritative input for W2+ budget equations and LLM prompts
    that need more than the three lens_row fields.

    Returned keys
    -------------
    quad              str | None   — e.g. "Q1"
    quad_name         str | None   — e.g. "Goldilocks"
    liquidity_overlay str | None   — e.g. "expanding"
    confidence        float | None — aggregate regime confidence 0‥1
    transition_state  str | None   — "STABLE" / "WEAKENING" / "ROLLING" / …
    contradicting     list | None  — list of contradicting-leg strings
    flip_condition    dict | None  — raw nested flip_condition object
    flip_margin       float | None — margin to regime flip (extracted from flip_condition)
    flag_confidence_decay bool | None — True when the decay flag is set
    date              str | None   — YYYY-MM-DD the regime file was published
    """
    raw = _read_raw(region)
    result: dict[str, Any] = {}
    for key in _FRAME_FIELDS:
        result[key] = raw.get(key)  # None on missing — invariant: never raise
    # Synthesise the two derived fields (override the raw.get() above with real logic).
    result["flip_margin"] = _flip_margin(raw)
    result["flag_confidence_decay"] = _flag_confidence_decay(raw)
    return result


def lens_row(region: str = "us") -> dict[str, Any]:
    """Return the 3-field regime dict consumed by LLM prompts and brief builders.

    THE CONTRACT: this dict must be byte-identical (same keys, same values, same
    insertion order) to the output that today's five ``_regime_dict()`` copies
    return.  The golden-output test in tests/test_regime_frame.py asserts this.
    Never add, remove, or reorder keys without updating that test.

    ``{"quad": …, "quad_name": …, "liquidity_overlay": …}``

    Missing fields → None (same as raw.get on an empty dict).
    """
    raw = _read_raw(region)
    # Key order is load-bearing (LLM prompts embed the dict repr/JSON directly).
    return {
        "quad": raw.get("quad"),
        "quad_name": raw.get("quad_name"),
        "liquidity_overlay": raw.get("liquidity_overlay"),
    }


def freshness(region: str = "us") -> int | None:
    """Return the number of calendar days between the regime file's date and today.

    Returns None if the ``date`` field is absent, unparseable, or the file is
    missing.  Zero means published today; positive means N days old.  This is
    calendar-day arithmetic (cheap, no trading-day calendar dependency) — callers
    that need trading-day precision should convert externally.

    A return of None is NOT the same as 0 — it means "we do not know how old
    this is" and callers should treat it as potentially stale.
    """
    raw = _read_raw(region)
    date_str = raw.get("date")
    if not date_str:
        return None
    try:
        regime_date = date.fromisoformat(str(date_str))
        today = datetime.now(tz=timezone.utc).date()
        return (today - regime_date).days
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# cycles() — the sole sector_cycles.json consumer (architecture Stage 1, W2)
# ---------------------------------------------------------------------------

def cycles() -> dict[str, dict[str, Any]]:
    """Return the per-sector cycle read keyed by ETF ticker, or {} when stale/absent.

    THIS IS THE ONLY CONSUMER of vendor/macro/site/sectordata/sector_cycles.json in the
    firm.  Leadership brakes (the entry-tilt seed and the late_cycle new-leg halving) read
    THIS dict — never the JSON — so the freshness gate here governs every downstream use.

    FRESHNESS GATE (fail-closed): if meta.asOf is older than the doctrine's
    ``cycles.stale_after_trading_days`` NYSE sessions (default 5), or asOf is missing /
    unparseable, this returns {} and every consumer degrades to a no-op.  Stale cycle data
    may only REMOVE a tilt/shrink, never impose one — per the master invariant.

    KEYING: the keys are the sector ETF tickers themselves (XLK, XLV, XLF, …) because in
    sector_cycles.json the sector rows ARE those ETFs (kind=='sector').  Basket-block
    instruments (SMH/QQQ/IGV/MTUM/…) live under ``baskets`` and are DELIBERATELY OMITTED —
    a caller mapping such a ticker gets no row (None) = unmapped = un-shrunk.  Fail-open on
    the mapping only ever removes a shrink, so it cannot un-cap or flip direction.

    Per-sector fields
    -----------------
    phase         str        — raw cycle phase (Peak/Downturn/Trough/Recovery/Expansion/…)
    phaseLabel    str | None — human label (Topping/…); passthrough, may be None
    pos           float|None — 0‥100 position within the cycle
    osc_slope     float|None — oscillator slope (None when absent — never guessed)
    late_cycle    bool       — phase∈{Peak,Downturn} AND osc_slope<0 AND pos≥70; any
                               missing component → False.  An ENTRY-size brake on NEW
                               leadership legs (halve to 0.5·lw) — NEVER an exit/veto.
    entry_favored bool       — phase∈{Trough,Recovery,Expansion}; the walk-forward-defensible
                               entry TILT for NEW additions only.

    Returns {} (not None) on every degraded path so ``for t, row in cycles().items()`` is
    always safe and a missing file is indistinguishable from a no-op.
    """
    try:
        if not _CYCLES_PATH.exists():
            return {}
        raw = json.loads(_CYCLES_PATH.read_text())
    except Exception:  # noqa: BLE001 — any I/O or parse error → no-op
        return {}

    meta = raw.get("meta") or {}
    as_of = meta.get("asOf") if isinstance(meta, dict) else None
    stale_after = _cycles_stale_after_td()
    age = _trading_days_since(as_of)
    # Unknown age (age is None) is treated as stale — fail-closed, never fail-open.
    if age is None or age > stale_after:
        return {}

    out: dict[str, dict[str, Any]] = {}
    for sec in raw.get("sectors") or []:
        if not isinstance(sec, dict):
            continue
        ticker = sec.get("ticker")
        if not ticker or not isinstance(ticker, str):
            continue
        now = sec.get("now") or {}
        if not isinstance(now, dict):
            now = {}
        phase = now.get("phase")
        pos = _as_float(now.get("pos"))
        osc_slope = _as_float(now.get("osc_slope"))
        out[ticker] = {
            "phase": phase,
            "phaseLabel": now.get("phaseLabel"),
            "pos": pos,
            "osc_slope": osc_slope,
            "late_cycle": _late_cycle(phase, osc_slope, pos),
            "entry_favored": isinstance(phase, str) and phase in _ENTRY_FAVORED_PHASES,
        }
    return out


def _as_float(val: Any) -> float | None:
    """Coerce to float, or None on any failure (missing/None/non-numeric)."""
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _late_cycle(phase: Any, osc_slope: float | None, pos: float | None) -> bool:
    """Derive the late_cycle boolean.  ALL three components required; any missing → False.

    This is an ENTRY-size brake (halve a NEW leadership leg to 0.5·lw), never an exit.  The
    walk-forward refuted the cycle veto on held/leading sectors — so this must never fire as
    a reason to trim or exit something already held; consumers apply it only to new legs.
    """
    if not isinstance(phase, str) or phase not in _LATE_CYCLE_PHASES:
        return False
    if osc_slope is None or osc_slope >= 0:
        return False
    if pos is None or pos < _LATE_CYCLE_POS_MIN:
        return False
    return True


def _cycles_stale_after_td() -> int:
    """The trading-day staleness horizon for cycles(), from doctrine.yml (fallback 5)."""
    try:
        cfg = _doctrine().get("cycles") or {}
        val = cfg.get("stale_after_trading_days")
        if val is not None:
            return int(val)
    except Exception:  # noqa: BLE001
        pass
    return _CYCLES_STALE_TD_FALLBACK


# ---------------------------------------------------------------------------
# rotation_evidence() — the shrink-only disagreement-agreement count (W-I task 6)
# ---------------------------------------------------------------------------
# THE ONE definition of "how many disagreement sources agree", consumed by BOTH budget()'s damp and
# rotation.fragility_signal()'s DEF_SLEEVE unthrottle so the two levers never count evidence two
# different ways.  Four tri-state sources ({nowcast doubt, liquidity stress/hollow, radar caution,
# defensive-RS crossover}); True counts, False/None do NOT (a MISSING source is never evidence).
# SHRINK-ONLY: the count can only DAMP the budget (down toward the floor) and LIFT the defensive
# floor — it can never un-cap, raise authority, or flip direction.

def _rotation_evidence_cfg() -> dict[str, Any]:
    """Merge doctrine's ``rotation_evidence:`` block over the mirrored fallback (fallback wins gaps)."""
    cfg = dict(_ROTATION_EVIDENCE_FALLBACK)
    try:
        block = _doctrine().get("rotation_evidence") or {}
        if isinstance(block, dict):
            for k, v in block.items():
                if v is not None:
                    cfg[k] = v
    except Exception:  # noqa: BLE001
        pass
    return cfg


def _nowcast_doubt_source(
    nowcast_out: Any = None,
    *,
    series_fn: Any = None,
    quad: Any = None,
    quad_name: Any = None,
    risk_state: Any = None,
    asof: Any = None,
) -> Optional[bool]:
    """True when the price-action nowcast DOUBTS the offensive label, None when undeterminable.

    Advisory-only per nowcast_validation.md (the gate FAILED, ``BUDGET_INPUT_QUALIFIED=False``) — but
    this is a shrink SIZE input (a lens surface the validation explicitly permits), NOT a wired
    budget-return predictor, so using it here is within the honest frame.  ``nowcast_out`` (a
    pre-computed ``nowcast()`` dict) may be injected for tests; otherwise it is computed with the
    supplied label (never a live regime read here — the label is passed in).  Any error → None."""
    out = nowcast_out
    if out is None:
        try:
            from brain import regime_nowcast as _nc
            out = _nc.nowcast(series_fn, quad=quad, quad_name=quad_name,
                              risk_state=risk_state, asof=asof)
        except Exception:  # noqa: BLE001 — a missing/broken nowcast leaves the source absent
            return None
    if not isinstance(out, dict):
        return None
    # applies==False (defensive label) means the nowcast no-ops — NOT a doubt (it is a no-op, absent).
    if out.get("applies") is False:
        return False
    stance = out.get("stance")
    if stance in ("doubt", "strong-doubt"):
        return True
    if stance == "confirm":
        return False
    return None  # unknown stance → undeterminable


def _liquidity_stress_source(
    liquidity_out: Any = None,
    *,
    series_fn: Any = None,
    cfg: Optional[dict] = None,
) -> Optional[bool]:
    """True when liquidity QUALITY reads stress-expansion / neutral-hollow, None when unknown.

    A 'benign-expansion' or 'contracting' read is a definite NON-agreement (False); an 'unknown' read
    (a missing quantity series) is UNDETERMINABLE (None) and never counts.  ``liquidity_out`` (a
    pre-computed ``classify()`` dict) may be injected for tests; otherwise it is computed from the
    production frames reader.  Any error → None."""
    ev_cfg = cfg or _rotation_evidence_cfg()
    stress_labels = {str(x).strip().lower() for x in (ev_cfg.get("liquidity_stress_labels") or ())}
    out = liquidity_out
    if out is None:
        try:
            from brain import liquidity_quality as _lq
            fn = series_fn if series_fn is not None else _lq.series_from_frames
            out = _lq.classify(fn)
        except Exception:  # noqa: BLE001
            return None
    if not isinstance(out, dict):
        return None
    label = out.get("label")
    if label is None or label == "unknown":
        return None  # cannot even measure liquidity → undeterminable, never counts
    return bool(str(label).strip().lower() in stress_labels)


def _radar_caution_source(
    radar_state: Any = None,
    *,
    cfg: Optional[dict] = None,
) -> Optional[bool]:
    """True when the dashboard's own risk_radar reads caution / risk_off, None when unavailable.

    Prefers ``radar_state`` if injected (a plain state string — the DI path tests use).  Otherwise it
    tries ``brain.risk_prior`` (which lands via the in-flight fix/bot-orphans-arming merge — its
    ``prior()`` exposes ``radar_state``), then falls back to the VALIDATED radar forward-log's last
    row.  A missing radar everywhere → None (non-agreeing; the incident's blinded-feed failure must
    degrade to 'no evidence', never to a fabricated caution OR a fabricated all-clear).  Any error → None."""
    ev_cfg = cfg or _rotation_evidence_cfg()
    caution_states = {
        str(x).strip().lower().replace("-", "_")
        for x in (ev_cfg.get("radar_caution_states") or ())
    }
    st = radar_state
    if st is None:
        # (a) risk_prior (present only after the in-flight merge) — never a hard dependency.
        try:
            from brain import risk_prior as _rp  # type: ignore
            p = _rp.prior()
            if isinstance(p, dict):
                st = p.get("radar_state") or (p.get("reconciliation") or {}).get("radar_state")
        except Exception:  # noqa: BLE001 — risk_prior not on this base / any error → try the log
            st = None
    if st is None:
        # (b) the validated forward-log tail (the read risk_prior's test binds against).
        st = _read_radar_forward_log_state()
    if st is None:
        return None
    return bool(str(st).strip().lower().replace("-", "_") in caution_states)


def _read_radar_forward_log_state() -> Optional[str]:
    """Return the ``state`` of the last risk_radar forward-log row, or None on any miss.  Read-only.

    The validated (non-shadow) radar plane the bot was BLIND to during the incident.  A missing file /
    empty log / parse error → None (the source is simply absent — never a fabricated read)."""
    try:
        if not _RADAR_FORWARD_LOG.exists():
            return None
        lines = [ln for ln in _RADAR_FORWARD_LOG.read_text().splitlines() if ln.strip()]
        if not lines:
            return None
        row = json.loads(lines[-1])
        st = row.get("state") if isinstance(row, dict) else None
        return str(st) if st is not None else None
    except Exception:  # noqa: BLE001
        return None


def _defensive_rs_source(
    rs_crossed: Any = None,
    *,
    series_fn: Any = None,
) -> Optional[bool]:
    """True when the defensive-vs-offensive 20d RS differential has crossed (defensives leading).

    ``rs_crossed`` (a bool) may be injected directly for tests; otherwise the shared
    ``distribution_tells.defensive_offensive_rs_diff`` is called (the ONE RS-diff construction, shared
    with the nowcast + the distribution escalator).  Absent baskets → crossed None → None (never
    counts).  Any error → None."""
    if rs_crossed is not None:
        return bool(rs_crossed)
    try:
        from portfolio import distribution_tells as _dt
        rs = _dt.defensive_offensive_rs_diff(series_fn=series_fn) if series_fn is not None \
            else _dt.defensive_offensive_rs_diff()
        if isinstance(rs, dict):
            crossed = rs.get("crossed")
            return None if crossed is None else bool(crossed)
    except Exception:  # noqa: BLE001
        return None
    return None


def rotation_evidence(
    *,
    nowcast_doubt: Any = None,
    liquidity_stress: Any = None,
    radar_caution: Any = None,
    defensive_rs_cross: Any = None,
    cfg: Optional[dict] = None,
) -> dict[str, Any]:
    """The shrink-only agreement count over the four disagreement sources (W-I task 6).

    Each of the four keyword args may be supplied DIRECTLY as a tri-state (True / False / None) — the
    DI path the tests and phase2 use so the shared mutating store is NEVER live-read inside this
    computation.  When an arg is left ``None`` the corresponding source is treated as ABSENT (None) —
    it does NOT trigger a live read here (callers that want the production reads pass the pre-computed
    source verdicts in; ``budget()``/``fragility_signal()`` accept a whole ``evidence`` dict).

    Returns
    -------
    {
      "n_agree": int,                # count of sources that are TRUE (0-4); the ONLY thing levers read
      "sources": {                   # the tri-state verdict per source (True / False / None)
         "nowcast_doubt": bool|None,
         "liquidity_stress": bool|None,
         "radar_caution": bool|None,
         "defensive_rs_cross": bool|None,
      },
    }

    INVARIANT: a None (missing) source is never counted (absent data can never manufacture a shrink);
    only an affirmative True agrees.  ``n_agree`` is therefore monotonic in real evidence and 0 on a
    total outage → the damp is 1.0 and the lift is 0.0 (byte-identical no-op)."""
    sources = {
        "nowcast_doubt": _tri(nowcast_doubt),
        "liquidity_stress": _tri(liquidity_stress),
        "radar_caution": _tri(radar_caution),
        "defensive_rs_cross": _tri(defensive_rs_cross),
    }
    n_agree = sum(1 for v in sources.values() if v is True)
    return {"n_agree": n_agree, "sources": sources}


def _tri(v: Any) -> Optional[bool]:
    """Coerce to True / False / None — anything non-bool-ish (including 0/1 ints) stays as-is if bool.

    None (or a non-bool) is 'absent' and never counts; only a real bool True agrees."""
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    # a non-None non-bool (e.g. a stray dict) is treated as absent — never a fabricated True.
    return None


def _evidence_damp(n_agree: int, cfg: dict[str, Any]) -> float:
    """The budget FLEX multiplier from the agreement count: 0-1 → 1.0; 2 → 0.9; 3+ → 0.8.  Shrink-only.

    Reads ``rotation_evidence.damp_2_agree`` / ``damp_3plus_agree`` (both in (0,1]); anything out of
    band degrades to 1.0 (no damp) so a bad config can never damp harder than the doctrine intends."""
    def _mult(key: str) -> float:
        try:
            m = float(cfg[key])
        except (TypeError, ValueError, KeyError):
            return 1.0
        return m if 0.0 < m <= 1.0 else 1.0
    if n_agree >= 3:
        return _mult("damp_3plus_agree")
    if n_agree == 2:
        return _mult("damp_2_agree")
    return 1.0


# ---------------------------------------------------------------------------
# budget() — the ONE leadership budget equation (architecture Stage 2, W2)
# ---------------------------------------------------------------------------

def budget(region: str = "us", *, evidence: Optional[dict] = None) -> dict[str, Any]:
    """Return the single leadership-sleeve budget for *region* + the inputs that produced it.

    THE ONE EQUATION (no double-counting — confidence × transition × flip-fragility is
    consumed exactly ONCE, here):

        lead_budget = clamp( base + slope · clamp(confidence,0,1) · T · F · D ,  floor, ceil )
          T = {WEAKENING: 0.6, ROLLING/DETERIORATING: 0.4, else 1.0}
          F = 0.75 if flip_margin < flip_margin_min else 1.0
          D = the ROTATION-EVIDENCE damp (W-I task 6): 0-1 agree → 1.0; 2 → 0.9; 3+ → 0.8

    THE EVIDENCE DAMP (D) — the incident's DETECTION fix.  ``evidence`` is the shrink-only agreement
    count over the four disagreement sources ({nowcast doubt, liquidity stress/hollow, radar caution,
    defensive-RS crossover}); it is PASSED IN (a ``rotation_evidence()`` dict, or ``{"n_agree": int}``)
    so this function stays standalone and never live-reads the mutating store.  ``None`` (the default,
    and every caller pre-W-I) means "no evidence supplied" → n_agree 0 → D 1.0 → the budget is
    BYTE-IDENTICAL to pre-W-I.  D multiplies the FLEX above the floor and the result is re-clamped, so
    D can only ever push the budget DOWN toward the 0.40 floor — never up (the master invariant: this
    lever is shrink-only; missing/insufficient evidence is a no-op, never an un-cap).

    DEGRADE-TO-MIDPOINT: if confidence OR transition_state OR flip_margin is missing from
    the frame, the equation is SKIPPED and lead_budget = midpoint (0.50 = today's behavior).
    A partial/absent frame can therefore never inflate the budget to the ceiling — it lands
    on the neutral midpoint.  This is the invariant in action: missing data shrinks toward
    neutral, never un-caps.  (The evidence damp does NOT further move the degraded midpoint — a
    missing regime has no flex to damp; the midpoint is already the conservative no-flex read.)

    All constants come from config/doctrine.yml's ``budget:`` / ``rotation_evidence:`` blocks (tagged
    unverified-prior); a missing config falls back to the mirrored priors above.

    Returns
    -------
    {
      "lead_budget": float,          # the clamped budget in [floor, ceil]
      "inputs": {
         "confidence": float|None,   # raw frame confidence (pre-clamp), None if absent
         "transition_state": str|None,
         "flip_margin": float|None,
         "T": float,                 # applied transition multiplier
         "F": float,                 # applied flip-fragility multiplier
         "D": float,                 # applied rotation-evidence damp (1.0 when no evidence)
         "evidence_n_agree": int,    # the agreement count that produced D (0 when no evidence)
         "evidence_sources": dict|None,  # per-source tri-state verdicts (None when no evidence dict)
      },
    }
    The inputs block exists so runlogs can show WHY a budget landed where it did.
    """
    # ── E2.2 THE SUBSUMPTION SHIM (charter P7 — one sizing consumer) ──────────────────────
    # When the posture decider is ARMED, this function is a THIN SHIM: the equation below
    # moved VERBATIM into posture_decider._offense_budget(), and the W-I evidence damp's
    # planes live in the decider's defense_pressure D — applying the damp HERE TOO would
    # consume the same signal twice (the W2 anti-compounding law). Flag ON ⇒ delegate to the
    # published posture artifact (never a recursive compute; the decider reads frame()/
    # cycles() from this module, not budget()). Every degrade path (flag off / artifact
    # absent / import failure) falls through to the original equation — byte-identical
    # flag-off is the E3 control arm, migration-tested.
    try:
        from brain import posture_decider as _pd
        if region == "us" and _pd.posture_flag():
            _rec = _pd.latest()
            if not (isinstance(_rec, dict) and _rec.get("offense_budget") is not None):
                _rec = _pd.decide()          # artifact missing → one fresh deterministic read
            _ob = _as_float(_rec.get("offense_budget"))
            if _ob is not None:
                _f = frame(region)
                return {
                    "lead_budget": _ob,
                    "inputs": {
                        "confidence": _as_float(_f.get("confidence")),
                        "transition_state": _f.get("transition_state"),
                        "flip_margin": _as_float(_f.get("flip_margin")),
                        "T": None, "F": None,
                        "D": 1.0,                  # damp BYPASSED — its planes live in posture D
                        "evidence_n_agree": 0,
                        "evidence_sources": None,
                        "posture_delegated": True,  # runlog provenance (single consumption)
                        "posture_class": _rec.get("posture_class"),
                        "shrink_provenance": _rec.get("shrink_provenance"),
                    },
                }
    except Exception:  # noqa: BLE001 — P2: any failure degrades to the original equation
        pass
    cfg = _budget_cfg()
    ev_cfg = _rotation_evidence_cfg()
    f = frame(region)
    confidence = f.get("confidence")
    transition_state = f.get("transition_state")
    flip_margin = f.get("flip_margin")

    conf_f = _as_float(confidence)
    fm_f = _as_float(flip_margin)

    # Resolve the evidence agreement count (0 when no evidence supplied → D 1.0 → byte-identical).
    n_agree = 0
    ev_sources = None
    if isinstance(evidence, dict):
        ev_sources = evidence.get("sources")
        try:
            n_agree = int(evidence.get("n_agree") or 0)
        except (TypeError, ValueError):
            n_agree = 0
    D = _evidence_damp(n_agree, ev_cfg)

    # DEGRADE-TO-MIDPOINT: any of the three load-bearing fields missing → neutral budget.
    # transition_state missing means we cannot know T; confidence missing means no flex basis;
    # flip_margin missing means we cannot know the fragility damp. In all cases we refuse to
    # guess a *larger* budget and return the midpoint (today's hardwired 0.50 behaviour).  The
    # evidence damp does NOT apply here: there is no flex to damp and the midpoint is already the
    # conservative no-flex read (D is still reported so runlogs see the evidence that WOULD apply).
    if conf_f is None or transition_state is None or fm_f is None:
        return {
            "lead_budget": float(cfg["midpoint"]),
            "inputs": {
                "confidence": conf_f,
                "transition_state": transition_state,
                "flip_margin": fm_f,
                "T": 1.0,
                "F": 1.0,
                "D": D,
                "evidence_n_agree": n_agree,
                "evidence_sources": ev_sources,
            },
        }

    conf_clamped = min(max(conf_f, 0.0), 1.0)
    T = _transition_mult(transition_state, cfg)
    F = 0.75 if fm_f < float(cfg["flip_margin_min"]) else 1.0

    raw_budget = float(cfg["base"]) + float(cfg["slope"]) * conf_clamped * T * F * D
    lead_budget = min(max(raw_budget, float(cfg["floor"])), float(cfg["ceil"]))

    return {
        "lead_budget": lead_budget,
        "inputs": {
            "confidence": conf_f,
            "transition_state": transition_state,
            "flip_margin": fm_f,
            "T": T,
            "F": F,
            "D": D,
            "evidence_n_agree": n_agree,
            "evidence_sources": ev_sources,
        },
    }


def _budget_cfg() -> dict[str, Any]:
    """Merge the doctrine budget: block over the hard-coded fallback (fallback wins on gaps)."""
    cfg = dict(_BUDGET_FALLBACK)
    try:
        block = _doctrine().get("budget") or {}
        if isinstance(block, dict):
            for k, v in block.items():
                if v is not None:
                    cfg[k] = v
    except Exception:  # noqa: BLE001
        pass
    return cfg


def _transition_mult(state: Any, cfg: dict[str, Any]) -> float:
    """T multiplier for the transition state; unlisted/None → 1.0 (calm-tape default)."""
    table = cfg.get("transition_mult") or {}
    if isinstance(state, str) and isinstance(table, dict) and state in table:
        try:
            return float(table[state])
        except (TypeError, ValueError):
            return 1.0
    return 1.0
