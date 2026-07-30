"""The autonomous portfolio — a free-form Opus Brain managing its own $1M paper book.

Once per trading day (after the close), the Brain:
  1. sees its current book (cash, holdings, live P&L) + the macro regime,
  2. researches freely — our macro-dashboard data tools OR web search, its choice,
  3. submits a COMPLETE target book, one rationale per holding (no gate, no research paper),
  4. and the deterministic layer rebalances the paper account to those weights at the latest
     close, marks NAV vs SPY, and logs the day's decision with the per-name rationale.

The SIBLING of bot/phase2.py (the gated flagship), but with none of its discipline: no
material-change gate, no sleeves, no confluence/veto/firebreaks, no research-paper
requirement. The only hard constraint is paper cash — no leverage. Everything is scoped to
portfolio_id="autonomous" so the flagship book is never touched.

Run:  python -m bot.autonomous        (or the APScheduler 'autonomous_daily' job, or
                                        POST /api/autonomous/run)
"""
from __future__ import annotations

import asyncio
import json
import os
from datetime import date, datetime, timezone
from pathlib import Path

import bot  # noqa: F401  -> vendor/macro onto sys.path

PORTFOLIO_ID = "autonomous"
SLEEVE = "brain"
_ROOT = Path(__file__).resolve().parent.parent
_MAX_TURNS = int(os.environ.get("AUTONOMOUS_MAX_TURNS", "30"))


def _firm_clamp_freeze_autonomous(priceable: dict[str, float], exc: Exception) -> dict[str, float]:
    """Exception-arm for the autonomous firm-clamp block (Charter P2).

    Called when ``firm_exposure.clamp_book`` raises inside ``run_autonomous``.  Returns
    ``priceable`` frozen to the prior published state: no new adds, no weight increases.

    Prior weights come from ``firm_exposure.published_weights(PORTFOLIO_ID)`` (the
    last-published latest.json, which carries explicit per-ticker weights).

    Downstream: ``execute_or_queue`` / ``rebalance`` treats absent names as liquidate-to-zero,
    so prior-only names are RETAINED in the output at prior weight (freeze = do-not-trade).

    Never raises.
    """
    from portfolio.freeze import freeze_to_prior as _ftp
    prior: dict[str, float] = {}
    try:
        from portfolio import firm_exposure as _fe
        prior = _fe.published_weights(PORTFOLIO_ID)
    except Exception:  # noqa: BLE001
        pass
    try:
        frozen = _ftp(priceable, prior)
    except Exception:  # noqa: BLE001
        frozen = {k: v for k, v in priceable.items() if k in prior}
    try:
        from control_plane.guardrail import GuardrailResult, Severity
        GuardrailResult.failed(
            "firm_clamp",
            Severity.FREEZE,
            detail=f"clamp_book raised: {exc!r}"[:200],
            action_taken="frozen to prior book (no new adds, no weight increases)",
        ).log(job="autonomous_build", book=PORTFOLIO_ID)
    except Exception:  # noqa: BLE001
        pass
    return frozen


# ---------------------------------------------------------------------------
# the daily entrypoint
# ---------------------------------------------------------------------------

def run_autonomous(asof: str | None = None, *, force: bool = False, armed: bool = True,
                   directive: str | None = None) -> dict:
    """Run one autonomous turn end-to-end. Best-effort: every step degrades gracefully so a
    missing credential / price never leaves the book in a half-traded state."""
    from portfolio import market_calendar, paper_account, position_log, registry

    asof = asof or date.today().isoformat()
    out: dict = {"portfolio_id": PORTFOLIO_ID, "asof": asof,
                 "ran_at": datetime.now(timezone.utc).isoformat()}
    today = _safe_date(asof)
    out["trading_day"] = market_calendar.is_trading_day(today) if today else None

    state0 = paper_account._load_account(PORTFOLIO_ID)
    inaugural = not _has_history() and not (state0.get("positions") or {})
    out["inaugural"] = inaugural

    # 0. NIGHTLY COST TRIPWIRE (before the Brain). The armed Opus seat below is the dominant
    #    cost (~$1+). If this book has already hit the configured per-night USD cap, SKIP the seat
    #    and carry the book unchanged — same shape as the feed-health abort. OFF by default (cap
    #    <= 0 → over_budget always False) so this is a no-op and the run is byte-identical.
    from brain import cost_guard
    if armed and cost_guard.over_budget(PORTFOLIO_ID, asof):
        print(f"autonomous turn {asof} — nightly cost cap hit "
              f"(${cost_guard.spent(PORTFOLIO_ID, asof):.2f} / ${cost_guard.cap():.2f}); "
              "skipping the Brain and carrying the book unchanged.")
        armed = False
        out["cost_capped"] = True

    # 1. run the Brain (armed) → it researches and submits a target book with rationales
    from brain import autonomous_mcp
    autonomous_mcp.clear_submission(PORTFOLIO_ID)   # never replay yesterday's decision
    brain: dict = {"ok": False, "skipped": not armed}
    if armed:
        try:
            # directive is an optional ad-hoc override (overnight reviews) — pass it through only when set
            brain = _run_brain(asof, inaugural, directive=directive) if directive else _run_brain(asof, inaugural)
        except Exception as e:                       # noqa: BLE001
            brain = {"ok": False, "error": repr(e)[:300]}
        # record this seat's known cost + token usage against the nightly per-book ledger.
        _usg = brain.get("usage") or {}
        cost_guard.record(
            PORTFOLIO_ID, brain.get("cost_usd"), asof,
            seat="autonomous_brain",
            model=str(brain.get("model") or ""),
            input_tokens=int(_usg.get("input_tokens") or 0),
            output_tokens=int(_usg.get("output_tokens") or 0),
            cache_read_tokens=int(_usg.get("cache_read_input_tokens") or 0),
            cache_creation_tokens=int(_usg.get("cache_creation_input_tokens") or 0),
        )
    out["brain"] = {k: brain.get(k) for k in ("ok", "cost_usd", "tools_used", "error", "run_id", "model")}

    # 2. read the submitted book
    submission = autonomous_mcp.read_submission(PORTFOLIO_ID)
    decided = bool(submission and submission.get("holdings"))
    out["decided"] = decided

    # 2b. PACKET GATE (ruling R6, Charter P2/P3/P8) — wire the DecisionPacket boundary.
    # Gate mode is controlled by MASTERMIND_PACKET_GATE (off | shadow[default] | enforce).
    # Shadow: build+validate+ledger but never reject; enforce: reject invalid → carry-forward.
    # The fallback on rejection is IDENTICAL to the Brain-errored path (decided=False → no
    # trade, book unchanged) so a rejection can never increase exposure vs the error path (P2).
    _pgr = None
    if decided:
        try:
            from control_plane.packet_gate import process as _packet_process
            _pgr = _packet_process(
                PORTFOLIO_ID,
                submission,
                paper_account._load_account(PORTFOLIO_ID),
                extras={
                    "run_id":                brain.get("run_id") if isinstance(brain, dict) else "",
                    "asof":                  asof,
                    # Mandate, falsifiers, and expected_failure_mode come from the Brain's
                    # submission narrative fields (shadow-mode accrual — Brains are prompted to
                    # supply these via tool description enrichment below; sentinel in v1 is OK
                    # for mandate/liquidity_notes but NOT for falsifiers once enforce is on).
                    "mandate":               (submission.get("mandate") or
                                              "Manage the autonomous paper book with full discretion."),
                    "evidence_planes":       submission.get("evidence_planes") or [],
                    "source_provenance":     submission.get("source_provenance") or [],
                    "falsifiers":            submission.get("falsifiers") or [],
                    "liquidity_notes":       submission.get("liquidity_notes") or "<not provided>",
                    "expected_failure_mode": submission.get("expected_failure_mode") or "<not provided>",
                },
            )
            out["packet_id"]   = _pgr.packet_id
            out["packet_meta"] = _pgr.to_meta()
            if not _pgr.ok:
                # ENFORCE mode + invalid packet → fall back to no-proposal path (P2: no new risk)
                decided = False
                out["decided"] = decided
                out["packet_rejected"] = True
        except Exception as _pg_exc:   # noqa: BLE001 — gate must never block the book
            out["packet_gate_error"] = repr(_pg_exc)[:200]

    # 3. price the universe we might trade (targets ∪ held ∪ SPY benchmark)
    held = list((paper_account._load_account(PORTFOLIO_ID).get("positions") or {}).keys())
    target = {h["ticker"]: float(h.get("weight") or 0.0)
              for h in (submission.get("holdings") if decided else [])}
    prices: dict[str, float] = {}
    for t in set(target) | set(held) | {"SPY"}:
        px = paper_account._current_price(t)
        if px and px > 0:
            prices[t] = px

    # 4. EXECUTE — market-hours-aware, with the safety de-gross firebreak applied to the target.
    #    First the ONE risk firebreak over the free-form Brain: a SUBTRACT-ONLY safety de-gross — if
    #    the Brain's target book measures fragile (deep drawdown / high beta / one-factor
    #    concentration / low score), scale it down and let the freed weight stay cash. Never levers
    #    up, never changes which names — risk control, not a second opinion on selection.
    #    Then route the (possibly de-grossed) target through the market-hours-aware settle: OPEN →
    #    rebalance to the target at the live mark; CLOSED (the normal post-close run, or any off-hours
    #    run) → QUEUE the target to settle at the next open, booking NO fills now. So the book never
    #    trades off-hours and re-running a closed session can't churn it (bot/settle.py + the
    #    scheduler's open settle). Unpriceable names are skipped (and surfaced honestly).
    from bot import settle as _settle
    executed: list[dict] = []
    skipped: list[str] = []
    queued = False
    _safety = None
    _safety_overlay = {"gross_mult": 1.0}
    if decided:
        priceable = {t: w for t, w in target.items() if t in prices}
        skipped = sorted(t for t in target if t not in prices)
        try:
            from portfolio import safety as _safety_mod
            if priceable:
                _inv = round(sum(priceable.values()), 4)
                _safety = _safety_mod.compute_safety(
                    PORTFOLIO_ID, asof=asof, weights=priceable,
                    cash_weight=round(max(0.0, 1.0 - _inv), 4), bootstrap=True, network=False)
                _safety_overlay = _safety_mod.gross_overlay(_safety)
                _gm = float(_safety_overlay.get("gross_mult", 1.0))
                if _gm < 1.0:
                    priceable = {t: round(w * _gm, 4) for t, w in priceable.items()}
                _safety["overlay"] = {**_safety_overlay, "applied": _gm < 1.0}
                try:
                    _safety_mod.persist(_safety, PORTFOLIO_ID)
                except Exception:
                    pass
        except Exception as e:                           # noqa: BLE001 — never block the book
            out["safety_error"] = repr(e)[:200]
        # W3 B1 — FIRM-WIDE headroom clamp (Stage 6.3). Clamp this book's target DOWN so the firm-wide
        # cluster/name caps hold across all US books (the audit: four books maxed the SAME SMH). Runs
        # after the safety de-gross, before settle → the freed weight simply stays cash. Subtract-only;
        # never raises a weight; byte-identical no-op when no peer file is readable. Flag-gated
        # (MASTERMIND_FIRM_CAPS, default ON). Sequential: US Brain clamps against Flagship's freshly
        # published book (Flagship builds first by design). Best-effort; never blocks the book.
        try:
            from portfolio import firm_exposure as _firm
            if _firm.caps_enabled():
                _fc = _firm.clamp_book(priceable, PORTFOLIO_ID)
                priceable = _fc["positions"]
                if _fc.get("bound"):
                    out["firm_clamp"] = {"book": PORTFOLIO_ID, "freed": _fc["freed"],
                                         "clamped": _fc["clamped"]}
        except Exception as e:                           # noqa: BLE001 — a firm cap must never block the book
            # GuardrailResult.FREEZE: freeze to prior book — no new adds, no weight increases.
            # Uses _firm_clamp_freeze_autonomous (module-level) so the logic is testable.
            priceable = _firm_clamp_freeze_autonomous(priceable, e)
            out["firm_clamp_error"] = repr(e)[:200]
        res = _settle.execute_or_queue(PORTFOLIO_ID, priceable, prices, asof)
        executed = res.get("executed") or []
        queued = bool(res.get("queued"))
        if res.get("error"):
            out["rebalance_error"] = res["error"]
        if executed:   # reconcile the rationale-bearing ledger only when fills actually happened
            ledger_positions = [{"ticker": t, "sleeve": SLEEVE, "weight": w, "entry_price": prices.get(t)}
                                for t, w in priceable.items()]
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

    # 6. publish the book contract + 7. append the daily decision log
    payload = _build_payload(asof, submission, prices, executed, skipped, brain)
    payload["safety"] = _safety                  # consumed risk backtest (drove the de-gross)
    payload["safety_overlay"] = _safety_overlay
    out["safety_overlay"] = _safety_overlay
    try:
        from bridge import build_portfolio
        out["paths"] = build_portfolio.write(payload, portfolio_id=PORTFOLIO_ID)
    except Exception as e:                           # noqa: BLE001
        out["write_error"] = repr(e)[:200]
    try:
        _append_decision_log(asof, submission, executed, skipped, brain,
                             packet_id=(_pgr.packet_id if _pgr else None))
    except Exception:
        pass

    try:
        out["nav"] = round(paper_account.nav(prices, PORTFOLIO_ID), 2)
    except Exception:
        out["nav"] = None
    out["holdings"] = len(target)

    # ── MW5: mandate-compliance packet (ADVISORY ONLY — never gates) ──────
    try:
        from portfolio import mandate_packet as _mp
        _pkt = _mp.build(PORTFOLIO_ID, out)
        out["mandate_packet"] = _pkt
        _mp.write_packet(_pkt, PORTFOLIO_ID)
        _mp.emit_run_event(_pkt, PORTFOLIO_ID, job="autonomous_daily")
    except Exception:  # noqa: BLE001
        pass

    return out


# ---------------------------------------------------------------------------
# the Brain
# ---------------------------------------------------------------------------

_PERSONA = (
    "You are the AUTONOMOUS PORTFOLIO MANAGER of a real-money-style $1,000,000 PAPER book. "
    "You run once per trading day, after the US close. You have FULL discretion: you decide every "
    "buy, sell, trim, and the cash level, and you rebalance the whole book daily. There is NO gate, "
    "NO committee, NO research-paper requirement, and NO doctrine constraining you — only paper cash "
    "(you cannot use leverage). \n\n"
    "Idle cash earns ~4% annualized (a money-market sweep), so holding cash when you lack "
    "high-conviction ideas is a REWARDED choice, not dead money — do not force marginal names just "
    "to stay invested. \n\n"
    "You have two research channels and may use EITHER or BOTH, your choice: (1) our in-house macro "
    "dashboard via the mcp__bot__* tools (regime, themes, the single-name decision matrix, divergences, "
    "alt-data, news, intel hub, fundamentals, options, anticipation, quotes) and (2) the open web via "
    "WebSearch / WebFetch. Form your own view; you are not obliged to agree with the in-house engine. \n\n"
    "Trade liquid US-listed equities and ETFs (use mcp__bot__get_quote to confirm a name is priceable "
    "before you rely on it — names we cannot price are skipped). Manage risk yourself through "
    "diversification, sizing, and cash. When you are done researching, call mcp__desk__submit_book ONCE "
    "with your COMPLETE target book for today: every name you want to hold, its weight (fraction of NAV), "
    "and a clear one-paragraph rationale for EACH holding (why you own it, now). Anything you currently "
    "hold but omit will be SOLD. Be decisive and concrete; this book is graded on its realized NAV vs the "
    "S&P 500."
)


def _run_brain(asof: str, inaugural: bool, directive: str | None = None) -> dict:
    from brain import autonomous_mcp, cli_bridge
    from brain import self_mirror, risk_lens, student   # lazy; all flag-gated, byte-identical OFF
    prompt = _build_prompt(asof, inaugural, directive=directive)
    prompt = student.inject(prompt, _safe_date(asof))   # #3 fast numeric prior (MASTERMIND_STUDENT; OFF→unchanged)
    persona = self_mirror.inject(_PERSONA, "autonomous", _safe_date(asof))
    persona = risk_lens.govern_persona(persona, "autonomous")   # RISK GOVERNOR mandate; OFF → unchanged
    coro = cli_bridge.reason(
        prompt,
        role="deep",                 # opus, per config/agents.yml
        arm=True,
        append_system=persona,
        mcp_servers=autonomous_mcp.build_servers(),
        allowed_tools=autonomous_mcp.allowed_tools(),
        max_turns=_MAX_TURNS,
        book=PORTFOLIO_ID,           # bot records against cost_guard; skip bridge double-count
    )
    return _run_coro(coro)


def _build_prompt(asof: str, inaugural: bool, directive: str | None = None) -> str:
    from portfolio import paper_account
    state = paper_account._load_account(PORTFOLIO_ID)
    cash = float(state.get("cash") or 0.0)
    positions = state.get("positions") or {}
    regime = _regime_brief()

    lines = [f"# Autonomous book — daily decision for {asof}", ""]
    if directive:
        lines += ["## ⚠ PRIORITY DIRECTIVE FOR THIS RUN", directive.strip(), ""]
    if regime:
        lines += [f"Macro regime (in-house read): {regime}", ""]
    # RISK GOVERNOR — the live risk-state block that governs sizing/gross (flag-gated; OFF → "").
    from brain import risk_lens
    brief = risk_lens.briefing("autonomous", regime=_regime_dict(), asof=asof, held=sorted(positions))
    if brief:
        lines += [brief, ""]
    # E2.5 — POSTURE block (flag-independent read-only prompt enrichment).
    # The autonomous Brain sees the shadow posture so it can observe whether it would have agreed.
    # Missing/absent artifact → section omitted (degrade silently; never blocks the book).
    try:
        from brain import posture_decider as _pd
        _posture_block = _pd.render_directive()
        if _posture_block:
            lines += [_posture_block]
    except Exception:  # noqa: BLE001 — additive; never block the book
        pass
    if inaugural:
        lines += [
            "This is your INAUGURAL run. The book is 100% cash: $1,000,000. Build the portfolio "
            "from scratch — buy whatever you are convinced of, sized however you see fit (keep some "
            "cash if you want).",
            "",
        ]
    else:
        lines += [f"Your current book: ${cash:,.0f} cash across {len(positions)} holdings "
                  f"({', '.join(sorted(positions)) or 'none'}). Call mcp__desk__get_my_book for the "
                  "full picture (weights, live P&L, and the rationale you last gave each name).", ""]
    lines += [
        "Do your research now (in-house tools and/or the web — your call), then submit your complete "
        "target book for today via mcp__desk__submit_book, with a one-paragraph rationale per holding. "
        "Rebalance with conviction; you are accountable for the NAV.",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# publish + log helpers
# ---------------------------------------------------------------------------

def _build_payload(asof: str, submission: dict | None, prices: dict, executed: list,
                   skipped: list, brain: dict) -> dict:
    from portfolio import market_calendar, paper_account, position_log, registry
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
            # shaped so the dashboard's existing position renderer shows the rationale
            "thesis_full": {"summary": rationale, "why_now": rationale, "bull": [], "bear": []}
            if rationale else None,
        })
    positions.sort(key=lambda p: (p.get("weight") or 0.0), reverse=True)

    gross = round(sum((p.get("weight") or 0.0) for p in positions), 4)
    decisions = []
    summary = (submission or {}).get("summary")
    if summary:
        decisions.append({"subject": "Autonomous book", "lean": summary,
                          "thesis": (submission or {}).get("sold_note") or "",
                          "logged_at": datetime.now(timezone.utc).isoformat()})
    return {
        "as_of": asof,
        "portfolio_id": PORTFOLIO_ID,
        "manager": "Mastermind AI (Codex-first)",
        "kind": "autonomous",
        "regime": _regime_dict(),
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
                         skipped: list, brain: dict,
                         *, packet_id: str | None = None) -> None:
    from portfolio import registry
    p = registry.data_dir(PORTFOLIO_ID) / "decisions.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "asof": asof,
        "ts": datetime.now(timezone.utc).isoformat(),
        "summary": (submission or {}).get("summary"),
        "sold_note": (submission or {}).get("sold_note"),
        "holdings": [{"ticker": h.get("ticker"), "weight": h.get("weight"),
                      "conviction": h.get("conviction"), "rationale": h.get("rationale")}
                     for h in ((submission or {}).get("holdings") or [])],
        "executed": executed,
        "skipped_unpriceable": skipped,
        # the Brain's closing reasoning + a pointer to the FULL step-trace (every tool it called +
        # what it pulled + its reasoning steps), retrievable via /api/runlog?run_id=<run_id>.
        "brain_text": (brain.get("text") or "")[:6000] if isinstance(brain, dict) else None,
        "run_id": brain.get("run_id") if isinstance(brain, dict) else None,
        "tools_used": brain.get("tools_used") if isinstance(brain, dict) else None,
        "cost_usd": brain.get("cost_usd") if isinstance(brain, dict) else None,
        "model": brain.get("model") if isinstance(brain, dict) else None,
        "error": brain.get("error") if isinstance(brain, dict) else None,
        "packet_id": packet_id,
    }
    # idempotent per date: keep exactly one entry per asof (latest SUBSTANTIVE run wins — a
    # failed re-run must not erase a good book; see bot/decision_rows)
    from bot import decision_rows
    existing = []
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                existing.append(json.loads(line))
            except Exception:
                continue
    rows = decision_rows.replace_for_asof(existing, entry, asof)
    p.write_text("\n".join(json.dumps(r, default=str, ensure_ascii=False) for r in rows) + "\n")


def load_decisions(limit: int = 60) -> list[dict]:
    """The daily decision log, NEWEST first. Backs /api/decisions."""
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
    """Re-emit the autonomous book's published contract from the LAST submission + current marks —
    no Brain call. Used by the open settle (bot/settle.py) so the dashboard reflects freshly-filled
    positions. Idempotent per asof."""
    from portfolio import paper_account
    from brain import autonomous_mcp
    asof = asof or date.today().isoformat()
    submission = autonomous_mcp.read_submission(PORTFOLIO_ID)
    held = list((paper_account._load_account(PORTFOLIO_ID).get("positions") or {}).keys())
    target = {h["ticker"]: float(h.get("weight") or 0.0) for h in ((submission or {}).get("holdings") or [])}
    prices: dict[str, float] = {}
    for t in set(target) | set(held) | {"SPY"}:
        px = paper_account._current_price(t)
        if px and px > 0:
            prices[t] = px
    payload = _build_payload(asof, submission, prices, [], [], {})
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
    # Delegated to the single regime reader (architecture Stage 1, W1).
    # lens_row() is golden-output tested to be byte-identical to the old 3-liner.
    from brain.regime_frame import lens_row
    return lens_row("us")


def _regime_brief() -> str:
    """The persona's regime line — upgraded from the bare label to the full perception read.

    HOT PATCH (charter P1, 2026-07-02 incident): on 07-02 this brief fed the Brain exactly
    'Goldilocks Q1, liquidity expanding' and it re-risked into semis two days into the breakdown
    while the cycles/radar/confidence planes all disagreed with the label. The Brain must see the
    SAME enriched slice the judgment seats get (pm_conviction._full_regime_slice, W4) — the label
    plus its own uncertainty and the planes that contradict it. Degrades to the old one-liner on
    any failure (P2: missing data coarsens, never inflates).

    E1.1 (W-E.1): now appends the market_view brief + label_vs_planes line from the E0.3 organ
    AFTER the existing W4 frame read.  Compose, don't duplicate: the frame fields above are kept
    as-is; the view block is an ADDITIVE tail section.  Absent view → section omitted (byte-identical
    to current behavior when the organ hasn't been built yet).
    """
    raw = _read_regime()
    if not raw:
        return ""
    label = ", ".join(p for p in (
        raw.get("quad_name") or raw.get("quad"),
        f"liquidity {raw['liquidity_overlay']}" if raw.get("liquidity_overlay") else None) if p)
    try:
        from brain.pm_conviction import _full_regime_slice
        fr = _full_regime_slice(raw)
        lines = [f"REGIME LABEL: {label}"]
        conf = fr.get("confidence")
        if conf is not None:
            lines.append(f"label confidence: {conf} — LOW confidence means the label itself is suspect")
        if fr.get("transition_state"):
            lines.append(f"transition: {fr['transition_state']}")
        contra = fr.get("contradicting") or []
        if contra:
            lines.append(f"legs CONTRADICTING the label: {', '.join(map(str, contra))}")
        cyc = fr.get("cycles") or {}
        if cyc.get("late_cycle"):
            lines.append(f"cycle engine reads LATE-CYCLE (do not add): {', '.join(cyc['late_cycle'])}")
        if cyc.get("entry_favored"):
            lines.append(f"cycle engine favors ENTRY: {', '.join(cyc['entry_favored'])}")
        mrs = fr.get("macro_risk") or {}
        if mrs.get("state"):
            lines.append(f"risk state (dwell): {mrs['state']}, fragility {mrs.get('fragility')}")
        lines.append("Charter P1: never size off the label alone — cite at least one more plane "
                     "(cycles / risk state / contradicting legs) agreeing with any add.")
        # E1.1 — market_view perception layer (ADDITIVE tail; absent view → section omitted).
        # Read the market_view enrichment from _full_regime_slice (it's already computed there);
        # do NOT re-read the artifact (W2: consume once per call chain).
        _mv = fr.get("market_view") or {}
        if _mv.get("label_vs_planes_line"):
            lines.append("")
            lines.append(f"PERCEPTION LAYER (market_view): {_mv['label_vs_planes_line']}")
            _mvb = _mv.get("market_view_brief") or {}
            if _mvb.get("posture_implication"):
                lines.append(f"Posture implication: {_mvb['posture_implication']}")
            if _mvb.get("wheres_the_risk"):
                lines.append(f"Where the risk is: {_mvb['wheres_the_risk']}")
        return "\n".join(lines)
    except Exception:  # noqa: BLE001 — degrade to the legacy one-liner
        return label


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
    this is called from the scheduler thread / a worker thread, never inside the event loop)."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    # a loop is already running in this thread — run the coro on a fresh loop in a worker thread
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        return ex.submit(lambda: asyncio.run(coro)).result()


if __name__ == "__main__":
    import sys
    _armed = "--offline" not in sys.argv
    o = run_autonomous(armed=_armed)
    print(f"=== autonomous {o['asof']} (inaugural={o['inaugural']}, trading_day={o['trading_day']}) ===")
    print("brain:", "ok" if o["brain"].get("ok") else o["brain"].get("error", "skipped"),
          "| decided:", o.get("decided"), "| holdings:", o.get("holdings"))
    print("executed:", len(o.get("executed") or []), "trades | skipped:", o.get("skipped_unpriceable"))
    print("nav:", o.get("nav"), "| paths:", (o.get("paths") or {}).get("hub"))
