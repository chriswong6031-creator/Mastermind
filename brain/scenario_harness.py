"""SCENARIO HARNESS (#9) — leakage-CONTROLLED stress-testing of the LLM, NOT in-sample replay.

Replaying pre-cutoff history to a frozen LLM measures MEMORIZATION, not skill — the model already knows
how those dates resolved (the research: ~37% of in-window 'predictive power' is recall that collapses
post-cutoff). This module is the machinery to test the Brain HONESTLY:

  1. POST-CUTOFF gate — only situations AFTER the model's knowledge cutoff are clean (the model can't
     have memorized a future it never saw). The Brain books began after the cutoff, so their live
     forward-graded decisions ALREADY are this honest test; this formalizes + reuses the gate.
  2. COUNTERFACTUAL constructors — perturb a real situation so the memorized outcome no longer applies
     (remove the catalyst / swap the price path for a random walk / anonymize the entity), forcing
     CAUSAL reasoning rather than recall — which also sidesteps the post-cutoff sample-scarcity problem.
  3. LOOKAHEAD-PROPENSITY (LAP) screen — a cheap heuristic for how contaminated a scenario is (pre-cutoff
     date, famous mega-cap, famous crash window), so high-LAP items are discarded BEFORE grading.

Pure DATA utilities — NO LLM here. The actual Opus-on-scenario grading is an INJECTABLE hook (`grade`
takes a caller fn), so it's LLM-free + testable by default and, when wired, runs on a CHEAP tier
(never Opus for the bulk; config/agents.yml — don't waste Opus). Degrade-safe; never raises.

v1 = the leakage-control core (gate + constructors + LAP). v2 = wire a cheap-tier caller + a Min-K%-Prob
contamination probe + persist a scored scenario set.
"""
from __future__ import annotations

from datetime import date

_CUTOFF = "2026-01-01"          # the reasoning model's knowledge cutoff (Jan 2026); post is the clean window
_LAP_MAX = 0.5                  # scenarios at/above this contamination score are discarded before grading
# famous mega-caps whose moves are heavily documented → higher memorization risk
_FAMOUS = {"AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "GOOG", "META", "TSLA", "SPY", "QQQ", "AVGO", "NFLX"}
# famous crash windows (well-documented → high recall risk) as (start, end) ISO
_CRASH_WINDOWS = [("2008-08-01", "2009-03-31"), ("2020-02-15", "2020-04-30"), ("2022-01-01", "2022-10-31")]
_SCENARIO_TYPES = ("catalyst_removed", "anonymized", "pricepath_swapped")


def is_post_cutoff(asof, cutoff: str = _CUTOFF) -> bool:
    """True iff `asof` is strictly after the knowledge cutoff (the only window where the LLM can't have
    memorized the outcome). Bad/empty dates → False (treated as not-clean)."""
    try:
        return date.fromisoformat(str(asof)[:10]) > date.fromisoformat(cutoff)
    except Exception:  # noqa: BLE001
        return False


def filter_post_cutoff(records: list, cutoff: str = _CUTOFF) -> list:
    """Keep only records whose `asof` is post-cutoff — the honest out-of-sample set."""
    return [r for r in (records or []) if isinstance(r, dict) and is_post_cutoff(r.get("asof"), cutoff)]


def _in_crash_window(asof) -> bool:
    try:
        d = str(asof)[:10]
        return any(a <= d <= b for a, b in _CRASH_WINDOWS)
    except Exception:  # noqa: BLE001
        return False


def lookahead_propensity(name: str, asof) -> float:
    """A cheap [0,1] CONTAMINATION score (higher = more likely the LLM's read is memorized, not reasoned):
    pre-cutoff date dominates, a famous mega-cap adds risk, a famous crash window adds risk. Post-cutoff,
    obscure name, calm window → 0.0 (clean)."""
    score = 0.0
    if not is_post_cutoff(asof):
        score += 0.6                                  # pre-cutoff: the outcome is in the training data
    if (name or "").upper().strip() in _FAMOUS:
        score += 0.25
    if _in_crash_window(asof):
        score += 0.25
    return round(min(1.0, score), 3)


def counterfactuals(name: str, asof, context: dict | None = None) -> list[dict]:
    """Build perturbed variants of a real situation so the memorized outcome no longer applies — each a
    DATA descriptor a (v2) cheap-tier grader would render into a prompt. Each carries its own LAP score
    (the perturbation lowers contamination vs the raw situation, but the date still matters)."""
    ctx = context or {}
    base_lap = lookahead_propensity(name, asof)
    out = []
    for st in _SCENARIO_TYPES:
        s = {"type": st, "subject": (name or "").upper().strip(), "asof": str(asof)[:10],
             "post_cutoff": is_post_cutoff(asof)}
        if st == "catalyst_removed":
            s["perturbation"] = "Present the setup WITHOUT the known catalyst/news — does the read survive?"
            s["lap"] = base_lap
        elif st == "anonymized":
            s["perturbation"] = "Replace the ticker/name with 'Company A' and strip identifying details."
            s["subject"] = "Company A"
            s["lap"] = round(max(0.0, base_lap - 0.25), 3)   # anonymizing removes the famous-name leak
        else:  # pricepath_swapped
            s["perturbation"] = "Swap the realized forward path for a random walk — the memorized outcome no longer holds."
            s["lap"] = round(max(0.0, base_lap - 0.4), 3)    # a fictional path can't be recalled
        if ctx:
            s["context"] = ctx
        out.append(s)
    return out


def grade(scenarios: list, caller=None, lap_max: float = _LAP_MAX) -> dict:
    """Grade scenarios, DISCARDING contaminated ones (lap >= lap_max) first. `caller(scenario) -> reaction`
    is the injectable (cheap-tier) LLM hook; when None (default), returns the clean scenario set ready to
    grade WITHOUT any LLM call. Never raises."""
    scen = scenarios or []
    clean = [s for s in scen if isinstance(s, dict) and float(s.get("lap", 0.0)) < lap_max]
    out = {"n_total": len(scen), "n_clean": len(clean), "lap_max": lap_max,
           "discarded_high_lap": len(scen) - len(clean), "graded": []}
    if caller is None:
        out["status"] = "no_caller"
        out["note"] = "inject a cheap-tier LLM caller(scenario)->reaction to grade; v1 returns the clean set"
        out["clean"] = clean
        return out
    for s in clean:
        try:
            out["graded"].append({**s, "reaction": caller(s)})
        except Exception:  # noqa: BLE001 — one scenario can't sink the batch
            continue
    out["status"] = "graded"
    return out
