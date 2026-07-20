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
import os
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


def _respect_standout_gate() -> bool:
    """Doctrine toggle (P-NEW-2): honour the standout board's own `gate_go` verdict. Default TRUE;
    a missing/unreadable doctrine key degrades to respecting the gate. Skipping only ever removes a
    corroboration source, never adds one, so defaulting to True is invariant-safe."""
    try:
        from bot.doctrine_config import load_doctrine
        v = load_doctrine().get("us_standouts_respect_gate_go")
        return True if v is None else bool(v)
    except Exception:  # noqa: BLE001
        return True


def _standout_ungated_enabled() -> bool:
    """Return True iff MASTERMIND_STANDOUT_UNGATED is set truthy (default OFF).

    LEARNING-LOOP gate_go fix: when ON, a board with an explicit ``gate_go=False`` is NOT skipped —
    its names flow in as CANDIDACY (never sizing) so the desk can look at them on their own merits,
    tagged as an unvalidated event-edge. Default OFF ⇒ the standout source is byte-identical to today
    (an explicit gate_go=False still skips the whole source). Mirrors nw_prompts_enabled's truthy set;
    fail-soft — an env read never sinks the funnel."""
    try:
        return os.environ.get("MASTERMIND_STANDOUT_UNGATED", "0").strip().lower() in (
            "1", "true", "yes", "on")
    except Exception:  # noqa: BLE001
        return False


def _board_learning_enabled() -> bool:
    """Return True iff MASTERMIND_BOARD_LEARNING is set truthy (default OFF).

    LEARNING-LOOP trust multiplier: when ON, each standout candidate's score is multiplied by
    board_learning.standout_trust_multiplier() (SHRINK-ONLY in [0.5, 1.0]; an empty/insufficient board
    → 1.0 so the funnel is unchanged). Default OFF ⇒ no multiplier is applied (byte-identical to today).
    Mirrors nw_prompts_enabled's truthy set; fail-soft."""
    try:
        return os.environ.get("MASTERMIND_BOARD_LEARNING", "0").strip().lower() in (
            "1", "true", "yes", "on")
    except Exception:  # noqa: BLE001
        return False


# when the ungated gate_go fix admits an unvalidated board (gate_go=False), its base conviction is
# damped by this factor to reflect that the board has NOT cleared its own Phase-0 gate — a candidate,
# never a veto, and modestly lower than a gate-cleared board. Only ever applied on the ungated path.
_UNGATED_SCORE_DAMP = 0.75


def _from_standouts() -> dict:
    d = _read("factordata/us_standouts.json") or {}
    # RESPECT THE BOARD'S OWN GATE (P-NEW-2): when the dashboard's `gate_go` verdict is explicitly
    # False the standout board is a confluence read, NOT a validated buy list — so it must contribute
    # NO positive corroboration to the intake funnel. Skip the whole source. Invariant-safe: gate_go
    # missing (None) or truthy → today's behaviour (ingest); only an explicit False skips, and only
    # while the doctrine toggle is on. Skipping removes a source, never adds one.
    #
    # LEARNING-LOOP gate_go fix (MASTERMIND_STANDOUT_UNGATED, default OFF): when the flag is ON the
    # gate_go=False board is NOT skipped — its names flow in as CANDIDACY (candidacy != sizing), tagged
    # as an unvalidated event-edge and damped by _UNGATED_SCORE_DAMP. When the flag is OFF (default) the
    # skip below is byte-identical to today's behaviour.
    gate_go = d.get("gate_go")
    ungated = gate_go is False and _respect_standout_gate() and _standout_ungated_enabled()
    if gate_go is False and _respect_standout_gate() and not _standout_ungated_enabled():
        rows = d.get("buy") or d.get("standouts") or []
        log.warning("us_standouts gate_go=False (board not statistically validated) — skipping "
                    "%d standout buy names from intake funnel", len(rows))
        return {}
    if ungated:
        rows = d.get("buy") or d.get("standouts") or []
        log.warning("us_standouts gate_go=False — MASTERMIND_STANDOUT_UNGATED on: admitting "
                    "%d standout names as candidacy (event-edge, unvalidated)", len(rows))
    out = {}
    for s in (d.get("buy") or d.get("standouts") or []):
        t = _u(s.get("ticker"))
        if not t:
            continue
        conv = _f(s.get("conviction"))
        base = min(max(conv if conv is not None else 0.5, 0.0), 1.0)
        # on the ungated path, damp the base conviction (candidacy, not sizing; board unvalidated) and
        # tag the reason so provenance shows WHY the name is here despite the board's own no-go gate.
        if ungated:
            base = round(base * _UNGATED_SCORE_DAMP, 3)
            reason = (f"buy-board (gate_go=false event-edge): "
                      f"{s.get('label') or s.get('state') or 'standout'}")
        else:
            reason = f"buy-board: {s.get('label') or s.get('state') or 'standout'}"
        # carry the PUBLISHED entry-risk levels (P-NEW-3) into the provenance record so the candidate
        # funnel shows the board's stop / buy_zone / entry_grade. Purely additive — these do NOT size
        # or gate the name (that stays the conviction sleeve's job); None-on-miss for legacy rows.
        _es = s.get("entry_signal") if isinstance(s.get("entry_signal"), dict) else {}
        out[t] = {"score": base,
                  "reason": reason,
                  "lean": -1 if "AVOID" in (s.get("label") or "").upper() else 1,
                  "confidence": None, "falsifier": None,
                  "stop": _es.get("stop"), "buy_zone": _es.get("buy_zone"),
                  "entry_grade": _es.get("entry_grade")}

    # LEARNING-LOOP trust multiplier (MASTERMIND_BOARD_LEARNING, default OFF): shrink each standout
    # candidate's score by the board's PROVEN forward edge. SHRINK-ONLY in [0.5, 1.0]; an empty /
    # insufficient board → 1.0 so this is a no-op. Default OFF ⇒ no multiplier (byte-identical). Lazy
    # import (codebase convention) so importing intake never pulls board_learning. Fail-soft: any
    # error leaves the scores untouched.
    if out and _board_learning_enabled():
        try:
            from brain import board_learning
            mult = board_learning.standout_trust_multiplier()
            if mult is not None and mult != 1.0:
                for rec in out.values():
                    rec["score"] = round(min(max(rec["score"] * float(mult), 0.0), 1.0), 3)
        except Exception as e:  # noqa: BLE001 — fail-soft: a broken learning loop never sinks intake
            log.debug("intake: board_learning multiplier failed (%s)", e)
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


# --------------------------------------------------------------------------- #
# P2 REWORK FUNNEL WIRING (roadmap docs/design/REWORK_ROADMAP_2026-07-11.md).
#
# Four ADDITIONAL candidacy sources that wire already-built, tested leaf modules
# (rotation_intake / divergence_clue / neural_web_context / regime_frame + universe_triage)
# into this funnel. THE CARDINAL RULE: with every flag at its default (OFF) each of these
# returns {} immediately and is BYTE-IDENTICAL to today's behaviour — the source contributes
# nothing to the merge. Each is fail-soft (any leaf error → {}, never a raise) and emits
# candidates in the SAME schema as the sources above
# ({score, reason, lean, confidence?, falsifier?}). Imports are LAZY inside each function
# (the codebase convention) so importing this module never pulls the leaf import graph.
#
# FLAG SPEC (all default OFF / inert):
#   MASTERMIND_ROTATION_IN     = off|watch|starter  (rotation-in active in {watch,starter})
#   MASTERMIND_DIVERGENCE_CLUE = 0|1                (via divergence_clue.clue_flag_enabled())
#   MASTERMIND_NW_DECISION     = off|shadow|candidacy|shrink|vote (NW active at >= candidacy;
#                                via neural_web_context.nw_decision_mode())
#   MASTERMIND_UNIVERSE_TRIAGE = 0|1                (cycle-bottoming active when truthy)
# --------------------------------------------------------------------------- #

# rotation-in candidacy score band by call state (× the call's confidence). Starter-grade
# priors, never authority — mirror the rotation seam's EARLY→CONFIRMED confidence ladder.
_ROTATION_IN_STATE_SCORE = {"EARLY": 0.35, "TURNING": 0.50, "CONFIRMED": 0.65}


def _flag_on(name: str) -> bool:
    """True iff env var `name` is set to a truthy 0|1-style token (default OFF). Fail-soft.

    Mirrors neural_web_context.nw_prompts_enabled / divergence_clue.clue_flag_enabled — the
    canonical truthy set. Used for the 0|1 flags (MASTERMIND_UNIVERSE_TRIAGE); the ladder flags
    (ROTATION_IN / NW_DECISION) read their own vocabularies below.
    """
    try:
        return os.environ.get(name, "0").strip().lower() in ("1", "true", "yes", "on")
    except Exception:  # noqa: BLE001 — fail-soft: an env read never sinks the funnel
        return False


def _rotation_in_mode() -> str:
    """Return the MASTERMIND_ROTATION_IN ladder value (off|watch|starter), default 'off'. Fail-soft.

    Rotation-in candidacy is active when the mode is in {'watch','starter'}; any unrecognized /
    empty value degrades to 'off' (inert).
    """
    try:
        # W8 (2026-07-19): default 'watch' — mirrors bot.phase2._rotation_in_mode verbatim.
        raw = os.environ.get("MASTERMIND_ROTATION_IN", "watch").strip().lower()
        return raw if raw in ("off", "watch", "starter") else "off"
    except Exception:  # noqa: BLE001
        return "off"


def _from_rotation_in() -> dict:
    """Rotation-calls → member-ticker candidacy (roadmap §2, MASTERMIND_ROTATION_IN).

    INERT unless MASTERMIND_ROTATION_IN in {watch, starter}. For each active rotation call
    (rotation_intake.active_calls), expand it to member rows (rotation_intake.expand) and emit
    one candidate per member: score = the state band (EARLY 0.35 / TURNING 0.50 / CONFIRMED 0.65)
    scaled by the call's confidence, lean +1, provenance-tagged with the call_id + state.
    Fail-soft → {} on any leaf error (the rotation lane is then provably inert this build)."""
    if _rotation_in_mode() not in ("watch", "starter"):
        return {}
    try:
        from brain import rotation_intake
        out: dict = {}
        for call in rotation_intake.active_calls() or []:
            if not isinstance(call, dict):
                continue
            state = call.get("state")
            base = _ROTATION_IN_STATE_SCORE.get(state)
            if base is None:                       # terminal / unknown state → not a candidacy read
                continue
            conf = _f(call.get("confidence"))
            score = min(max(base * (conf if conf is not None else 1.0), 0.0), 1.0)
            cid = call.get("call_id") or call.get("target") or "?"
            for m in rotation_intake.expand(call) or []:
                if not isinstance(m, dict):
                    continue
                t = _u(m.get("ticker"))
                if not t or t in out:              # first (highest-ranked) member row wins per name
                    continue
                out[t] = {"score": round(score, 3),
                          "reason": f"rotation_in {cid} {state}",
                          "lean": 1, "confidence": conf, "falsifier": call.get("falsifier")}
        return out
    except Exception as e:  # noqa: BLE001 — fail-soft: never raise into the funnel
        log.debug("intake: rotation_in source failed (%s)", e)
        return {}


def _from_divergence_clue() -> dict:
    """Single-stock early-divergence clues → candidacy (roadmap B5, MASTERMIND_DIVERGENCE_CLUE).

    INERT unless divergence_clue.clue_flag_enabled() (reads MASTERMIND_DIVERGENCE_CLUE, default 0).
    Emits one candidate per scanned clue row carrying the row's own score / sector / safe_haven /
    falsifier. lean +1 (a clue is a BULLISH diverger by construction). Fail-soft → {}."""
    try:
        from brain import divergence_clue
        if not divergence_clue.clue_flag_enabled():
            return {}
        out: dict = {}
        for row in divergence_clue.scan() or []:
            if not isinstance(row, dict):
                continue
            t = _u(row.get("ticker"))
            if not t or t in out:
                continue
            sector = row.get("sector") or row.get("sector_etf")
            out[t] = {"score": min(max(_f(row.get("score")) or 0.0, 0.0), 1.0),
                      "reason": f"divergence_clue {sector} safe_haven={bool(row.get('safe_haven'))}",
                      "lean": 1, "confidence": None, "falsifier": row.get("falsifier")}
        return out
    except Exception as e:  # noqa: BLE001 — fail-soft
        log.debug("intake: divergence_clue source failed (%s)", e)
        return {}


def _from_neural_web() -> dict:
    """Neural-Web typed candidacy signals → candidacy (roadmap A5, MASTERMIND_NW_DECISION).

    INERT unless neural_web_context.nw_decision_mode() is at 'candidacy' or above. For each name
    in the NW candidate_context, ask decision_signals(TICKER) for its typed candidacy signal; when
    present ({state, score, lean}) emit a candidate carrying that score + lean and the bottom state.
    The NW reader is itself fully flag-gated (an inert no-op at off/shadow), so this only ever sees
    a candidacy signal when the ladder is armed. Fail-soft → {}."""
    try:
        from brain import neural_web_context as nw
        if not _mode_ge(nw.nw_decision_mode(), "candidacy"):
            return {}
        cc = (nw.context() or {}).get("candidate_context")
        if not isinstance(cc, dict):
            return {}
        out: dict = {}
        for ticker in cc:
            t = _u(ticker)
            if not t or t in out:
                continue
            try:
                sig = nw.decision_signals(ticker)
            except Exception:  # noqa: BLE001 — a single bad name never sinks the source
                continue
            cand = sig.get("candidacy") if isinstance(sig, dict) else None
            if not isinstance(cand, dict):
                continue
            sc = _f(cand.get("score"))
            if sc is None:
                continue
            lean = cand.get("lean")
            out[t] = {"score": min(max(sc, 0.0), 1.0),
                      "reason": f"nw bottom={cand.get('state')}",
                      "lean": lean if isinstance(lean, int) and not isinstance(lean, bool) else None,
                      "confidence": None, "falsifier": None}
        return out
    except Exception as e:  # noqa: BLE001 — fail-soft
        log.debug("intake: neural_web source failed (%s)", e)
        return {}


# NW decision-ladder order — a local mirror of neural_web_context's ordinal ladder so the
# ">= candidacy" threshold here does not depend on importing a private constant. Unrecognized /
# empty modes resolve to rank 0 (off) → inert.
_NW_MODE_ORDER = {"off": 0, "shadow": 1, "candidacy": 2, "shrink": 3, "vote": 4}


def _mode_ge(mode: str, threshold: str) -> bool:
    """True iff NW decision `mode` is at/above `threshold` on the ladder (unknown → off, inert)."""
    return _NW_MODE_ORDER.get(mode, 0) >= _NW_MODE_ORDER.get(threshold, 0)


def _from_cycles_bottoming() -> dict:
    """Cycle-bottoming sectors → sector-ETF (+member) candidacy (roadmap A4, MASTERMIND_UNIVERSE_TRIAGE).

    INERT unless MASTERMIND_UNIVERSE_TRIAGE is truthy. From regime_frame.cycles(), every sector that
    is entry_favored AND turning up (osc_slope > 0) contributes its sector ETF as a starter candidate
    (score 0.4, lean +1). If universe_triage.favored_sectors() is reachable AND agrees the sector is
    favored, that corroboration is noted in the reason (the ETF itself is what we surface — a sector
    call has no per-name membership here). Fail-soft → {}."""
    if not _flag_on("MASTERMIND_UNIVERSE_TRIAGE"):
        return {}
    try:
        from brain import regime_frame
        cyc = regime_frame.cycles() or {}
        if not isinstance(cyc, dict):
            return {}
        # optional corroboration: the universe-triage verdict's own favored-sector list.
        favored_verdict: set[str] = set()
        try:
            from brain import universe_triage
            favored_verdict = {_u(s) for s in (universe_triage.favored_sectors() or [])}
        except Exception:  # noqa: BLE001 — universe_triage is optional; absence just drops corroboration
            favored_verdict = set()

        out: dict = {}
        for sector, row in cyc.items():
            if not isinstance(row, dict) or not row.get("entry_favored"):
                continue
            osc = _f(row.get("osc_slope"))
            if osc is None or osc <= 0:            # require turning UP — absent slope never fabricates
                continue
            etf = _u(sector)
            if not etf or etf in out:
                continue
            corrob = " (triage-favored)" if etf in favored_verdict else ""
            out[etf] = {"score": 0.4, "reason": f"cycle_bottoming {etf}{corrob}",
                        "lean": 1, "confidence": None, "falsifier": None}
        return out
    except Exception as e:  # noqa: BLE001 — fail-soft
        log.debug("intake: cycles_bottoming source failed (%s)", e)
        return {}


# simple source name -> loader attribute name. Resolved through module globals at call time
# (NOT captured here) so a monkeypatch on the loader takes effect and a single failing source
# never sinks the funnel. Order is display-only; scoring is provenance-blended.
# The four P2 rework sources (rotation_in / divergence_clue / neural_web / cycles_bottoming) are
# registered here so they participate in the funnel — but each returns {} when its flag is off, so
# with all flags at default this list contributes exactly nothing (byte-identical to pre-rework).
_SIMPLE_SOURCES = ("standout", "radar", "altdata", "news", "thesis",
                   "rotation_in", "divergence_clue", "neural_web", "cycles_bottoming")
_LOADERS = {"standout": "_from_standouts", "radar": "_from_radar", "altdata": "_from_altdata",
            "news": "_from_news_surge", "thesis": "_from_open_theses",
            "rotation_in": "_from_rotation_in", "divergence_clue": "_from_divergence_clue",
            "neural_web": "_from_neural_web", "cycles_bottoming": "_from_cycles_bottoming"}


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
