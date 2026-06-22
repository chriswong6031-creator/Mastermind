"""Unified China candidate INTAKE — the China Brain's funnel from the macro China desks.

The all-China book's analogue of ``brain/intake.py`` (which funnels the US dashboard). It
reads every per-ticker China signal surface the macro dashboard publishes — the A-share buy
board (``china_standouts``), the residual-alpha leaders (``china_alpha``), the oversold
reversal watch (``china_reversal``), and the Hong-Kong buy board (``hk_standouts``) — plus the
China macro-regime frame, and reduces them to ONE deduped, ranked candidate queue with full
PROVENANCE. Corroboration across independent desks lifts a name.

CONTRACT (same as intake): pure-ish + degrade-never-raise. Every source is optional; when a
vendored China artifact is absent the queue degrades (ultimately to a small all-China seed) so
the Brain is never empty. Nothing here sizes or executes — it decides WHAT to look at. Tickers
keep their venue suffix (``*.SS`` / ``*.SZ`` mainland, ``*.HK`` Hong Kong, bare = US ADR) so the
pricing layer can route + FX-convert them.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)

_ROOT = Path(__file__).resolve().parent.parent
_V = _ROOT / "vendor" / "macro"

_CORROBORATION = 0.08          # each INDEPENDENT desk beyond the first adds this

# A small, liquid all-China seed across venues so the queue is never empty pre-build.
_SEED = ["600519.SS", "300750.SZ", "601318.SS", "000858.SZ",   # mainland A-shares
         "0700.HK", "9988.HK", "3690.HK", "1810.HK",            # Hong Kong
         "BABA", "PDD", "JD"]                                    # US-listed ADRs


def _read(rel: str):
    """Read a JSON artifact from the vendored macro site/data tree. None if absent."""
    for base in ("site", "data"):
        p = _V / base / rel
        try:
            if p.exists():
                return json.loads(p.read_text())
        except Exception as e:  # noqa: BLE001
            log.debug("china_intake: read %s failed (%s)", p, e)
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


def _conviction_score(rec: dict):
    """China buy-board ``conviction`` is a DICT ({score, band, ...}) — not a scalar like the US
    board. Pull the numeric 0–100 score out safely (returns None if shaped unexpectedly)."""
    c = rec.get("conviction")
    if isinstance(c, dict):
        return _f(c.get("score"))
    return _f(c)


def _entry_gate(rec: dict) -> tuple[bool, str | None]:
    """Read the board's ENTRY-timing gate out of the conviction object. ~40% of A-share buy rows
    are 'good company, bad entry' (``conviction.size.bucket == 'avoid'`` or ``cycle_blocked`` —
    "Extended — don't chase; wait for a pullback"). Returns (blocked, verdict-note) so the funnel
    can refuse to rank a blocked-entry name at the top with a BUY lean. Degrade-safe."""
    c = rec.get("conviction")
    if not isinstance(c, dict):
        return False, None
    size = c.get("size") if isinstance(c.get("size"), dict) else {}
    blocked = (size.get("bucket") == "avoid") or bool(c.get("cycle_blocked"))
    return blocked, (c.get("verdict") or size.get("note"))


# --------------------------------------------------------------------------- #
# per-source loaders → {TICKER: {"score": 0..1, "reason", "lean", "confidence", "falsifier"}}
# --------------------------------------------------------------------------- #
def _from_standouts() -> dict:
    """A-share buy board (china_standouts / china_setups): the ranked single-name desk."""
    d = _read("factordata/china_standouts.json") or _read("factordata/china_setups.json") or {}
    out = {}
    for s in (d.get("buy") or d.get("standouts") or []):
        if not isinstance(s, dict):
            continue                                  # one malformed row must not sink the desk
        t = _u(s.get("ticker"))
        if not t:
            continue
        conv = _conviction_score(s)
        label = (s.get("label") or s.get("state") or "standout")
        up = (s.get("dir") or s.get("eq_dir")) == "up"
        blocked, verdict = _entry_gate(s)
        score = min(max((conv if conv is not None else 50.0) / 100.0, 0.0), 1.0)
        # 'good company, bad entry': haircut the score so clean setups outrank it, and never emit a
        # BUY lean on a blocked entry (the conviction.score alone would float 'don't chase' to the top).
        lean = 1 if up else -1 if (s.get("dir") == "down") else 0
        if blocked:
            score *= 0.6
            lean = 0
        reason = f"A-share buy-board: {label}" + (f" ({s['urgency']})" if s.get("urgency") else "")
        if blocked and verdict:
            reason += f" — entry gated: {verdict}"
        out[t] = {"score": score, "reason": reason, "lean": lean,
                  "confidence": None, "falsifier": None}
    return out


def _from_alpha() -> dict:
    """Residual-alpha leaders (china_alpha.top): momentum/quality screen, lean up."""
    d = _read("factordata/china_alpha.json") or {}
    out = {}
    rows = d.get("top") or []
    if not rows and isinstance(d.get("per_ticker"), dict):
        rows = [{"ticker": k, **(v or {})} for k, v in d["per_ticker"].items() if isinstance(v, dict)][:30]
    for r in rows:
        if not isinstance(r, dict):
            continue
        t = _u(r.get("ticker"))
        a = _f(r.get("alpha"))
        if not t or a is None:
            continue
        intact = (r.get("entry") == "intact")
        out[t] = {"score": min(max(a / 3.0, 0.0), 1.0),   # alpha ~0–3 → 0–1
                  "reason": f"alpha leader (resid α {a:.2f}, entry {r.get('entry') or '?'})",
                  "lean": 1 if intact else 0, "confidence": None, "falsifier": None}
    return out


def _from_reversal() -> dict:
    """Oversold reversal watch (china_reversal.watch): bottoming candidates, capped weak lean."""
    d = _read("factordata/china_reversal.json") or {}
    out = {}
    for r in (d.get("watch") or []):
        if not isinstance(r, dict):
            continue
        t = _u(r.get("ticker"))
        z = _f(r.get("rev_z"))
        if not t or z is None:
            continue
        out[t] = {"score": min(max(z / 4.0, 0.0), 0.5),   # reversal alone is weak — capped
                  "reason": f"reversal watch (rev_z {z:.1f}, 3m {r.get('ret_3m')}%)",
                  "lean": 1, "confidence": None, "falsifier": None}
    return out


def _from_hk() -> dict:
    """Hong-Kong buy board (hk_standouts.buy): the HK leg of the universe."""
    d = _read("factordata/hk_standouts.json") or {}
    out = {}
    for s in (d.get("buy") or d.get("standouts") or []):
        if not isinstance(s, dict):
            continue
        t = _u(s.get("ticker"))
        if not t:
            continue
        conv = _conviction_score(s)
        up = (s.get("dir") or s.get("eq_dir")) == "up"
        blocked, verdict = _entry_gate(s)
        score = min(max((conv if conv is not None else 55.0) / 100.0, 0.0), 1.0)
        lean = 1 if up else -1 if (s.get("dir") == "down") else 0
        if blocked:
            score *= 0.6
            lean = 0
        reason = (f"HK buy-board: {s.get('label') or 'standout'}"
                  + (f" ({s['role']})" if s.get("role") else ""))
        if blocked and verdict:
            reason += f" — entry gated: {verdict}"
        out[t] = {"score": score, "reason": reason, "lean": lean,
                  "confidence": None, "falsifier": None}
    return out


# source name -> loader attribute (resolved through globals at call time so a monkeypatch on a
# loader takes effect and a single failing source never sinks the funnel).
_SOURCES = ("standout", "alpha", "reversal", "hk")
_LOADERS = {"standout": "_from_standouts", "alpha": "_from_alpha",
            "reversal": "_from_reversal", "hk": "_from_hk"}


def _china_frame() -> dict:
    """The China macro-regime frame for the Brain's context (quad / liquidity)."""
    raw = _read("china_regime/latest.json") or {}
    if not raw:
        return {}
    return {"as_of": raw.get("date"), "quad": raw.get("quad"),
            "quad_name": raw.get("quad_name"), "liquidity_overlay": raw.get("liquidity_overlay"),
            "cycle_tag": raw.get("cycle_tag")}


def build(limit: int = 40) -> dict:
    """The unified China intake queue + macro frame. Reads vendored artifacts; never raises.

    Returns {as_of?, macro_context, n_universe, candidates:[{ticker, score, sources, reasons,
    lean, confidence, falsifier, n_sources, venue}], note}."""
    per_source: dict[str, dict] = {}
    for name in _SOURCES:
        try:
            per_source[name] = globals()[_LOADERS[name]]() or {}
        except Exception as e:  # noqa: BLE001
            log.debug("china_intake: source %s failed (%s)", name, e)
            per_source[name] = {}

    macro = _china_frame()

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
            if isinstance(_lean, (int, float)) and not isinstance(_lean, bool):
                m["lean_votes"].append(int(_lean))
            if rec.get("confidence") is not None and (m["confidence"] is None or rec["confidence"] > m["confidence"]):
                m["confidence"] = rec["confidence"]
            if rec.get("falsifier") and not m["falsifier"]:
                m["falsifier"] = rec["falsifier"]

    out = []
    for t, m in merged.items():
        indep = len(set(m["sources"]))
        base = max(m["_scores"]) if m["_scores"] else 0.0
        score = round(min(base + _CORROBORATION * max(indep - 1, 0), 1.0), 3)
        votes = m["lean_votes"]
        lean = (1 if sum(votes) > 0 else -1 if sum(votes) < 0 else 0) if votes else None
        out.append({"ticker": t, "score": score, "sources": sorted(set(m["sources"])),
                    "n_sources": indep, "reasons": m["reasons"][:4], "lean": lean,
                    "confidence": m["confidence"], "falsifier": m["falsifier"],
                    "venue": _venue(t)})

    if not out:
        for t in _SEED:
            out.append({"ticker": t, "score": 0.3, "sources": ["seed"], "n_sources": 0,
                        "reasons": ["all-China seed (China desks not built yet)"],
                        "lean": None, "confidence": None, "falsifier": None, "venue": _venue(t)})

    out.sort(key=lambda x: (x["score"], x["n_sources"]), reverse=True)
    return {"as_of": macro.get("as_of"), "macro_context": macro,
            "n_universe": len(out), "candidates": out[:max(0, limit)],
            "note": "Unified China intake across the A-share buy board, alpha leaders, reversal "
                    "watch, and the HK board — corroboration across independent desks lifts a "
                    "name. Context-only; decides what to look at, never sizes."}


def _venue(ticker: str) -> str:
    t = (ticker or "").upper()
    if t.endswith(".SS") or t.endswith(".SZ"):
        return "A-share"
    if t.endswith(".HK"):
        return "HK"
    return "ADR"


def _stock_name(sub: str, ticker: str) -> str | None:
    """The `name` field from a vendored per-name stockdata file, if present."""
    raw = _read(f"{sub}/{ticker}.json")
    if isinstance(raw, dict):
        n = raw.get("name")
        return n if isinstance(n, str) and n.strip() else None
    return None


def display_name(ticker: str) -> str:
    """A human display name for a holding, by venue:
      * A-shares (`*.SS`/`*.SZ`) → the **Chinese** name (the macro `name` is "English / 中文";
        we take the 中文 half).
      * Hong Kong (`*.HK`) and US ADRs → the **English** name (take the English half if combined).
    Falls back to the ticker when no name is published. Degrade-never-raise."""
    t = (ticker or "").upper().strip()
    if not t:
        return ""
    if t.endswith(".SS") or t.endswith(".SZ"):
        raw = _stock_name("chinastockdata", t)
        if raw and " / " in raw:
            return raw.split(" / ", 1)[1].strip() or t   # Chinese half
        return raw or t
    sub = "hkstockdata" if t.endswith(".HK") else "stockdata"
    raw = _stock_name(sub, t)
    if raw and " / " in raw:
        return raw.split(" / ", 1)[0].strip() or t       # English half
    return raw or t


def queue(limit: int = 40) -> list[dict]:
    """Just the ranked candidate list (provenance kept)."""
    return build(limit)["candidates"]


def tickers(limit: int = 40, min_score: float = 0.0) -> list[str]:
    """Ranked tickers only — for callers that just want the expanded universe."""
    return [c["ticker"] for c in queue(limit) if c["score"] >= min_score]
