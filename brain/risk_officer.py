"""RISK OFFICER / EXIT MANAGER — the judgment exit over the HELD Flagship book.

This is the sell-side judgment half of P2. The mechanical detectors (D5 dead-capital time-stop,
the gate-closed hard-veto sweep, hysteresis) have ALREADY run by the time this seat fires; its job
is the JUDGMENT exit they miss — thesis invalidation from a guidance cut, a competitive shift, a
regime rotation, a relative-strength roll, or distribution. Once per build day it reviews every
held conviction position against its entry thesis + falsifier + time-stop and the current
return/technicals/regime, and rules HOLD / TRIM / EXIT.

SUBTRACT-ONLY by construction: the Risk Officer may TRIM or EXIT only — never add, never average
down, never re-enter. The code enforces this regardless of the LLM reply (only tickers actually
held may appear; the action is coerced to hold/trim/exit). A NEVER-BLOW-TO-CASH guard caps exits
per session and refuses to drop the held conviction count below a floor, so one pass can never
liquidate the book to cash.

Mirrors the committee/strategist seat pattern: an information-boundary input builder
(``_risk_input``), a seat executor (``risk_assess``) returning a gradable dict, an artifact writer
(``_write_artifacts``), plus a PURE helper (``apply_exits``) that enforces the never-blow-to-cash
guard from a decisions list with NO LLM — exhaustively unit-testable offline.

Additive + reversible: gated behind ``MASTERMIND_RISK_OFFICER`` (default OFF) AND an available
LLM. Returns ``{decisions:[], ran:False}`` on any LLM/parse failure; never raises. Model-agnostic
via ``role="deep"`` (Opus/Fable resolved by config/agents.yml).
"""
from __future__ import annotations

import json
import os
from datetime import date
from pathlib import Path

from brain import client

_ARTIFACTS = Path(__file__).resolve().parent.parent / "data" / "risk_officer"

_ACTIONS = ("hold", "trim", "exit")
# never-blow-to-cash guard: at most this many EXITS per session, and never below this many held.
_MAX_EXITS_PER_SESSION = 2
_MIN_INVESTED_NAMES = 3


def enabled() -> bool:
    """The Risk Officer runs only when explicitly armed AND an LLM is reachable. Default OFF so the
    live exit path is byte-identical until enabled."""
    flag = os.environ.get("MASTERMIND_RISK_OFFICER", "0").strip().lower()
    if flag not in ("1", "true", "yes", "on"):
        return False
    try:
        return client.available()
    except Exception:  # noqa: BLE001
        return False


# ─────────────────────────────────────────────────────────────────────────────
# input — per HELD conviction name, assembled from the position log (caller passes
# position_log.open_positions()), the thesis ledger, and the published stockdata
# lenses (current technicals). Every layer is read defensively → degrades to None
# fields so the seat is testable fully offline.
# ─────────────────────────────────────────────────────────────────────────────
def _risk_input(held: list[dict], regime: dict | None) -> dict:
    from brain import ledger
    from portfolio import lenses, paper_account

    try:
        theses = {t.get("thesis_id"): t for t in (ledger.all_theses() or [])}
    except Exception:  # noqa: BLE001
        theses = {}

    rows = []
    for p in (held or []):
        if (p or {}).get("sleeve") != "conviction":
            continue
        t = str(p.get("ticker") or "").upper().strip()
        if not t:
            continue
        try:
            sd = lenses._load(f"site/stockdata/{t}.json") or {}
        except Exception:  # noqa: BLE001
            sd = {}
        _g = lenses._g
        th = theses.get(p.get("thesis_id"), {}) or {}
        entry = p.get("entry_price")
        try:
            cur = paper_account._current_price(t)
        except Exception:  # noqa: BLE001
            cur = None
        rel = None
        try:
            if entry and cur and float(entry) > 0:
                rel = (float(cur) / float(entry)) - 1.0
        except (TypeError, ValueError):
            rel = None
        rows.append({
            "ticker": t,
            "held_days": p.get("held_days"),
            "current_weight": p.get("current_weight"),
            "time_stop_by": p.get("time_stop_by"),
            "thesis": str(th.get("thesis", ""))[:300],
            "falsifier": th.get("falsifier"),
            "check_by": th.get("check_by"),
            "rel_return_since_entry": round(rel, 4) if rel is not None else None,
            "pct_vs_200dma": _g(sd, "tech.pct_vs_200dma"),
            "rs": _g(sd, "momentum.alpha.rs"),
            "eq_grade": (_g(sd, "conviction.ext.grade") or _g(sd, "conviction.extension.grade")),
            "parabolic": bool(_g(sd, "conviction.ext.parabolic")
                              or _g(sd, "conviction.extension.parabolic")),
        })
    return {"held": rows,
            "regime": {k: (regime or {}).get(k) for k in ("quad", "quad_name", "liquidity_overlay")}}


_RISK_SYS = (
    "You are the RISK OFFICER / EXIT MANAGER over the HELD Flagship book. Mechanical detectors (D5 "
    "dead-capital time-stop, hard-veto sweep) have ALREADY run; your job is the JUDGMENT exit they "
    "miss: thesis invalidation from guidance cuts, competitive shift, regime rotation, "
    "relative-strength roll, or distribution. For each held name compare its entry thesis + "
    "falsifier + time-stop against current return/technicals/regime. You are SUBTRACT-ONLY: TRIM or "
    "EXIT only — never add, never average down, never re-enter. Hold by default; exit only when the "
    "thesis is concretely breaking. "
    "Reply ONLY JSON: {\"decisions\":[{\"ticker\":str,\"action\":\"hold|trim|exit\","
    "\"scale\":0.0-1.0,\"reason\":str,\"falsifier\":str}],\"rationale\":str}."
)


def _parse_json(txt: str) -> dict | None:
    if not txt:
        return None
    try:
        return json.loads(txt[txt.find("{"):txt.rfind("}") + 1])
    except Exception:  # noqa: BLE001
        return None


def _held_map(held: list[dict]) -> dict[str, dict]:
    """{ticker: position} over HELD CONVICTION names only (the universe the seat may act on)."""
    out: dict[str, dict] = {}
    for p in (held or []):
        if (p or {}).get("sleeve") != "conviction":
            continue
        t = str((p or {}).get("ticker") or "").upper().strip()
        if t:
            out[t] = p
    return out


def _normalize_decisions(j: dict | None, held_map: dict[str, dict]) -> list[dict]:
    """Coerce the raw LLM reply into subtract-only, held-only decisions. Pure; never raises.

    INVARIANT: only tickers in ``held_map`` survive (no re-entry / no add). Action coerced to
    hold/trim/exit; ``scale`` clamped 0..1 for trim, ignored for exit. A ``rel_return`` field is
    attached from the position (used by the never-blow-to-cash tie-break)."""
    out: list[dict] = []
    seen: set[str] = set()
    for d in (j or {}).get("decisions") or []:
        if not isinstance(d, dict):
            continue
        t = str(d.get("ticker", "")).upper().strip()
        if not t or t not in held_map or t in seen:
            continue  # only held conviction names; one decision per name
        seen.add(t)
        act = d.get("action") if d.get("action") in _ACTIONS else "hold"
        if act == "trim":
            try:
                sc = max(0.0, min(1.0, float(d.get("scale", 1.0) or 1.0)))
            except (TypeError, ValueError):
                sc = 1.0
        else:
            sc = 0.0 if act == "exit" else 1.0
        out.append({
            "ticker": t, "action": act, "scale": sc,
            "reason": str(d.get("reason", ""))[:400],
            "falsifier": str(d.get("falsifier", ""))[:300],
            "rel_return": held_map[t].get("rel_return_since_entry"),
        })
    return out


# ─────────────────────────────────────────────────────────────────────────────
# apply_exits — PURE never-blow-to-cash guard. Given the held names + normalized
# decisions, returns {exits:[...], trims:[...]} after enforcing: at most
# `max_exits` exits per session, AND the held conviction count must not drop below
# `min_invested`. Surplus exits are demoted to HOLD (kept lowest-rel_return first
# — let the worst losers go first). NO LLM → exhaustively unit-testable.
# ─────────────────────────────────────────────────────────────────────────────
def _asof_d(asof) -> date | None:
    """Coerce an asof string/date to a date for self-mirror's leakage-safe window (None → today)."""
    if isinstance(asof, date):
        return asof
    try:
        return date.fromisoformat(str(asof)[:10])
    except Exception:  # noqa: BLE001
        return None


def apply_exits(held: list[dict], decisions: list[dict], *,
                max_exits: int = _MAX_EXITS_PER_SESSION,
                min_invested: int = _MIN_INVESTED_NAMES) -> dict:
    """Resolve ``decisions`` into final ``{"exits": [...], "trims": [...]}`` under the
    never-blow-to-cash guard. Pure; never raises.

    The guard keeps only the lowest-``rel_return`` exits up to two limits — at most ``max_exits``
    AND never enough exits to push the surviving held-conviction count below ``min_invested`` —
    and demotes the rest to HOLD. Trims always pass through (they de-risk without removing a name).
    """
    held_map = _held_map(held)
    n_held = len(held_map)

    exit_decs = [d for d in (decisions or []) if d.get("action") == "exit" and d.get("ticker") in held_map]
    trim_decs = [d for d in (decisions or []) if d.get("action") == "trim" and d.get("ticker") in held_map]

    # how many exits may we honour? cap by max_exits AND by the invested-floor.
    floor_room = max(0, n_held - max(0, int(min_invested)))
    allowed = min(int(max_exits), floor_room)

    # worst losers exit first: ascending rel_return (None sorts as +inf → exits last).
    def _key(d):
        rr = d.get("rel_return")
        return rr if isinstance(rr, (int, float)) else float("inf")

    ranked = sorted(exit_decs, key=_key)
    keep_exit = ranked[:allowed]
    exits = [{"ticker": d["ticker"], "action": "exit", "scale": 0.0,
              "reason": d.get("reason", ""), "falsifier": d.get("falsifier", ""),
              "rel_return": d.get("rel_return")} for d in keep_exit]
    trims = [{"ticker": d["ticker"], "action": "trim", "scale": d.get("scale", 1.0),
              "reason": d.get("reason", ""), "falsifier": d.get("falsifier", "")} for d in trim_decs]
    return {"exits": exits, "trims": trims}


def risk_assess(book: list[dict] | None, asof: str, *, regime: dict | None = None,
                held_positions: list[dict] | None = None) -> dict:
    """Run the Risk Officer over the HELD conviction book. Returns a gradable dict
    ``{decisions, rationale, ran, artifacts}`` where ``decisions`` is the guard-enforced exit/trim
    list (the never-blow-to-cash cap already applied). Subtract-only + held-only enforced IN CODE.
    Additive; never raises. When OFF / no LLM / parse fails → ``{decisions:[], ran:False}`` and the
    caller leaves the book untouched.

    ``book`` is accepted for signature parity with the other seats but the Risk Officer reasons
    over ``held_positions`` (the held book), not the proposed buy book."""
    held_positions = held_positions or []
    txt = None
    if enabled():
        try:
            payload = _risk_input(held_positions, regime or {})
            # self-mirror: append the Risk Officer's own exit-timing record (flag-gated; OFF → identical).
            try:
                from brain import self_mirror
                sys_prompt = self_mirror.inject(_RISK_SYS, "risk", _asof_d(asof))
            except Exception:  # noqa: BLE001 — self-mirror is additive; never break the seat
                sys_prompt = _RISK_SYS
            txt, _meta = client.call_model(sys_prompt, json.dumps(payload, default=str),
                                           role="deep", max_tokens=1600, seat="risk_officer")
        except Exception:  # noqa: BLE001 — the seat is additive; never break the build
            txt = None
    j = _parse_json(txt) or {}

    held_map = _held_map(held_positions)
    norm = _normalize_decisions(j, held_map)
    guarded = apply_exits(held_positions, norm)

    # the final gradable decisions: guard-honoured exits + all trims (held decisions that were
    # demoted to hold by the guard simply do not appear → no action taken on them).
    decisions = guarded["exits"] + guarded["trims"]
    res = {
        "decisions": decisions,
        "rationale": str(j.get("rationale", ""))[:1000],
        "ran": bool(decisions),
    }
    # publish the seat's realised de-confidence multiplier (shrink-only; 1.0 until graded → no
    # behaviour change OFF). The caller/CIO reads it; the seat itself never auto-acts on it.
    try:
        from brain import calibration as _calib
        res["calibration_multiplier"] = round(_calib.multiplier("risk"), 3)
    except Exception:  # noqa: BLE001
        res["calibration_multiplier"] = 1.0
    try:
        res["artifacts"] = _write_artifacts(
            asof, "FLAGSHIP", _risk_input(held_positions, regime or {}), res)
    except Exception:  # noqa: BLE001 — persistence is best-effort
        res["artifacts"] = None
    return res


def _write_artifacts(asof: str, scope: str, payload: dict | None, result: dict | None) -> str | None:
    """Write the Risk Officer decision to ``data/risk_officer/<asof>/`` (decisions.json + risk.md).
    Best-effort, never raises."""
    d = _ARTIFACTS / (str(asof)[:10] or date.today().isoformat())
    d.mkdir(parents=True, exist_ok=True)
    (d / "decisions.json").write_text(json.dumps(
        {"agent": "risk_officer", "scope": scope, "input": payload, "result": result},
        indent=2, default=str))
    res = result or {}
    md = [f"# Risk Officer — {scope} ({str(asof)[:10]})", "",
          f"**Rationale:** {res.get('rationale', '')}", "", "## Decisions", ""]
    for dec in (res.get("decisions") or []):
        md.append(f"- **{dec.get('ticker')}** → {str(dec.get('action', '')).upper()} "
                  f"(scale {dec.get('scale')}) — {dec.get('reason', '')} "
                  f"[falsifier: {dec.get('falsifier', '')}]")
    if not (res.get("decisions") or []):
        md.append("- (no judgment exits — held book stands)")
    (d / "risk.md").write_text("\n".join(md))
    return str(d)
