"""The Heavyweight portfolio — an Opus Brain that concentrates Flagship's BEST ideas.

Once per trading day (after Flagship's nightly build), the Brain:
  1. sees Flagship's full state — holdings + weights, trade history, per-name research papers,
     and the reasoning trace — via the read-only mcp__heavydesk__get_flagship_* tools,
  2. is told its universe is Flagship's CURRENT holdings (enforced in Python, below),
  3. submits a COMPLETE concentrated target book — a short, high-conviction subset,
  4. and the deterministic layer ENFORCES the universe + the 5–50% sizing rails, rebalances the
     heavyweight paper account, marks NAV vs SPY, publishes, and logs the day.

The SIBLING of bot/autonomous.py, but with one hard discipline the free-form book lacks: the
universe constraint + concentration rails, both enforced here in trusted Python — never on the
LLM's good behaviour. Everything is scoped to portfolio_id="heavyweight"; Flagship/Autonomous
are only ever READ.

Sizing doctrine (user-set): tight ~5–8 names, each 5%–50% of NAV, sub-5% nibbles DROPPED, and a
hard never-liquidate guard — if the universe/sizing rails strip the whole submission, the prior
book is HELD rather than blown to cash. "Add to winners" is expressed by the Brain through size
(the rebalance sets absolute weights; there is no separate delta path).

Run:  python -m bot.heavyweight        (or the APScheduler 'heavyweight_daily' job, or
                                         POST /api/heavyweight/run)
"""
from __future__ import annotations

import asyncio
import json
import os
from datetime import date, datetime, timezone
from pathlib import Path

import bot  # noqa: F401  -> vendor/macro onto sys.path

PORTFOLIO_ID = "heavyweight"
FLAGSHIP_ID = "flagship"
SLEEVE = "heavy"
_ROOT = Path(__file__).resolve().parent.parent
_MAX_TURNS = int(os.environ.get("HEAVYWEIGHT_MAX_TURNS", "30"))

# Sizing rails (env-overridable): concentrated book, 5–50% per name, ~8 names max.
MIN_W = float(os.environ.get("HEAVYWEIGHT_MIN_WEIGHT", "0.05"))
MAX_W = float(os.environ.get("HEAVYWEIGHT_MAX_WEIGHT", "0.50"))
MAX_NAMES = int(os.environ.get("HEAVYWEIGHT_MAX_NAMES", "8"))


# ---------------------------------------------------------------------------
# the deterministic gate — the genuinely new logic vs the autonomous clone
# ---------------------------------------------------------------------------

def _flagship_universe() -> set[str]:
    """The names Heavyweight may hold = Flagship's last-published book: the tickers in its
    latest.json positions[] ∪ pending_orders[]. The union covers the market-closed state where
    Flagship's buys are still queued (positions reflect the INTENDED book). Empty set when
    Flagship has no published book → the caller fails closed and does not trade."""
    from portfolio import registry
    allowed: set[str] = set()
    try:
        p = registry.data_dir(FLAGSHIP_ID) / "latest.json"
        if not p.exists():
            return allowed
        d = json.loads(p.read_text())
        for row in (d.get("positions") or []):
            t = (row.get("ticker") or "").upper().strip()
            if t:
                allowed.add(t)
        for o in (d.get("pending_orders") or []):
            t = (o.get("ticker") or "").upper().strip()
            if t:
                allowed.add(t)
    except Exception:
        pass
    return allowed


def _enforce(holdings: list[dict], allowed: set[str]) -> tuple[dict, list[dict], dict]:
    """Apply the universe + concentration rails to the Brain's raw submission. Returns
    (final_weights {ticker: weight}, kept [full holding dicts], notes). Order:
      1. drop names NOT in Flagship's universe,
      2. clamp each weight DOWN to MAX_W,
      3. drop names sized below MIN_W (sub-5% nibbles — off the concentrated mandate),
      4. keep only the top MAX_NAMES by weight,
      5. renormalize DOWN to gross ≤ 1.0 (no leverage)."""
    notes: dict = {"out_of_universe": [], "clamped": [], "dropped_below_floor": [],
                   "dropped_overflow": []}
    kept: list[dict] = []
    for h in holdings:
        t = (h.get("ticker") or "").upper().strip()
        try:
            w = float(h.get("weight") or 0.0)
        except (TypeError, ValueError):
            w = 0.0
        if not t or t not in allowed:
            if t:
                notes["out_of_universe"].append(t)
            continue
        if w > MAX_W:
            notes["clamped"].append({"ticker": t, "from": round(w, 4), "to": MAX_W})
            w = MAX_W
        if w < MIN_W:
            notes["dropped_below_floor"].append({"ticker": t, "weight": round(w, 4)})
            continue
        kept.append({**h, "ticker": t, "weight": w})

    kept.sort(key=lambda x: x["weight"], reverse=True)
    if len(kept) > MAX_NAMES:
        for h in kept[MAX_NAMES:]:
            notes["dropped_overflow"].append(h["ticker"])
        kept = kept[:MAX_NAMES]

    gross = sum(h["weight"] for h in kept)
    if gross > 1.0 and kept:
        scale = 1.0 / gross
        for h in kept:
            h["weight"] = round(h["weight"] * scale, 6)
        notes["renormalized_from_gross"] = round(gross, 4)

    final = {h["ticker"]: round(float(h["weight"]), 6) for h in kept}
    return final, kept, notes


# ---------------------------------------------------------------------------
# the daily entrypoint
# ---------------------------------------------------------------------------

def run_heavyweight(asof: str | None = None, *, force: bool = False, armed: bool = True) -> dict:
    """Run one Heavyweight turn end-to-end. Best-effort: every step degrades gracefully."""
    from portfolio import market_calendar, paper_account, position_log
    from brain import heavyweight_mcp

    asof = asof or date.today().isoformat()
    out: dict = {"portfolio_id": PORTFOLIO_ID, "asof": asof,
                 "ran_at": datetime.now(timezone.utc).isoformat()}
    today = _safe_date(asof)
    out["trading_day"] = market_calendar.is_trading_day(today) if today else None

    state0 = paper_account._load_account(PORTFOLIO_ID)
    inaugural = not _has_history() and not (state0.get("positions") or {})
    out["inaugural"] = inaugural

    # 0. NIGHTLY COST TRIPWIRE (before the Brain). The armed Opus seat below is the dominant cost
    #    (~$1+). If this book has already hit the configured per-night USD cap, SKIP the seat and
    #    carry the prior book unchanged. OFF by default (cap <= 0 → over_budget always False) so
    #    this is a no-op and the run is byte-identical.
    from brain import cost_guard
    if armed and cost_guard.over_budget(PORTFOLIO_ID, asof):
        print(f"heavyweight turn {asof} — nightly cost cap hit "
              f"(${cost_guard.spent(PORTFOLIO_ID, asof):.2f} / ${cost_guard.cap():.2f}); "
              "skipping the Brain and carrying the book unchanged.")
        armed = False
        out["cost_capped"] = True

    # 1. run the Brain (armed) → it studies Flagship and submits a concentrated target book
    heavyweight_mcp.clear_submission(PORTFOLIO_ID)
    brain: dict = {"ok": False, "skipped": not armed}
    if armed:
        try:
            brain = _run_brain(asof, inaugural)
        except Exception as e:                       # noqa: BLE001
            brain = {"ok": False, "error": repr(e)[:300]}
        # record this seat's known cost against the nightly per-book ledger (no-op when unknown).
        cost_guard.record(PORTFOLIO_ID, brain.get("cost_usd"), asof)
    out["brain"] = {k: brain.get(k) for k in ("ok", "cost_usd", "tools_used", "error", "run_id", "model")}

    # 2. read the submitted book
    submission = heavyweight_mcp.read_submission(PORTFOLIO_ID)
    submitted = bool(submission and submission.get("holdings"))
    out["decided"] = submitted

    # 3. DETERMINISTIC universe + sizing rails (the hard gate — Python owns it, not the prompt)
    allowed = _flagship_universe()
    out["flagship_universe_size"] = len(allowed)
    final_weights: dict[str, float] = {}
    kept: list[dict] = []
    notes: dict = {}
    held_prior = False
    if submitted:
        if not allowed:
            out["universe_empty"] = True             # no Flagship book → fail closed, do not trade
            held_prior = True
        else:
            final_weights, kept, notes = _enforce(submission["holdings"], allowed)
            if not final_weights:
                # the rails stripped the WHOLE submission — never blow the book to cash; hold prior.
                held_prior = True
                notes["held_prior_reason"] = "all submitted names dropped by universe/sizing rails"
    out["enforcement"] = notes
    out["held_prior_book"] = held_prior

    # 4. price the universe we might trade (targets ∪ held ∪ SPY)
    held = list((paper_account._load_account(PORTFOLIO_ID).get("positions") or {}).keys())
    prices: dict[str, float] = {}
    for t in set(final_weights) | set(held) | {"SPY"}:
        px = paper_account._current_price(t)
        if px and px > 0:
            prices[t] = px

    # 5. EXECUTE — rebalance to the FINAL (enforced) weights at close prices.
    executed: list[dict] = []
    skipped: list[str] = []
    do_trade = submitted and bool(final_weights) and not held_prior
    if do_trade:
        priceable = {t: w for t, w in final_weights.items() if t in prices}
        skipped = sorted(t for t in final_weights if t not in prices)
        before = dict((paper_account._load_account(PORTFOLIO_ID).get("positions") or {}))
        try:
            paper_account.rebalance(priceable, prices, asof, portfolio_id=PORTFOLIO_ID)
        except Exception as e:                       # noqa: BLE001
            out["rebalance_error"] = repr(e)[:200]
        after = dict((paper_account._load_account(PORTFOLIO_ID).get("positions") or {}))
        executed = _diff_trades(before, after, prices)
        ledger_positions = [{"ticker": t, "sleeve": SLEEVE, "weight": w,
                             "entry_price": prices.get(t)} for t, w in priceable.items()]
        try:
            position_log.update(ledger_positions, asof, portfolio_id=PORTFOLIO_ID)
        except Exception:
            pass
    out["executed"] = executed
    out["skipped_unpriceable"] = skipped

    # 6. mark NAV vs SPY (idempotent per date)
    try:
        paper_account.mark(prices, asof, portfolio_id=PORTFOLIO_ID)
    except Exception as e:                           # noqa: BLE001
        out["mark_error"] = repr(e)[:200]

    # 7. publish the book contract + 8. append the daily decision log
    payload = _build_payload(asof, submission, kept, notes, prices, executed, skipped, brain, held_prior)
    try:
        from bridge import build_portfolio
        out["paths"] = build_portfolio.write(payload, portfolio_id=PORTFOLIO_ID)
    except Exception as e:                           # noqa: BLE001
        out["write_error"] = repr(e)[:200]
    try:
        _append_decision_log(asof, submission, kept, notes, executed, skipped, brain, held_prior)
    except Exception:
        pass

    try:
        out["nav"] = round(paper_account.nav(prices, PORTFOLIO_ID), 2)
    except Exception:
        out["nav"] = None
    out["holdings"] = len(final_weights)
    return out


# ---------------------------------------------------------------------------
# the Brain
# ---------------------------------------------------------------------------

_PERSONA = (
    "You are the HEAVYWEIGHT PORTFOLIO MANAGER of a $1,000,000 PAPER book. You run once per trading "
    "day, after the US close. Your EDGE is concentration and conviction: the Flagship book already "
    "holds a broad, engine-disciplined set of names — your job is to find the BEST SUBSET of what "
    "Flagship holds and bet on it with SIZE.\n\n"
    "Your tradable universe is FLAGSHIP'S CURRENT HOLDINGS — period. You may ONLY hold names Flagship "
    "currently holds; anything you submit that Flagship does not hold is dropped automatically. Use the "
    "mcp__heavydesk__get_flagship_book / get_flagship_trades / get_flagship_research / "
    "get_flagship_thinking tools to see EXACTLY what Flagship is doing — its holdings and weights, its "
    "trade history, its per-name research papers, and its full reasoning trace — and one-up it by "
    "concentrating into the highest-conviction, most ASYMMETRIC winners.\n\n"
    "Mandate: ASYMMETRIC RETURNS. Bet on your winners; add to your winners. When a name is working and "
    "the thesis is intact, SIZE UP into it rather than trimming. Run a CONCENTRATED book — roughly 5 to "
    "8 names, each 5% to 50% of NAV. Sub-5% nibbles are DROPPED and only your top ~8 by size are kept, "
    "so submit a short, decisive list. Hold cash when you lack conviction; do not dilute the book with "
    "marginal names.\n\n"
    "Idle cash earns ~4% annualized (a money-market sweep) — holding it when you lack a "
    "high-conviction asymmetric bet is a REWARDED choice, not dead money. \n\n"
    "You also have the macro dashboard (mcp__bot__*) and the open web for context, but your universe is "
    "Flagship's holdings. When done, call mcp__heavydesk__submit_book ONCE with your complete "
    "concentrated target book, a one-paragraph conviction rationale per holding, and a summary of how "
    "you are pressing Flagship's best ideas. You are graded on realized NAV vs the S&P 500 — and vs "
    "Flagship itself."
)


def _run_brain(asof: str, inaugural: bool) -> dict:
    from brain import heavyweight_mcp, cli_bridge
    from brain import self_mirror, risk_lens, student   # lazy; all flag-gated, byte-identical OFF
    prompt = _build_prompt(asof, inaugural)
    prompt = student.inject(prompt, _safe_date(asof))   # #3 fast numeric prior (MASTERMIND_STUDENT; OFF→unchanged)
    persona = self_mirror.inject(_PERSONA, "heavyweight", _safe_date(asof))
    persona = risk_lens.govern_persona(persona, "heavyweight")  # RISK GOVERNOR (concentration); OFF → unchanged
    coro = cli_bridge.reason(
        prompt,
        role="deep",                 # opus, per config/agents.yml
        arm=True,
        append_system=persona,
        mcp_servers=heavyweight_mcp.build_servers(),
        allowed_tools=heavyweight_mcp.allowed_tools(),
        max_turns=_MAX_TURNS,
    )
    return _run_coro(coro)


def _build_prompt(asof: str, inaugural: bool) -> str:
    from portfolio import paper_account
    state = paper_account._load_account(PORTFOLIO_ID)
    cash = float(state.get("cash") or 0.0)
    positions = state.get("positions") or {}
    regime = _regime_brief()
    allowed = sorted(_flagship_universe())

    lines = [f"# Heavyweight book — daily decision for {asof}", ""]
    if regime:
        lines += [f"Macro regime (in-house read): {regime}", ""]
    # RISK GOVERNOR — the live risk-state block that governs CONCENTRATION (flag-gated; OFF → "").
    from brain import risk_lens
    brief = risk_lens.briefing("heavyweight", regime=_regime_dict(), asof=asof, held=sorted(positions))
    if brief:
        lines += [brief, ""]
    lines += [
        f"FLAGSHIP currently holds {len(allowed)} names — this is your ENTIRE tradable universe. You "
        "may ONLY hold names from this list (anything else you submit is dropped):",
        (", ".join(allowed) if allowed else "(none — Flagship has not published a book yet)"),
        "",
    ]
    if inaugural:
        lines += [
            "This is your INAUGURAL run. The book is 100% cash: $1,000,000. Study Flagship's book, "
            "trades, research papers, and reasoning trace (mcp__heavydesk__get_flagship_*), then "
            "concentrate into the 5–8 highest-conviction, most asymmetric names — 5% to 50% each.",
            "",
        ]
    else:
        lines += [
            f"Your current book: ${cash:,.0f} cash across {len(positions)} holdings "
            f"({', '.join(sorted(positions)) or 'none'}). Call mcp__heavydesk__get_my_book for the full "
            "picture (weights, live P&L, the rationale you last gave each name).", "",
        ]
    lines += [
        "Research Flagship now (its holdings, trades, per-name research, and thinking), then submit your "
        "complete concentrated target book via mcp__heavydesk__submit_book — one conviction rationale per "
        "holding. Press your winners; be decisive; you are accountable for the NAV.",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# publish + log helpers
# ---------------------------------------------------------------------------

def _build_payload(asof: str, submission: dict | None, kept: list[dict], notes: dict, prices: dict,
                   executed: list, skipped: list, brain: dict, held_prior: bool) -> dict:
    from portfolio import market_calendar, paper_account, position_log
    state = paper_account._load_account(PORTFOLIO_ID)
    pnl = paper_account.positions_pnl(prices, PORTFOLIO_ID)
    nav = paper_account.nav(prices, PORTFOLIO_ID)
    cash = float(state.get("cash") or 0.0)
    rationale_by_tk = {h["ticker"].upper(): h for h in (kept or [])}

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
            "thesis_full": {"summary": rationale, "why_now": rationale, "bull": [], "bear": []}
            if rationale else None,
        })
    positions.sort(key=lambda p: (p.get("weight") or 0.0), reverse=True)

    gross = round(sum((p.get("weight") or 0.0) for p in positions), 4)
    total_return_pct = None
    try:
        perf = paper_account.performance(portfolio_id=PORTFOLIO_ID)
        total_return_pct = perf.get("total_return_pct")
    except Exception:
        pass
    decisions = []
    summary = (submission or {}).get("summary")
    if summary:
        decisions.append({"subject": "Heavyweight book", "lean": summary,
                          "thesis": (submission or {}).get("sold_note") or "",
                          "logged_at": datetime.now(timezone.utc).isoformat()})
    return {
        "as_of": asof,
        "portfolio_id": PORTFOLIO_ID,
        "manager": "Heavyweight Opus Brain",
        "kind": "heavyweight",
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
        "enforcement": notes,                 # what the rails dropped/clamped (honesty)
        "held_prior_book": held_prior,
        "vs_flagship_pct": _vs_flagship(total_return_pct),
        "market_status": market_calendar.status(),
        "brain": {k: brain.get(k) for k in ("cost_usd", "tools_used", "model")},
    }


def _vs_flagship(hw_return_pct) -> float | None:
    """Heavyweight's total return minus Flagship's — the 'beating Flagship?' read the persona promises."""
    if hw_return_pct is None:
        return None
    try:
        from portfolio import paper_account
        fr = paper_account.performance(portfolio_id=FLAGSHIP_ID).get("total_return_pct")
        if fr is not None:
            return round(float(hw_return_pct) - float(fr), 2)
    except Exception:
        pass
    return None


def _append_decision_log(asof: str, submission: dict | None, kept: list[dict], notes: dict,
                         executed: list, skipped: list, brain: dict, held_prior: bool) -> None:
    from portfolio import registry
    p = registry.data_dir(PORTFOLIO_ID) / "decisions.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "asof": asof,
        "ts": datetime.now(timezone.utc).isoformat(),
        "summary": (submission or {}).get("summary"),
        "sold_note": (submission or {}).get("sold_note"),
        # the FINAL (enforced) holdings — what the book actually targets, not the raw submission
        "holdings": [{"ticker": h.get("ticker"), "weight": h.get("weight"),
                      "conviction": h.get("conviction"), "rationale": h.get("rationale")}
                     for h in (kept or [])],
        "enforcement": notes,
        "held_prior_book": held_prior,
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
    """The daily decision log, NEWEST first. Backs /api/decisions?portfolio=heavyweight."""
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


# ---------------------------------------------------------------------------
# small helpers (cloned from bot/autonomous.py — portfolio-agnostic plumbing)
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
    """Run an async coroutine to completion from a sync context (scheduler/worker thread)."""
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
    o = run_heavyweight(armed=_armed)
    print(f"=== heavyweight {o['asof']} (inaugural={o['inaugural']}, trading_day={o['trading_day']}) ===")
    print("brain:", "ok" if o["brain"].get("ok") else o["brain"].get("error", "skipped"),
          "| decided:", o.get("decided"), "| universe:", o.get("flagship_universe_size"),
          "| holdings:", o.get("holdings"), "| held_prior:", o.get("held_prior_book"))
    print("enforcement:", json.dumps(o.get("enforcement") or {}, default=str))
    print("executed:", len(o.get("executed") or []), "trades | skipped:", o.get("skipped_unpriceable"))
    print("nav:", o.get("nav"), "| paths:", (o.get("paths") or {}).get("hub"))
