"""Empirical calibration — the safe, auto-closing self-learning loop.

Each reasoning agent states a confidence. Calibration measures whether that confidence MATCHED
realized outcomes and emits a per-agent multiplier in (FLOOR, 1.0] that SHRINKS overconfidence
toward reality (de-confidencing only — it never inflates). It is the *safe* half of self-learning:
it changes NO prompts, NO sizing rules, NO permissions — it only re-weights confidence, is bounded
and reversible, and stays inert until an agent has MIN_N resolved decisions (an auditor with 10
trades has opinions; with 100 timestamped ones it has evidence).

  FORGE     graded from the thesis ledger (prob_correct) vs the realized falsifier outcome.
  SENTINEL  graded from committee artifacts (stance + confidence) vs the name's realized rel-return:
            an OPPOSE that preceded underperformance was right; a SUPPORT that preceded
            outperformance was right.

`multiplier(agent)` is what agents read at decision time to de-confidence themselves; the loop is
refreshed (recomputed + persisted) each build from the latest resolved outcomes.
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_PATH = _ROOT / "data" / "brain" / "calibration.json"
_COMMITTEE = _ROOT / "data" / "committee"

MIN_N = 12          # below this many resolved decisions, do not adjust (cold-start safety)
FLOOR = 0.5         # never shrink confidence below half (bounded)


def _mult(reliability: float | None, mean_conf: float | None, n: int) -> float:
    """De-confidence-only multiplier: shrink toward realized reliability, clamp to [FLOOR, 1.0]."""
    if n < MIN_N or not mean_conf or reliability is None:
        return 1.0
    return round(max(FLOOR, min(1.0, reliability / mean_conf)), 3)


def _summarize(rows: list[tuple[int, float]]) -> dict:
    n = len(rows)
    if not n:
        return {"n": 0, "reliability": None, "mean_confidence": None,
                "multiplier": 1.0, "status": "building"}
    rel = sum(o for o, _ in rows) / n
    mc = sum(c for _, c in rows) / n
    return {"n": n, "reliability": round(rel, 3), "mean_confidence": round(mc, 3),
            "multiplier": _mult(rel, mc, n), "status": "scoring" if n >= MIN_N else "building"}


def _forge_reliability(asof: date) -> dict:
    """FORGE: realized falsifier hit-rate vs stated prob_correct, over resolved theses."""
    from brain import outcomes
    from brain.ledger import all_theses
    rows: list[tuple[int, float]] = []
    for t in all_theses():
        cb = t.get("check_by")
        try:
            due = cb and date.fromisoformat(str(cb)[:10]) <= asof
        except Exception:  # noqa: BLE001
            due = False
        if not due:
            continue
        lab = outcomes.label_thesis(t, asof)
        if not (lab and lab.get("resolved") and lab.get("rel_return") is not None):
            continue
        # grade against the RAW (pre-de-confidencing) probability so the loop measures the model's
        # native overconfidence and converges to a fixed point — grading the already-shrunk value
        # would oscillate (shrink → look calibrated → un-shrink → overconfident again).
        pc = t.get("raw_prob_correct", t.get("prob_correct"))
        if pc is None:
            continue
        chk = (t.get("falsifier") or {}).get("check") or {}
        r, thr, op = lab["rel_return"], chk.get("threshold", 0), chk.get("op", "<")
        miss = (r < thr) if op == "<" else (r > thr)
        rows.append((0 if miss else 1, float(pc)))
    return _summarize(rows)


def _sentinel_reliability(asof: date) -> dict:
    """SENTINEL: was its directional stance right? OPPOSE→correct if the name underperformed;
    SUPPORT→correct if it outperformed. CONDITIONAL is non-directional and excluded."""
    if not _COMMITTEE.exists():
        return _summarize([])
    from brain import outcomes
    from brain.ledger import all_theses
    idx = {}
    for t in all_theses():
        subj = ((t.get("entry_levels") or {}).get("ticker") or t.get("subject") or "").upper()
        d = str(t.get("state_asof") or "")[:10]
        if subj and d:
            idx[(subj, d)] = t
    rows: list[tuple[int, float]] = []
    try:
        for datedir in _COMMITTEE.iterdir():
            if not datedir.is_dir():
                continue
            for tdir in datedir.iterdir():
                sf = tdir / "sentinel.json"
                if not sf.exists():
                    continue
                try:
                    s = json.loads(sf.read_text())
                except Exception:  # noqa: BLE001
                    continue
                stance, conf = s.get("stance"), s.get("raw_confidence", s.get("confidence"))
                if stance not in ("OPPOSE", "SUPPORT") or conf is None:
                    continue
                th = idx.get((tdir.name.upper(), datedir.name))
                if not th:
                    continue
                lab = outcomes.label_thesis(th, asof)
                if not (lab and lab.get("resolved") and lab.get("rel_return") is not None):
                    continue
                r = lab["rel_return"]
                correct = (stance == "OPPOSE" and r < 0) or (stance == "SUPPORT" and r >= 0)
                rows.append((1 if correct else 0, float(conf)))
    except Exception:  # noqa: BLE001 — calibration is best-effort, never fatal
        pass
    return _summarize(rows)


def compute(asof: date | None = None) -> dict:
    asof = asof or date.today()
    try:
        forge = _forge_reliability(asof)
    except Exception:  # noqa: BLE001
        forge = _summarize([])
    try:
        sentinel = _sentinel_reliability(asof)
    except Exception:  # noqa: BLE001
        sentinel = _summarize([])
    return {"as_of": asof.isoformat(), "min_n": MIN_N, "floor": FLOOR,
            "agents": {"forge": forge, "sentinel": sentinel}}


def persist(asof: date | None = None) -> dict:
    """Recompute + write calibration.json. Returns the computed block. Never raises."""
    block = compute(asof)
    try:
        _PATH.parent.mkdir(parents=True, exist_ok=True)
        _PATH.write_text(json.dumps(block, indent=2))
    except Exception:  # noqa: BLE001
        pass
    return block


def load() -> dict:
    try:
        return json.loads(_PATH.read_text())
    except Exception:  # noqa: BLE001
        return {}


def multiplier(agent: str) -> float:
    """The de-confidencing multiplier an agent applies to its stated confidence (1.0 if unknown)."""
    try:
        m = ((load().get("agents") or {}).get(agent) or {}).get("multiplier")
        return float(m) if m is not None else 1.0
    except Exception:  # noqa: BLE001
        return 1.0
