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


# Desk-quorum modes (MASTERMIND_DESK_QUORUM) — the staged ladder that admits the TECHNICIAN
# entry-timing seat into the live buy decision. "off" is the default and is BYTE-IDENTICAL to base.
_DESK_QUORUM_MODES = ("off", "shadow", "enforce")


def desk_quorum_mode() -> str:
    """Return the desk-quorum mode: ``off`` | ``shadow`` | ``enforce`` (default ``off``).

    THE STAGED LADDER for admitting the TECHNICIAN seat into the live buy gate:
      * ``off`` (default) → the technician seat is NOT run, ``nexus()`` keeps its 2-arg behaviour,
        no ledger write. ZERO cost, ZERO behaviour change — byte-identical to base.
      * ``shadow``        → run the technician seat + LOG its verdict to the pipeline ledger, but
        DO NOT change the book (pure observability; trading byte-identical).
      * ``enforce``       → additionally APPLY the verdict SUBTRACT-ONLY (``wait`` → park the name;
        ``staged_starter`` → cap scale at 0.7). Never forces a buy, never escalates.

    Fail-soft: any unrecognised / empty value degrades to ``off`` (the safe default), so a typo in
    the env can only ever make the seat inert — it can never accidentally arm enforcement."""
    val = os.environ.get("MASTERMIND_DESK_QUORUM", "off").strip().lower()
    return val if val in _DESK_QUORUM_MODES else "off"


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
# TECHNICIAN — subtract-only entry-timing ratchet applied on top of the NEXUS action
# ─────────────────────────────────────────────────────────────────────────────
# The TECHNICIAN verdict caps (never lifts) the SUBTRACT-ONLY scale each verdict may leave a name at:
#   wait           → 0.0  (park the name — the seat's strongest withhold)
#   staged_starter → 0.7  (a starter only — cap the scale here)
#   now / unknown  → 1.0  (no cap — the seat leaves NEXUS's action untouched)
# Because these are CAPS applied with min(), the seat can only ever RATCHET DOWN: a "now" verdict's
# 1.0 cap is a no-op (min(x, 1.0) == x for any x ≤ 1), so the seat can never raise a scale.
_TECH_SCALE_CAP = {"wait": 0.0, "staged_starter": 0.7}


def apply_technician(action: str, scale: float, technician_verdict: str | None) -> tuple[str, float]:
    """Apply the TECHNICIAN entry-timing verdict to a (action, scale) pair — SUBTRACT-ONLY.

    Pure + total; the single source of truth for how a technician verdict ratchets an action, so the
    phase2 gate and the ``nexus`` extension share ONE tested decision. Returns the (possibly reduced)
    ``(action, scale)``:

      * ``wait``           → force ``("drop", 0.0)`` — park the name (scale 0).
      * ``staged_starter`` → cap the scale at 0.7; if that lowers it, the action becomes ``"trim"``.
      * ``now`` / None / unknown → UNCHANGED. The cap for "now" is 1.0, i.e. ``min(scale, 1.0)`` which
        equals ``scale`` for any legal scale — so a "now" (or absent) verdict can NEVER raise the scale.

    CAN ONLY RATCHET DOWN: the returned scale is always ``<= scale`` and the action never escalates
    (confirm/trim → drop/trim only; never the reverse). ``None`` (seat not run) is a pure no-op."""
    if technician_verdict is None:
        return action, scale
    v = str(technician_verdict).strip().lower()
    cap = _TECH_SCALE_CAP.get(v)
    if cap is None:                       # "now" or any unrecognised verdict → no change (no ratchet up)
        return action, scale
    new_scale = min(scale, cap)
    if new_scale <= 0.0:
        return "drop", 0.0
    if new_scale < scale:
        return "trim", round(new_scale, 4)
    return action, scale


def technician_gate(mode: str, action: str, scale: float,
                    technician_verdict: str | None) -> dict:
    """Mode-aware, SUBTRACT-ONLY resolution of a technician verdict for the phase2 buy gate.

    Factored out of the phase2 loop so the park/scale decision is a PURE, exhaustively-testable
    function (the loop only translates the result into the existing park+continue / scale paths).
    The single source of truth for how the desk-quorum ladder turns a verdict into a book effect:

      * ``off``     → always a NO-OP: ``{"park": False, "scale": scale, "action": action}`` —
        the input ``(action, scale)`` is returned unchanged (byte-identical to base).
      * ``shadow``  → also a NO-OP on the BOOK (log-only observability): the verdict is NOT applied,
        so the returned ``(action, scale)`` is identical to the input. (The caller still logs it.)
      * ``enforce`` → apply the verdict SUBTRACT-ONLY via ``apply_technician``: ``wait`` → park
        (``park=True``, scale 0); ``staged_starter`` → cap scale at 0.7; ``now``/None/unknown →
        unchanged. Never escalates, never forces a buy.

    ``park`` is True ONLY in enforce mode on a ``wait`` verdict (the ``("drop", 0.0)`` outcome). A
    seat FAILURE surfaces to the caller as a ``None`` verdict → no-op (a failed seat NEVER parks a
    name; only a real ``wait`` verdict parks). Pure; never raises."""
    if mode not in ("shadow", "enforce"):
        # "off" (or any unrecognised mode) → inert no-op, byte-identical to base.
        return {"park": False, "scale": scale, "action": action}
    if mode == "shadow":
        # observability only — the verdict is logged by the caller but NEVER changes the book.
        return {"park": False, "scale": scale, "action": action}
    # enforce: subtract-only application of the verdict.
    new_action, new_scale = apply_technician(action, scale, technician_verdict)
    park = (new_action == "drop" and new_scale <= 0.0)
    return {"park": park, "scale": new_scale, "action": new_action}


# ─────────────────────────────────────────────────────────────────────────────
# NEXUS — deterministic, subtract-only synthesis
# ─────────────────────────────────────────────────────────────────────────────
def nexus(breakdown: dict, sentinel: dict | None, *, technician: dict | None = None) -> dict:
    """Combine FORGE (breakdown) + SENTINEL into a final action. SUBTRACT-ONLY: the committee can
    confirm, trim, or drop a buy the engine+FORGE already approved — it can NEVER escalate size or
    rescue a name FORGE did not confirm. Pure function (no LLM) → exhaustively testable.

    ``technician`` (optional, default None) is the entry-timing seat's verdict dict
    ({"verdict": "now|staged_starter|wait", ...}). When ``None`` the result is BYTE-IDENTICAL to the
    original 2-arg ``nexus``: the technician ratchet is skipped entirely. When a verdict is passed the
    final (action, scale) is routed through ``apply_technician`` — SUBTRACT-ONLY, applied on top of the
    SENTINEL synthesis (both are subtract-only; the STRICTER wins because a cap of 0.0/0.7 can only
    lower a scale SENTINEL already produced). "now" → no change; "staged_starter" → cap 0.7; "wait" →
    drop/park (scale 0). The technician can only ratchet DOWN, never up.

    Returns {action: confirm|trim|drop, scale: 0..1, lean, rationale, sentinel_stance}."""
    decision = _nexus_synthesis(breakdown, sentinel)
    if technician is None:
        return decision           # BYTE-IDENTICAL to the original 2-arg path — no ratchet, no mutation.
    verdict = technician.get("verdict") if isinstance(technician, dict) else technician
    new_action, new_scale = apply_technician(decision["action"], decision["scale"], verdict)
    if (new_action, new_scale) == (decision["action"], decision["scale"]):
        return decision           # "now"/unknown/absent verdict — the seat left the action untouched.
    out = dict(decision)
    out["action"], out["scale"] = new_action, new_scale
    out["lean"] = "watch" if new_action == "drop" else out.get("lean")
    out["technician_verdict"] = str(verdict) if verdict is not None else None
    out["rationale"] = (f"{out.get('rationale', '')} | TECHNICIAN {verdict}: "
                        f"{'parked (wait)' if new_action == 'drop' else f'scale capped at {new_scale}'}").strip(" |")
    return out


def _nexus_synthesis(breakdown: dict, sentinel: dict | None) -> dict:
    """The original SENTINEL-only synthesis (unchanged). Factored out so ``nexus`` can layer the
    subtract-only technician ratchet on top without touching this proven branch structure."""
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
