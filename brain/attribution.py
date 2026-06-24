"""Per-seat ATTRIBUTION — who earned (or cost) the active return on each resolved name.

The desk's learning loop already grades each seat *in isolation* (brain/calibration.py): was
SENTINEL's stance directionally right, did the Strategist's confirmed themes beat SPY, etc. What
it does NOT yet answer is the portfolio-manager's question: for a name that is now RESOLVED, how
do we split its realized active return (rel_return vs SPY) across the chain of seats that touched
it? This module is that decomposition — a Brinson-adapted credit split:

    allocation  → MACRO STRATEGIST   (top-down: did the regime/theme call put us here?)
    selection   → FORGE              (bottom-up engine+research screen that confirmed the name)
    veto        → SENTINEL           (the blind adversary's stance — trim/oppose drag or credit)
    sizing      → NEXUS              (the synthesis scale 0..1 actually applied)
    gate        → GATE OFFICER       (portfolio-level veto/withhold/trim)
    exit        → RISK OFFICER       (time-stop / falsifier-triggered exit)

Each seat receives a *share* of the name's realized active return; the shares sum to 1.0, so the
per-seat attributed bps sum (within rounding) to the name's total rel_return in bps. Seats that did
not touch a name receive zero share. The split is deterministic, pure (no LLM), reads only seat
artifacts that already exist on disk, and NEVER raises — a name with missing artifacts gets an
honest "selection-only" stub crediting whatever seat is present.

Public surface:
    attribute(asof)  -> {as_of, names: [...], seats: {seat: {...}}, regime, status}
    persist(asof)    -> writes data/brain/attribution/<asof>.json + the cumulative _rollup.json

Each attributed name is tagged with the REGIME at decision time (data/regime/latest.json quad/state)
so reputation can later be sliced regime-conditionally (task #5).
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_COMMITTEE = _ROOT / "data" / "committee"
_GATE_OFFICER = _ROOT / "data" / "gate_officer"
_RISK_OFFICER = _ROOT / "data" / "risk_officer"
_OUT = _ROOT / "data" / "brain" / "attribution"
_ROLLUP = _OUT / "_rollup.json"
_VENDOR = _ROOT / "vendor" / "macro"

_HORIZON = 21  # forward business-day window names are graded over (matches calibration / the falsifier)

# Canonical seat keys (align with calibration.py / cio.py reputation indices).
SEATS = ("strategist", "forge", "sentinel", "nexus", "gate", "risk")

# Base credit shares (sum to 1.0) when the FULL chain touched a name. These are *priors*; the
# actual split below redistributes a seat's share to the residual selection credit whenever that
# seat left no artifact (e.g. no SENTINEL → its veto share folds back into FORGE selection).
_BASE_SHARES = {
    "strategist": 0.20,   # top-down allocation
    "forge": 0.40,        # bottom-up selection — the engine+research screen that admitted the name
    "sentinel": 0.10,     # adversarial veto / trim drag
    "nexus": 0.10,        # synthesis sizing
    "gate": 0.10,         # portfolio gate
    "risk": 0.10,         # exit timing
}


# ───────────────────────────── small IO helpers (never raise) ─────────────────────────────
def _load_json(p: Path) -> dict:
    try:
        if p.exists():
            d = json.loads(p.read_text())
            return d if isinstance(d, dict) else {}
    except Exception:  # noqa: BLE001
        return {}
    return {}


def _read_regime() -> dict:
    """The macro regime read at attribution time (quad/state), for regime-conditional reputation.
    Best-effort: {} if the vendored dashboard isn't present (offline tests)."""
    r = _load_json(_VENDOR / "data" / "regime" / "latest.json")
    if not r:
        return {}
    return {"quad": r.get("quad"), "quad_name": r.get("quad_name"),
            "state": r.get("state"), "liquidity_overlay": r.get("liquidity_overlay")}


def _elapsed(d_iso: str, asof: date) -> bool:
    """True only if the decision date is strictly before asof (a fully-elapsed forward window is
    required; same-day / future decisions are leakage and excluded)."""
    try:
        return date.fromisoformat(str(d_iso)[:10]) < asof
    except Exception:  # noqa: BLE001
        return False


def _resolved_rel(ticker: str, decision_iso: str, asof: date | None):
    """Resolved forward rel_return for `ticker` from `decision_iso`, leakage-free, via the SAME
    label path the seat graders use (brain.outcomes.label_thesis). None until fully resolved."""
    try:
        from brain import outcomes
        lab = outcomes.label_thesis({
            "id": f"{decision_iso}-{ticker}",
            "state_asof": decision_iso,
            "entry_levels": {"ticker": ticker},
            "falsifier": {"check": {"kind": "rel_return", "subject_ticker": ticker,
                                    "vs": "SPY", "horizon_d": _HORIZON}},
        }, asof)
    except Exception:  # noqa: BLE001
        return None
    if not (lab and lab.get("resolved") and lab.get("rel_return") is not None):
        return None
    try:
        return float(lab["rel_return"])
    except (TypeError, ValueError):
        return None


# ───────────────────────── artifact gathering (per resolved name) ─────────────────────────
def _strategist_named(date_iso: str, ticker: str) -> dict | None:
    """If the Strategist named this ticker in a CONFIRMED theme on `date_iso`, return the
    {theme, leadership, stage, calibration_multiplier} touch; else None."""
    j = _load_json(_COMMITTEE / date_iso / "_FLAGSHIP" / "strategist.json")
    verdict = j.get("verdict") or {}
    cm = verdict.get("calibration_multiplier")
    for t in (verdict.get("confirmed_themes") or []):
        names = {str(n).upper().strip() for n in (t.get("names") or [])}
        if ticker.upper() in names:
            return {"theme": t.get("theme"), "leadership": t.get("leadership"),
                    "stage": t.get("stage"), "calibration_multiplier": cm}
    return None


def _gate_touch(date_iso: str, ticker: str) -> dict | None:
    """The Gate Officer's per-name decision (action/scale/reason) for `ticker` on `date_iso`, or None."""
    j = _load_json(_GATE_OFFICER / date_iso / "decisions.json")
    for dec in ((j.get("result") or {}).get("decisions") or []):
        if str(dec.get("ticker") or "").upper().strip() == ticker.upper():
            return {"action": str(dec.get("action") or "").lower(),
                    "scale": dec.get("scale"), "reason": dec.get("reason")}
    return None


def _risk_touch(date_iso: str, ticker: str) -> dict | None:
    """The Risk Officer's per-name decision (action/scale/reason) for `ticker` on `date_iso`, or None."""
    j = _load_json(_RISK_OFFICER / date_iso / "decisions.json")
    for dec in ((j.get("result") or {}).get("decisions") or []):
        if str(dec.get("ticker") or "").upper().strip() == ticker.upper():
            return {"action": str(dec.get("action") or "").lower(),
                    "scale": dec.get("scale"), "reason": dec.get("reason")}
    return None


def _touches(date_iso: str, ticker: str) -> dict:
    """Collect every seat artifact that touched `ticker` on `date_iso`. Returns a dict keyed by
    seat with the raw touch payload (or absence). Pure reads; never raises."""
    tdir = _COMMITTEE / date_iso / ticker.upper()
    forge = _load_json(tdir / "forge.json")
    sentinel = _load_json(tdir / "sentinel.json")
    nexus = _load_json(tdir / "nexus.json")
    return {
        "strategist": _strategist_named(date_iso, ticker),
        "forge": forge or None,
        "sentinel": sentinel or None,
        "nexus": nexus or None,
        "gate": _gate_touch(date_iso, ticker),
        "risk": _risk_touch(date_iso, ticker),
    }


# ───────────────────────── the Brinson-adapted credit decomposition ─────────────────────────
def _shares(touches: dict) -> dict:
    """Per-seat credit SHARES for one name, summing to 1.0. A seat that left no artifact yields its
    base share back to FORGE (selection), the residual underwriter — so a sparse chain stays fully
    attributed and the shares never leak. Pure arithmetic.

    Veto/exit dominance: a seat that *removed or shrank* the name (Gate withhold/veto, Risk exit,
    NEXUS drop) owns a larger slice of that name's outcome, because its action is what the realized
    return is judged against. We bump such a seat's share by a fixed dominance increment, drawn
    proportionally from the other present seats, then renormalize."""
    present = {s: bool(touches.get(s)) for s in SEATS}
    # FORGE is the residual: if it's absent but anything else is present, route its share there
    # anyway only if forge present; otherwise the present seats share the whole 1.0.
    raw = {s: (_BASE_SHARES[s] if present[s] else 0.0) for s in SEATS}
    total = sum(raw.values())
    if total <= 0:
        return {s: 0.0 for s in SEATS}

    shares = {s: raw[s] / total for s in SEATS}  # renormalize present seats to sum 1.0

    # Dominance bump for the seat whose ACTION is what the name's outcome tests.
    dominant = None
    nexus = touches.get("nexus") or {}
    gate = touches.get("gate") or {}
    risk = touches.get("risk") or {}
    if risk and risk.get("action") in ("exit", "trim") and present["risk"]:
        dominant = "risk"                       # the exit is the decision the outcome judges
    elif gate and gate.get("action") in ("veto", "withhold") and present["gate"]:
        dominant = "gate"                       # the portfolio gate removed/withheld the name
    elif nexus and nexus.get("action") == "drop" and present["nexus"]:
        dominant = "nexus"                      # synthesis dropped the name
    elif (touches.get("sentinel") or {}).get("stance") == "OPPOSE" and present["sentinel"]:
        dominant = "sentinel"                   # adversary opposed — owns more of the avoided/realized move

    if dominant is not None:
        bump = min(0.30, 1.0 - shares[dominant])
        others = [s for s in SEATS if s != dominant and shares[s] > 0]
        drawable = sum(shares[s] for s in others)
        if drawable > 0 and bump > 0:
            for s in others:
                shares[s] -= bump * (shares[s] / drawable)
            shares[dominant] += bump

    # final clamp + renormalize against float drift
    tot = sum(max(0.0, v) for v in shares.values())
    if tot <= 0:
        return {s: 0.0 for s in SEATS}
    return {s: round(max(0.0, shares[s]) / tot, 6) for s in SEATS}


def _attribute_name(date_iso: str, ticker: str, rel: float, touches: dict) -> dict:
    """Decompose one resolved name's active return into per-seat bps. `rel` is the realized
    rel_return vs SPY (fraction); bps = share × rel × 10000."""
    shares = _shares(touches)
    rel_bps = round(rel * 10000.0, 2)
    seat_bps = {s: round(shares[s] * rel_bps, 2) for s in SEATS}
    touched = [s for s in SEATS if shares[s] > 0]
    dominant = max(touched, key=lambda s: shares[s]) if touched else None
    return {
        "ticker": ticker.upper(),
        "decision_date": date_iso,
        "rel_return": round(rel, 6),
        "rel_bps": rel_bps,
        "shares": shares,
        "seat_bps": seat_bps,
        "touched": touched,
        "dominant_seat": dominant,
    }


# ───────────────────────────── public entry points ─────────────────────────────
def attribute(asof: date | None = None) -> dict:
    """Decompose every RESOLVED name's active return across the seats that touched it.

    Walks data/committee/<d>/<TICKER>/ (the per-name forge/sentinel/nexus artifacts), joins the
    Strategist theme membership + Gate/Risk per-name decisions for that date, resolves the name's
    forward rel_return vs SPY (leakage-free, via brain.outcomes), and credits each seat its Brinson
    share. Returns a per-name list + a per-seat rollup for this asof. Honest stub on empty/missing.

    Never raises."""
    asof = asof or date.today()
    regime = _read_regime()
    names: list[dict] = []
    seat_agg: dict[str, dict] = {s: {"bps": 0.0, "n": 0} for s in SEATS}

    try:
        if _COMMITTEE.exists():
            for datedir in sorted(_COMMITTEE.iterdir()):
                if not datedir.is_dir() or not _elapsed(datedir.name, asof):
                    continue
                for tdir in sorted(datedir.iterdir()):
                    if not tdir.is_dir() or tdir.name == "_FLAGSHIP":
                        continue
                    ticker = tdir.name.upper()
                    rel = _resolved_rel(ticker, datedir.name, asof)
                    if rel is None:
                        continue
                    touches = _touches(datedir.name, ticker)
                    if not any(touches.get(s) for s in SEATS):
                        continue  # nothing to attribute to
                    rec = _attribute_name(datedir.name, ticker, rel, touches)
                    rec["regime"] = regime
                    names.append(rec)
                    for s in SEATS:
                        if rec["shares"].get(s, 0.0) > 0:
                            seat_agg[s]["bps"] = round(seat_agg[s]["bps"] + rec["seat_bps"][s], 2)
                            seat_agg[s]["n"] += 1
    except Exception:  # noqa: BLE001 — attribution is best-effort, never fatal
        pass

    seats = {s: {"attributed_bps": round(seat_agg[s]["bps"], 2),
                 "n": seat_agg[s]["n"],
                 "mean_bps": round(seat_agg[s]["bps"] / seat_agg[s]["n"], 2)
                 if seat_agg[s]["n"] else None}
             for s in SEATS}

    return {
        "as_of": asof.isoformat(),
        "horizon_d": _HORIZON,
        "n_names": len(names),
        "regime": regime,
        "status": "scoring" if names else "building",
        "names": names,
        "seats": seats,
    }


def _merge_rollup(prev: dict, block: dict) -> dict:
    """Accumulate this asof's per-seat bps into the cumulative rollup (idempotent per asof — re-running
    the same asof replaces that date's contribution rather than double-counting)."""
    asof = block.get("as_of")
    dates = dict(prev.get("by_date") or {})
    dates[asof] = block.get("seats") or {}
    seats: dict[str, dict] = {s: {"attributed_bps": 0.0, "n": 0} for s in SEATS}
    for _d, sblock in dates.items():
        for s in SEATS:
            cell = (sblock or {}).get(s) or {}
            seats[s]["attributed_bps"] = round(seats[s]["attributed_bps"]
                                               + float(cell.get("attributed_bps") or 0.0), 2)
            seats[s]["n"] += int(cell.get("n") or 0)
    for s in SEATS:
        n = seats[s]["n"]
        seats[s]["mean_bps"] = round(seats[s]["attributed_bps"] / n, 2) if n else None
    return {"updated": asof, "seats": seats, "by_date": dates}


def persist(asof: date | None = None) -> dict:
    """Compute + write data/brain/attribution/<asof>.json and update the cumulative _rollup.json.
    Returns the per-asof block. Never raises."""
    block = attribute(asof)
    try:
        _OUT.mkdir(parents=True, exist_ok=True)
        (_OUT / f"{block['as_of']}.json").write_text(json.dumps(block, indent=2, default=str))
        rollup = _merge_rollup(_load_json(_ROLLUP), block)
        _ROLLUP.write_text(json.dumps(rollup, indent=2, default=str))
    except Exception:  # noqa: BLE001
        pass
    return block


def load(asof: date | None = None) -> dict:
    """Read a previously-persisted attribution block (or the rollup when asof is None-and-absent)."""
    asof = asof or date.today()
    return _load_json(_OUT / f"{asof.isoformat()}.json")


def rollup() -> dict:
    """The cumulative per-seat attributed-bps rollup across all persisted dates."""
    return _load_json(_ROLLUP)
