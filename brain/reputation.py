"""REWARD / INFLUENCE — let well-calibrated seats earn more weight, regime-conditionally and capped.

Calibration (brain/calibration.py) measures whether a seat's stated confidence matched realized
outcomes and emits a de-confidence multiplier in [FLOOR, 1.0]. That is the *safe* half: it can only
shrink overconfidence. This module is the *compounding* half: a seat that has been reliable IN THE
CURRENT REGIME earns proportionally more influence over the synthesis — but bounded, capped, and
regime-conditional, so the desk cannot be captured by one lucky seat.

Three properties make this safe:

  1. REGIME-CONDITIONAL. A seat's reputation is its reliability computed ONLY over grades whose
     regime-at-decision-time matches the regime now. A seat that was a genius in a trending quad is
     COLD in a fresh chop quad until it has earned effective-n of grades IN that quad. A trend
     streak cannot buy influence in a regime where it has no track record.

  2. BOUNDED + CAPPED. `influence_weight(seat, regime)` lives in [W_FLOOR, W_CEIL] = [0.5, 1.0].
     `cap_influence(weight, stated_confidence)` further clamps the product so a single seat's
     effective vote can never exceed MAX_QUORUM_SHARE of the notional quorum — no seat dominates.

  3. SUBTRACT-ONLY PRESERVED. The weight only ever modulates a DE-RISKING action (a trim/veto). It
     is fed into committee.nexus() so a high-influence adversary can veto/trim HARDER (lower the
     OPPOSE bar, deepen the trim) but can NEVER create additive size or rescue a name. NEXUS still
     owns the additive sizing floor.

FLAG-GATED: env MASTERMIND_REPUTATION_WEIGHTING (default OFF). OFF → `influence_weight` returns 1.0
for every seat, so the synthesis is BYTE-IDENTICAL to today. Inert anyway until a seat clears MIN_N
resolved grades IN the current regime — cold-start returns 1.0.

Reads only artifacts that already exist on disk (committee seat artifacts, the predictions ledger,
the attribution rollup). Pure-ish + never raises.
"""
from __future__ import annotations

import json
import os
from datetime import date
from pathlib import Path

from brain import calibration as _calib

_ROOT = Path(__file__).resolve().parent.parent
_COMMITTEE = _ROOT / "data" / "committee"
_GATE_OFFICER = _ROOT / "data" / "gate_officer"
_RISK_OFFICER = _ROOT / "data" / "risk_officer"
_VENDOR = _ROOT / "vendor" / "macro"

# Bounded influence band. A reliability ratio <= 1.0 (de-confidence) maps onto [W_FLOOR, W_CEIL]:
# a perfectly-calibrated seat in this regime earns W_CEIL; a poorly-calibrated one is floored at
# W_FLOOR (it still gets a vote, it just can't dominate). The band never exceeds 1.0 — influence
# weighting cannot inflate a seat above its nominal vote, preserving the subtract-only spirit.
W_FLOOR = 0.5
W_CEIL = 1.0

# No single seat's effective vote (influence_weight x stated_confidence) may exceed this share of the
# notional quorum. The committee has 6 seats; 0.45 lets a strong, well-calibrated adversary lead but
# never unilaterally own the synthesis.
MAX_QUORUM_SHARE = 0.45

MIN_N = _calib.MIN_N  # reuse the calibration cold-start floor (12) — applied PER REGIME here


def _flag_on() -> bool:
    """Influence weighting is OFF unless explicitly armed → synthesis byte-identical to today."""
    v = os.environ.get("MASTERMIND_REPUTATION_WEIGHTING", "0").strip().lower()
    return v in ("1", "true", "yes", "on")


def _load_json(p: Path) -> dict:
    try:
        if p.exists():
            d = json.loads(p.read_text())
            return d if isinstance(d, dict) else {}
    except Exception:  # noqa: BLE001
        return {}
    return {}


def current_regime() -> str:
    """Canonical regime label NOW — the quad from the live macro read. Best-effort; 'unknown' offline
    (which makes every seat COLD, so the desk degrades to flag-OFF behavior when it can't tell the
    regime — fail safe, never fail loud)."""
    r = _load_json(_VENDOR / "data" / "regime" / "latest.json")
    return _regime_key(r)


def _regime_key(regime: dict | None) -> str:
    """Stable, low-cardinality regime key. Prefer the quad number (1-4); fall back to quad_name /
    state; 'unknown' when nothing is legible. Low cardinality keeps per-regime effective-n reachable."""
    regime = regime or {}
    q = regime.get("quad")
    if q is not None and str(q).strip() not in ("", "None"):
        return f"quad{str(q).strip()}"
    for k in ("quad_name", "state"):
        v = regime.get(k)
        if v:
            return str(v).strip().lower().replace(" ", "_")
    return "unknown"


def _date_regime(date_iso: str) -> str:
    """The regime that was live on `date_iso`, read from that date's Strategist artifact
    (input.regime is the canonical regime payload every seat saw). 'unknown' if absent."""
    j = _load_json(_COMMITTEE / date_iso / "_FLAGSHIP" / "strategist.json")
    return _regime_key((j.get("input") or {}).get("regime"))


# ───────────────────────── regime-conditional per-seat reliability ─────────────────────────
# Each grader mirrors the calibration body but (a) keeps the decision DATE so it can be regime-tagged
# and (b) keeps only rows whose date-regime == the target regime. Below MIN_N rows IN THAT regime the
# seat is COLD (reliability None) — a streak in another quad cannot leak influence here.

def _sentinel_rows(asof: date) -> list[tuple[str, int, float]]:
    """(date_iso, outcome 0|1, confidence) for every resolved directional SENTINEL stance."""
    if not _COMMITTEE.exists():
        return []
    try:
        from brain import outcomes
        from brain.ledger import all_theses
    except Exception:  # noqa: BLE001
        return []
    idx = {}
    for t in all_theses():
        subj = ((t.get("entry_levels") or {}).get("ticker") or t.get("subject") or "").upper()
        d = str(t.get("state_asof") or "")[:10]
        if subj and d:
            idx[(subj, d)] = t
    rows: list[tuple[str, int, float]] = []
    try:
        for datedir in _COMMITTEE.iterdir():
            if not datedir.is_dir():
                continue
            for tdir in datedir.iterdir():
                sf = tdir / "sentinel.json"
                if not sf.exists():
                    continue
                s = _load_json(sf)
                stance = s.get("stance")
                conf = s.get("raw_confidence", s.get("confidence"))
                if stance not in ("OPPOSE", "SUPPORT") or conf is None:
                    continue
                th = idx.get((tdir.name.upper(), datedir.name))
                if not th:
                    continue
                try:
                    lab = outcomes.label_thesis(th, asof)
                except Exception:  # noqa: BLE001
                    continue
                if not (lab and lab.get("resolved") and lab.get("rel_return") is not None):
                    continue
                r = lab["rel_return"]
                correct = (stance == "OPPOSE" and r < 0) or (stance == "SUPPORT" and r >= 0)
                rows.append((datedir.name, 1 if correct else 0, float(conf)))
    except Exception:  # noqa: BLE001
        pass
    return rows


def _strategist_rows(asof: date) -> list[tuple[str, int, float]]:
    """(date_iso, outcome 0|1, confidence) per CONFIRMED theme — outcome = median member beat SPY."""
    if not _COMMITTEE.exists():
        return []
    rows: list[tuple[str, int, float]] = []
    try:
        for datedir in sorted(_COMMITTEE.iterdir()):
            if not datedir.is_dir() or not _calib._elapsed(datedir.name, asof):
                continue
            j = _load_json(datedir / "_FLAGSHIP" / "strategist.json")
            for t in ((j.get("verdict") or {}).get("confirmed_themes") or []):
                if str(t.get("stage") or "").lower() != "confirmed":
                    continue
                rels = []
                for tk in (t.get("names") or []):
                    r = _calib._resolved_rel(str(tk).upper().strip(), datedir.name, asof)
                    if r is not None:
                        rels.append(r)
                if not rels:
                    continue
                rels.sort()
                m = len(rels)
                median = rels[m // 2] if m % 2 else (rels[m // 2 - 1] + rels[m // 2]) / 2.0
                lead = t.get("leadership")
                conf = float(lead) if lead is not None else 0.5
                rows.append((datedir.name, 1 if median > 0 else 0, conf))
    except Exception:  # noqa: BLE001
        pass
    return rows


def _seat_decision_rows(root: Path, asof: date, actions: tuple) -> list[tuple[str, int, float]]:
    """(date_iso, outcome 0|1, 1.0) per GATE/RISK decision in `actions`; good ⇔ name underperformed."""
    if not root.exists():
        return []
    rows: list[tuple[str, int, float]] = []
    try:
        for datedir in sorted(root.iterdir()):
            if not datedir.is_dir() or not _calib._elapsed(datedir.name, asof):
                continue
            j = _load_json(datedir / "decisions.json")
            for dec in ((j.get("result") or {}).get("decisions") or []):
                if str(dec.get("action") or "").lower() not in actions:
                    continue
                tk = str(dec.get("ticker") or "").upper().strip()
                if not tk:
                    continue
                r = _calib._resolved_rel(tk, datedir.name, asof)
                if r is None:
                    continue
                rows.append((datedir.name, 1 if r < 0 else 0, 1.0))
    except Exception:  # noqa: BLE001
        pass
    return rows


# Seat -> dated-rows builder. FORGE/PM/TIMING are graded against tickers without a per-date committee
# regime artifact join here; they are scored regime-blind by calibration already, so reputation only
# regime-conditions the seats whose grades carry a legible decision-date regime (the committee seats).
_ROW_BUILDERS = {
    "sentinel": _sentinel_rows,
    "strategist": _strategist_rows,
    "gate": lambda a: _seat_decision_rows(_GATE_OFFICER, a, ("veto", "withhold")),
    "risk": lambda a: _seat_decision_rows(_RISK_OFFICER, a, ("exit",)),
}


def regime_reliability(seat: str, regime: str | None = None, asof: date | None = None) -> dict:
    """Per-seat reliability computed ONLY over grades whose decision-time regime == `regime`.

    Returns {regime, n, reliability, mean_confidence, multiplier, status}. COLD (multiplier 1.0,
    status 'building') until the seat has MIN_N grades IN that regime — a streak in another regime
    cannot raise this seat's weight in a fresh one. Never raises."""
    asof = asof or date.today()
    regime = regime or current_regime()
    builder = _ROW_BUILDERS.get(seat)
    if builder is None:
        return {"regime": regime, "n": 0, "reliability": None, "mean_confidence": None,
                "multiplier": 1.0, "status": "building"}
    try:
        dated = builder(asof)
    except Exception:  # noqa: BLE001
        dated = []
    rows = [(o, c) for d, o, c in dated if _date_regime(d) == regime]
    s = _calib._summarize(rows)
    s["regime"] = regime
    return s


def _attributed_bps(seat: str) -> float | None:
    """The seat's cumulative attributed active return (bps) from the attribution rollup — a coarse,
    regime-blind sign-check that the seat has NET HELPED. None if attribution hasn't run. Used only as
    a damper (a seat with negative cumulative attribution cannot earn above-floor influence)."""
    try:
        from brain import attribution
        cell = ((attribution.rollup() or {}).get("seats") or {}).get(seat) or {}
        v = cell.get("attributed_bps")
        return float(v) if v is not None else None
    except Exception:  # noqa: BLE001
        return None


def influence_weight(seat: str, regime: str | None = None, asof: date | None = None) -> float:
    """The bounded influence weight for `seat` in `regime`, in [W_FLOOR, W_CEIL].

    Formula (only when the flag is ON and the seat is regime-scoring):
        ratio   = reliability / mean_confidence              # how well-calibrated, in this regime
        w       = clamp(ratio, W_FLOOR, W_CEIL)               # bounded [0.5, 1.0], never inflates >1
        if cumulative attributed bps < 0 → w = W_FLOOR        # a net-negative seat is floored

    Returns 1.0 (nominal, no reweighting) when:
        * the flag MASTERMIND_REPUTATION_WEIGHTING is OFF  → BYTE-IDENTICAL synthesis, or
        * the seat is below MIN_N grades IN this regime (cold-start), or
        * reliability / mean_confidence is unavailable.
    Never raises; never returns > W_CEIL (cannot create additive influence)."""
    if not _flag_on():
        return 1.0
    try:
        rel = regime_reliability(seat, regime, asof)
    except Exception:  # noqa: BLE001
        return 1.0
    if rel.get("status") != "scoring":
        return 1.0  # cold in this regime → nominal vote
    reliability = rel.get("reliability")
    mc = rel.get("mean_confidence")
    if not mc or reliability is None:
        return 1.0
    try:
        ratio = float(reliability) / float(mc)
    except (TypeError, ValueError, ZeroDivisionError):
        return 1.0
    w = max(W_FLOOR, min(W_CEIL, ratio))
    bps = _attributed_bps(seat)
    if bps is not None and bps < 0:
        w = W_FLOOR  # net-negative cumulative attribution → floored, never rewarded
    return round(w, 4)


def influence_active(seat: str, regime: str | None = None, asof: date | None = None) -> bool:
    """True iff influence weighting is ACTUALLY in effect for `seat` right now: the flag is ON AND the
    seat is regime-scoring (cleared MIN_N grades IN this regime). This is the single guard the
    synthesis uses to decide whether to apply the weight — needed because a perfectly-calibrated seat
    earns w == 1.0, which is numerically identical to the nominal/flag-OFF weight, so `w != 1.0` is
    NOT a safe activeness test. When this returns False the synthesis must use the legacy path
    (byte-identical). Never raises."""
    if not _flag_on():
        return False
    try:
        return regime_reliability(seat, regime, asof).get("status") == "scoring"
    except Exception:  # noqa: BLE001
        return False


def cap_influence(weight: float, stated_confidence: float,
                  max_share: float = MAX_QUORUM_SHARE) -> float:
    """Clamp the EFFECTIVE confidence (influence_weight x stated_confidence) so a single seat can
    never exceed `max_share` of the notional quorum. Returns the capped effective confidence in
    [0, max_share]. This is the hard guarantee that no seat — however well-calibrated — dominates."""
    try:
        eff = float(weight) * float(stated_confidence)
    except (TypeError, ValueError):
        return 0.0
    return round(max(0.0, min(max_share, eff)), 6)
