"""PM-CONVICTION — the armed deep-reasoning portfolio-manager seat.

This is the judgment half of the Flagship deep-reasoning buy layer. Where the engine
(``portfolio.conviction.build``) is a disciplined gate that only DE-escalates, this seat is a
genuine buy-side PM: it takes the engine's confirmed candidates, its rejected pool, the Macro
Strategist's confirmed themes, and the FORGE research summaries, then ANTICIPATES and CHAMPIONS
confirmed-leadership thematic names with conviction — the same posture as the proven autonomous
desk, but constrained to Flagship discipline.

It reuses the PROVEN autonomous tool surface (``brain/autonomous_mcp.py``): identical
regime/themes/baskets/divergences/decision-matrix/quote tools and the ``submit_book`` flow, so the
PM gets the full thematic read. The PM may ADD high-conviction thematic names the engine missed and
DROP engine names that lack a live thesis (real autonomy) — but every name it champions is then run
through the existing blind SENTINEL adversary + a NEXUS-style subtract-only cap pass (orchestrated
by the caller in ``brain/judgment_book.py``; this module exposes the helpers and the no-leverage /
concentration enforcement so the PM's reshaped book is checked, never blindly trusted).

Additive + reversible: gated behind ``MASTERMIND_FLAGSHIP_JUDGMENT`` (default OFF). Returns a book
with ``ran=False`` (or ``None``) on any failure; never raises. Submits to a Flagship-scoped path
(``portfolio_id="flagship_judgment"``) so it can NEVER collide with the live autonomous book.
Model-agnostic via ``role="deep"`` (Opus/Fable resolved by config/agents.yml).
"""
from __future__ import annotations

import json
import os

from brain import client

PORTFOLIO_ID = "flagship_judgment"


def enabled() -> bool:
    """The PM seat runs only when the Flagship judgment layer is armed AND an LLM is reachable."""
    flag = os.environ.get("MASTERMIND_FLAGSHIP_JUDGMENT", "0").strip().lower()
    if flag not in ("1", "true", "yes", "on"):
        return False
    try:
        return client.available()
    except Exception:  # noqa: BLE001
        return False


# ─────────────────────────────────────────────────────────────────────────────
# input — everything the PM sees, seeded with the engine candidates, the rejected
# thematic pool it may champion, the Strategist themes, and the FORGE summaries.
# ─────────────────────────────────────────────────────────────────────────────
def _pm_input(sized: list[dict], rejected: list[dict], strategist: dict | None,
              regime: dict | None, gate_info: dict | None, asof: str) -> dict:
    sized = sized or []
    rejected = rejected or []
    gate_info = gate_info or {}

    engine_candidates = [{
        "ticker": c.get("ticker"),
        "weight": c.get("weight"),
        "confluence": c.get("confluence"),
        "bull": str(c.get("bull") or "")[:240],
        "bear": str(c.get("bear") or "")[:240],
        "divergences": (c.get("divergences") or [])[:6],
        "retained": bool(c.get("retained")),
    } for c in sized if c.get("ticker")][:40]

    engine_rejected = [{
        "ticker": r.get("ticker"),
        "confluence": r.get("confluence"),
        "vetoes": (r.get("vetoes") or [])[:4],
        "bear": (r.get("bear") or [])[:3],
    } for r in rejected if r.get("ticker")][:40]

    forge_summaries = []
    for t, info in gate_info.items():
        block = (info or {}).get("research_block") or {}
        if not block:
            continue
        forge_summaries.append({
            "ticker": t,
            "combined": block.get("combined"),
            "viability": block.get("viability"),
            "confirmed": block.get("confirmed"),
            "summary": str(block.get("summary") or "")[:280],
        })
    forge_summaries = forge_summaries[:40]

    reg = {k: (regime or {}).get(k) for k in
           ("quad", "quad_name", "growth_score", "inflation_score", "liquidity_overlay",
            "cycle_tag", "sector_rs_top")}

    return {
        "asof": str(asof)[:10],
        "engine_candidates": engine_candidates,
        "engine_rejected": engine_rejected,
        "strategist": strategist or {},
        "forge_summaries": forge_summaries,
        "regime": reg,
    }


_PM_PERSONA = (
    "You are the PORTFOLIO MANAGER of the FLAGSHIP $1,000,000 paper book. Like the autonomous desk "
    "you ANTICIPATE and CHAMPION confirmed-leadership thematic names with conviction — concentrate "
    "where the leadership narrative has ALREADY turned (let the dashboard's confirmed themes and the "
    "Macro Strategist read tell you WHICH themes those are today — do not assume any in advance), and "
    "sidestep crowded late themes. \n\n"
    "EXPRESS EACH THEME THROUGH SINGLE NAMES, NOT ETFs. Prefer the highest-conviction individual "
    "equities that capture a confirmed theme; broad sector/index/factor ETFs (e.g. SMH, XLK, MTUM, "
    "IWM) are a LAST-RESORT placeholder only when no single name cleanly expresses the theme — they "
    "must never be the core of the book. Single-name selection is where the alpha is. \n\n"
    "IDLE CASH EARNS ~4% ANNUALIZED (a money-market sweep), so holding cash when you lack "
    "high-conviction ideas is a REWARDED choice, not dead money. NEVER dilute the book with marginal "
    "names just to stay fully invested — a smaller book of your best ideas plus paid cash beats a "
    "padded one. \n\n"
    "You are SEEDED with: the engine's confirmed candidates, its rejected pool (names you MAY "
    "champion if they have a live thesis), the Macro Strategist's confirmed themes + backdrop "
    "stance, and FORGE research summaries. You have FULL discretion within paper cash (NO leverage): "
    "you MAY add high-conviction thematic names the engine missed and DROP engine names that lack a "
    "live thesis. \n\n"
    "Confirmation over prediction — every name needs a why-now, not a prophecy. Use the mcp__bot__* "
    "tools (regime, themes, decision matrix, divergences, intel hub, fundamentals) and "
    "mcp__bot__get_quote to confirm a name is priceable before you rely on it, and the open web if "
    "useful. When done, submit your COMPLETE Flagship target book via mcp__desk__submit_book ONCE — "
    "every name you want to hold, its weight (fraction of NAV), and a one-paragraph rationale per "
    "holding (why you own it, now). Anything you omit is SOLD. Your champions are then checked by a "
    "BLIND adversary and HARD risk caps (no-leverage, per-name + concentration limits) — size with "
    "that in mind; conviction leads, the adversary checks."
)


def _build_prompt(payload: dict) -> str:
    strat = payload.get("strategist") or {}
    reg = payload.get("regime") or {}
    lines = [f"# Flagship book — deep-reasoning decision for {payload.get('asof')}", ""]
    if reg.get("quad_name") or reg.get("quad"):
        lines += [f"Macro regime (in-house read): quad {reg.get('quad')} "
                  f"({reg.get('quad_name')}), liquidity {reg.get('liquidity_overlay')}, "
                  f"cycle {reg.get('cycle_tag')}.", ""]
    if strat.get("confirmed_themes"):
        lines += ["Macro Strategist — CONFIRMED leadership themes right now:"]
        for th in strat["confirmed_themes"][:12]:
            nm = ", ".join(th.get("names", [])[:8])
            lines.append(f"  • {th.get('theme')} [{th.get('stage')}, lead "
                         f"{th.get('leadership')}] — {nm} — {th.get('why')}")
        lines += [f"Backdrop stance: {strat.get('backdrop_stance')} "
                  f"(supportive={strat.get('supportive')}). "
                  f"Crowding flags: {', '.join(strat.get('crowding_flags', []) or ['none'])}.", ""]
    lines += [
        "You are seeded below (JSON) with the engine's confirmed candidates, its rejected pool "
        "(names you may champion), the full Strategist read, and FORGE research summaries. Research "
        "with the in-house tools and/or the web, then submit your COMPLETE Flagship target book via "
        "mcp__desk__submit_book — add the thematic names you have conviction in, drop engine names "
        "without a live thesis, one rationale per holding.",
        "",
        "```json",
        json.dumps(payload, indent=2, default=str)[:9000],
        "```",
    ]
    return "\n".join(lines)


def build_book(sized: list[dict], rejected: list[dict], *, regime: dict | None, asof: str,
               strategist: dict | None, gate_info: dict | None,
               portfolio_ctx: dict | None = None) -> dict | None:
    """Run the armed Opus PM. Returns the target book
    ``{holdings:[{ticker,weight,thesis,conviction}], cash, summary, sold_note, ran}`` — or a stub
    with ``ran=False`` when no LLM is reachable / the PM did not submit. Additive; never raises.

    The PM submits via ``mcp__desk__submit_book``; the submission is read back from the
    Flagship-scoped path (``portfolio_id="flagship_judgment"``) so it never collides with the live
    autonomous book."""
    stub = {"holdings": [], "cash": 1.0, "summary": "", "sold_note": "", "ran": False}
    if not client.available():
        return stub
    try:
        # FLAGSHIP-scoped desk (aliased): get_my_book shows the real Flagship book and submit_book
        # writes to the isolated "flagship_judgment" path this function reads back. Reusing the
        # autonomous desk here was a bug (live dry-run): its submit_book wrote to the AUTONOMOUS
        # book, so build_book always read nothing → ran=False → P1 silently no-op'd in production.
        from brain import flagship_desk_mcp as autonomous_mcp, cli_bridge
    except Exception:  # noqa: BLE001
        return stub

    payload = _pm_input(sized, rejected, strategist, regime, gate_info, asof)
    prompt = _build_prompt(payload)

    # self-mirror: append the PM's own champion track record to its persona (flag-gated; OFF →
    # the persona is byte-identical to P1/P2).
    try:
        from datetime import date as _date
        from brain import self_mirror
        try:
            _asof = _date.fromisoformat(str(asof)[:10])
        except Exception:  # noqa: BLE001
            _asof = None
        persona = self_mirror.inject(_PM_PERSONA, "pm", _asof)
    except Exception:  # noqa: BLE001 — self-mirror is additive; never break the seat
        persona = _PM_PERSONA

    # clear any stale Flagship-scoped submission first so a no-submit turn can't replay yesterday
    try:
        autonomous_mcp.clear_submission(PORTFOLIO_ID)
    except Exception:  # noqa: BLE001
        pass

    try:
        coro = cli_bridge.reason(
            prompt,
            role="deep",
            arm=True,
            append_system=persona,
            mcp_servers=autonomous_mcp.build_servers(),
            allowed_tools=autonomous_mcp.allowed_tools(),
            max_turns=int(os.environ.get("FLAGSHIP_PM_MAX_TURNS", "30")),
        )
        # cli_bridge.reason is a coroutine; run it on a fresh loop (or a thread if one is live).
        _run_coro(coro)
    except Exception:  # noqa: BLE001 — the seat is additive; never break the build
        return stub

    sub = None
    try:
        sub = autonomous_mcp.read_submission(PORTFOLIO_ID)
    except Exception:  # noqa: BLE001
        sub = None
    if not sub:
        return stub

    holdings = []
    gross = 0.0
    seen: set[str] = set()
    for h in (sub.get("holdings") or []):
        t = str(h.get("ticker") or "").upper().strip()
        try:
            w = float(h.get("weight") or 0.0)
        except (TypeError, ValueError):
            w = 0.0
        thesis = str(h.get("rationale") or h.get("thesis") or "").strip()
        if not t or t in seen or w <= 0 or not thesis:
            continue
        seen.add(t)
        holdings.append({"ticker": t, "weight": w, "thesis": thesis,
                         "conviction": (h.get("conviction") or "medium")})
        gross += w

    # no-leverage scale-down (defence-in-depth; submit_book already enforces this).
    if gross > 1.0 and holdings:
        scale = 1.0 / gross
        for h in holdings:
            h["weight"] = round(h["weight"] * scale, 6)
        gross = 1.0

    # publish the PM's realised de-confidence multiplier (shrink-only; 1.0 until graded → no change
    # OFF). Read by the caller/CIO; the seat never auto-acts on it.
    try:
        from brain import calibration as _calib
        pm_mult = round(_calib.multiplier("pm"), 3)
    except Exception:  # noqa: BLE001
        pm_mult = 1.0

    return {
        "holdings": holdings,
        "cash": round(max(0.0, 1.0 - gross), 4),
        "summary": str(sub.get("summary") or "").strip()[:2000],
        "sold_note": str(sub.get("sold_note") or "").strip()[:1000],
        "ran": bool(holdings),
        "calibration_multiplier": pm_mult,
    }


def _run_coro(coro):
    """Run an async coroutine from a sync caller — directly if no loop is running, else on a
    worker thread (mirrors ``bot/autonomous._run_coro``)."""
    import asyncio
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    # a loop is already running — dispatch to a fresh loop on a worker thread
    import concurrent.futures

    def _runner():
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        return ex.submit(_runner).result()


# ─────────────────────────────────────────────────────────────────────────────
# discipline — the PM's champions are checked by the existing blind SENTINEL +
# a NEXUS-style subtract-only / no-leverage / concentration pass. Exposed here so
# the orchestrator (brain/judgment_book.py) can reuse it; this seat never trusts
# the PM blindly. A truly novel PM-added name (no engine decision matrix) gets a
# synthetic breakdown={"confirmed": True} so NEXUS treats it as a confirmed buy
# SENTINEL can still veto — "judgment leads, adversary checks".
# ─────────────────────────────────────────────────────────────────────────────
def adversary_check(ticker: str, asof: str, *, engine_full: dict | None, breakdown: dict | None,
                    regime: dict | None, portfolio_ctx: dict | None) -> dict:
    """Run one PM holding through the existing blind SENTINEL + subtract-only NEXUS pass via
    ``committee.assess``. Returns the committee decision (action/scale/lean/rationale/sentinel).
    PM-added names with no engine matrix get a synthetic confirmed breakdown so SENTINEL can still
    oppose them. Never raises — degrades to a pass-through confirm on any failure."""
    from brain import committee
    bd = breakdown or {"confirmed": True}
    try:
        return committee.assess(ticker, asof, engine_full=(engine_full or {}), breakdown=bd,
                                regime=regime or {}, portfolio_ctx=portfolio_ctx or {})
    except Exception:  # noqa: BLE001 — additive; never break the build
        return {"action": "confirm", "scale": 1.0, "lean": "add",
                "rationale": "committee unavailable — PM decision stands.",
                "sentinel_stance": None, "sentinel": None, "artifacts": None, "ran": False}


def enforce_no_leverage(holdings: list[dict], *, name_cap: float = 0.08) -> list[dict]:
    """Final NEXUS-style cap pass on the PM's reshaped book (subtract-only, in place semantics on a
    copy): clamp each weight to ``name_cap`` then scale the whole book down so gross ≤ 1.0. Reuses
    the same no-leverage discipline as ``submit_book`` / the conviction sleeve. Never raises."""
    out = []
    for h in (holdings or []):
        try:
            w = min(float(name_cap), float(h.get("weight") or 0.0))
        except (TypeError, ValueError):
            w = 0.0
        if w <= 0:
            continue
        out.append({**h, "weight": round(w, 4)})
    gross = sum(h["weight"] for h in out)
    if gross > 1.0 and out:
        scale = 1.0 / gross
        for h in out:
            h["weight"] = round(h["weight"] * scale, 4)
    return out
