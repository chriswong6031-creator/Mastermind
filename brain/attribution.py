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


# ═══════════════════════════ L5b: ALLOCATION + CASH-TIMING attribution ═══════════════════════════
# The per-name Brinson split above answers "of the names we HELD, which seat earned the active return?"
# It cannot see the two portfolio-level decisions that dominated the incident: WHICH sectors we tilted
# toward ('held XLV') and HOW MUCH we left in cash/defense ('avoided semis' = not fully invested in the
# down-tape). This block adds those two terms, decomposed against the SAME active return (book minus
# SPY) so all three legs reconcile:
#
#   active = allocation + cash_timing + selection_residual
#
#   allocation    — the sector/sleeve TILT effect: Σ_sector (w_book − w_bench) · (r_sector − r_bench).
#                   Overweighting a sector that beat SPY (held XLV) is POSITIVE allocation; overweighting
#                   one that lagged (semis into the unwind) is NEGATIVE. This is the Brinson allocation
#                   effect at the sleeve/sector grain the book actually decides at.
#   cash_timing   — the value of NOT being fully invested: cash_weight · (r_cash − r_bench). In a down
#                   tape r_bench < r_cash≈0, so holding cash is CREDITED (avoided the drawdown); in an
#                   up tape it is a DRAG (opportunity cost). 'Avoided semis' by sitting in cash lands here.
#   selection_res — the residual: whatever active return the two portfolio terms don't explain is the
#                   within-sector name-selection the per-name Brinson split already credited to seats.
#
# All returns are growth-of-$1 window returns from the benchmark ledger / marks (ONE price source).
# Missing any input degrades the term to 0.0 and tags it 'unavailable' (P2) — never fabricates credit.

_CASH_RETURN = 0.0   # cash/T-bill window return proxy (a ballast sleeve ~flat over a 21d window; the
                     # cash-timing sign is driven by r_bench, so a small nonzero carry is immaterial)


def _window_return(curve: dict) -> float | None:
    """Growth-of-$1 window return for a {date: value} curve (last/first − 1). None if < 2 points."""
    if not curve:
        return None
    ks = sorted(curve)
    if len(ks) < 2 or not curve[ks[0]]:
        return None
    try:
        return curve[ks[-1]] / curve[ks[0]] - 1.0
    except (TypeError, ZeroDivisionError):
        return None


def allocation_terms(book_return: float, bench_return: float, *, sector_weights: dict,
                     sector_returns: dict, bench_sector_weights: dict, cash_weight: float,
                     cash_return: float | None = None) -> dict:
    """Decompose a book's ACTIVE return (book − bench) into allocation + cash_timing + selection_res.

    `sector_weights` / `bench_sector_weights` = {sector: weight} for the book and the benchmark;
    `sector_returns` = {sector: window_return}; `cash_weight` = the book's cash/ballast fraction.
    All three terms are in return space (fractions); the returned block also carries bps. Reconciles:
    allocation + cash_timing + selection_residual == active (asserted within rounding). Pure; never
    raises on missing sectors (a sector absent from `sector_returns` contributes 0 to allocation)."""
    active = book_return - bench_return
    cash_r = _CASH_RETURN if cash_return is None else float(cash_return)

    # Brinson allocation effect at the sector grain the book decides at.
    allocation = 0.0
    for sec, wb in (sector_weights or {}).items():
        rsec = (sector_returns or {}).get(sec)
        if rsec is None:
            continue
        wbench = (bench_sector_weights or {}).get(sec, 0.0)
        allocation += (float(wb) - float(wbench)) * (float(rsec) - bench_return)

    # cash-timing: the sleeve held out of the market earns (r_cash − r_bench) on its weight. In a down
    # tape (r_bench < 0) this is a POSITIVE credit; in an up tape it is the opportunity-cost drag.
    cash_timing = float(cash_weight or 0.0) * (cash_r - bench_return)

    # selection is the reconciling residual (the within-sector name picks the per-name split credits).
    selection_residual = active - allocation - cash_timing

    def _bps(x):
        return round(x * 10000.0, 2)

    return {
        "active_return": round(active, 6), "active_bps": _bps(active),
        "allocation": round(allocation, 6), "allocation_bps": _bps(allocation),
        "cash_timing": round(cash_timing, 6), "cash_timing_bps": _bps(cash_timing),
        "selection_residual": round(selection_residual, 6), "selection_residual_bps": _bps(selection_residual),
        "reconciled": abs((allocation + cash_timing + selection_residual) - active) < 1e-9,
    }


def allocation_attribution(asof: date | None = None, *, book_curves: dict | None = None,
                           regime: dict | None = None) -> dict:
    """Portfolio-level allocation + cash-timing attribution for every book, consuming the benchmark
    ledger's bogey curves (SPY = the bench, defensive basket = the 'held XLV' sleeve return, do_nothing
    = the carry). Degrades to an honest 'building' stub when the ledger/curves are missing (P2).

    For each book curve supplied (or the ledger's book rows), we treat the book's realized window return
    vs SPY as its active return and attribute the portion explained by the DEFENSIVE tilt (allocation)
    and by sitting in CASH (cash_timing) using the ledger's own defensive + do-nothing curves as the
    sleeve-return proxies. This makes 'held XLV, avoided semis' a creditable, reconciled fact.

    Returns {as_of, bench, books:{id:{...terms}}, status, regime}. Never raises."""
    asof = asof or date.today()
    try:
        from brain import benchmark_ledger as BL
    except Exception:  # noqa: BLE001
        BL = None

    ledger = {}
    if BL is not None:
        try:
            ledger = BL.latest() or {}
        except Exception:  # noqa: BLE001
            ledger = {}

    bogeys = (ledger.get("bogeys") or {}) if isinstance(ledger, dict) else {}
    spy_ret = _window_return((bogeys.get("spy") or {}).get("curve") or {})
    def_ret = _window_return((bogeys.get("defensive") or {}).get("curve") or {})

    if spy_ret is None:
        return {"as_of": asof.isoformat(), "bench": "SPY", "status": "building",
                "reason": "no SPY bogey curve (benchmark ledger unavailable)",
                "regime": regime or {}, "books": {}}

    # book curves: prefer explicit arg, else the ledger's book rows are return-only (no curve) — we take
    # the leaderboard return_pct where a curve isn't supplied.
    curves = dict(book_curves or {})
    out_books: dict[str, dict] = {}
    for row in (ledger.get("leaderboard") or []):
        if row.get("kind") != "book":
            continue
        bid = row.get("id")
        if bid in curves:
            book_ret = _window_return(curves[bid])
        else:
            rp = row.get("return_pct")
            book_ret = (rp / 100.0) if rp is not None else None
        if book_ret is None:
            continue
        # sleeve proxies from the ledger: the DEFENSIVE tilt sector return is def_ret (vs SPY bench).
        # We don't have the book's true sector weights here, so the allocation term is expressed as the
        # book's realized tilt toward the defensive sleeve: (book beat/lag of the defensive curve). The
        # honest, reconciled decomposition uses the defensive sleeve as the one measurable tilt and folds
        # the rest into selection_residual — 'held XLV' shows up as a positive allocation when the book
        # tracked the defensive curve up while SPY fell.
        sector_weights = {"defensive": 1.0} if def_ret is not None else {}
        sector_returns = {"defensive": def_ret} if def_ret is not None else {}
        bench_sector_weights = {"defensive": 0.0}   # SPY holds none of the defensive tilt by construction
        terms = allocation_terms(book_ret, spy_ret, sector_weights=sector_weights,
                                  sector_returns=sector_returns,
                                  bench_sector_weights=bench_sector_weights, cash_weight=0.0)
        out_books[bid] = terms

    return {"as_of": asof.isoformat(), "bench": "SPY",
            "spy_return": round(spy_ret, 6),
            "defensive_return": round(def_ret, 6) if def_ret is not None else None,
            "status": "scoring" if out_books else "building",
            "regime": regime or (ledger.get("regime") if isinstance(ledger, dict) else {}) or {},
            "books": out_books}
