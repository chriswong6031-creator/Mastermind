"""TECHNICIAN — the entry-timing seat. Judges "is NOW the right time to enter THIS chart?"

The audit found no seat owns entry timing: the pipeline confirms a *name* (FORGE) and reads the
*macro backdrop* (STRATEGIST) and the *bear case* (SENTINEL), but nobody rules on whether the
chart deserves an entry TODAY, a staged starter, or a WAIT-for-setup. This module fills that gap.
It is the TECHNICIAN / TACTICIAN role from ``docs/design/desk/02-organization-structure.md`` §2.2
and the ``TECH_NOW / TECH_STAGE / TECH_WAIT`` verdict from ``03-buy-pipeline-and-watchlist.md``.

Mirrors the committee seat pattern (``brain/committee.py`` — ``_sentinel_input`` / ``sentinel_assess``):
an information-boundary input builder → a seat executor returning a normalised, gradable verdict
dict → a durable per-name artifact writer for the outcome/timing grader.

STRUCTURAL BLINDNESS INVARIANT (mirrors SENTINEL). The Technician rules ONLY on the CHART / TIMING
— never on thesis quality. Its input MUST NOT carry the bull thesis, the combined/conviction score,
viability, fair value, recommend, or size_mult. This blindness is a hard invariant (see
``tests/test_technician.py::test_technician_input_is_blind_to_thesis``) so the timing call cannot
be anchored by how much the desk likes the *idea*.

FAIL-CONSERVATIVE DEFAULT (the INVERSE of the old SENTINEL "no-LLM → CONFIRM" hole). When no LLM is
reachable (``cli_bridge`` unavailable) OR on ANY error/parse failure, the verdict is ``"wait"`` — the
seat can only ever WITHHOLD or stage, NEVER force a buy. Subtract-only: this seat's authority is to
ADD ``wait``/``staged_starter`` on top of the deterministic floor; it can never turn a would-be-no
into a yes and never escalate a would-be-starter to a full entry.

RELATION TO THE DETERMINISTIC FLOOR. ``portfolio.watchlist.timing_withhold()`` remains the HARD
timing predicate (extended / weak-RS / parabolic / 'avoid' / weak-eq → park). THIS seat is an
ADDITIVE judgment layer *on top* of that floor (it can only withhold/stage further), gated behind
``MASTERMIND_TECHNICIAN`` (default OFF) and — eventually — the desk quorum / A/B experiment. It is
NOT wired into ``bot/phase2.py`` in this increment (that is the separate desk-quorum wiring step);
building it DARK and TESTABLE is the whole of this increment.

Model tier: Sonnet (``role="analyst"`` per ``config/agents.yml`` — the Technician is a
per-name analytical pass, not a deep-synthesis Opus seat). Invoked headlessly via
``brain.cli_bridge`` (the Claude Code subscription bridge). Degrades to ``"wait"`` fully offline,
so the seat is testable without a live LLM.
"""
from __future__ import annotations

import json
import os
from datetime import date
from pathlib import Path

from brain import cli_bridge

_ARTIFACTS = Path(__file__).resolve().parent.parent / "data" / "committee"

# The three entry-timing verdicts (TECH_NOW / TECH_STAGE / TECH_WAIT). "wait" is the
# fail-conservative default and the only verdict the seat may reach without an LLM.
_VERDICTS = ("now", "staged_starter", "wait")
_DEFAULT_VERDICT = "wait"

# Fields the input must NEVER contain — the blindness invariant. The Technician rules on the
# CHART, never on the thesis. Kept as a module constant so the guard and the test share one list.
_FORBIDDEN_INPUT_KEYS = (
    "combined", "conviction", "viability", "fair_value", "fair_val", "recommend",
    "research_score", "engine_score", "size_mult", "bull", "thesis", "confirmed",
    "report_md", "paper",
)


def technician_enabled() -> bool:
    """The Technician seat runs only when explicitly armed via ``MASTERMIND_TECHNICIAN``.

    DARK by default: unset or falsy → OFF (byte-identical to base; the seat never runs). Only an
    explicit truthy value ({1, true, yes, on}) arms it. Note this is a pure *gating* read — even
    when armed, the assess path still fails CONSERVATIVE (→ "wait") if no LLM is reachable."""
    flag = os.environ.get("MASTERMIND_TECHNICIAN", "0").strip().lower()
    return flag in ("1", "true", "yes", "on")


# ─────────────────────────────────────────────────────────────────────────────
# input — the ONLY context the Technician sees. Entry-timing data ONLY: risk levels,
# chart extension/momentum, catalyst proximity, options geometry. Deliberately EXCLUDES
# the bull thesis / combined score / viability / fair_value (the blindness invariant).
# ─────────────────────────────────────────────────────────────────────────────
def technician_input(ticker: str, *, entry_signal: dict | None, tech: dict | None,
                     anticipation: dict | None = None, options: dict | None = None) -> dict:
    """Assemble the Technician's input slice from ENTRY-TIMING data ONLY.

    Parameters
    ----------
    entry_signal : the published per-name risk levels — ``{stop, buy_zone, entry_grade, chase_above}``
        (``bot.phase2._published_entry_signal`` shape).
    tech : the entry-technical block — ``{pct_vs_50dma, pct_vs_200dma, rsi14, off_52w_high_pct, rs,
        urgency, eq_grade, parabolic}`` (a superset of ``bot.phase2._entry_tech_fields``; only the
        chart/timing fields are read — every field nullable, missing → None).
    anticipation : optional catalyst/earnings window — ``{next_date, days_to_event, vol_cone, sue_z,
        horizon}``.
    options : optional entry geometry — ``{gamma_flip, expected_move, magnets, walls}``.

    BLINDNESS: no thesis/combined/viability/fair_value/recommend/size_mult field may appear — the
    output is defensively re-filtered against ``_FORBIDDEN_INPUT_KEYS`` before return, so even a
    caller that stuffs a forbidden key into ``tech``/``entry_signal`` cannot leak it into the seat.
    Pure; never raises (a non-dict argument degrades to {} for that slice)."""
    es = entry_signal if isinstance(entry_signal, dict) else {}
    tc = tech if isinstance(tech, dict) else {}
    an = anticipation if isinstance(anticipation, dict) else {}
    op = options if isinstance(options, dict) else {}

    payload = {
        "ticker": str(ticker or "").upper(),
        # risk levels — where's the stop, the buy zone, the entry grade, the do-not-chase line
        "entry_signal": {
            "stop": es.get("stop"),
            "buy_zone": es.get("buy_zone"),
            "entry_grade": es.get("entry_grade"),
            "chase_above": es.get("chase_above"),
        },
        # the chart itself — extension vs the MAs, RSI, distance off the 52w high, RS, quality/urgency
        "tech": {
            "pct_vs_50dma": tc.get("pct_vs_50dma"),
            "pct_vs_200dma": tc.get("pct_vs_200dma"),
            "rsi14": tc.get("rsi14"),
            "off_52w_high_pct": tc.get("off_52w_high_pct"),
            "rs": tc.get("rs"),
            "urgency": tc.get("urgency"),
            "eq_grade": tc.get("eq_grade"),
            "parabolic": bool(tc.get("parabolic")) if tc.get("parabolic") is not None else None,
        },
        # catalyst window — is an earnings/Fed/expiry event near enough to size around
        "anticipation": {
            "next_date": an.get("next_date"),
            "days_to_event": an.get("days_to_event") or an.get("days_to_next"),
            "vol_cone": an.get("vol_cone"),
            "sue_z": an.get("sue_z"),
            "horizon": an.get("horizon"),
        } if an else {},
        # options geometry — gamma flip, expected move, magnets/walls that shape the tranche timing
        "options": {
            "gamma_flip": op.get("gamma_flip"),
            "expected_move": op.get("expected_move"),
            "magnets": op.get("magnets"),
            "walls": op.get("walls"),
        } if op else {},
    }
    return _strip_forbidden(payload)


def _strip_forbidden(payload):
    """Recursively drop any key matching a forbidden thesis/score field (case-insensitive substring).

    The last line of defence for the blindness invariant: even if a caller's ``tech``/``entry_signal``
    dict carried an out-of-contract key (e.g. a stray ``combined`` or ``fair_value``), it is removed
    here so it can never reach the LLM or the artifact. Pure; never raises."""
    if isinstance(payload, dict):
        clean = {}
        for k, v in payload.items():
            kl = str(k).lower()
            if any(bad in kl for bad in _FORBIDDEN_INPUT_KEYS):
                continue
            clean[k] = _strip_forbidden(v)
        return clean
    if isinstance(payload, list):
        return [_strip_forbidden(x) for x in payload]
    return payload


_TECHNICIAN_SYS = (
    "You are the TECHNICIAN (tactician) on an equity investment committee. You rule ONLY on the "
    "CHART and the TIMING — is NOW the right moment to enter THIS name? You have deliberately NOT "
    "seen the bull thesis, the conviction score, the fair value, or the recommendation, and you "
    "MUST NOT speculate about them: reason ONLY from the price structure, extension, relative "
    "strength, entry/stop geometry, catalyst proximity, and options geometry you are given. "
    "Judge: is the entry clean (valid base / constructive setup, not extended or distributing, RS "
    "leading), or is it a chase into a stretched/parabolic/weak-RS tape that should WAIT for a "
    "better setup? A binary event inside the window with no defined risk caps you to a starter. "
    "Reply ONLY with JSON: {\"verdict\": \"now|staged_starter|wait\", \"confidence\": 0.0-1.0, "
    "\"rationale\": str}. "
    "now = enter now (clean setup); staged_starter = enter a STARTER only (acceptable but imperfect "
    "setup — imperfect base, catalyst pending, RS below leader); wait = do NOT enter now, park for a "
    "better setup. You can only WITHHOLD or stage; you can never force a full entry. Be blunt and "
    "specific about the price structure; no thesis talk, no moralizing."
)

_JSON_ONLY = (
    "\n\nCRITICAL OUTPUT CONTRACT: reply with ONLY a single valid JSON object — no markdown, no "
    "headers, no prose, no code fences. Your ENTIRE response must parse with json.loads, beginning "
    "with '{' and ending with '}'."
)


def _parse_json(txt: str | None) -> dict | None:
    if not txt:
        return None
    try:
        return json.loads(txt[txt.find("{"):txt.rfind("}") + 1])
    except Exception:  # noqa: BLE001
        return None


def _clamp01(x, default: float = 0.0) -> float:
    try:
        return max(0.0, min(1.0, float(x)))
    except (TypeError, ValueError):
        return default


def _wait(rationale: str, *, confidence: float = 0.0) -> dict:
    """The fail-conservative verdict. The ONLY verdict the seat reaches with no LLM / on any error."""
    return {"agent": "technician", "verdict": _DEFAULT_VERDICT,
            "confidence": round(_clamp01(confidence), 3), "rationale": str(rationale)[:600]}


def technician_assess(inp: dict, *, asof: str | None = None, ticker: str | None = None) -> dict:
    """Rule on the entry timing for one name. Returns ``{agent, verdict, confidence, rationale}`` with
    ``verdict ∈ {"now","staged_starter","wait"}``.

    Uses ``cli_bridge`` (Sonnet ``role="analyst"``) when a local Claude Code LLM is available.

    FAIL-CONSERVATIVE: if no LLM is reachable (``cli_bridge.available()`` is False) OR the call/parse
    fails for ANY reason → verdict ``"wait"`` (the seat can only ever withhold/stage — never force a
    buy). SUBTRACT-ONLY: an unrecognised verdict is coerced to ``"wait"``, never to a more aggressive
    entry. Never raises."""
    _inp_tk = inp.get("ticker") if isinstance(inp, dict) else None
    tk = str(ticker or _inp_tk or "").upper() or None

    # fail-conservative gate: no LLM → WITHHOLD (the inverse of the old SENTINEL default-CONFIRM hole)
    try:
        if not cli_bridge.available():
            return _wait("no LLM available — fail-conservative WITHHOLD (Technician can only wait/stage).")
    except Exception:  # noqa: BLE001 — an availability probe failure is itself a reason to withhold
        return _wait("LLM availability probe failed — fail-conservative WITHHOLD.")

    prompt = json.dumps(inp or {}, default=str)
    try:
        res = cli_bridge.reason_sync(prompt, role="analyst", system=_TECHNICIAN_SYS + _JSON_ONLY,
                                     log_run=False)
    except Exception:  # noqa: BLE001 — any call failure fails conservative, never breaks the caller
        return _wait("Technician LLM call failed — fail-conservative WITHHOLD.")
    if not isinstance(res, dict) or not res.get("ok"):
        return _wait("Technician LLM returned no usable result — fail-conservative WITHHOLD.")

    j = _parse_json(res.get("text"))
    if not j:
        return _wait("Technician reply did not parse as JSON — fail-conservative WITHHOLD.")

    verdict = str(j.get("verdict", "")).strip().lower().replace("-", "_").replace(" ", "_")
    # normalise the design's TECH_* / enum spellings onto the canonical three
    _alias = {"tech_now": "now", "enter_now": "now", "buy_now": "now",
              "tech_stage": "staged_starter", "stage": "staged_starter", "starter": "staged_starter",
              "stage_starter": "staged_starter", "tech_wait": "wait"}
    verdict = _alias.get(verdict, verdict)
    if verdict not in _VERDICTS:
        # SUBTRACT-ONLY: an unknown/garbage verdict can only degrade to the conservative floor.
        return _wait(f"Technician returned an unrecognised verdict ({j.get('verdict')!r}) — "
                     "coerced to conservative WITHHOLD.", confidence=j.get("confidence", 0.0))

    _ = (tk, asof)  # ticker/asof are for the caller's artifact keying, not the verdict shape
    return {
        "agent": "technician",
        "verdict": verdict,
        "confidence": round(_clamp01(j.get("confidence", 0.5), default=0.5), 3),
        "rationale": str(j.get("rationale", ""))[:600],
    }


def _write_artifact(asof: str | None, ticker: str | None, inp: dict | None,
                    verdict: dict | None) -> str | None:
    """Persist the Technician verdict to ``data/committee/<asof>/<TICKER>/technician.json`` (mirrors
    where the committee writes ``sentinel.json``). Best-effort; never raises."""
    _inp_tk = inp.get("ticker") if isinstance(inp, dict) else None
    d = _ARTIFACTS / (str(asof)[:10] if asof else date.today().isoformat()) / (
        str(ticker or _inp_tk or "UNK").upper())
    d.mkdir(parents=True, exist_ok=True)
    (d / "technician.json").write_text(json.dumps(
        {"agent": "technician", "input": inp, "verdict": verdict}, indent=2, default=str))
    return str(d)


def run(ticker: str, *, entry_signal: dict | None, tech: dict | None,
        anticipation: dict | None = None, options: dict | None = None,
        asof: str | None = None) -> dict:
    """Convenience wrapper: build the blind input → assess → persist the artifact, in one call.

    Returns the verdict dict (fail-conservative ``"wait"`` when no LLM is available). The artifact is
    written best-effort; a persistence failure never changes the returned verdict. Never raises."""
    try:
        inp = technician_input(ticker, entry_signal=entry_signal, tech=tech,
                               anticipation=anticipation, options=options)
    except Exception:  # noqa: BLE001 — an input-build failure still yields a conservative verdict
        inp = {"ticker": str(ticker or "").upper()}
    verdict = technician_assess(inp, asof=asof, ticker=ticker)
    try:
        _write_artifact(asof, ticker, inp, verdict)
    except Exception:  # noqa: BLE001 — persistence is best-effort
        pass
    return verdict
