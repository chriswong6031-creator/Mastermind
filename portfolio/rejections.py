"""Off-policy / counterfactual log of REJECTED names — the desk's negative space, forward-graded.

A deterministic, greedy desk only records what it BOUGHT, so it can never answer two questions that
bound its edge: "did the gate veto winners?" (false-negative / veto-regret) and "what would an
alternative selection rule have earned?" (off-policy value). This module logs EVERY name the desk
rejected — conviction veto / research hold / committee drop / timing withhold — with the logging
policy's SELECTION PROPENSITY at decision time, then forward-grades each name's rel_return vs SPY
(leakage-free, reusing brain.outcomes.label_thesis: it caps the price window at asof). Two reads:

  * veto-regret / false-negative — how often rejected names beat SPY, split by reject stage. This is
    identifiable from outcomes ALONE (no exploration needed) — the "veto-shadow cohort".
  * inverse-propensity / doubly-robust off-policy VALUE of an alternative selection rule — this needs a
    NON-ZERO logged propensity on rejected names. A greedy desk logs propensity 0.0 (IPS undefined),
    so the propensity field is captured from day one and a flag-gated ε-exploration lever
    (MASTERMIND_SELECTION_EXPLORE / MASTERMIND_EXPLORE_EPS, default OFF) makes it identifiable once
    armed. See docs/design/desk/OFF_POLICY_EXPLORATION.md for the budget decision.

Isolated under data/shadow/rejections/ (NEVER the prod conviction ledger); no LLM, no look-ahead.
Best-effort throughout — never breaks the build.
"""
from __future__ import annotations

import hashlib
import json
import os
from collections import defaultdict
from datetime import date
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_DIR = _ROOT / "data" / "shadow" / "rejections"
_LEDGER = _DIR / "ledger.jsonl"

_HORIZON = 21              # business days — matches the conviction falsifier (what these names competed for)
_MAX_RESOLVED = 60_000     # soft cap so the ledger can't grow without bound
_MIN_RESOLVED = 20         # below this the scorecard is honestly 'building'
# Stages eligible for ε-EXPLORATION (an actual floor-weight buy of a rejected name, to make off-policy
# value estimable). ONLY the truly-borderline "confirmed-then-dropped" stages: a committee drop and a
# timing withhold both already CLEARED conviction + research-confirm and were dropped by a soft overlay.
# research_hold spans non-confirmed names, and hard conviction vetoes (parabolic/distress/downtrend) are
# safety rules — neither is ever explored. See docs/design/desk/OFF_POLICY_EXPLORATION.md.
_EXPLORE_STAGES = ("committee_drop", "timing_withhold")


# ─────────────────────────────────────────────────────────────────────────────
# exploration flags (default OFF — see the design note). Until armed, every reject logs
# propensity 0.0 / policy 'deterministic' and the veto-regret read still works.
# ─────────────────────────────────────────────────────────────────────────────
def _explore_enabled() -> bool:
    return os.environ.get("MASTERMIND_SELECTION_EXPLORE", "0").strip().lower() in ("1", "true", "yes", "on")


def _explore_eps() -> float:
    if not _explore_enabled():
        return 0.0
    try:
        return max(0.0, min(0.25, float(os.environ.get("MASTERMIND_EXPLORE_EPS", "0.05"))))
    except (TypeError, ValueError):
        return 0.05


def _explore_weight() -> float:
    """Target weight for an EXPLORED buy. Must clear master's new-position floor
    (MASTERMIND_MIN_POSITION_FRAC, default 0.5% NAV) or paper_account would silently drop it as
    sub-floor — so default 0.6% and never below 0.5%."""
    try:
        return max(0.005, min(0.05, float(os.environ.get("MASTERMIND_EXPLORE_WEIGHT", "0.006"))))
    except (TypeError, ValueError):
        return 0.006


def _draw(ticker: str, asof) -> float:
    """A DETERMINISTIC uniform draw in [0,1) from (ticker, asof). Deterministic (not random.random) so a
    re-run of the same build day makes the SAME exploration choice — idempotent across phase2 reruns and
    the loop_maintenance re-grade, and reproducible/auditable. Uses sha1 (hash() is process-salted)."""
    h = hashlib.sha1(f"{(ticker or '').upper()}|{str(asof)[:10]}".encode()).hexdigest()
    return int(h[:8], 16) / float(0xFFFFFFFF)


def explore_buy(ticker: str, asof, stage: str) -> bool:
    """Should the desk EXPLORE-buy this rejected name? True iff exploration is armed
    (MASTERMIND_SELECTION_EXPLORE), the stage is borderline (committee_drop / timing_withhold), and the
    deterministic draw falls under ε. Default OFF → always False → the live buy path is byte-identical."""
    eps = _explore_eps()
    if not eps or stage not in _EXPLORE_STAGES:
        return False
    return _draw(ticker, asof) < eps


def _propensity(stage: str, eps: float) -> float:
    """The logged exploration probability P(explore-buy | candidate) under the CURRENT policy. 0.0 for a
    deterministic greedy desk (the name was not selectable). With ε armed, BORDERLINE rejects
    (committee_drop / timing_withhold) carry ε; hard vetoes and research holds stay 0.0. OPE value
    estimation needs this > 0 on the explored cohort (see the design note)."""
    if eps and stage in _EXPLORE_STAGES:
        return round(float(eps), 4)
    return 0.0


def _held_stage(reason: str) -> str:
    """Classify a research_held item by its reason prefix (set in bot/phase2)."""
    r = (reason or "").lower()
    if r.startswith("timing withhold"):
        return "timing_withhold"
    if r.startswith("committee"):
        return "committee_drop"
    return "research_hold"


# ─────────────────────────────────────────────────────────────────────────────
# isolated ledger
# ─────────────────────────────────────────────────────────────────────────────
def _load_ledger() -> list:
    try:
        return [json.loads(l) for l in _LEDGER.read_text().splitlines() if l.strip()]
    except Exception:  # noqa: BLE001
        return []


def _save_ledger(rows: list) -> None:
    openr = [r for r in rows if r.get("status") == "open"]
    resr = [r for r in rows if r.get("status") != "open"]
    if len(resr) > _MAX_RESOLVED:
        resr = sorted(resr, key=lambda r: r.get("resolved_on") or "")[-_MAX_RESOLVED:]
    try:
        _DIR.mkdir(parents=True, exist_ok=True)
        _LEDGER.write_text("".join(json.dumps(r, default=str) + "\n" for r in (openr + resr)))
    except Exception:  # noqa: BLE001
        pass


# ─────────────────────────────────────────────────────────────────────────────
# leakage-free forward grader (delegates to brain.outcomes.label_thesis)
# ─────────────────────────────────────────────────────────────────────────────
def _grade(ticker: str, entry_iso: str, horizon: int, asof: date) -> float | None:
    """rel_return (ticker − SPY over `horizon` bdays from entry), or None if unresolved. Anchors to the
    entry-date close and never reads a price past asof — exactly the prod/desk_ab labeler. Never raises."""
    try:
        from brain import outcomes
        lab = outcomes.label_thesis({
            "id": f"rej:{ticker}:{str(entry_iso)[:10]}", "subject": ticker,
            "state_asof": str(entry_iso)[:10], "horizon_d": int(horizon),
            "entry_levels": {"ticker": ticker},
            "falsifier": {"check": {"kind": "rel_return", "subject_ticker": ticker, "vs": "SPY",
                                    "horizon_d": int(horizon), "op": "<", "threshold": -0.05}},
        }, asof)
        if lab and lab.get("resolved") and lab.get("rel_return") is not None:
            return round(float(lab["rel_return"]), 4)
    except Exception:  # noqa: BLE001
        pass
    return None


# ─────────────────────────────────────────────────────────────────────────────
# driver — open one row per newly-rejected name (deduped while open), grade matured ones
# ─────────────────────────────────────────────────────────────────────────────
def record(asof: str, rejected: list | None = None, held: list | None = None,
           explored: list | None = None) -> dict:
    """Sync the rejection ledger: open one row per newly-rejected name (deduped while open, like the
    prod ledger), then forward-grade every open row and resolve the matured ones. `rejected` is the
    conviction-gate veto list ({ticker, reason, vetoes, bear, confluence}); `held` is the research-gate
    hold list ({ticker, reason, **research_block, [committee]}); `explored` is the borderline rejects the
    desk ε-EXPLORE-BOUGHT this run ({ticker, stage, reason, ...}) — logged action='explored_buy' at
    propensity ε so their forward outcome makes the off-policy value estimable. All may be omitted (a
    carried day just grades open rows forward). Returns coverage. Best-effort; never raises."""
    try:
        asof_iso = str(asof)[:10]
        ledger = _load_ledger()
        open_subj = {r["ticker"] for r in ledger if r.get("status") == "open"}
        eps = _explore_eps()
        # (action, default_stage, items) — explored buys are NOT rejects: they were actually bought.
        batches = (
            ("reject", "conviction_veto", rejected or []),
            ("reject", None, held or []),
            ("explored_buy", None, explored or []),
        )
        for action, default_stage, items in batches:
            for it in items:
                if not isinstance(it, dict):
                    continue
                tk = (it.get("ticker") or "").upper().strip()
                if not tk or tk in open_subj:
                    continue
                st = it.get("stage") or default_stage or _held_stage(it.get("reason"))
                score = it.get("combined")
                if score is None:
                    score = it.get("confluence")
                prop = round(float(eps), 4) if action == "explored_buy" else _propensity(st, eps)
                ledger.append({
                    "id": f"{asof_iso}-{tk}-rej", "ticker": tk, "asof": asof_iso,
                    "action": action, "stage": st, "reason": (it.get("reason") or "")[:200],
                    "score": score, "confluence": it.get("confluence"),
                    "propensity": prop,
                    "policy": "epsilon_greedy" if eps else "deterministic",
                    "horizon_d": _HORIZON, "status": "open", "realized": None, "resolved_on": None})
                open_subj.add(tk)
        asof_d = date.fromisoformat(asof_iso)
        for r in ledger:
            if r.get("status") != "open":
                continue
            rel = _grade(r["ticker"], r["asof"], int(r.get("horizon_d") or _HORIZON), asof_d)
            if rel is not None:
                r["status"], r["realized"], r["resolved_on"] = "resolved", rel, asof_iso
        _save_ledger(ledger)
        return coverage(ledger)
    except Exception:  # noqa: BLE001
        return coverage()


# ─────────────────────────────────────────────────────────────────────────────
# coverage + the veto-regret scorecard (the false-negative read)
# ─────────────────────────────────────────────────────────────────────────────
def coverage(ledger: list | None = None) -> dict:
    ledger = ledger if ledger is not None else _load_ledger()
    resolved = [r for r in ledger if r.get("status") == "resolved"]
    by_stage: dict = defaultdict(int)
    for r in ledger:
        by_stage[r.get("stage")] += 1
    dates = sorted({r.get("asof") for r in ledger if r.get("asof")})
    return {"n_total": len(ledger), "n_open": len(ledger) - len(resolved), "n_resolved": len(resolved),
            "by_stage": dict(by_stage), "n_entry_dates": len(dates),
            "first_date": dates[0] if dates else None, "last_date": dates[-1] if dates else None,
            "exploration_armed": _explore_enabled(), "epsilon": _explore_eps()}


def scorecard(asof: str | None = None) -> dict:
    """The veto-regret / false-negative read over RESOLVED REJECTED names: how often a name the desk
    rejected went on to BEAT SPY (a false negative the gate should have caught), split by reject stage —
    plus the EXPLORED-BUY cohort (borderline rejects the desk actually ε-bought) so you can see whether
    exploring them paid. Descriptive (rates + n), not a significance claim — overlapping windows make it
    serially correlated, so it reports counts, not a t-stat. 'building' until enough resolves."""
    ledger = _load_ledger()
    res = [r for r in ledger if r.get("status") == "resolved" and r.get("realized") is not None]
    rejects = [r for r in res if r.get("action", "reject") == "reject"]
    explored = [r for r in res if r.get("action") == "explored_buy"]
    out = {"status": "building", "n_resolved": len(res), "n_open": len(ledger) - len(res),
           "horizon_d": _HORIZON, "veto_regret_rate": None, "avg_rel_return": None, "by_stage": {},
           "explored_cohort": {"n": 0, "paid_rate": None, "avg_rel": None},
           "note": "veto_regret_rate = fraction of REJECTED names that beat SPY (gate said no, tape said yes)"}
    if rejects:
        rels = [r["realized"] for r in rejects]
        out["avg_rel_return"] = round(sum(rels) / len(rels), 4)
        out["veto_regret_rate"] = round(sum(1 for x in rels if x > 0) / len(rels), 3)
        for st in sorted({r.get("stage") for r in rejects if r.get("stage")}):
            b = [r["realized"] for r in rejects if r.get("stage") == st]
            out["by_stage"][st] = {"n": len(b),
                                   "beat_spy_rate": round(sum(1 for x in b if x > 0) / len(b), 3),
                                   "avg_rel": round(sum(b) / len(b), 4)}
    if explored:
        erels = [r["realized"] for r in explored]
        out["explored_cohort"] = {"n": len(explored),
                                  "paid_rate": round(sum(1 for x in erels if x > 0) / len(erels), 3),
                                  "avg_rel": round(sum(erels) / len(erels), 4)}
    if res:
        out["status"] = "scoring" if len(res) >= _MIN_RESOLVED else "building"
    if isinstance(asof, str):
        out["as_of"] = asof[:10]
    return out


def summary(asof: str | None = None) -> dict:
    """Coverage + veto-regret scorecard for /api/rejections."""
    return {"coverage": coverage(), "scorecard": scorecard(asof), "horizon_d": _HORIZON}
