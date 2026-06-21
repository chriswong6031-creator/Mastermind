"""Unified ticker INTAKE — the brain's candidate funnel from the dashboard's signal engines.

The bot used to consider a STATIC 20-name shortlist (portfolio/conviction._SHORTLIST) plus
whatever theses were already open. That is a bottleneck: the dashboard surfaces dozens of
fresh, ranked signals every day (the Phase-5 briefing queue, the divergence radar, the
alt-data desk, the factor buy-board, news surges) and none of them flowed into what the
brain actually looks at.

This module is the funnel. It reads every per-ticker signal surface the macro dashboard
publishes (via the vendored macro checkout) and reduces them to ONE deduped, ranked
candidate queue with full PROVENANCE — for each name, which engines flagged it, why, the
directional lean, the confidence, and the falsifier. Corroboration across independent
engines lifts a name; a lone weak signal stays low.

CONTRACT: pure-ish + degrade-never-raise. Every source is optional; when the vendored
dashboard artifacts are absent (e.g. before the macro side has built them) the queue
degrades to the open ledger theses plus the static seed, so the bot is never empty. Nothing
here sizes or executes — it decides WHAT to look at, not what to do.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)

_ROOT = Path(__file__).resolve().parent.parent
_V = _ROOT / "vendor" / "macro"

# corroboration bonus: each INDEPENDENT engine beyond the first adds this to the base score
_CORROBORATION = 0.08
# a name flagged by the briefing's divergence block is high-information — lift it
_DIVERGENCE_BONUS = 0.12

# static seed so the queue is never empty when the dashboard hasn't built yet
_SEED = ["NVDA", "AVGO", "AMD", "MU", "PLTR", "GEV", "MSFT", "GOOGL", "META", "ANET"]


def _read(rel: str):
    """Read a JSON artifact from the vendored macro site/data tree. None if absent."""
    for base in ("site", "data"):
        p = _V / base / rel
        try:
            if p.exists():
                return json.loads(p.read_text())
        except Exception as e:  # noqa: BLE001
            log.debug("intake: read %s failed (%s)", p, e)
    return None


def _f(x):
    if x is None or isinstance(x, (dict, list, bool)):
        return None
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _u(t) -> str:
    return (t or "").upper().strip()


# --------------------------------------------------------------------------- #
# per-source loaders → {TICKER: {"score": 0..1, "reason": str, "lean": int|None,
#                                "confidence": float|None, "falsifier": str|None}}
# Each loader is independent and degrade-safe (returns {} on any miss).
# --------------------------------------------------------------------------- #
def _from_briefing() -> tuple[dict, dict, dict]:
    """The Phase-5 ranked briefing: priority_queue (gold ranking) + divergences + macro frame."""
    b = _read("intelligence/briefing.json") or {}
    queue, div = {}, {}
    for x in b.get("priority_queue") or []:
        t = _u(x.get("ticker"))
        if not t:
            continue
        queue[t] = {"score": _f(x.get("priority")) or 0.0,
                    "reason": x.get("situation") or x.get("read") or "briefing priority",
                    "lean": x.get("lean"), "confidence": _f(x.get("confidence")),
                    "falsifier": x.get("falsifier")}
    for x in b.get("divergences") or []:
        t = _u(x.get("ticker"))
        if t:
            div[t] = {"score": _DIVERGENCE_BONUS,
                      "reason": f"divergence: {x.get('read') or 'tape vs smart-money disagree'}",
                      "lean": x.get("lean"), "confidence": _f(x.get("confidence")),
                      "falsifier": x.get("falsifier")}
    macro = dict(b.get("macro_context") or {})
    if b.get("as_of"):
        macro.setdefault("as_of", b.get("as_of"))     # surface the briefing date on the frame
    return queue, div, macro


def _from_standouts() -> dict:
    d = _read("factordata/us_standouts.json") or {}
    out = {}
    for s in (d.get("buy") or d.get("standouts") or []):
        t = _u(s.get("ticker"))
        if not t:
            continue
        conv = _f(s.get("conviction"))
        out[t] = {"score": min(max(conv if conv is not None else 0.5, 0.0), 1.0),
                  "reason": f"buy-board: {s.get('label') or s.get('state') or 'standout'}",
                  "lean": -1 if "AVOID" in (s.get("label") or "").upper() else 1,
                  "confidence": None, "falsifier": None}
    return out


_POS_RADAR = {"POSITIVE_DIVERGENCE", "CONFIRMED_UP"}
_NEG_RADAR = {"NEGATIVE_DIVERGENCE", "CONFIRMED_DOWN"}


def _from_radar() -> dict:
    d = _read("basketdata/radar_ticker.json") or {}
    rows = d.get("tickers") or []
    if isinstance(rows, dict):                       # tolerate either shape
        rows = list(rows.values())
    out = {}
    for r in rows:
        t = _u(r.get("ticker"))
        state = r.get("state")
        if not t or state == "QUIET" or state is None:
            continue
        edge = _f(r.get("edge_score"))
        out[t] = {"score": min((edge or 0) / 100.0, 1.0),
                  "reason": f"radar {state} (edge {r.get('edge_score')})",
                  "lean": 1 if state in _POS_RADAR else -1 if state in _NEG_RADAR else 0,
                  "confidence": None, "falsifier": r.get("note")}
    return out


def _from_altdata() -> dict:
    d = _read("altdata/mastermind.json") or {}
    out = {}
    for s in (d.get("signals") or []):
        t = _u(s.get("ticker"))
        sc = _f(s.get("signal_score"))
        if not t or sc is None:
            continue
        act = s.get("action")
        out[t] = {"score": min(abs(sc - 50.0) / 50.0, 1.0),
                  "reason": f"alt-data {act or ''} (score {int(sc)}, {','.join(s.get('channels') or [])})".strip(),
                  "lean": 1 if (sc >= 65 and act != "AVOID") else -1 if (act == "AVOID" or sc < 35) else 0,
                  "confidence": None, "falsifier": s.get("falsifier")}
    return out


def _from_news_surge(min_recent: int = 4) -> dict:
    d = _read("news/by_ticker.json") or {}
    out = {}
    for t, rec in (d.get("tickers") or {}).items():
        t = _u(t)
        n = _f(rec.get("n_recent"))
        if not t or n is None or n < min_recent:
            continue
        lean = {"pos": 1, "neg": -1}.get(rec.get("sentiment_lean"), 0)
        out[t] = {"score": min(n / 12.0, 0.5),       # news alone is weak — capped
                  "reason": f"news surge: {int(n)} recent headlines ({rec.get('sentiment_lean')})",
                  "lean": lean, "confidence": None, "falsifier": None}
    return out


def _from_open_theses() -> dict:
    try:
        from brain import ledger
        out = {}
        for th in ledger.all_theses():
            if th.get("status") != "open":
                continue
            t = _u(th.get("subject"))
            if t:
                out[t] = {"score": 0.55, "reason": "open thesis (in play)",
                          "lean": None, "confidence": None,
                          "falsifier": (th.get("falsifier") or {}).get("text") if isinstance(th.get("falsifier"), dict) else None}
        return out
    except Exception as e:  # noqa: BLE001
        log.debug("intake: open-theses load failed (%s)", e)
        return {}


# simple source name -> loader attribute name. Resolved through module globals at call time
# (NOT captured here) so a monkeypatch on the loader takes effect and a single failing source
# never sinks the funnel. Order is display-only; scoring is provenance-blended.
_SIMPLE_SOURCES = ("standout", "radar", "altdata", "news", "thesis")
_LOADERS = {"standout": "_from_standouts", "radar": "_from_radar", "altdata": "_from_altdata",
            "news": "_from_news_surge", "thesis": "_from_open_theses"}


def build(limit: int = 40) -> dict:
    """The unified intake queue + macro frame. PURE w.r.t. inputs; reads vendored artifacts.

    Returns {as_of?, macro_context, n_universe, candidates:[{ticker, score, sources:[...],
    reasons:[...], lean, confidence, falsifier, n_sources}], note}. Never raises."""
    briefing_q, briefing_div, macro = _from_briefing()
    per_source: dict[str, dict] = {"briefing": briefing_q, "divergence": briefing_div}
    for name in _SIMPLE_SOURCES:
        try:
            per_source[name] = globals()[_LOADERS[name]]() or {}   # late-bound → monkeypatch-able
        except Exception as e:  # noqa: BLE001
            log.debug("intake: source %s failed (%s)", name, e)
            per_source[name] = {}

    # merge by ticker — collect provenance, blend score (max base + corroboration bonus)
    merged: dict[str, dict] = {}
    for src, table in per_source.items():
        for t, rec in table.items():
            m = merged.setdefault(t, {"ticker": t, "sources": [], "reasons": [],
                                      "_scores": [], "lean_votes": [], "confidence": None,
                                      "falsifier": None})
            m["sources"].append(src)
            if rec.get("reason"):
                m["reasons"].append(rec["reason"])
            m["_scores"].append(rec.get("score") or 0.0)
            _lean = rec.get("lean")
            # only NUMERIC leans vote — a source JSON can carry a string lean (e.g. an arrow glyph),
            # which would TypeError in the sum() below; coerce/skip rather than crash the funnel.
            if isinstance(_lean, (int, float)) and not isinstance(_lean, bool):
                m["lean_votes"].append(int(_lean))
            if rec.get("confidence") is not None and (m["confidence"] is None or rec["confidence"] > m["confidence"]):
                m["confidence"] = rec["confidence"]
            if rec.get("falsifier") and not m["falsifier"]:
                m["falsifier"] = rec["falsifier"]

    out = []
    for t, m in merged.items():
        indep = len([s for s in m["sources"] if s != "divergence"])   # divergence is a flag, not an engine
        base = max(m["_scores"]) if m["_scores"] else 0.0
        score = round(min(base + _CORROBORATION * max(indep - 1, 0)
                          + (_DIVERGENCE_BONUS if "divergence" in m["sources"] else 0.0), 1.0), 3)
        votes = m["lean_votes"]
        lean = (1 if sum(votes) > 0 else -1 if sum(votes) < 0 else 0) if votes else None
        out.append({"ticker": t, "score": score, "sources": sorted(set(m["sources"])),
                    "n_sources": indep, "reasons": m["reasons"][:4], "lean": lean,
                    "confidence": m["confidence"], "falsifier": m["falsifier"],
                    "divergent": "divergence" in m["sources"]})

    # seed fallback so the queue is never empty (inert/pre-build state)
    if not out:
        for t in _SEED:
            out.append({"ticker": t, "score": 0.3, "sources": ["seed"], "n_sources": 0,
                        "reasons": ["static seed (dashboard signals not built yet)"],
                        "lean": None, "confidence": None, "falsifier": None, "divergent": False})

    out.sort(key=lambda x: (x["score"], x["n_sources"]), reverse=True)
    return {"as_of": macro.get("as_of"), "macro_context": macro,
            "n_universe": len(out), "candidates": out[:max(0, limit)],
            "note": "Unified intake across the dashboard signal engines — corroboration across "
                    "independent engines lifts a name. Context-only; decides what to look at, never sizes."}


def queue(limit: int = 40) -> list[dict]:
    """Just the ranked candidate list (provenance kept)."""
    return build(limit)["candidates"]


def tickers(limit: int = 40, min_score: float = 0.0) -> list[str]:
    """Ranked tickers only — for callers that just want the expanded universe."""
    return [c["ticker"] for c in queue(limit) if c["score"] >= min_score]


def salience_tiers(limit: int = 40) -> dict:
    """Split the queue into a two-stage triage for the research desk:
    ACT (high score, corroborated), WATCH (lower), and the DIVERGENCE focus list."""
    cands = queue(limit)
    act = [c for c in cands if c["score"] >= 0.6 and c["n_sources"] >= 2]
    watch = [c for c in cands if c not in act]
    divergent = [c for c in cands if c["divergent"]]
    return {"act": act, "watch": watch, "divergent": divergent}
