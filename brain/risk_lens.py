"""RISK GOVERNOR — make risk a GOVERNING input for the free-form Opus Brain books.

The free-form Brain books (US / CN / HK / Heavyweight) carry an act / lean-in imperative and
treat risk as a sidecar. On 2026-06-23 (SMH −7%, DRAM −14%, an active risk event) they ADDED to the
most crowded, correlated bubble names (SMH, EME, APH, ANET) into contracting liquidity while their
OWN write-ups admitted "contracting liquidity, risk event, no new conviction" — the US Brain even
SAW the risk, expressed it as ONE token 5% hedge (MCY), then went ~95% into the correlated bet.

ROOT CAUSE: the brains are risk-AWARE but not risk-GOVERNED. Risk is a per-name sidecar, not a
constraint on the whole book's gross + correlation, and the persona has no risk veto over the
lean-in. This module fixes that by (1) reading the live risk tape and deriving a coarse, explicit
risk POSTURE, and (2) injecting both a standing RISK-GOVERNOR mandate into the Brain's persona and a
live risk BRIEFING into its daily prompt — so the Brain must evaluate risk BEFORE it sizes and the
governor can OVERRIDE "be decisive / press your winners".

Design (mirrors brain/strategist + brain/self_mirror):
  * ``risk_state`` is the PURE, offline core: it reads published dashboard JSON (regime liquidity,
    vol_sentiment, etf_pulse risk/credit, dealer gamma/GEX, crowded leadership) — degrading every
    layer to empty when the vendor data is absent — and derives a posture + evidence. If a sibling
    "Macro Risk Officer" publishes a risk-state (``data/risk_officer/macro/latest.json``), that
    posture is AUTHORITATIVE and overrides the heuristic.
  * ``briefing`` formats that state for the daily prompt; ``govern_persona`` appends the standing
    mandate (the Heavyweight variant governs CONCENTRATION, not gross).
  * Both injectors are flag-gated behind ``MASTERMIND_RISK_GOVERNOR`` (default OFF). When OFF,
    ``briefing`` returns "" and ``govern_persona`` returns the prompt UNCHANGED — the personas +
    prompts are byte-identical to today. Read-only, additive, never raises.
"""
from __future__ import annotations

import json
import os
from datetime import date
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_V = _ROOT / "vendor" / "macro"
# where a sibling Macro Risk Officer effort would publish its authoritative risk-state, if it exists.
_MACRO_OFFICER = _ROOT / "data" / "risk_officer" / "macro" / "latest.json"

# per-book benchmark — used for the dealer-gamma read + crowded-leader framing.
_BENCHMARK = {"autonomous": "SPY", "heavyweight": "SPY", "etf": "SPY", "china": "FXI", "hk": "FXI"}
# the free-form books share one mandate; Heavyweight governs concentration instead of gross.
_FREEFORM = {"autonomous", "china", "hk", "etf"}

# posture buckets, worst → best, by heuristic risk score.
_RISK_OFF, _CAUTION, _NEUTRAL, _UNKNOWN = "RISK-OFF", "CAUTION", "NEUTRAL", "UNKNOWN"


def _on() -> bool:
    """The governor injects into prompts/personas only when explicitly armed. Default OFF → the
    brain prompts + personas are byte-identical to today."""
    return os.environ.get("MASTERMIND_RISK_GOVERNOR", "0").strip().lower() in ("1", "true", "yes", "on")


def _load(rel: str):
    """Read a published dashboard JSON under vendor/macro, degrading to None on any miss (the same
    vendor tree the engine + Strategist see, so the read is offline-degradable and testable)."""
    p = _V / rel
    try:
        return json.loads(p.read_text()) if p.exists() else None
    except Exception:  # noqa: BLE001
        return None


def _read_json(p: Path):
    try:
        return json.loads(p.read_text()) if p.exists() else None
    except Exception:  # noqa: BLE001
        return None


def _num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _label(d) -> str:
    """Coerce a signal's label field (str, or {en/label_en/state/label}) to a lowercase string."""
    if isinstance(d, str):
        return d.lower()
    if isinstance(d, dict):
        for k in ("label_en", "label", "state", "en"):
            v = d.get(k)
            if isinstance(v, str):
                return v.lower()
    return ""


# ─────────────────────────────────────────────────────────────────────────────
# the pure risk read — every layer degrades to empty, so this is fully offline.
# ─────────────────────────────────────────────────────────────────────────────
def _vol_read() -> dict:
    vs = _load("site/basketdata/vol_sentiment.json") or {}
    return {k: vs.get(k) for k in ("vix", "vix_pctile", "term_structure", "put_call", "sentiment_en")}


def _pulse_read() -> dict:
    pulse = _load("site/basketdata/etf_pulse.json") or {}
    return {
        "risk_label": _label(pulse.get("risk")),
        "credit_label": _label(pulse.get("credit")),
        "leaders": [str(x).upper() for x in (pulse.get("leaders") or [])][:8],
        "laggards": [str(x).upper() for x in (pulse.get("laggards") or [])][:8],
    }


def _gamma_read(book: str, held: list[str] | None) -> dict:
    """Dealer gamma for the benchmark + a few held names: SHORT gamma means dealers amplify moves
    (a selloff accelerates) — the 'active risk event amplifier' read. Empty when no GEX files."""
    bench = _BENCHMARK.get(book, "SPY")
    names = [bench] + [t for t in (held or []) if t and t.upper() != bench][:8]
    short: list[str] = []
    seen: set[str] = set()
    for t in names:
        t = (t or "").upper().strip()
        if not t or t in seen:
            continue
        seen.add(t)
        d = _load(f"site/gex/{t}.json")
        summ = (d or {}).get("summary") or {}
        if str(summ.get("regime") or "").lower().startswith("short"):
            short.append(t)
    return {"benchmark": bench, "benchmark_short_gamma": bench in short, "short_gamma": short}


def _backwardation(term_structure) -> bool:
    """Inverted/backwardated vol term structure (front > back) is a stress tell. Handles a label or a
    front/back ratio (>1 → inverted)."""
    if isinstance(term_structure, str):
        s = term_structure.lower()
        return any(k in s for k in ("back", "invert", "stress", "kink"))
    n = _num(term_structure)
    return n is not None and n > 1.0


def _macro_officer() -> dict | None:
    """A sibling Macro Risk Officer's published risk-state, if one exists. Authoritative when present.
    Expected (soft) contract: {asof, posture: risk_off|caution|neutral|risk_on, headline, directive,
    drivers:[...]}. Best-effort; None when absent/unreadable."""
    d = _read_json(_MACRO_OFFICER)
    if not isinstance(d, dict):
        return None
    posture = str(d.get("posture") or d.get("risk_state") or "").strip().lower()
    if not posture:
        return None
    return {
        "posture": posture,
        "headline": str(d.get("headline") or "")[:300],
        "directive": str(d.get("directive") or "")[:400],
        "drivers": [str(x)[:120] for x in (d.get("drivers") or [])][:6],
        "asof": str(d.get("asof") or "")[:10],
    }


_OFFICER_POSTURE = {"risk_off": _RISK_OFF, "risk-off": _RISK_OFF, "defensive": _RISK_OFF,
                    "caution": _CAUTION, "cautious": _CAUTION, "neutral": _NEUTRAL,
                    "risk_on": _NEUTRAL, "risk-on": _NEUTRAL}


def risk_state(book: str, *, regime: dict | None = None, asof=None, held: list[str] | None = None) -> dict:
    """The PURE risk read for ``book``: live signals + a derived posture + evidence. Never raises.

    Heuristic score (conservative — each independent risk-off tell adds weight): contracting
    liquidity, elevated/backwardated vol, ETF-pulse risk-off, credit risk-off, complacency at the
    top, and dealers short gamma. A published Macro Risk Officer posture, when present, OVERRIDES the
    heuristic. Posture buckets: RISK-OFF (≥4) / CAUTION (≥2) / NEUTRAL (<2), or UNKNOWN when the tape
    is entirely unreadable (offline) — UNKNOWN never reads as 'calm'."""
    regime = regime or {}
    held = [str(t).upper().strip() for t in (held or []) if str(t or "").strip()]
    liquidity = str(regime.get("liquidity_overlay") or "").strip().lower() or None

    vol = _vol_read()
    pulse = _pulse_read()
    gamma = _gamma_read(book, held)

    score = 0
    evidence: list[str] = []

    if liquidity and any(k in liquidity for k in ("contract", "tighten", "drain", "off")):
        score += 2
        evidence.append(f"Liquidity overlay: {liquidity} (a headwind — de-risk into it, not toward it)")
    elif liquidity:
        evidence.append(f"Liquidity overlay: {liquidity}")

    vix, vpc = _num(vol.get("vix")), _num(vol.get("vix_pctile"))
    if (vpc is not None and vpc >= 70) or (vix is not None and vix >= 20):
        score += 1
        evidence.append("Volatility elevated: "
                        + ", ".join(p for p in (f"VIX {vix:.1f}" if vix is not None else "",
                                                 f"{vpc:.0f}th pctile" if vpc is not None else "") if p))
    if _backwardation(vol.get("term_structure")):
        score += 2
        evidence.append("Vol term structure inverted/backwardated (acute-stress tell)")
    sentiment = str(vol.get("sentiment_en") or "").lower()
    complacent = any(k in sentiment for k in ("complacent", "greed", "euphoria", "frothy"))
    if complacent and (vpc is None or vpc <= 40):
        score += 1
        evidence.append(f"Sentiment '{sentiment}' at low vol — complacency at the top (crowding risk)")

    if "risk-off" in pulse["risk_label"] or "risk_off" in pulse["risk_label"] or "defensive" in pulse["risk_label"]:
        score += 2
        evidence.append(f"ETF-pulse risk regime: {pulse['risk_label']}")
    elif pulse["risk_label"]:
        evidence.append(f"ETF-pulse risk regime: {pulse['risk_label']}")
    if pulse["credit_label"] and any(k in pulse["credit_label"] for k in ("risk-off", "risk_off", "widen", "stress", "defensive")):
        score += 2
        evidence.append(f"Credit: {pulse['credit_label']} (credit leads equity — respect it)")

    if gamma["benchmark_short_gamma"]:
        score += 1
        evidence.append(f"Dealers SHORT gamma on {gamma['benchmark']} — moves get amplified, a selloff accelerates")
    extra_short = [t for t in gamma["short_gamma"] if t != gamma["benchmark"]]
    if extra_short:
        evidence.append("Short-gamma (amplified) names you may hold: " + ", ".join(extra_short))

    # crowded leadership + the book's overlap with it — the 2026-06-23 failure was adding to the
    # crowded leader you already hold. Surface both so the Brain can govern correlation, not just names.
    crowded = pulse["leaders"]
    overlap = [t for t in held if t in set(crowded)]
    if crowded:
        evidence.append("Crowded market leadership now: " + ", ".join(crowded))
    if overlap:
        evidence.append("YOUR book already sits in that crowd: " + ", ".join(overlap)
                        + " — adding here concentrates INTO the crowded trade")

    officer = _macro_officer()

    # posture: officer authoritative, else heuristic; UNKNOWN when nothing was readable.
    any_signal = bool(liquidity or vol.get("vix") is not None or pulse["risk_label"]
                      or pulse["credit_label"] or gamma["short_gamma"] or crowded)
    if officer:
        posture = _OFFICER_POSTURE.get(officer["posture"], _CAUTION)
        source = "macro_risk_officer"
    elif not any_signal:
        posture, source = _UNKNOWN, "no_signal"
    else:
        posture = _RISK_OFF if score >= 4 else _CAUTION if score >= 2 else _NEUTRAL
        source = "heuristic"

    return {
        "book": book,
        "asof": str(asof or "")[:10] or None,
        "posture": posture,
        "source": source,
        "score": score,
        "evidence": evidence,
        "liquidity": liquidity,
        "vol": vol,
        "etf_pulse": pulse,
        "gamma": gamma,
        "crowded_leaders": crowded,
        "held_overlap": overlap,
        "macro_officer": officer,
    }


# ─────────────────────────────────────────────────────────────────────────────
# the per-posture governing directive + the standing persona mandate.
# ─────────────────────────────────────────────────────────────────────────────
_DIRECTIVE = {
    _RISK_OFF: (
        "Your DEFAULT today is CAPITAL PROTECTION, not offense: CUT gross, RAISE cash (it earns ~4% — "
        "rewarded), and rotate toward genuine, uncorrelated defensives. Do NOT add to crowded or "
        "extended leaders. Adding correlated beta today requires extraordinary, written justification "
        "that explicitly overrides this governor — and a single token hedge does NOT count as risk "
        "management."),
    _CAUTION: (
        "Tighten the book: do NOT increase gross or concentration into the crowded leaders, and trim "
        "the most extended / most correlated names. Risk is governed at the level of the WHOLE book's "
        "gross + correlation — a token 5% hedge while you stay heavy in the correlated bet is not risk "
        "management."),
    _NEUTRAL: (
        "No blanket risk override — size on conviction. Still keep the whole book's gross + correlation "
        "in check and stay alert to crowding and liquidity; do not let a calm tape drift you into the "
        "crowded leader."),
    _UNKNOWN: (
        "The in-house risk tape is unreadable right now — do NOT assume calm. Pull get_overnight_tape, "
        "get_options (dealer gamma) and get_divergences yourself and evaluate risk BEFORE you size."),
}

_MANDATE_FREEFORM = (
    "\n\n=== RISK GOVERNOR (overrides your act / lean-in imperative) ===\n"
    "Before you size ANYTHING, evaluate the risk tape — the macro regime + liquidity, volatility and "
    "sentiment, ETF-pulse risk/credit, dealer gamma, and how CROWDED + EXTENDED your leaders are. This "
    "risk evaluation GOVERNS the whole book: it can and should force 'cut, do not add, go defensive, "
    "raise cash' and OVERRIDE 'be decisive / press your winners'.\n"
    "When the macro/positioning tape is risk-off, OR liquidity is contracting into an active risk "
    "event, OR your leaders are crowded + extended, your job flips from offense to CAPITAL PROTECTION: "
    "trim gross, raise cash (idle cash earns ~4% — a rewarded, legitimate choice), add GENUINELY "
    "uncorrelated defensives, and do NOT chase the crowded leader. Risk is governed at the level of "
    "the WHOLE book's gross + correlation — NOT one position. A single token 5% hedge while you stay "
    "~95% in the correlated bet is NOT risk management; it is the exact mistake this governor exists "
    "to stop. Confirm the live risk yourself (get_overnight_tape / get_options / get_divergences) "
    "before you commit size."
)

_MANDATE_HEAVYWEIGHT = (
    "\n\n=== RISK GOVERNOR (overrides your concentrate-and-press imperative) ===\n"
    "Maximum concentration is a CHOICE you must justify each day, NOT a default. Before you press "
    "size, run your OWN risk evaluation and answer one question: is NOW the time to be maximally "
    "concentrated and high-beta? Read the macro regime + liquidity, vol/sentiment, ETF-pulse "
    "risk/credit, dealer gamma, and how crowded + extended Flagship's leaders already are.\n"
    "When the tape is risk-off, liquidity is contracting, or those leaders are crowded + extended, "
    "MAXIMUM concentration into the highest-beta names is the WORST posture — it converts a market "
    "wobble into a book-killing drawdown. In that regime your job is to DE-CONCENTRATE: hold MORE cash "
    "(it earns ~4% — rewarded), spread across MORE of Flagship's names, tilt toward its lower-beta / "
    "more defensive holdings, and REFUSE to go heavy. You are NOT obliged to be concentrated every "
    "day. Pressing the crowded leader into a selloff is the cardinal sin this governor exists to stop; "
    "going lighter or to cash is a legitimate, rewarded decision. Confirm the live risk yourself "
    "(get_overnight_tape / get_options / get_divergences) before you press."
)


def govern_persona(persona: str, book: str) -> str:
    """Append the standing RISK-GOVERNOR mandate to a Brain's persona, gated by
    MASTERMIND_RISK_GOVERNOR. Returns ``persona`` UNCHANGED when the flag is OFF, so the personas are
    byte-identical to today in the default configuration. Never raises."""
    if not _on():
        return persona
    try:
        mandate = _MANDATE_HEAVYWEIGHT if book == "heavyweight" else _MANDATE_FREEFORM
        return persona + mandate
    except Exception:  # noqa: BLE001 — the governor is additive; never break the seat
        return persona


def _format_briefing(st: dict) -> str:
    posture = st.get("posture") or _UNKNOWN
    src = {"macro_risk_officer": "Macro Risk Officer", "heuristic": "in-house heuristic",
           "no_signal": "tape unreadable"}.get(st.get("source"), "in-house heuristic")
    lines = ["## ⚠ RISK GOVERNOR — evaluate this BEFORE you size (it can override your lean-in)",
             f"In-house risk posture: **{posture}** ({src})."]
    officer = st.get("macro_officer")
    if officer and officer.get("headline"):
        lines.append(f"Macro Risk Officer: {officer['headline']}")
    if st.get("evidence"):
        lines.append("Live risk tape:")
        lines += [f"  - {e}" for e in st["evidence"]]
    lines.append("Directive: " + _DIRECTIVE.get(posture, _DIRECTIVE[_NEUTRAL]))
    if officer and officer.get("directive"):
        lines.append("Macro Risk Officer directive: " + officer["directive"])
    return "\n".join(lines)


def briefing(book: str, *, regime: dict | None = None, asof=None, held: list[str] | None = None) -> str:
    """The live risk-state block for ``book``'s daily prompt, gated by MASTERMIND_RISK_GOVERNOR.
    Returns "" when the flag is OFF (prompt byte-identical to today). Never raises."""
    if not _on():
        return ""
    try:
        st = risk_state(book, regime=regime, asof=asof, held=held)
        return _format_briefing(st)
    except Exception:  # noqa: BLE001 — additive; never break the build
        return ""
