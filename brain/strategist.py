"""MACRO STRATEGIST — the top-down seat for the Flagship deep-reasoning buy layer.

The committee's SENTINEL is a bottom-up, per-name adversary. This seat is its top-down
counterpart: ONE pass over the in-house macro dashboard that judges which thematic narratives
are in CONFIRMED leadership RIGHT NOW (confirmed-stage basket + positive breadth + leading
sector + regime-compatible), names their member tickers, and decides whether the backdrop is
supportive of adding thematic risk. It is the Flagship analogue of how the proven autonomous
desk reads regime/themes/baskets before it concentrates.

Mirrors the committee seat pattern (``brain/committee.py``): an information-boundary input
builder (``_strategist_input``) → a seat executor (``strategist_assess``) returning a gradable
dict → an artifact writer. Headless like ``committee.sentinel_assess`` (no MCP) — it reads the
published dashboard JSON directly via a local ``_load`` helper copied from ``conviction._load``,
so it degrades to empty signals (and the seat still returns) when the vendor data is absent.
That makes the seat testable fully offline.

Additive + reversible: gated behind ``MASTERMIND_FLAGSHIP_JUDGMENT`` (default OFF) AND an
available LLM. Returns ``None`` on any LLM/parse failure; never raises. Model-agnostic via
``role="deep"`` (Opus/Fable resolved by config/agents.yml).
"""
from __future__ import annotations

import json
import os
from datetime import date
from pathlib import Path

from brain import client

_ARTIFACTS = Path(__file__).resolve().parent.parent / "data" / "committee"
_V = Path(__file__).resolve().parent.parent / "vendor" / "macro"

_STANCES = ("risk_on", "neutral", "risk_off")
_STAGES = ("confirmed", "emerging")


def enabled() -> bool:
    """The Strategist runs only when the Flagship judgment layer is armed AND an LLM is reachable."""
    flag = os.environ.get("MASTERMIND_FLAGSHIP_JUDGMENT", "0").strip().lower()
    if flag not in ("1", "true", "yes", "on"):
        return False
    try:
        return client.available()
    except Exception:  # noqa: BLE001
        return False


def _load(rel: str):
    """Read a published dashboard JSON, degrading to None on any miss (copied from
    ``conviction._load`` so the seat sees the same vendor tree as the engine)."""
    p = _V / rel
    try:
        return json.loads(p.read_text()) if p.exists() else None
    except Exception:  # noqa: BLE001
        return None


# ─────────────────────────────────────────────────────────────────────────────
# input — the exact macro signals the Strategist sees (every list capped, strings
# truncated, missing files degraded to {}/[] so the payload stays < ~6k tokens and
# the offline tests pass with synthetic stand-ins).
# ─────────────────────────────────────────────────────────────────────────────
def _strategist_input(regime: dict | None, asof: str) -> dict:
    regime = regime or {}
    reg = {k: regime.get(k) for k in
           ("quad", "quad_name", "growth_score", "inflation_score", "liquidity_overlay",
            "cycle_tag", "sector_rs_top")}
    # W4 3a — upgrade the confidence-blind slice to the full regime_frame read (confidence /
    # transition_state / contradicting / flip_margin). Best-effort overlay: a missing/stale frame
    # leaves reg untouched (the 3-field slice), never inflating the read (the master invariant).
    try:
        from brain import regime_frame as _rf
        fr = _rf.frame("us") or {}
        for k in ("confidence", "transition_state", "contradicting", "flip_margin",
                  "flag_confidence_decay"):
            if fr.get(k) is not None:
                reg[k] = fr[k]
    except Exception:  # noqa: BLE001 — degrade to the 3-field slice
        pass

    # basket_flow[] — the PRIMARY confirmed/emerging stage ranking
    flow_raw = _load("site/basketdata/flow.json") or {}
    flow_rows = flow_raw.get("baskets") or flow_raw.get("flow") or flow_raw.get("rows") or []
    basket_flow = []
    for b in flow_rows:
        if not isinstance(b, dict):
            continue
        stage = str(b.get("stage") or "").lower()
        if stage not in ("confirmed", "emerging"):
            continue
        basket_flow.append({
            "id": b.get("id") or b.get("basket_id"),
            "name": str(b.get("name") or b.get("id") or "")[:60],
            "stage": stage,
            "breadth": b.get("breadth"),
            "leadership": b.get("leadership"),
            "perf_20d_rel": b.get("perf_20d_rel") or b.get("ret_20d_rel"),
        })
    basket_flow = basket_flow[:30]

    # sector_pulse — sector rotation leaders/laggards + risk + style tilts
    pulse = _load("site/basketdata/etf_pulse.json") or {}
    sec_raw = pulse.get("sector") or {}
    sectors = {}
    if isinstance(sec_raw, dict):
        for sec, v in list(sec_raw.items())[:14]:
            if isinstance(v, dict):
                sectors[sec] = {k: v.get(k) for k in ("mom_20d", "pctile_252d", "above_200d")}
    sector_pulse = {
        "leaders": (pulse.get("leaders") or [])[:8],
        "laggards": (pulse.get("laggards") or [])[:8],
        "sectors": sectors,
        "risk_label": ((pulse.get("risk") or {}).get("label_en")),
        "style": [{"pair": s.get("pair") or s.get("name"), "lead_en": s.get("lead_en"),
                   "tilt": s.get("tilt")} for s in (pulse.get("style") or []) if isinstance(s, dict)][:6],
    }

    # emergence[] — pre-basket narrative radar (early watch only; score>65)
    emg_raw = _load("site/basketdata/narrative_emergence.json") or {}
    emg_rows = emg_raw.get("clusters") or emg_raw.get("emergence") or emg_raw.get("rows") or []
    emergence = []
    for c in emg_rows:
        if not isinstance(c, dict):
            continue
        lbl = c.get("score_label")
        if isinstance(lbl, dict):
            lbl = lbl.get("en")
        emergence.append({
            "name_en": str(c.get("name_en") or c.get("name") or "")[:60],
            "score": c.get("score"),
            "score_label": lbl,
            "momentum": c.get("momentum") or c.get("mom_window"),
            "basket_overlap": c.get("basket_overlap"),
            "attention_aligned": c.get("attention_aligned"),
            "caveats_en": str(c.get("caveats_en") or "")[:160],
        })
    emergence = emergence[:12]

    # macro_narrative — dominant theme counts (backdrop only)
    macro = _load("site/allocationdata/macro_narrative.json") or {}
    macro_narrative = {"dominant_themes": (macro.get("dominant_themes") or [])[:10]}

    # vol_sentiment — VIX / term structure / put-call crowding overlay
    vs = _load("site/basketdata/vol_sentiment.json") or {}
    vol_sentiment = {k: vs.get(k) for k in
                     ("vix", "vix_pctile", "term_structure", "put_call", "sentiment_en")}

    # E1.1 — market_view perception enrichment (ADDITIVE; absent view degrades to {} key).
    # Compose from the market_view artifact — do NOT re-read regime/cycles sub-blocks here
    # (already covered by the frame() overlay above, following the W2 one-reader rule).
    market_view_enrichment: dict = {}
    try:
        from brain.pm_conviction import _read_market_view, _market_view_enrichment
        mv = _read_market_view()
        market_view_enrichment = _market_view_enrichment(mv)
    except Exception:  # noqa: BLE001 — additive; never break the seat
        pass

    # W-NW.1 — neural_web_context payload key (flag-gated; compact market_plane() distillation).
    # Uses market_plane() — NOT context() — so the injected dict is ~7 scalar fields, not the
    # full 60-120 KB artifact.  The full artifact would violate the <~6k-token payload budget
    # (strategist.py:60-61) and could break the brace-extract JSON parse (documented live
    # incident in the docstring at strategist.py:265).
    # {} when flag OFF or context absent — key always present so the payload schema is stable.
    neural_web_ctx: dict = {}
    try:
        from brain.neural_web_context import market_plane as _nw_market_plane, nw_prompts_enabled as _nw_flag
        if _nw_flag():
            neural_web_ctx = _nw_market_plane()
    except Exception:  # noqa: BLE001 — additive; never break the seat
        pass

    # W-TSY.1 — treasury_context payload key (flag-gated; compact snapshot() scalars only).
    # Mirrors the neural_web_context embed above but the key is ABSENT when the flag is OFF
    # or the snapshot is absent/stale — flag OFF keeps the payload byte-identical (dark-ship).
    # snapshot() is a tiny scalar dict (tga/netliq/headline/impulse), never the full artifact,
    # so the <~6k-token payload budget and the brace-extract JSON parse are unaffected.
    treasury_ctx: dict | None = None
    try:
        from brain import treasury_context as _tsy_mod
        if _tsy_mod.enabled():
            _tsy_snap = _tsy_mod.snapshot()
            if isinstance(_tsy_snap, dict) and _tsy_snap and not _tsy_snap.get("stale"):
                treasury_ctx = _tsy_snap
    except Exception:  # noqa: BLE001 — additive; never break the seat
        pass

    out = {
        "asof": str(asof)[:10],
        "regime": reg,
        "basket_flow": basket_flow,
        "sector_pulse": sector_pulse,
        "emergence": emergence,
        "macro_narrative": macro_narrative,
        "vol_sentiment": vol_sentiment,
        # market_view is absent ({}) when the organ is unbuilt — the strategist still
        # runs without it; the key is always present so the payload schema is stable.
        "market_view": market_view_enrichment,
        # neural_web_context is absent ({}) when flag OFF or context absent/stale.
        "neural_web_context": neural_web_ctx,
    }
    # treasury_context key present iff flag ON and snapshot fresh (see W-TSY.1 above).
    if treasury_ctx is not None:
        out["treasury_context"] = treasury_ctx
    return out


_STRATEGIST_SYS = (
    "You are the MACRO STRATEGIST on an equity investment committee. Given ONLY the in-house macro "
    "dashboard read — regime/quad, per-basket flow with stage+breadth, sector rotation leaders, "
    "narrative-emergence radar, dominant macro narratives, and vol/sentiment — identify which "
    "thematic narratives are in CONFIRMED leadership RIGHT NOW (confirmed stage + positive breadth + "
    "leading sector + regime-compatible), name their member tickers, and judge whether the backdrop "
    "is supportive of adding thematic risk. Distinguish confirmed leadership from merely "
    "emerging/forming narratives. Flag crowding (crowded late themes, complacent sentiment). "
    "Reply ONLY with JSON: {\"confirmed_themes\": [{\"theme\": str, \"stage\": "
    "\"confirmed|emerging\", \"leadership\": 0.0-1.0, \"names\": [str], \"why\": str}], "
    "\"backdrop_stance\": \"risk_on|neutral|risk_off\", \"supportive\": bool, "
    "\"watch_emerging\": [str], \"crowding_flags\": [str], \"rationale\": str}. "
    "Be blunt; distinguish observed signals from inferred ones; no moralizing."
)


def _asof_d(asof) -> date | None:
    """Coerce an asof string/date to a date for self-mirror's leakage-safe window (None → today)."""
    if isinstance(asof, date):
        return asof
    try:
        return date.fromisoformat(str(asof)[:10])
    except Exception:  # noqa: BLE001
        return None


def _parse_json(txt: str) -> dict | None:
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


_JSON_ONLY = (
    "\n\nCRITICAL OUTPUT CONTRACT: reply with ONLY a single valid JSON object — no markdown, no "
    "headers, no prose, no code fences. Your ENTIRE response must parse with json.loads, beginning "
    "with '{' and ending with '}'."
)


def _reformat_to_json(base_sys: str, txt: str | None) -> dict | None:
    """The model sometimes returns a prose/markdown analysis despite the JSON instruction (seen live
    in the dry-run). One cheap format-only retry converts its OWN analysis into the required JSON
    schema — a far easier task it reliably obeys. Never raises."""
    if not txt:
        return None
    try:
        # format-only retry → role="analyst" (sonnet; 2026-07-25 cost ruling — was deep/opus)
        fix, _ = client.call_model(
            base_sys + _JSON_ONLY,
            "Convert the analysis below into the exact JSON object its schema requires. Output JSON "
            "ONLY.\n\n" + str(txt)[:8000],
            role="analyst", max_tokens=2800, seat="strategist", record_book="flagship")
        return _parse_json(fix)
    except Exception:  # noqa: BLE001
        return None


def strategist_assess(asof: str, regime: dict | None) -> dict | None:
    """Run the top-down macro read. Returns a normalised, gradable verdict dict, or None if no
    LLM is reachable / the reply fails to parse. Additive; never raises."""
    if not client.available():
        return None
    payload = _strategist_input(regime, asof)
    # self-mirror: append this seat's own graded track record (flag-gated; OFF → byte-identical).
    try:
        from brain import self_mirror
        sys_prompt = self_mirror.inject(_STRATEGIST_SYS, "strategist", _asof_d(asof))
    except Exception:  # noqa: BLE001 — self-mirror is additive; never break the seat
        sys_prompt = _STRATEGIST_SYS
    try:
        # 2800 tokens: the confirmed-themes JSON (themes × member-ticker arrays + rationale) can be
        # long; 1400 truncated it mid-JSON → the brace-extract parse failed → None (caught live).
        txt, _meta = client.call_model(sys_prompt + _JSON_ONLY, json.dumps(payload, default=str),
                                       role="deep", max_tokens=2800, seat="strategist")
    except Exception:  # noqa: BLE001 — the seat is additive; never break the build
        return None
    j = _parse_json(txt) or _reformat_to_json(_STRATEGIST_SYS, txt)
    if not j:
        return None

    themes = []
    for t in (j.get("confirmed_themes") or []):
        if not isinstance(t, dict):
            continue
        stage = str(t.get("stage", "")).lower()
        if stage not in _STAGES:
            stage = "emerging"
        themes.append({
            "theme": str(t.get("theme", ""))[:120],
            "stage": stage,
            "leadership": round(_clamp01(t.get("leadership")), 3),
            "names": [str(n).upper().strip() for n in (t.get("names") or []) if str(n).strip()][:24],
            "why": str(t.get("why", ""))[:300],
        })

    stance = str(j.get("backdrop_stance", "")).lower()
    if stance not in _STANCES:
        stance = "neutral"

    out = {
        "agent": "strategist",
        "confirmed_themes": themes[:20],
        "backdrop_stance": stance,
        "supportive": bool(j.get("supportive", stance == "risk_on")),
        "watch_emerging": [str(w)[:80] for w in (j.get("watch_emerging") or [])][:12],
        "crowding_flags": [str(c)[:120] for c in (j.get("crowding_flags") or [])][:8],
        "rationale": str(j.get("rationale", ""))[:1200],
    }
    # de-confidence any stored confidence by the seat's realised calibration (shrink-only).
    try:
        from brain import calibration as _calib
        out["calibration_multiplier"] = round(_calib.multiplier("strategist"), 3)
    except Exception:  # noqa: BLE001
        out["calibration_multiplier"] = 1.0
    return out


def _write_artifacts(asof: str, regime: dict | None, payload: dict | None,
                     verdict: dict | None) -> str | None:
    """Write the Strategist verdict to ``data/committee/<asof>/_FLAGSHIP/strategist.json``.
    Best-effort, never raises."""
    d = _ARTIFACTS / (str(asof)[:10] or date.today().isoformat()) / "_FLAGSHIP"
    d.mkdir(parents=True, exist_ok=True)
    (d / "strategist.json").write_text(json.dumps(
        {"agent": "strategist", "input": payload, "verdict": verdict}, indent=2, default=str))
    return str(d)


def run(asof: str, regime: dict | None) -> dict | None:
    """Convenience wrapper: assess + write artifact in one call. Returns the verdict (or None)."""
    payload = None
    try:
        payload = _strategist_input(regime, asof)
    except Exception:  # noqa: BLE001
        payload = None
    verdict = strategist_assess(asof, regime)
    try:
        _write_artifacts(asof, regime, payload, verdict)
    except Exception:  # noqa: BLE001
        pass
    return verdict
