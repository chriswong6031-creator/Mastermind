"""FORGE ⇄ SENTINEL → NEXUS — a blind adversarial second opinion on a confirmed buy.

The existing research gate (FORGE = research_paper) builds the bull thesis. This module adds the
missing half: an INDEPENDENT bear. For a NEW conviction name the engine already wants, SENTINEL
argues the strongest reason NOT to own it — **blind to FORGE's verdict, score and paper**,
reasoning only over the engine decision matrix + macro regime + portfolio fit. NEXUS then
synthesises the two.

Doctrine-faithful, subtract-only: the committee can only DE-ESCALATE (confirm → trim → drop),
never escalate or rescue. The engine still owns selection, size, hard vetoes and the falsifier.
Every agent verdict is written to a durable, agent-labelled artifact so the outcome loop
(brain.outcomes) can later grade SENTINEL and NEXUS calls, not just FORGE's.

Degrades gracefully: no LLM → SENTINEL is skipped and the FORGE/engine decision passes through
unchanged. Enable/disable with env MASTERMIND_COMMITTEE (default: on when an LLM is available).
"""
from __future__ import annotations

import json
import os
from datetime import date
from pathlib import Path

from brain import client

_ARTIFACTS = Path(__file__).resolve().parent.parent / "data" / "committee"
_STANCES = ("SUPPORT", "CONDITIONAL", "OPPOSE")


def enabled() -> bool:
    """Committee runs only when explicitly allowed AND an LLM is reachable."""
    flag = os.environ.get("MASTERMIND_COMMITTEE", "1").strip().lower()
    if flag in ("0", "false", "no", ""):
        return False
    try:
        return client.available()
    except Exception:  # noqa: BLE001
        return False


# ─────────────────────────────────────────────────────────────────────────────
# SENTINEL — the blind adversary
# ─────────────────────────────────────────────────────────────────────────────
def _sentinel_input(ticker: str, engine_full: dict, regime: dict, portfolio_ctx: dict) -> dict:
    """The ONLY context SENTINEL sees. Deliberately excludes FORGE's paper, verdict, scores and
    recommendation, so its bear case is genuinely independent (no anchoring on the bull thesis)."""
    syn = (engine_full or {}).get("synthesis") or {}
    rows = (engine_full or {}).get("rows") or []
    lens = [{"lens": r.get("lens") or r.get("name"), "status": r.get("status"),
             "dir": r.get("direction") or r.get("dir"), "note": (r.get("note") or "")[:120]}
            for r in rows][:24]
    return {
        "ticker": ticker,
        "engine_decision_matrix": lens,
        "engine_synthesis": {k: syn.get(k) for k in
                             ("size_authority", "confluence", "vetoes", "divergences", "quad")},
        "macro_regime": {k: (regime or {}).get(k) for k in
                         ("quad", "quad_name", "liquidity_overlay")},
        "portfolio": portfolio_ctx,
    }


_SENTINEL_SYS = (
    "You are SENTINEL, the adversarial macro + portfolio risk officer on an investment committee. "
    "Assume the bull thesis is WRONG and your job is to find the strongest reason this stock should "
    "NOT enter THIS portfolio NOW. You are given ONLY the deterministic engine read, the macro "
    "regime, and the current book — you have NOT seen the underwriter's thesis or score, and must "
    "reason independently. Judge: macro-regime fit, sector/narrative health, crowding/concentration, "
    "correlation with existing holdings, whether the narrative is late, and whether a better "
    "expression of the same idea is already owned. "
    "Reply ONLY with JSON: {\"stance\": \"SUPPORT|CONDITIONAL|OPPOSE\", \"strongest_bear\": str, "
    "\"macro_fit\": str, \"portfolio_fit\": str, \"crowding\": str, \"narrative_maturity\": str, "
    "\"better_alternative\": str, \"conditions\": [str], \"confidence\": 0.0-1.0}. "
    "OPPOSE = should not enter now; CONDITIONAL = only under stated conditions; SUPPORT = no strong "
    "objection. Be blunt and specific; no hedging."
)


def _parse_json(txt: str) -> dict | None:
    if not txt:
        return None
    try:
        return json.loads(txt[txt.find("{"):txt.rfind("}") + 1])
    except Exception:  # noqa: BLE001
        return None


def sentinel_assess(ticker: str, engine_full: dict, regime: dict, portfolio_ctx: dict) -> dict | None:
    """Run the blind adversary. Returns a normalised verdict dict, or None if no LLM / failure."""
    if not client.available():
        return None
    payload = _sentinel_input(ticker, engine_full, regime, portfolio_ctx)
    try:
        txt, _meta = client.call_model(_SENTINEL_SYS, json.dumps(payload), role="deep", max_tokens=1100)
    except Exception:  # noqa: BLE001 — adversary is additive; never break the gate
        return None
    j = _parse_json(txt)
    if not j:
        return None
    stance = str(j.get("stance", "")).upper()
    if stance not in _STANCES:
        stance = "CONDITIONAL"
    try:
        conf = float(j.get("confidence", 0.5))
    except (TypeError, ValueError):
        conf = 0.5
    raw_conf = max(0.0, min(1.0, conf))
    # de-confidence SENTINEL by its realized calibration (≤1.0 shrink only): an adversary whose
    # high-confidence OPPOSE calls were historically wrong loses the power to hard-DROP a name (its
    # shrunk confidence won't clear NEXUS's 0.6 bar) — it can still trim. RAW is kept for grading.
    try:
        from brain import calibration as _calib
        conf = round(raw_conf * _calib.multiplier("sentinel"), 2)
    except Exception:  # noqa: BLE001
        conf = round(raw_conf, 2)
    return {
        "agent": "sentinel", "stance": stance, "confidence": conf,
        "raw_confidence": round(raw_conf, 2),
        "strongest_bear": str(j.get("strongest_bear", ""))[:600],
        "macro_fit": str(j.get("macro_fit", ""))[:300],
        "portfolio_fit": str(j.get("portfolio_fit", ""))[:300],
        "crowding": str(j.get("crowding", ""))[:200],
        "narrative_maturity": str(j.get("narrative_maturity", ""))[:200],
        "better_alternative": str(j.get("better_alternative", ""))[:200],
        "conditions": [str(c)[:160] for c in (j.get("conditions") or [])][:5],
        "blind": True,
    }


# ─────────────────────────────────────────────────────────────────────────────
# NEXUS — deterministic, subtract-only synthesis
# ─────────────────────────────────────────────────────────────────────────────
def nexus(breakdown: dict, sentinel: dict | None) -> dict:
    """Combine FORGE (breakdown) + SENTINEL into a final action. SUBTRACT-ONLY: the committee can
    confirm, trim, or drop a buy the engine+FORGE already approved — it can NEVER escalate size or
    rescue a name FORGE did not confirm. Pure function (no LLM) → exhaustively testable.

    Returns {action: confirm|trim|drop, scale: 0..1, lean, rationale, sentinel_stance}."""
    forge_confirmed = bool((breakdown or {}).get("confirmed"))
    combined = (breakdown or {}).get("combined")

    # The committee never rescues a name FORGE/the engine already blocked.
    if not forge_confirmed:
        return {"action": "drop", "scale": 0.0, "lean": "watch",
                "rationale": "FORGE/engine did not confirm; committee cannot rescue.",
                "sentinel_stance": (sentinel or {}).get("stance")}

    if not sentinel:                                   # no independent bear available
        return {"action": "confirm", "scale": 1.0, "lean": "add",
                "rationale": "FORGE confirmed; no adversary available — engine decision stands.",
                "sentinel_stance": None}

    stance = sentinel.get("stance", "CONDITIONAL")
    conf = float(sentinel.get("confidence", 0.5) or 0.5)
    bear = sentinel.get("strongest_bear", "")

    # REWARD / INFLUENCE (task #5) — a well-calibrated adversary (in THIS regime) earns the right to
    # de-risk HARDER. Its influence weight w ∈ [W_FLOOR, W_CEIL] (1.0 = best-calibrated) does two
    # subtract-only things: (a) LOWERS the OPPOSE→drop bar (a trusted adversary can veto on slightly
    # less stated confidence), and (b) DEEPENS the OPPOSE trim toward zero. It can do NEITHER additive
    # thing — it never raises a scale, never lifts the bar, never confirms a name FORGE didn't.
    # The drop bar is capped so no seat can drop on trivial confidence: bar ∈ [DROP_BAR_FLOOR, 0.6].
    # Flag-gated + cold-start inert → BYTE-IDENTICAL when OFF: w == 1.0, so bar == 0.6 and trim == 0.5,
    # reproducing the original branches exactly.
    drop_bar, trim_scale = 0.6, 0.5
    try:
        from brain import reputation as _rep
        # influence_active is True ONLY when the flag is ON AND SENTINEL is regime-scoring; a perfectly
        # calibrated seat earns w == 1.0 (== nominal), so we must gate on activeness, not on w != 1.0.
        if _rep.influence_active("sentinel"):
            w = _rep.influence_weight("sentinel")       # in [W_FLOOR, W_CEIL], 1.0 = best-calibrated
            # higher influence → lower bar (down to a floor) and deeper trim. cap_influence guarantees
            # the seat's effective vote can't exceed MAX_QUORUM_SHARE of the quorum (no domination).
            capped = _rep.cap_influence(w, conf)        # in [0, MAX_QUORUM_SHARE]
            lean_in = max(0.0, min(1.0, capped / _rep.MAX_QUORUM_SHARE))  # 0..1 "trust" this name
            drop_bar = round(max(0.45, 0.6 - 0.15 * lean_in), 4)          # ≤ 0.6, floored at 0.45
            trim_scale = round(0.5 * w, 4)                                # ≤ 0.5, deeper for trusted
    except Exception:  # noqa: BLE001 — influence is additive-safe; never break the synthesis
        drop_bar, trim_scale = 0.6, 0.5

    if stance == "OPPOSE" and conf >= drop_bar:
        return {"action": "drop", "scale": 0.0, "lean": "watch",
                "rationale": f"SENTINEL opposes with high conviction ({conf:.2f}): {bear}",
                "sentinel_stance": stance}
    if stance == "OPPOSE":
        return {"action": "trim", "scale": trim_scale, "lean": "hold",
                "rationale": f"SENTINEL opposes ({conf:.2f}) — size halved pending confirmation: {bear}",
                "sentinel_stance": stance}
    if stance == "CONDITIONAL":
        return {"action": "trim", "scale": 0.66, "lean": "add",
                "rationale": "SENTINEL conditional support — entered at reduced size with monitoring conditions.",
                "sentinel_stance": stance}
    return {"action": "confirm", "scale": 1.0, "lean": "add",
            "rationale": "FORGE confirmed and SENTINEL finds no strong independent objection.",
            "sentinel_stance": stance}


def assess(ticker: str, asof: str, *, engine_full: dict, breakdown: dict, regime: dict,
           portfolio_ctx: dict | None = None) -> dict:
    """Full committee pass for one confirmed candidate. Returns the NEXUS decision augmented with
    the SENTINEL verdict + an artifacts path. Subtract-only; never raises."""
    portfolio_ctx = portfolio_ctx or {}
    sentinel = None
    try:
        if enabled():
            sentinel = sentinel_assess(ticker, engine_full, regime, portfolio_ctx)
    except Exception:  # noqa: BLE001
        sentinel = None
    decision = nexus(breakdown, sentinel)
    artifacts = None
    try:
        artifacts = _write_artifacts(asof, ticker, breakdown, sentinel, decision)
    except Exception:  # noqa: BLE001 — persistence is best-effort
        artifacts = None
    return {**decision, "sentinel": sentinel, "artifacts": artifacts, "ran": sentinel is not None}


def _write_artifacts(asof: str, ticker: str, breakdown: dict, sentinel: dict | None,
                     decision: dict) -> str | None:
    d = _ARTIFACTS / (str(asof)[:10] or date.today().isoformat()) / (ticker or "UNK").upper()
    d.mkdir(parents=True, exist_ok=True)
    # FORGE is the existing research paper; record only the gate result here (the paper itself is
    # already persisted by research_paper.save_paper), so each committee folder is self-describing.
    (d / "forge.json").write_text(json.dumps({"agent": "forge", **(breakdown or {})}, indent=2, default=str))
    if sentinel is not None:
        (d / "sentinel.json").write_text(json.dumps(sentinel, indent=2, default=str))
    (d / "nexus.json").write_text(json.dumps({"agent": "nexus", **decision}, indent=2, default=str))
    debate = [f"# Committee — {ticker} ({asof})", "",
              f"**FORGE (underwriter):** combined {breakdown.get('combined')} "
              f"(engine {breakdown.get('engine_score')} + research {breakdown.get('research_score')}), "
              f"viability {breakdown.get('viability')}, confirmed={breakdown.get('confirmed')}", ""]
    if sentinel:
        debate += [f"**SENTINEL (blind adversary):** {sentinel['stance']} "
                   f"(confidence {sentinel['confidence']})",
                   f"- Strongest bear: {sentinel.get('strongest_bear', '')}",
                   f"- Macro fit: {sentinel.get('macro_fit', '')}",
                   f"- Portfolio fit: {sentinel.get('portfolio_fit', '')}",
                   f"- Better alternative: {sentinel.get('better_alternative', '')}", ""]
    else:
        debate += ["**SENTINEL:** not run (no LLM available).", ""]
    debate += [f"**NEXUS (synthesis):** {decision['action'].upper()} · scale {decision['scale']} · "
               f"lean {decision['lean']}", f"- {decision['rationale']}"]
    (d / "debate.md").write_text("\n".join(debate))
    return str(d)
