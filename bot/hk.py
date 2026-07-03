"""The HK portfolio — a free-form Opus Brain managing its own $1M Hong Kong paper book.

The HK sibling of ``bot/china.py``. Once per Asia trading day (after the HK session
closes), the HK Brain:
  1. sees its current book (cash, holdings, live HKD P&L) + the China macro regime,
  2. researches freely — the macro-dashboard HK desks (regime, standouts, intake, brief)
     OR web search, its choice,
  3. submits a COMPLETE target book, one rationale per holding (no gate, no research paper),
  4. and the deterministic layer rebalances the paper account to those weights at the latest
     close, marks NAV in HKD vs FXI (iShares China Large-Cap, marked in HKD), and logs the day.

The universe is HONG KONG listed names only: ``*.HK`` tickers quoted in HKD. The book's
base currency is **HKD**: prices are sourced via Yahoo Finance (``data_layer.yahoo_feed``),
and the NAV stays in HKD throughout — there is NO cross-FX conversion to CNY (unlike the
China book, which converts HKD and USD prices to CNY). Everything is scoped to
portfolio_id="hk" so no other book is touched.

Run:  python -m bot.hk        (or the APScheduler 'hk_daily' job, or POST /api/hk/run)
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import date, datetime, timezone
from pathlib import Path

import bot  # noqa: F401  -> vendor/macro onto sys.path

log = logging.getLogger(__name__)

PORTFOLIO_ID = "hk"
SLEEVE = "brain"
BENCHMARK = "FXI"
_ROOT = Path(__file__).resolve().parent.parent
_MAX_TURNS = int(os.environ.get("HK_MAX_TURNS", "30"))

# Base currency + tradeable venue are registry-driven (portfolio.registry, id="hk").
# The HK book is Hong Kong listings ONLY (*.HK), marked natively in HKD — no cross-FX.
from portfolio import registry as _registry
CURRENCY = _registry.currency(PORTFOLIO_ID)            # "HKD"
ALLOWED_VENUES = set(_registry.venues(PORTFOLIO_ID))   # {"HK"} — Hong Kong listings only


# ---------------------------------------------------------------------------
# the daily entrypoint
# ---------------------------------------------------------------------------

def run_hk(asof: str | None = None, *, force: bool = False, armed: bool = True,
           directive: str | None = None) -> dict:
    """Run one HK turn end-to-end. Best-effort: every step degrades gracefully so a missing
    credential / price never leaves the book in a half-traded state."""
    from portfolio import china_calendar, paper_account, position_log

    asof = asof or date.today().isoformat()
    out: dict = {"portfolio_id": PORTFOLIO_ID, "asof": asof,
                 "ran_at": datetime.now(timezone.utc).isoformat()}
    today = _safe_date(asof)
    out["trading_day"] = china_calendar.is_trading_day(today, venue="HK") if today else None

    state0 = paper_account._load_account(PORTFOLIO_ID)
    inaugural = not _has_history() and not (state0.get("positions") or {})
    out["inaugural"] = inaugural

    # 0. FEED-HEALTH GATE (before the Brain). The HK live feed (Yahoo) is effectively
    #    all-or-nothing for a basket. When it is DOWN, currently-held names still mark off the
    #    stale vendored snapshot while brand-new candidates return priceable=false — an ASYMMETRIC
    #    map (the China leg hit exactly this on 2026-06-22, parking ~48% cash on a false "no
    #    investable candidates"). Detect the outage and refuse to transact on it: skip the Brain
    #    and carry the book unchanged. `force=True` overrides (operator escape hatch).
    from data_layer import feed_health
    venue = next(iter(ALLOWED_VENUES), "HK")
    out["feed_health"] = feed_health.status(venue, asof)
    if armed and not force and out["feed_health"]["status"] == "down":
        log.warning(
            "HK turn %s ABORTED — the %s live feed is unavailable. Held names would price off the "
            "stale snapshot while fresh candidates return priceable=false (an asymmetric map); "
            "skipping the Brain and carrying the book unchanged. Re-run with force=True to override.",
            asof, venue)
        armed = False
        out["feed_aborted"] = True

    # 0b. NIGHTLY COST TRIPWIRE (before the Brain). The armed Opus seat below is the dominant cost
    #    (~$1+). If this book has already hit the configured per-night USD cap, SKIP the seat and
    #    carry the book unchanged — same posture as the feed-health abort above. OFF by default
    #    (cap <= 0 → over_budget always False) so this is a no-op and the run is byte-identical.
    from brain import cost_guard
    if armed and cost_guard.over_budget(PORTFOLIO_ID, asof):
        log.warning("HK turn %s — nightly cost cap hit ($%.2f / $%.2f); skipping the Brain and "
                    "carrying the book unchanged.", asof,
                    cost_guard.spent(PORTFOLIO_ID, asof), cost_guard.cap())
        armed = False
        out["cost_capped"] = True

    # 1. run the Brain (armed) → it researches and submits a target book with rationales
    from brain import hk_mcp as china_mcp
    china_mcp.clear_submission()                 # never replay yesterday's decision
    brain: dict = {"ok": False, "skipped": not armed}
    if armed:
        try:
            # directive is an optional ad-hoc override (overnight reviews) — pass it through only when set
            brain = _run_brain(asof, inaugural, directive=directive) if directive else _run_brain(asof, inaugural)
        except Exception as e:                   # noqa: BLE001
            brain = {"ok": False, "error": repr(e)[:300]}
        # record this seat's known cost against the nightly per-book ledger (no-op when unknown).
        cost_guard.record(PORTFOLIO_ID, brain.get("cost_usd"), asof)
    out["brain"] = {k: brain.get(k) for k in ("ok", "cost_usd", "tools_used", "error", "run_id", "model")}

    # 2. read the submitted book — then ENFORCE the venue restriction in the trusted layer: drop any
    #    holding outside the book's allowed venues (A-share only) even if the Brain slipped one in.
    submission = china_mcp.read_submission()
    if submission and ALLOWED_VENUES:
        from brain import china_intake as _intake
        all_h = submission.get("holdings") or []
        kept = [h for h in all_h if _intake._venue(h.get("ticker")) in ALLOWED_VENUES]
        rejected = [h.get("ticker") for h in all_h if _intake._venue(h.get("ticker")) not in ALLOWED_VENUES]
        if rejected:
            submission = {**submission, "holdings": kept}
            out["rejected_offvenue"] = rejected
    decided = bool(submission and submission.get("holdings"))
    out["decided"] = decided

    # 3. price the universe we might trade (targets ∪ held ∪ benchmark) — all converted to CNY,
    #    the book's base currency. The shared price store returns USD (A-share/HK already FX'd to
    #    USD there); we convert that to CNY so A-shares stay native CNY, HK (HKD) and US ADRs (USD)
    #    are marked at the prevailing rate, and the FXI benchmark is marked in CNY too.
    from portfolio import fx
    held = list((paper_account._load_account(PORTFOLIO_ID).get("positions") or {}).keys())
    target = {h["ticker"]: float(h.get("weight") or 0.0)
              for h in (submission.get("holdings") if decided else [])}
    prices: dict[str, float] = {}
    _warm_live(set(target) | set(held))
    for t in set(target) | set(held) | {BENCHMARK}:
        base = fx.usd_to(paper_account._current_price(t), CURRENCY)
        if base and base > 0:
            prices[t] = base

    # 4. EXECUTE — market-hours-aware (HK session, on the A-share calendar). OPEN → rebalance to the
    #    target at the live HKD mark; CLOSED (the normal post-close run, or any off-hours run) → QUEUE
    #    the target to settle at the next open, booking NO fills now — so the book never trades
    #    off-hours and re-running a closed session can't churn it (bot/settle.py + the open settle).
    #    Names we cannot price are skipped (the full target still flows so an unpriceable held name is
    #    carried, not liquidated on a feed gap — critical for the Asia leg).
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
        if executed:
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

    # 5. mark NAV vs FXI (benchmark auto-resolved per-book from the registry)
    try:
        paper_account.mark(prices, asof, portfolio_id=PORTFOLIO_ID)
    except Exception as e:                       # noqa: BLE001
        out["mark_error"] = repr(e)[:200]

    # 6. publish the book contract + 7. append the daily decision log
    payload = _build_payload(asof, submission, prices, executed, skipped, brain,
                             feed_health=out.get("feed_health"))
    try:
        from bridge import build_portfolio
        out["paths"] = build_portfolio.write(payload, portfolio_id=PORTFOLIO_ID)
    except Exception as e:                       # noqa: BLE001
        out["write_error"] = repr(e)[:200]
    try:
        _append_decision_log(asof, submission, executed, skipped, brain,
                             feed_health=out.get("feed_health"))
    except Exception:
        pass

    # 8. delegate the Chinese translation of today's report to the Haiku tier so the dashboard
    #    renders zh the moment it's toggled — automatic after every run, never blocks the book.
    try:
        out["translated"] = _translate_report(submission, brain)
    except Exception:
        out["translated"] = False

    try:
        out["nav"] = round(paper_account.nav(prices, PORTFOLIO_ID), 2)
    except Exception:
        out["nav"] = None
    out["holdings"] = len(target)
    return out


# ---------------------------------------------------------------------------
# the Brain
# ---------------------------------------------------------------------------

_PERSONA = (
    "You are the HONG KONG PORTFOLIO MANAGER of a real-money-style HK$1,000,000 PAPER book, marked "
    "in HKD (Hong Kong dollars). You run once per Asia trading day, after the HK close. You have "
    "FULL discretion: you decide every buy, sell, trim, and the cash level, and you rebalance the "
    "whole book daily. There is NO gate, NO committee, NO research-paper requirement, and NO "
    "doctrine constraining you — only paper cash (you cannot use leverage). \n\n"
    "Idle cash earns ~4% annualized (a money-market sweep), so holding cash when you lack "
    "high-conviction ideas is a REWARDED choice, not dead money — do not force marginal names just "
    "to stay invested. \n\n"
    "Your universe is HONG KONG LISTINGS ONLY — names with the ``*.HK`` suffix, quoted in HKD "
    "(e.g. 0700.HK Tencent, 9988.HK Alibaba, 3690.HK Meituan, 0939.HK CCB). You MAY NOT hold "
    "mainland A-shares (``*.SS`` / ``*.SZ``) or US-listed ADRs — those belong to the separate China "
    "book, and any non-HK ticker you submit will be REJECTED by the desk. HK names are native HKD, so "
    "size every weight as a fraction of the one HKD NAV. \n\n"
    "You have two research channels and may use EITHER or BOTH: (1) the in-house macro China desks "
    "via mcp__hk__* tools — get_china_regime (top-down quad + PBoC liquidity), get_china_intake "
    "(the corroborated HK candidate funnel), get_china_standouts (the Hong-Kong buy board), "
    "get_china_brief — and (2) the open web via WebSearch / WebFetch. Form your own view; you are not "
    "obliged to agree with the in-house engine. \n\n"
    "ALWAYS confirm a name is priceable with mcp__hk__get_quote before you rely on it — it "
    "returns the venue, the local-currency price, and the HKD price the book will actually transact "
    "at; a name with priceable=false will be SKIPPED. When you are done researching, call "
    "mcp__hk__submit_book ONCE with your COMPLETE target book for today: every HK name you want to "
    "hold, its weight (fraction of NAV), and a clear one-paragraph rationale for EACH holding. "
    "Anything you currently hold but omit will be SOLD. Be decisive and concrete; this book is "
    "graded on its realized HKD NAV vs FXI (iShares China Large-Cap, marked in HKD). \n\n"
    "NAMING — in EVERY piece of prose you write (each holding's rationale, the overall summary, the "
    "sold note, and your closing write-up / decision log), refer to a company by its NAME alongside "
    "the ticker, e.g. write 'Tencent (0700.HK)', never a bare '0700.HK'. get_my_book, get_quote, and "
    "the HK buy board all return the English name for every HK ticker — use it. Never leave a stock "
    "code unnamed in the decision log."
)


def _run_brain(asof: str, inaugural: bool, directive: str | None = None) -> dict:
    from brain import hk_mcp as china_mcp, cli_bridge
    from brain import self_mirror, risk_lens   # lazy (package-attr lesson); both flag-gated, byte-identical OFF
    prompt = _build_prompt(asof, inaugural, directive=directive)
    persona = self_mirror.inject(_PERSONA, "hk", _safe_date(asof))
    persona = risk_lens.govern_persona(persona, "hk")          # RISK GOVERNOR mandate; OFF → unchanged
    coro = cli_bridge.reason(
        prompt,
        role="deep",                 # opus, per config/agents.yml
        arm=True,
        append_system=persona,
        mcp_servers=china_mcp.build_servers(),
        allowed_tools=china_mcp.allowed_tools(),
        max_turns=_MAX_TURNS,
    )
    return _run_coro(coro)


def _build_prompt(asof: str, inaugural: bool, directive: str | None = None) -> str:
    from portfolio import paper_account
    state = paper_account._load_account(PORTFOLIO_ID)
    cash = float(state.get("cash") or 0.0)
    positions = state.get("positions") or {}
    regime = _regime_brief()

    lines = [f"# HK book — daily decision for {asof}", ""]
    if directive:
        lines += ["## ⚠ PRIORITY DIRECTIVE FOR THIS RUN", directive.strip(), ""]
    if regime:
        lines += [f"China macro regime (in-house read): {regime}", ""]
    # E2.5 — POSTURE block (flag-independent read-only prompt enrichment).
    # The HK Brain sees the shadow posture so it can observe whether it would have agreed.
    # Missing/absent artifact → section omitted (degrade silently; never blocks the book).
    try:
        from brain import posture_decider as _pd
        _posture_block = _pd.render_directive()
        if _posture_block:
            lines += [_posture_block]
    except Exception:  # noqa: BLE001 — additive; never block the book
        pass
    # RISK GOVERNOR — the live risk-state block that governs sizing/gross (flag-gated; OFF → "").
    from brain import risk_lens
    brief = risk_lens.briefing("hk", regime=_regime_dict(), asof=asof, held=sorted(positions))
    if brief:
        lines += [brief, ""]
    if inaugural:
        lines += [
            "This is your INAUGURAL run. The book is 100% cash: HK$1,000,000 (HKD). Build the "
            "Hong-Kong portfolio from scratch — buy whatever HK listings (*.HK) you are "
            "convinced of, sized however you see fit (keep some cash if you want). No A-share or ADR names.",
            "",
        ]
    else:
        lines += [f"Your current book: HK${cash:,.0f} cash across {len(positions)} holdings "
                  f"({', '.join(sorted(positions)) or 'none'}). Call mcp__hk__get_my_book for the "
                  "full picture (weights, live HKD P&L, and the rationale you last gave each name).", ""]
    lines += [
        "Do your research now (the in-house China desks and/or the web — your call), then submit "
        "your complete target book for today via mcp__hk__submit_book, with a one-paragraph "
        "rationale per holding. Confirm each name is priceable with get_quote first. Rebalance with "
        "conviction; you are accountable for the HKD NAV vs FXI.",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# publish + log helpers
# ---------------------------------------------------------------------------

def _build_payload(asof: str, submission: dict | None, prices: dict, executed: list,
                   skipped: list, brain: dict, feed_health: dict | None = None) -> dict:
    from portfolio import china_calendar, paper_account, position_log
    from brain import china_intake
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
            "name": china_intake.display_name(tk),     # Chinese for A-shares, English for HK/ADR
            "sleeve": SLEEVE,
            "venue": china_intake._venue(tk),
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
        decisions.append({"subject": "China book", "lean": summary,
                          "thesis": (submission or {}).get("sold_note") or "",
                          "logged_at": datetime.now(timezone.utc).isoformat()})
    return {
        "as_of": asof,
        "portfolio_id": PORTFOLIO_ID,
        "manager": "HK Opus Brain",
        "kind": "hk_brain",
        "currency": CURRENCY,
        "benchmark": BENCHMARK,
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
        "feed_health": feed_health,
        "market_status": china_calendar.status(),
        "brain": {k: brain.get(k) for k in ("cost_usd", "tools_used", "model")},
    }


def _append_decision_log(asof: str, submission: dict | None, executed: list,
                         skipped: list, brain: dict, feed_health: dict | None = None) -> None:
    from portfolio import registry
    from brain import china_intake as _intake_mod
    p = registry.data_dir(PORTFOLIO_ID) / "decisions.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "asof": asof,
        "ts": datetime.now(timezone.utc).isoformat(),
        "summary": (submission or {}).get("summary"),
        "sold_note": (submission or {}).get("sold_note"),
        "feed_health": feed_health,
        "holdings": [{"ticker": h.get("ticker"), "name": _intake_mod.display_name(h.get("ticker")),
                      "venue": h.get("venue"), "weight": h.get("weight"),
                      "conviction": h.get("conviction"), "rationale": h.get("rationale")}
                     for h in ((submission or {}).get("holdings") or [])],
        "executed": [{**e, "name": _intake_mod.display_name(e.get("ticker"))} for e in (executed or [])],
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


def _translate_report(submission: dict | None, brain: dict | None) -> bool:
    """Warm the Simplified-Chinese cache for every translatable string in today's report by
    delegating to the Haiku tier (``brain.translate.translate_and_cache`` → role="scout"). Covers
    the overall summary, the sold note, the Brain's closing write-up, and EACH holding's rationale
    — exactly the strings ``/api/decisions`` and the book view re-render in zh via ``cached_zh``.
    Best-effort: returns True if it ran, False on any miss; never raises, never blocks publishing.

    Display NAMES are intentionally NOT translated — A-share names are already Chinese and HK/ADR
    names are English proper nouns that should read the same under either toggle."""
    try:
        from brain import translate
    except Exception:
        return False
    texts: list[str] = []
    sub = submission or {}
    for k in ("summary", "sold_note"):
        v = sub.get(k)
        if v and isinstance(v, str):
            texts.append(v)
    for h in (sub.get("holdings") or []):
        r = h.get("rationale")
        if r and isinstance(r, str):
            texts.append(r)
    bt = (brain or {}).get("text")
    if bt and isinstance(bt, str):
        texts.append(bt[:6000])
    if not texts:
        return False
    try:
        translate.translate_and_cache(texts)
        return True
    except Exception:
        return False


def republish(asof: str | None = None) -> dict:
    """Re-emit the CURRENT book's published contract (with display names) + re-warm the zh cache
    from the LAST submission and today's CNY marks — WITHOUT a new Brain call. Use to refresh the
    live book after a code change (e.g. names / translation) or an FX move. Idempotent per asof."""
    from portfolio import fx, paper_account
    from brain import hk_mcp as china_mcp
    asof = asof or date.today().isoformat()
    submission = china_mcp.read_submission()
    if not submission or not submission.get("holdings"):
        return {"ok": False, "error": "no current submission to republish"}
    held = list((paper_account._load_account(PORTFOLIO_ID).get("positions") or {}).keys())
    target = {h["ticker"]: float(h.get("weight") or 0.0) for h in (submission.get("holdings") or [])}
    prices: dict[str, float] = {}
    _warm_live(set(target) | set(held))
    for t in set(target) | set(held) | {BENCHMARK}:
        base = fx.usd_to(paper_account._current_price(t), CURRENCY)
        if base and base > 0:
            prices[t] = base
    payload = _build_payload(asof, submission, prices, [], [], {})
    out: dict = {"ok": True, "holdings": len(target)}
    try:
        from bridge import build_portfolio
        build_portfolio.write(payload, portfolio_id=PORTFOLIO_ID)
    except Exception as e:                           # noqa: BLE001
        return {"ok": False, "error": repr(e)[:200]}
    # republish has no live Brain object, so recover its closing write-up from the decision log
    # to translate it too (the automated run_china path passes the live brain directly).
    brain = {}
    try:
        decs = load_decisions(1)
        if decs and decs[0].get("brain_text"):
            brain = {"text": decs[0]["brain_text"]}
    except Exception:
        pass
    out["translated"] = _translate_report(submission, brain)
    return out


def load_decisions(limit: int = 60) -> list[dict]:
    """The daily decision log, NEWEST first. Backs /api/decisions?portfolio=china."""
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
# small helpers
# ---------------------------------------------------------------------------

def _has_history() -> bool:
    from portfolio import registry
    nav_path = registry.data_dir(PORTFOLIO_ID) / "nav_history.jsonl"
    try:
        return nav_path.exists() and bool(nav_path.read_text().strip())
    except Exception:
        return False


def _warm_live(tickers) -> None:
    """Batch-prefetch live HK marks so building the price map is ONE Yahoo request, not one per name
    (Yahoo can throttle rapid single-symbol calls). Best-effort; never raises."""
    try:
        from data_layer import yahoo_feed
        yahoo_feed.warm([t for t in tickers if (t or "").upper().endswith(".HK")])
    except Exception:
        pass


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
    # HK uses the China regime frame; lens_row("hk") routes to china_regime/latest.json.
    # lens_row() is golden-output tested to be byte-identical to the old 3-liner.
    from brain.regime_frame import lens_row
    return lens_row("hk")


def _regime_brief() -> str:
    raw = _read_china_regime()
    if not raw:
        return ""
    parts = [raw.get("quad_name") or raw.get("quad")]
    if raw.get("liquidity_overlay"):
        parts.append(f"PBoC liquidity {raw['liquidity_overlay']}")
    return ", ".join(p for p in parts if p)


def _read_china_regime() -> dict:
    try:
        p = _ROOT / "vendor" / "macro" / "data" / "china_regime" / "latest.json"
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
    o = run_hk(armed=_armed)
    print(f"=== hk {o['asof']} (inaugural={o['inaugural']}, trading_day={o['trading_day']}) ===")
    print("brain:", "ok" if o["brain"].get("ok") else o["brain"].get("error", "skipped"),
          "| decided:", o.get("decided"), "| holdings:", o.get("holdings"))
    print("executed:", len(o.get("executed") or []), "trades | skipped:", o.get("skipped_unpriceable"))
    print("nav:", o.get("nav"), "| paths:", (o.get("paths") or {}).get("hub"))
