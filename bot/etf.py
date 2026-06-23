"""The ETF book — a free-form Opus Brain rotating its own $1M paper book across US-listed ETFs.

The US-ETF sibling of ``bot/autonomous.py``: once per US trading day (after the close), the Brain
  1. reads the ETF ROTATION BOARD (``get_etf_board`` — the dashboard's regime / sector-RS / risk
     signals pre-digested) + its current book,
  2. researches freely (the macro desks + the board, or the web — its choice),
  3. submits a COMPLETE target book over US-listed ETFs ONLY, one rationale per holding,
  4. and the deterministic layer RE-ENFORCES the ETF-only universe + the risk guardrails, rebalances
     the paper account to those weights at the latest close, marks NAV vs SPY, and logs the day.

The difference from the naked autonomous book is DISCIPLINE, not signals: the Brain is handed an
ETF-adapted doctrine (confirmation over prediction, a regime→tilt map, cash-is-a-position, a crisis
ladder into duration/T-bills, three exit stops) and the trusted layer enforces a few hard guardrails
the Brain cannot override — an ETF-only allowlist, a single-ETF cap, a turnover throttle, and a
crisis floor that caps offensive gross when the dashboard reads stressed. The engine disciplines;
Opus decides. Universe + pricing live in ``portfolio.etf_universe``; the board in ``brain.etf_board``.
Everything is scoped to portfolio_id="etf" so no other book is touched.

Run:  python -m bot.etf        (or the APScheduler 'etf_daily' job, or POST /api/etf/run)
"""
from __future__ import annotations

import asyncio
import json
import os
from datetime import date, datetime, timezone
from pathlib import Path

import bot  # noqa: F401  -> vendor/macro onto sys.path

from portfolio import etf_universe as _eu

PORTFOLIO_ID = "etf"
SLEEVE = "brain"
BENCHMARK = "SPY"
_ROOT = Path(__file__).resolve().parent.parent
_MAX_TURNS = int(os.environ.get("ETF_MAX_TURNS", "30"))

# Risk guardrails are read FRESH per run from the externalized spec (config/etf_strategy.yml via
# portfolio.etf_universe.guardrails()), with env vars layered on top — so a spec edit retunes BOTH
# what the persona advertises AND what the trusted layer enforces on the next run, with no drift and
# no restart. The Brain owns selection; these are the hard limits (see _apply_guardrails).
def _guardrails() -> dict:
    g = _eu.guardrails()

    def _f(env: str, default) -> float:
        v = os.environ.get(env)
        try:
            return float(v) if v is not None else float(default)
        except (TypeError, ValueError):
            return float(default)

    return {
        "max_single_weight": _f("ETF_MAX_SINGLE_WEIGHT", g["max_single_weight"]),
        "min_trade": _f("ETF_MIN_TRADE", g["min_trade"]),
        "offensive_cap": {
            "stressed": _f("ETF_OFFENSIVE_CAP_STRESSED", g["offensive_cap"]["stressed"]),
            "elevated": _f("ETF_OFFENSIVE_CAP_ELEVATED", g["offensive_cap"]["elevated"]),
        },
    }


# ---------------------------------------------------------------------------
# the daily entrypoint
# ---------------------------------------------------------------------------

def run_etf(asof: str | None = None, *, force: bool = False, armed: bool = True,
            directive: str | None = None) -> dict:
    """Run one ETF turn end-to-end. Best-effort: every step degrades gracefully so a missing
    credential / price never leaves the book in a half-traded state.

    `directive` injects an ad-hoc instruction at the TOP of the Brain's prompt for this run only
    (e.g. an urgent reconsideration: "the dashboard is stale, check live overnight futures yourself
    and de-risk to SGOV if warranted"). The market-hours gate still applies — off-hours the decided
    book is QUEUED for the next open, never filled on the spot."""
    from portfolio import etf_universe, market_calendar, paper_account, position_log

    asof = asof or date.today().isoformat()
    out: dict = {"portfolio_id": PORTFOLIO_ID, "asof": asof,
                 "ran_at": datetime.now(timezone.utc).isoformat()}
    today = _safe_date(asof)
    out["trading_day"] = market_calendar.is_trading_day(today) if today else None

    state0 = paper_account._load_account(PORTFOLIO_ID)
    inaugural = not _has_history() and not (state0.get("positions") or {})
    out["inaugural"] = inaugural

    # 1. run the Brain (armed) → it researches and submits a target book with rationales
    from brain import etf_mcp
    etf_mcp.clear_submission()                       # never replay yesterday's decision
    brain: dict = {"ok": False, "skipped": not armed}
    if armed:
        try:
            # directive is an optional ad-hoc override (overnight reviews) — pass it through only when set
            brain = _run_brain(asof, inaugural, directive=directive) if directive else _run_brain(asof, inaugural)
        except Exception as e:                       # noqa: BLE001
            brain = {"ok": False, "error": repr(e)[:300]}
    out["brain"] = {k: brain.get(k) for k in ("ok", "cost_usd", "tools_used", "error", "run_id", "model")}

    # 2. read the submitted book — then RE-ENFORCE the ETF-only allowlist in the trusted layer: drop
    #    any holding that is not a US-listed ETF in the universe, even if the Brain slipped one in.
    submission = etf_mcp.read_submission()
    if submission:
        all_h = submission.get("holdings") or []
        kept = [h for h in all_h if etf_universe.is_etf(h.get("ticker"))]
        rejected = [h.get("ticker") for h in all_h if not etf_universe.is_etf(h.get("ticker"))]
        if rejected:
            submission = {**submission, "holdings": kept}
            out["rejected_offlist"] = rejected
    decided = bool(submission and submission.get("holdings"))
    out["decided"] = decided

    # 3. price the universe we might trade (targets ∪ held ∪ SPY benchmark) — ETF-aware (live Yahoo
    #    USD mark with the vendored snapshot / engine parquet as fallback), so off-cache ETFs price.
    held = list((paper_account._load_account(PORTFOLIO_ID).get("positions") or {}).keys())
    target = {h["ticker"]: float(h.get("weight") or 0.0)
              for h in (submission.get("holdings") if decided else [])}
    etf_universe.warm(set(target) | set(held) | {BENCHMARK})
    prices: dict[str, float] = {}
    for t in set(target) | set(held) | {BENCHMARK}:
        px = etf_universe.price(t)
        if px and px > 0:
            prices[t] = px

    # 3b. apply the risk guardrails to the submitted target (single-ETF cap, turnover throttle,
    #     crisis offensive-gross floor). The Brain owns selection; these are the hard limits.
    from brain import etf_board
    risk = etf_board.risk_state()
    out["risk_state"] = risk.get("state")
    guardrail_notes: list[str] = []
    if decided:
        target, guardrail_notes = _apply_guardrails(target, prices, risk)
    out["guardrails"] = guardrail_notes

    # 4. EXECUTE — market-hours-aware. When the US session is OPEN, rebalance to the (guardrailed)
    #    target at the live mark. When CLOSED (the normal post-close run, or any off-hours manual
    #    run), QUEUE the target to settle at the next open — book NO fills now, so the book never
    #    trades off-hours and re-running a closed session can't churn it. Names we cannot price are
    #    skipped (and surfaced). See bot/settle.py + the scheduler's open settle.
    from bot import settle as _settle
    executed: list[dict] = []
    skipped: list[str] = []
    queued = False
    if decided:
        skipped = sorted(t for t in target if t not in prices)
        res = _settle.execute_or_queue(PORTFOLIO_ID, target, prices, asof)
        executed = res.get("executed") or []
        queued = bool(res.get("queued"))
        if res.get("error"):
            out["rebalance_error"] = res["error"]
        if executed:   # only reconcile the rationale ledger when fills actually happened
            ledger_positions = [{"ticker": t, "sleeve": SLEEVE, "weight": w, "entry_price": prices.get(t)}
                                for t, w in target.items() if t in prices]
            try:
                position_log.update(ledger_positions, asof, portfolio_id=PORTFOLIO_ID)
            except Exception:
                pass
    out["executed"] = executed
    out["queued_for_open"] = queued
    out["market_open"] = _settle.is_open(PORTFOLIO_ID)
    out["skipped_unpriceable"] = skipped

    # 5. mark NAV vs SPY (idempotent per date)
    try:
        paper_account.mark(prices, asof, portfolio_id=PORTFOLIO_ID)
    except Exception as e:                           # noqa: BLE001
        out["mark_error"] = repr(e)[:200]

    # 6. append the daily decision log FIRST (so the accountability loop can record today's picks),
    #    7. run the accountability loop (record today + resolve matured forward grades vs SPY), then
    #    8. publish the book contract with the fresh scorecard attached.
    try:
        _append_decision_log(asof, submission, executed, skipped, brain, risk, guardrail_notes)
    except Exception:
        pass
    try:
        from portfolio import etf_outcomes
        out["accountability"] = etf_outcomes.grade(asof)
    except Exception:
        pass
    payload = _build_payload(asof, submission, prices, executed, skipped, brain, risk, guardrail_notes)
    try:
        from bridge import build_portfolio
        out["paths"] = build_portfolio.write(payload, portfolio_id=PORTFOLIO_ID)
    except Exception as e:                           # noqa: BLE001
        out["write_error"] = repr(e)[:200]

    try:
        out["nav"] = round(paper_account.nav(prices, PORTFOLIO_ID), 2)
    except Exception:
        out["nav"] = None
    out["holdings"] = len(target)
    return out


# ---------------------------------------------------------------------------
# risk guardrails (the trusted-layer discipline the Brain cannot override)
# ---------------------------------------------------------------------------

def _apply_guardrails(target: dict[str, float], prices: dict[str, float],
                      risk: dict) -> tuple[dict[str, float], list[str]]:
    """Adjust the Brain's submitted target weights to the book's hard limits, returning the adjusted
    target + a list of plain-English notes (logged for transparency — the doctrine is blunt, not silent).

      G1 turnover throttle — a name whose weight moves < _MIN_TRADE from its CURRENT weight is snapped
         to current (no churn for a sub-1.5% drift; a brand-new sub-threshold name is dropped to dust-0).
         Names the Brain OMITTED are untouched here → rebalance still sells them in full (an explicit exit).
      G2 single-ETF cap — any one ETF over _MAX_SINGLE is clamped (the excess falls to cash).
      G3 crisis floor — in a stressed/elevated risk_state, OFFENSIVE (growth/cyclical) gross is capped
         per _OFFENSIVE_CAP; the freed weight falls to cash (the Brain can pre-empt by holding duration/
         T-bills itself — defensives are exempt from the cap).
    """
    from portfolio import etf_universe, paper_account
    gr = _guardrails()                                   # fresh from the spec (+ env) — no import-time drift
    max_single, min_trade, off_caps = gr["max_single_weight"], gr["min_trade"], gr["offensive_cap"]
    adj = dict(target)
    notes: list[str] = []

    # current weights at these marks (for the turnover throttle)
    state = paper_account._load_account(PORTFOLIO_ID)
    positions = state.get("positions") or {}
    cash = float(state.get("cash") or 0.0)
    cur_nav = cash + sum(float(p.get("shares") or 0.0) * prices.get(t, float(p.get("avg_cost") or 0.0))
                         for t, p in positions.items())
    cur_w: dict[str, float] = {}
    if cur_nav > 0:
        for t, p in positions.items():
            mv = float(p.get("shares") or 0.0) * prices.get(t, float(p.get("avg_cost") or 0.0))
            cur_w[t] = mv / cur_nav

    # G1 turnover throttle
    throttled = 0
    for t in list(adj):
        c = cur_w.get(t, 0.0)
        if abs(adj[t] - c) < min_trade and adj[t] != c:
            adj[t] = round(c, 6)
            throttled += 1
    adj = {t: w for t, w in adj.items() if w > 1e-9}     # drop dust-0 (tiny new names snapped away)
    if throttled:
        notes.append(f"turnover throttle: {throttled} sub-{min_trade:.1%} change(s) not traded")

    # G2 single-ETF cap
    for t in list(adj):
        if adj[t] > max_single + 1e-9:
            notes.append(f"capped {t} {adj[t]*100:.0f}%→{max_single*100:.0f}%")
            adj[t] = max_single

    # G3 crisis floor
    off_cap = off_caps.get(risk.get("state"))
    if off_cap is not None:
        off = {t: w for t, w in adj.items() if t in etf_universe.OFFENSIVE}
        off_gross = sum(off.values())
        if off_gross > off_cap + 1e-9 and off_gross > 0:
            scale = off_cap / off_gross
            for t in off:
                adj[t] = round(adj[t] * scale, 6)
            notes.append(f"risk={risk.get('state')}: offensive gross {off_gross*100:.0f}%→"
                         f"{off_cap*100:.0f}% (freed to cash / defensives)")
    return adj, notes


# ---------------------------------------------------------------------------
# the Brain
# ---------------------------------------------------------------------------

# The doctrine is externalized to config/etf_strategy.yml (so it's a tunable, reviewable artifact);
# this is the in-code fallback used only when the spec is missing/empty. _build_persona() generates
# the UNIVERSE block and the live guardrail numbers from the spec too, so the persona can never
# drift from what the universe filter + the trusted layer actually enforce.
_DEFAULT_DOCTRINE = (
    "1. CONFIRMATION OVER PREDICTION. You cannot time ignition. Prefer ETFs that are ALREADY ranked "
    "high in the board's sector_rs table AND above their 200d trend; do not buy a falling knife on a "
    "narrative. Early-following the confirmed leader beats prophesying the next one.\n"
    "2. REGIME->TILT, conditioned on the tape. Use the board's quad: Q1 Goldilocks -> lean growth/tech/"
    "momentum (XLK QQQ SMH MTUM QUAL); Q2 Reflation -> cyclicals/value/energy/materials (XLE XLF XLB "
    "VLUE IWM); Q3 Stagflation -> energy/defensives/short-duration (XLE XLU XLP + SGOV/SHY); Q4 Growth-"
    "scare -> defensives/duration/min-vol (XLU XLP XLV TLT USMV + cash). A PRIOR — defer to sector_rs "
    "and trend when the tape disagrees.\n"
    "3. CASH IS A POSITION. Hold T-bill ETFs (SGOV/BIL) deliberately as the option premium that funds "
    "rotation; do not be reflexively fully invested.\n"
    "4. CRISIS LADDER. When the board's risk_state is ELEVATED or STRESSED, de-gross into duration "
    "(TLT/IEF), T-bill cash (SGOV/BIL) and optionally gold (GLD). The desk caps offensive gross in a "
    "stressed read regardless — get there first, deliberately.\n"
    "5. THREE EXIT STOPS per holding, set on entry: a trend break (below its 200d), a thesis "
    "invalidation (the regime/leadership that justified it flips), and a time stop (dead capital "
    "lagging the leader gets rotated). Never average down into a name diverging from a rotating tape."
)

_GROUP_LABEL = {"core_index": "core index", "sectors": "sectors", "factors": "factors/style",
                "duration": "duration", "cash": "cash-with-yield (T-bill ETFs)", "diversifier": "diversifier"}


def _build_persona() -> str:
    """Assemble the Brain's system persona from the externalized strategy spec: the UNIVERSE block
    and the live guardrail numbers are generated from config/etf_strategy.yml (so they can never
    drift from what's enforced), and the DOCTRINE is injected from the spec (fallback _DEFAULT_DOCTRINE)."""
    from portfolio import etf_universe
    groups = etf_universe.GROUPS
    uni = "\n".join(f"  • {_GROUP_LABEL.get(g, g)}: {' '.join(groups[g])}" for g in groups)
    doctrine = (etf_universe.load_spec().get("doctrine") or "").strip() or _DEFAULT_DOCTRINE
    g = _guardrails()                  # same source the trusted layer enforces (spec + env), no drift
    return (
        "You are the ETF PORTFOLIO MANAGER of a real-money-style $1,000,000 PAPER book. You run once per "
        "US trading day, after the close, and rebalance the whole book daily. You have FULL discretion over "
        "selection and sizing — but you operate a DOCTRINE, not a whim, and the desk enforces hard risk "
        "limits you cannot override. Paper cash only; no leverage. Graded on realized NAV vs the S&P 500 (SPY).\n\n"
        "UNIVERSE — US-listed ETFs ONLY. NO single stocks; any non-ETF or off-list ticker you submit is "
        "REJECTED. Your tools:\n" + uni + "\n\n"
        "DOCTRINE (how to think):\n" + doctrine + "\n\n"
        "DESK GUARDRAILS (enforced after you submit, so size with them in mind): any single ETF over "
        f"{g['max_single_weight'] * 100:.0f}% is clamped; a weight change under ~{g['min_trade'] * 100:.1f}% of NAV "
        f"is NOT traded (don't churn — only move when the board moved); in a stressed risk_state offensive "
        f"(growth/cyclical) gross is capped ~{g['offensive_cap']['stressed'] * 100:.0f}% "
        f"(~{g['offensive_cap']['elevated'] * 100:.0f}% elevated), the rest forced to cash/defensives.\n\n"
        "PROCESS: call mcp__desk__get_etf_board FIRST (regime + sector_rs + risk_state + per-ETF trend), then "
        "mcp__desk__get_my_book to see what you hold; deepen with the macro mcp__bot__* desks and/or the web. "
        "Confirm any name with mcp__desk__get_quote. When done, call mcp__desk__submit_book ONCE with your "
        "COMPLETE target book — every ETF you want to hold, its weight (fraction of NAV), and a one-paragraph "
        "rationale for EACH. Anything you hold but omit is SOLD. Be decisive and disciplined."
    )


def _run_brain(asof: str, inaugural: bool, directive: str | None = None) -> dict:
    from brain import etf_mcp, cli_bridge
    prompt = _build_prompt(asof, inaugural, directive=directive)
    coro = cli_bridge.reason(
        prompt,
        role="deep",                 # opus, per config/agents.yml
        arm=True,
        append_system=_build_persona(),
        mcp_servers=etf_mcp.build_servers(),
        allowed_tools=etf_mcp.allowed_tools(),
        max_turns=_MAX_TURNS,
    )
    return _run_coro(coro)


def _build_prompt(asof: str, inaugural: bool, directive: str | None = None) -> str:
    from portfolio import paper_account
    from brain import etf_board
    state = paper_account._load_account(PORTFOLIO_ID)
    cash = float(state.get("cash") or 0.0)
    positions = state.get("positions") or {}
    regime = _regime_brief()
    risk = etf_board.risk_state()

    lines = [f"# ETF book — daily decision for {asof}", ""]
    if directive:
        # an ad-hoc instruction for THIS run only, pinned at the top so it frames the whole session.
        lines += ["## ⚠ PRIORITY DIRECTIVE FOR THIS RUN", directive.strip(), ""]
    if regime:
        lines += [f"Macro regime (in-house read): {regime}", ""]
    lines += [f"Risk state (drives your duration/cash): {risk.get('state')} — {', '.join(risk.get('reasons') or [])}", ""]
    # the perception-to-outcome loop: show the Brain its OWN realized track record so it self-corrects
    try:
        from portfolio import etf_outcomes
        track = etf_outcomes.prompt_line()
        if track:
            lines += [track, "Use this to calibrate: if your high-conviction picks aren't beating SPY, "
                      "tighten selection or trim conviction; if the book edge is negative, lean more on "
                      "the board's confirmed leaders and less on contrarian calls.", ""]
    except Exception:
        pass
    if inaugural:
        lines += [
            "This is your INAUGURAL run. The book is 100% cash: $1,000,000. Build the ETF portfolio "
            "from scratch — read the rotation board, then buy whatever US-listed ETFs the regime + "
            "sector_rs + trend support, sized however you see fit (keep T-bill cash if you want).",
            "",
        ]
    else:
        lines += [f"Your current book: ${cash:,.0f} cash across {len(positions)} holdings "
                  f"({', '.join(sorted(positions)) or 'none'}). Call mcp__desk__get_my_book for the "
                  "full picture (weights, live P&L, and the rationale you last gave each name).", ""]
    lines += [
        "Call get_etf_board first, then submit your complete target book via mcp__desk__submit_book "
        "with a one-paragraph rationale per holding. Rotate with discipline, not churn; you are "
        "accountable for the NAV vs SPY.",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# publish + log helpers
# ---------------------------------------------------------------------------

def _build_payload(asof: str, submission: dict | None, prices: dict, executed: list,
                   skipped: list, brain: dict, risk: dict, guardrails: list) -> dict:
    from portfolio import etf_universe, market_calendar, paper_account, position_log
    state = paper_account._load_account(PORTFOLIO_ID)
    pnl = paper_account.positions_pnl(prices, PORTFOLIO_ID)
    nav = paper_account.nav(prices, PORTFOLIO_ID)
    cash = float(state.get("cash") or 0.0)
    rationale_by_tk = {h["ticker"].upper(): h for h in ((submission or {}).get("holdings") or [])}

    positions = []
    for tk, rec in pnl.items():
        mv = rec.get("market_value")
        h = rationale_by_tk.get(tk, {})
        rationale = h.get("rationale")
        entry = position_log.get_entry_info(SLEEVE, tk, portfolio_id=PORTFOLIO_ID)
        positions.append({
            "ticker": tk,
            "sleeve": SLEEVE,
            "group": etf_universe.group_of(tk),
            "weight": round(mv / nav, 4) if (mv and nav) else None,
            "verdict": "hold",
            "conviction": h.get("conviction"),
            "rationale": rationale,
            "opened_at": entry.get("opened_at"),
            "held_days": entry.get("held_days"),
            "cost_basis": rec.get("avg_cost"),
            "current_price": rec.get("current_price"),
            "market_value": mv,
            "unrealized_pnl": rec.get("unrealized_pnl"),
            "unrealized_pct": rec.get("unrealized_pct"),
            "thesis_full": {"summary": rationale, "why_now": rationale, "bull": [], "bear": []}
            if rationale else None,
        })
    positions.sort(key=lambda p: (p.get("weight") or 0.0), reverse=True)

    gross = round(sum((p.get("weight") or 0.0) for p in positions), 4)
    decisions = []
    summary = (submission or {}).get("summary")
    if summary:
        decisions.append({"subject": "ETF book", "lean": summary,
                          "thesis": (submission or {}).get("sold_note") or "",
                          "logged_at": datetime.now(timezone.utc).isoformat()})
    accountability = {}
    try:
        from portfolio import etf_outcomes
        accountability = etf_outcomes.scorecard()    # forward track record vs SPY (resolved picks)
    except Exception:
        accountability = {}
    return {
        "as_of": asof,
        "portfolio_id": PORTFOLIO_ID,
        "manager": "ETF Opus Brain",
        "kind": "etf_brain",
        "benchmark": BENCHMARK,
        "regime": _regime_dict(),
        "risk_state": risk.get("state"),
        "risk_reasons": risk.get("reasons"),
        "guardrails": guardrails,
        "accountability": accountability,
        "gross": gross,
        "cash": round(1.0 - gross, 4) if gross <= 1.0 else 0.0,
        "cash_usd": round(cash, 2),
        "nav": round(nav, 2),
        "summary": summary,
        "sold_note": (submission or {}).get("sold_note"),
        "positions": positions,
        "decisions": decisions,
        "executed_today": executed,
        "skipped_unpriceable": skipped,
        "market_status": market_calendar.status(),
        "brain": {k: brain.get(k) for k in ("cost_usd", "tools_used", "model")},
    }


def _append_decision_log(asof: str, submission: dict | None, executed: list,
                         skipped: list, brain: dict, risk: dict, guardrails: list) -> None:
    from portfolio import etf_universe, registry
    p = registry.data_dir(PORTFOLIO_ID) / "decisions.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "asof": asof,
        "ts": datetime.now(timezone.utc).isoformat(),
        "summary": (submission or {}).get("summary"),
        "sold_note": (submission or {}).get("sold_note"),
        "risk_state": risk.get("state"),
        "risk_reasons": risk.get("reasons"),
        "guardrails": guardrails,
        "holdings": [{"ticker": h.get("ticker"), "group": etf_universe.group_of(h.get("ticker")),
                      "weight": h.get("weight"), "conviction": h.get("conviction"),
                      "rationale": h.get("rationale")}
                     for h in ((submission or {}).get("holdings") or [])],
        "executed": executed,
        "skipped_unpriceable": skipped,
        "brain_text": (brain.get("text") or "")[:6000] if isinstance(brain, dict) else None,
        "run_id": brain.get("run_id") if isinstance(brain, dict) else None,
        "tools_used": brain.get("tools_used") if isinstance(brain, dict) else None,
        "cost_usd": brain.get("cost_usd") if isinstance(brain, dict) else None,
        "model": brain.get("model") if isinstance(brain, dict) else None,
        "error": brain.get("error") if isinstance(brain, dict) else None,
    }
    rows = []
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            if r.get("asof") != asof:
                rows.append(r)
    rows.append(entry)
    p.write_text("\n".join(json.dumps(r, default=str, ensure_ascii=False) for r in rows) + "\n")


def load_decisions(limit: int = 60) -> list[dict]:
    """The daily decision log, NEWEST first. Backs /api/decisions?portfolio=etf."""
    from portfolio import registry
    p = registry.data_dir(PORTFOLIO_ID) / "decisions.jsonl"
    rows: list[dict] = []
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except Exception:
                pass
    rows.sort(key=lambda r: (r.get("asof") or "", r.get("ts") or ""), reverse=True)
    return rows[:limit]


def republish(asof: str | None = None) -> dict:
    """Re-emit the ETF book's published contract from the LAST submission + current marks — no Brain
    call. Used by the open settle (bot/settle.py) so the dashboard reflects the freshly-filled
    positions, and to refresh the book after a code change. Idempotent per asof."""
    from portfolio import etf_universe, paper_account
    from brain import etf_board, etf_mcp
    asof = asof or date.today().isoformat()
    submission = etf_mcp.read_submission()
    held = list((paper_account._load_account(PORTFOLIO_ID).get("positions") or {}).keys())
    target = {h["ticker"]: float(h.get("weight") or 0.0) for h in ((submission or {}).get("holdings") or [])}
    etf_universe.warm(set(target) | set(held) | {BENCHMARK})
    prices: dict[str, float] = {}
    for t in set(target) | set(held) | {BENCHMARK}:
        px = etf_universe.price(t)
        if px and px > 0:
            prices[t] = px
    risk = etf_board.risk_state()
    payload = _build_payload(asof, submission, prices, [], [], {}, risk, [])
    try:
        from bridge import build_portfolio
        build_portfolio.write(payload, portfolio_id=PORTFOLIO_ID)
        return {"ok": True, "holdings": len(target)}
    except Exception as e:                               # noqa: BLE001
        return {"ok": False, "error": repr(e)[:200]}


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------

def _has_history() -> bool:
    from portfolio import registry
    nav_path = registry.data_dir(PORTFOLIO_ID) / "nav_history.jsonl"
    try:
        return nav_path.exists() and bool(nav_path.read_text().strip())
    except Exception:
        return False


def _diff_trades(before: dict, after: dict, prices: dict) -> list[dict]:
    trades = []
    for t in sorted(set(before) | set(after)):
        b = float((before.get(t) or {}).get("shares") or 0.0)
        a = float((after.get(t) or {}).get("shares") or 0.0)
        d = a - b
        if abs(d) < 1e-6:
            continue
        px = prices.get(t)
        trades.append({
            "ticker": t,
            "side": "buy" if d > 0 else "sell",
            "shares": round(abs(d), 4),
            "price": round(px, 4) if px else None,
            "value": round(abs(d) * px, 2) if px else None,
        })
    return trades


def _safe_date(asof: str):
    try:
        return date.fromisoformat(asof)
    except Exception:
        return None


def _regime_dict() -> dict:
    raw = _read_regime()
    return {"quad": raw.get("quad"), "quad_name": raw.get("quad_name"),
            "liquidity_overlay": raw.get("liquidity_overlay")}


def _regime_brief() -> str:
    raw = _read_regime()
    if not raw:
        return ""
    parts = [raw.get("quad_name") or raw.get("quad")]
    if raw.get("cycle_tag"):
        parts.append(f"{raw['cycle_tag']}-cycle")
    if raw.get("liquidity_overlay"):
        parts.append(f"liquidity {raw['liquidity_overlay']}")
    return ", ".join(p for p in parts if p)


def _read_regime() -> dict:
    try:
        p = _ROOT / "vendor" / "macro" / "data" / "regime" / "latest.json"
        if p.exists():
            return json.loads(p.read_text())
    except Exception:
        pass
    return {}


def _run_coro(coro):
    """Run an async coroutine to completion from a sync context (no running loop expected —
    called from the scheduler thread / a worker thread, never inside the event loop)."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        return ex.submit(lambda: asyncio.run(coro)).result()


if __name__ == "__main__":
    import sys
    _armed = "--offline" not in sys.argv
    o = run_etf(armed=_armed)
    print(f"=== etf {o['asof']} (inaugural={o['inaugural']}, trading_day={o['trading_day']}) ===")
    print("brain:", "ok" if o["brain"].get("ok") else o["brain"].get("error", "skipped"),
          "| decided:", o.get("decided"), "| holdings:", o.get("holdings"),
          "| risk:", o.get("risk_state"))
    print("guardrails:", o.get("guardrails"))
    print("executed:", len(o.get("executed") or []), "trades | skipped:", o.get("skipped_unpriceable"))
    print("nav:", o.get("nav"), "| paths:", (o.get("paths") or {}).get("hub"))
