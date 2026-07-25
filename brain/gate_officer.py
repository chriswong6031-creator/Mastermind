"""GATE OFFICER — the portfolio-level veto on the PM's proposed BUY book.

This is the discipline half of P2: the final separation-of-powers check on the armed PM's
autonomy (``brain/pm_conviction.py``). Where SENTINEL (``brain/committee.py``) is a BLIND,
per-name bear adversary, the Gate Officer is a portfolio-level risk/discipline judge that SEES
the WHOLE proposed book at once and rules on it as a portfolio: concentration / HHI, sector &
theme crowding, per-name engine risk flags (vetoes / distribution / crowding / blocked
size-authority), and regime fit.

SUBTRACT-ONLY by construction: the Gate Officer may VETO a name, WITHHOLD it to the watchlist,
or TRIM an oversized one — it may NEVER approve a name the PM did not propose, never add, never
upsize. The code enforces this invariant regardless of what the LLM returns (any decision naming
a non-proposed ticker is dropped; ``approve`` is a no-op).

Mirrors the committee/strategist seat pattern: an information-boundary input builder
(``_gate_input``), a seat executor (``gate_assess``) returning a gradable dict, an artifact
writer (``_write_artifacts``), plus a PURE helper (``apply_gate``) that reshapes the book from a
decisions list with NO LLM — exhaustively unit-testable offline.

Additive + reversible: gated behind ``MASTERMIND_GATE_OFFICER`` (default OFF) AND an available
LLM. Returns an empty/honest dict on any LLM/parse failure; never raises. Model-agnostic via
``role="deep"`` (Opus/Fable resolved by config/agents.yml).
"""
from __future__ import annotations

import json
import os
from datetime import date
from pathlib import Path

from brain import client

_ARTIFACTS = Path(__file__).resolve().parent.parent / "data" / "gate_officer"

_ACTIONS = ("approve", "veto", "withhold", "trim")
_SECTOR_CAP = 0.30          # flag any single-sector exposure above ~30% of the book


def enabled() -> bool:
    """The Gate Officer runs only when explicitly armed AND an LLM is reachable. Default OFF so the
    live judgment loop is byte-identical until enabled."""
    flag = os.environ.get("MASTERMIND_GATE_OFFICER", "0").strip().lower()
    if flag not in ("1", "true", "yes", "on"):
        return False
    try:
        return client.available()
    except Exception:  # noqa: BLE001
        return False


# ─────────────────────────────────────────────────────────────────────────────
# input — the WHOLE proposed book, summarised into a gradable payload. Sees the
# GT3 `out` row schema (ticker/weight/confluence/committee/divergences/bull/bear).
# Sector + engine-risk flags are pulled defensively from the published stockdata
# lenses so the payload degrades to {} when vendor data is absent (offline tests).
# ─────────────────────────────────────────────────────────────────────────────
def _gate_input(book: list[dict], regime: dict | None, portfolio_ctx: dict | None) -> dict:
    book = book or []
    regime = regime or {}
    portfolio_ctx = portfolio_ctx or {}
    from portfolio import lenses

    holdings = []
    weights: list[float] = []
    sector_exposure: dict[str, float] = {}
    engine_risk_flags: dict[str, dict] = {}

    for r in book:
        t = str(r.get("ticker") or "").upper().strip()
        if not t:
            continue
        try:
            w = float(r.get("weight") or 0.0)
        except (TypeError, ValueError):
            w = 0.0
        weights.append(w)
        cm = r.get("committee") or {}
        holdings.append({
            "ticker": t,
            "weight": round(w, 4),
            "confluence": r.get("confluence"),
            "committee_action": cm.get("action"),
            "sentinel_stance": cm.get("sentinel_stance"),
            "divergences": (r.get("divergences") or [])[:4],
            "bull": str(r.get("bull") or "")[:160],
            "bear": str(r.get("bear") or "")[:160],
        })

        # sector bucket from the published stockdata lens (degrades to "unknown")
        sec = "unknown"
        try:
            sd = lenses._load(f"site/stockdata/{t}.json") or {}
            sec = str((sd.get("sector") or "unknown")).strip() or "unknown"
        except Exception:  # noqa: BLE001
            sec = "unknown"
        sector_exposure[sec] = round(sector_exposure.get(sec, 0.0) + w, 4)

        # per-name engine risk flags from the full-lens synthesis (the same fields the engine's
        # hard-exit sweep reads). Defensive; absent → omitted.
        try:
            syn = (lenses.full(t, "name") or {}).get("synthesis") or {}
            flags = {k: syn.get(k) for k in ("vetoes", "distribution", "crowding", "size_authority")
                     if syn.get(k) is not None}
            if flags:
                engine_risk_flags[t] = flags
        except Exception:  # noqa: BLE001
            pass

    gross = round(sum(weights), 4)
    hhi = round(sum(w * w for w in weights), 4)
    top_weight = round(max(weights), 4) if weights else 0.0
    cap = float((portfolio_ctx or {}).get("name_cap") or 0.08)
    names_over_cap = [h["ticker"] for h in holdings if (h["weight"] or 0.0) > cap + 1e-9]

    concentration = {
        "top_weight": top_weight,
        "gross": gross,
        "n_names": len(holdings),
        "hhi": hhi,
        "names_over_cap": names_over_cap,
    }
    crowded_sectors = {s: w for s, w in sector_exposure.items() if w > _SECTOR_CAP}

    return {
        "holdings": holdings,
        "concentration": concentration,
        "sector_exposure": sector_exposure,
        "crowded_sectors": crowded_sectors,
        "engine_risk_flags": engine_risk_flags,
        "regime": {k: regime.get(k) for k in ("quad", "quad_name", "liquidity_overlay")},
    }


_GATE_SYS = (
    "You are the GATE OFFICER — the final portfolio-level risk/discipline check on the PM's "
    "proposed BUY book, the separation-of-powers veto over PM autonomy. You SEE the whole proposed "
    "book (unlike the blind per-name SENTINEL). Judge it AS A WHOLE: concentration/HHI, sector & "
    "theme crowding, per-name engine risk flags (vetoes/distribution/crowding/blocked "
    "size-authority), and regime fit. You are SUBTRACT-ONLY: you may VETO a name, WITHHOLD it to "
    "the watchlist, or TRIM an oversized one — you may NEVER approve a name the PM did not propose, "
    "never add, never upsize. Default to approve; only act on a concrete risk. "
    "Reply ONLY JSON: {\"decisions\":[{\"ticker\":str,\"action\":\"approve|veto|withhold|trim\","
    "\"scale\":0.0-1.0,\"reason\":str}],\"book_view\":str,\"rationale\":str}."
)


def _parse_json(txt: str) -> dict | None:
    if not txt:
        return None
    try:
        return json.loads(txt[txt.find("{"):txt.rfind("}") + 1])
    except Exception:  # noqa: BLE001
        return None


def _asof_d(asof) -> date | None:
    """Coerce an asof string/date to a date for self-mirror's leakage-safe window (None → today)."""
    if isinstance(asof, date):
        return asof
    try:
        return date.fromisoformat(str(asof)[:10])
    except Exception:  # noqa: BLE001
        return None


def _normalize_decisions(j: dict | None, proposed: set[str]) -> list[dict]:
    """Coerce the raw LLM reply into subtract-only, proposed-only decisions. Pure; never raises.

    INVARIANT: a decision naming a ticker NOT in ``proposed`` is dropped (the Gate Officer can
    never inject a name). ``approve`` keeps the name at full weight; ``veto``/``withhold`` zero it
    (scale 0.0); ``trim`` clamps the supplied scale to 0..1. Anything else degrades to ``approve``."""
    out: list[dict] = []
    seen: set[str] = set()
    for d in (j or {}).get("decisions") or []:
        if not isinstance(d, dict):
            continue
        t = str(d.get("ticker", "")).upper().strip()
        if not t or t not in proposed or t in seen:
            continue  # never act on a non-proposed name; one decision per name
        seen.add(t)
        act = d.get("action") if d.get("action") in _ACTIONS else "approve"
        if act == "trim":
            try:
                sc = max(0.0, min(1.0, float(d.get("scale", 1.0) or 1.0)))
            except (TypeError, ValueError):
                sc = 1.0
        elif act in ("veto", "withhold"):
            sc = 0.0
        else:  # approve
            sc = 1.0
        out.append({"ticker": t, "action": act, "scale": sc,
                    "reason": str(d.get("reason", ""))[:400]})
    return out


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
            role="analyst", max_tokens=2400, seat="gate_officer", record_book="flagship")
        return _parse_json(fix)
    except Exception:  # noqa: BLE001
        return None


def gate_assess(book: list[dict], asof: str, *, regime: dict | None = None,
                portfolio_ctx: dict | None = None) -> dict:
    """Run the Gate Officer over the whole proposed book. Returns a gradable dict
    ``{decisions, book_view, rationale, ran, artifacts}``. Subtract-only enforced IN CODE — the
    LLM is never trusted to honour the invariant. Additive; never raises.

    ``decisions`` carries ONLY proposed tickers; non-``approve`` rows are what the caller acts on
    (drop on veto/withhold, scale on trim). When the seat is OFF / no LLM / parse fails, returns an
    empty decisions list with ``ran=False`` so the caller leaves the book untouched."""
    book = book or []
    proposed = {str(r.get("ticker") or "").upper().strip() for r in book if r.get("ticker")}
    txt = None
    if enabled():
        try:
            payload = _gate_input(book, regime or {}, portfolio_ctx or {})
            # self-mirror: append the Gate Officer's own veto track record (flag-gated; OFF → identical).
            try:
                from brain import self_mirror
                sys_prompt = self_mirror.inject(_GATE_SYS, "gate", _asof_d(asof))
            except Exception:  # noqa: BLE001 — self-mirror is additive; never break the seat
                sys_prompt = _GATE_SYS
            txt, _meta = client.call_model(sys_prompt + _JSON_ONLY, json.dumps(payload, default=str),
                                           role="deep", max_tokens=2400, seat="gate_officer")
        except Exception:  # noqa: BLE001 — the seat is additive; never break the build
            txt = None
    j = _parse_json(txt) or _reformat_to_json(_GATE_SYS, txt) or {}
    decisions = _normalize_decisions(j, proposed)
    res = {
        "decisions": decisions,
        "book_view": str(j.get("book_view", ""))[:600],
        "rationale": str(j.get("rationale", ""))[:800],
        # ran = the seat produced an actionable (non-approve) decision
        "ran": any(d["action"] != "approve" for d in decisions),
    }
    # publish the seat's realised de-confidence multiplier (shrink-only; 1.0 until graded → no
    # behaviour change OFF). The caller/CIO reads it; the seat itself never auto-acts on it.
    try:
        from brain import calibration as _calib
        res["calibration_multiplier"] = round(_calib.multiplier("gate"), 3)
    except Exception:  # noqa: BLE001
        res["calibration_multiplier"] = 1.0
    try:
        res["artifacts"] = _write_artifacts(
            asof, "FLAGSHIP", _gate_input(book, regime or {}, portfolio_ctx or {}), res)
    except Exception:  # noqa: BLE001 — persistence is best-effort
        res["artifacts"] = None
    return res


# ─────────────────────────────────────────────────────────────────────────────
# apply_gate — PURE reshaper. Given the proposed book + a decisions list, returns
# the gate-filtered book (drops vetoed/withheld, trims the trims) and parks the
# vetoed/withheld names to the watchlist. NO LLM → exhaustively unit-testable.
# SUBTRACT-ONLY: a row is iterated only if it is already in `book`; a decision can
# never inject a name (the normalizer already dropped non-proposed tickers, and
# this loop walks `book`, not `decisions`).
# ─────────────────────────────────────────────────────────────────────────────
def apply_gate(book: list[dict], decisions: list[dict], *, asof: str | None = None,
               watchlist=None) -> list[dict]:
    """Reshape ``book`` per ``decisions``. Returns a NEW filtered list (never mutates the input
    rows' identity beyond setting ``weight``/``gate`` on trimmed survivors). Pure aside from the
    optional watchlist side-effect; never raises.

    * ``veto`` / ``withhold`` → name DROPPED from the book and parked to ``watchlist`` (if given).
    * ``trim``                → ``weight *= scale``; row tagged ``gate={action,scale,rationale}``;
                                dropped if the trimmed weight is non-positive.
    * ``approve`` / no decision → kept unchanged.
    """
    by_ticker = {d["ticker"]: d for d in (decisions or []) if isinstance(d, dict) and d.get("ticker")}
    kept: list[dict] = []
    for r in (book or []):
        t = str(r.get("ticker") or "").upper().strip()
        d = by_ticker.get(t)
        if not d or d.get("action") == "approve":
            kept.append(r)
            continue
        act = d.get("action")
        if act in ("veto", "withhold"):
            if watchlist is not None and asof is not None:
                try:
                    watchlist.append(t, asof, reason=f"gate_officer: {d.get('reason', '')}",
                                     tech=None, combined=r.get("confluence"))
                except Exception:  # noqa: BLE001 — logging must never break the build
                    pass
            continue  # dropped — never re-added
        if act == "trim":
            try:
                sc = max(0.0, min(1.0, float(d.get("scale", 1.0) or 1.0)))
            except (TypeError, ValueError):
                sc = 1.0
            try:
                new_w = round(float(r.get("weight") or 0.0) * sc, 4)
            except (TypeError, ValueError):
                new_w = 0.0
            r["weight"] = new_w
            r["gate"] = {"action": "trim", "scale": sc, "rationale": d.get("reason", "")}
            if new_w > 0:
                kept.append(r)
            continue
        kept.append(r)  # unknown action → fail-open (keep)
    return kept


def _write_artifacts(asof: str, scope: str, payload: dict | None, result: dict | None) -> str | None:
    """Write the Gate Officer decision to ``data/gate_officer/<asof>/`` (decisions.json + gate.md).
    Best-effort, never raises."""
    d = _ARTIFACTS / (str(asof)[:10] or date.today().isoformat())
    d.mkdir(parents=True, exist_ok=True)
    (d / "decisions.json").write_text(json.dumps(
        {"agent": "gate_officer", "scope": scope, "input": payload, "result": result},
        indent=2, default=str))
    res = result or {}
    md = [f"# Gate Officer — {scope} ({str(asof)[:10]})", "",
          f"**Book view:** {res.get('book_view', '')}", "",
          f"**Rationale:** {res.get('rationale', '')}", "", "## Decisions", ""]
    for dec in (res.get("decisions") or []):
        if dec.get("action") == "approve":
            continue
        md.append(f"- **{dec.get('ticker')}** → {str(dec.get('action', '')).upper()} "
                  f"(scale {dec.get('scale')}) — {dec.get('reason', '')}")
    if not any(x.get("action") != "approve" for x in (res.get("decisions") or [])):
        md.append("- (no de-risking actions — whole book approved)")
    (d / "gate.md").write_text("\n".join(md))
    return str(d)
