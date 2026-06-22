"""The China portfolio — a free-form Opus Brain managing its own $1M all-China paper book.

The Greater-China sibling of ``bot/autonomous.py``. Once per Asia trading day (after the
A-share close), the China Brain:
  1. sees its current book (cash, holdings, live USD P&L) + the China macro regime,
  2. researches freely — the macro-dashboard China desks (regime, standouts, intake, brief)
     OR web search, its choice,
  3. submits a COMPLETE target book, one rationale per holding (no gate, no research paper),
  4. and the deterministic layer rebalances the paper account to those weights at the latest
     close, marks NAV in CNY vs FXI (iShares China Large-Cap, marked in CNY), and logs the day.

The universe is ALL of Greater China: mainland A-shares (``*.SS`` / ``*.SZ``, quoted CNY), Hong
Kong (``*.HK``, HKD), and US-listed China ADRs (USD). The book's base currency is **CNY**:
A-shares are native, while HK (HKD) and ADR (USD) prices are converted to CNY at the prevailing
rate (``portfolio.fx.usd_to_cny`` over the shared price store) so the single-currency NAV stays
honest. Everything is scoped to portfolio_id="china" so no other book is touched.

Run:  python -m bot.china        (or the APScheduler 'china_daily' job, or POST /api/china/run)
"""
from __future__ import annotations

import asyncio
import json
import os
from datetime import date, datetime, timezone
from pathlib import Path

import bot  # noqa: F401  -> vendor/macro onto sys.path

PORTFOLIO_ID = "china"
SLEEVE = "brain"
BENCHMARK = "FXI"
_ROOT = Path(__file__).resolve().parent.parent
_MAX_TURNS = int(os.environ.get("CHINA_MAX_TURNS", "30"))


# ---------------------------------------------------------------------------
# the daily entrypoint
# ---------------------------------------------------------------------------

def run_china(asof: str | None = None, *, force: bool = False, armed: bool = True) -> dict:
    """Run one China turn end-to-end. Best-effort: every step degrades gracefully so a missing
    credential / price never leaves the book in a half-traded state."""
    from portfolio import china_calendar, paper_account, position_log

    asof = asof or date.today().isoformat()
    out: dict = {"portfolio_id": PORTFOLIO_ID, "asof": asof,
                 "ran_at": datetime.now(timezone.utc).isoformat()}
    today = _safe_date(asof)
    out["trading_day"] = china_calendar.is_trading_day(today) if today else None

    state0 = paper_account._load_account(PORTFOLIO_ID)
    inaugural = not _has_history() and not (state0.get("positions") or {})
    out["inaugural"] = inaugural

    # 1. run the Brain (armed) → it researches and submits a target book with rationales
    from brain import china_mcp
    china_mcp.clear_submission()                 # never replay yesterday's decision
    brain: dict = {"ok": False, "skipped": not armed}
    if armed:
        try:
            brain = _run_brain(asof, inaugural)
        except Exception as e:                   # noqa: BLE001
            brain = {"ok": False, "error": repr(e)[:300]}
    out["brain"] = {k: brain.get(k) for k in ("ok", "cost_usd", "tools_used", "error", "run_id", "model")}

    # 2. read the submitted book
    submission = china_mcp.read_submission()
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
    for t in set(target) | set(held) | {BENCHMARK}:
        cny = fx.usd_to_cny(paper_account._current_price(t))
        if cny and cny > 0:
            prices[t] = cny

    # 4. EXECUTE — rebalance the paper book to the target at close prices (USD). Free trades: no
    #    gate, no veto, no caps. Names we cannot price are skipped (and surfaced honestly).
    executed: list[dict] = []
    skipped: list[str] = []
    if decided:
        priceable = {t: w for t, w in target.items() if t in prices}
        skipped = sorted(t for t in target if t not in prices)
        before = dict((paper_account._load_account(PORTFOLIO_ID).get("positions") or {}))
        try:
            # Pass the FULL target (not just the priceable subset) so rebalance can distinguish a
            # name the Brain DROPPED (→ sell) from one merely unpriceable THIS run (→ carry, don't
            # liquidate). rebalance only trades names it can price and only closes out names absent
            # from the target; handing it `priceable` would make an unpriceable held name look
            # "dropped" and wrongly sell it on a transient feed gap — critical for the Asia leg.
            paper_account.rebalance(target, prices, asof, portfolio_id=PORTFOLIO_ID)
        except Exception as e:                   # noqa: BLE001
            out["rebalance_error"] = repr(e)[:200]
        after = dict((paper_account._load_account(PORTFOLIO_ID).get("positions") or {}))
        executed = _diff_trades(before, after, prices)
        ledger_positions = [{"ticker": t, "sleeve": SLEEVE, "weight": w,
                             "entry_price": prices.get(t)}
                            for t, w in priceable.items()]
        try:
            position_log.update(ledger_positions, asof, portfolio_id=PORTFOLIO_ID)
        except Exception:
            pass
    out["executed"] = executed
    out["skipped_unpriceable"] = skipped

    # 5. mark NAV vs FXI (benchmark auto-resolved per-book from the registry)
    try:
        paper_account.mark(prices, asof, portfolio_id=PORTFOLIO_ID)
    except Exception as e:                       # noqa: BLE001
        out["mark_error"] = repr(e)[:200]

    # 6. publish the book contract + 7. append the daily decision log
    payload = _build_payload(asof, submission, prices, executed, skipped, brain)
    try:
        from bridge import build_portfolio
        out["paths"] = build_portfolio.write(payload, portfolio_id=PORTFOLIO_ID)
    except Exception as e:                       # noqa: BLE001
        out["write_error"] = repr(e)[:200]
    try:
        _append_decision_log(asof, submission, executed, skipped, brain)
    except Exception:
        pass

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
    "You are the CHINA PORTFOLIO MANAGER of a real-money-style ¥1,000,000 PAPER book, marked in "
    "CNY (renminbi). You run once per Asia trading day, after the mainland A-share close. You have "
    "FULL discretion: you decide every buy, sell, trim, and the cash level, and you rebalance the "
    "whole book daily. There is NO gate, NO committee, NO research-paper requirement, and NO "
    "doctrine constraining you — only paper cash (you cannot use leverage). \n\n"
    "Your universe is ALL of Greater China across three venues, and you may hold any mix: mainland "
    "A-shares (tickers like 600519.SS / 300750.SZ, quoted in CNY), Hong Kong (0700.HK, HKD), and "
    "US-listed China ADRs (BABA, PDD, JD, quoted in USD). A-shares are already in your base "
    "currency; the desk converts HK (HKD) and ADR (USD) prices to CNY at the prevailing rate "
    "automatically, so size every weight as a fraction of the one CNY NAV. \n\n"
    "You have two research channels and may use EITHER or BOTH: (1) the in-house macro China desks "
    "via mcp__china__* tools — get_china_regime (top-down quad + PBoC liquidity), get_china_intake "
    "(the unified, corroborated candidate funnel across the A-share buy board, alpha leaders, "
    "reversal watch, and the HK board), get_china_standouts, get_china_brief — and (2) the open web "
    "via WebSearch / WebFetch. Form your own view; you are not obliged to agree with the in-house "
    "engine. \n\n"
    "ALWAYS confirm a name is priceable with mcp__china__get_quote before you rely on it — it "
    "returns the venue, the local-currency price, and the CNY price the book will actually transact "
    "at; a name with priceable=false will be SKIPPED. When you are done researching, call "
    "mcp__china__submit_book ONCE with your COMPLETE target book for today: every name you want to "
    "hold, its weight (fraction of NAV), and a clear one-paragraph rationale for EACH holding. "
    "Anything you currently hold but omit will be SOLD. Be decisive and concrete; this book is "
    "graded on its realized CNY NAV vs FXI (iShares China Large-Cap, marked in CNY)."
)


def _run_brain(asof: str, inaugural: bool) -> dict:
    from brain import china_mcp, cli_bridge
    prompt = _build_prompt(asof, inaugural)
    coro = cli_bridge.reason(
        prompt,
        role="deep",                 # opus, per config/agents.yml
        arm=True,
        append_system=_PERSONA,
        mcp_servers=china_mcp.build_servers(),
        allowed_tools=china_mcp.allowed_tools(),
        max_turns=_MAX_TURNS,
    )
    return _run_coro(coro)


def _build_prompt(asof: str, inaugural: bool) -> str:
    from portfolio import paper_account
    state = paper_account._load_account(PORTFOLIO_ID)
    cash = float(state.get("cash") or 0.0)
    positions = state.get("positions") or {}
    regime = _regime_brief()

    lines = [f"# China book — daily decision for {asof}", ""]
    if regime:
        lines += [f"China macro regime (in-house read): {regime}", ""]
    if inaugural:
        lines += [
            "This is your INAUGURAL run. The book is 100% cash: ¥1,000,000 (CNY). Build the "
            "all-China portfolio from scratch — buy whatever you are convinced of across A-shares, "
            "Hong Kong, and China ADRs, sized however you see fit (keep some cash if you want).",
            "",
        ]
    else:
        lines += [f"Your current book: ¥{cash:,.0f} cash across {len(positions)} holdings "
                  f"({', '.join(sorted(positions)) or 'none'}). Call mcp__china__get_my_book for the "
                  "full picture (weights, live CNY P&L, and the rationale you last gave each name).", ""]
    lines += [
        "Do your research now (the in-house China desks and/or the web — your call), then submit "
        "your complete target book for today via mcp__china__submit_book, with a one-paragraph "
        "rationale per holding. Confirm each name is priceable with get_quote first. Rebalance with "
        "conviction; you are accountable for the USD NAV vs FXI.",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# publish + log helpers
# ---------------------------------------------------------------------------

def _build_payload(asof: str, submission: dict | None, prices: dict, executed: list,
                   skipped: list, brain: dict) -> dict:
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
        "manager": "China Opus Brain",
        "kind": "china_brain",
        "currency": "CNY",
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
        "market_status": china_calendar.status(),
        "brain": {k: brain.get(k) for k in ("cost_usd", "tools_used", "model")},
    }


def _append_decision_log(asof: str, submission: dict | None, executed: list,
                         skipped: list, brain: dict) -> None:
    from portfolio import registry
    p = registry.data_dir(PORTFOLIO_ID) / "decisions.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "asof": asof,
        "ts": datetime.now(timezone.utc).isoformat(),
        "summary": (submission or {}).get("summary"),
        "sold_note": (submission or {}).get("sold_note"),
        "holdings": [{"ticker": h.get("ticker"), "venue": h.get("venue"), "weight": h.get("weight"),
                      "conviction": h.get("conviction"), "rationale": h.get("rationale")}
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
    raw = _read_china_regime()
    return {"quad": raw.get("quad"), "quad_name": raw.get("quad_name"),
            "liquidity_overlay": raw.get("liquidity_overlay")}


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
    o = run_china(armed=_armed)
    print(f"=== china {o['asof']} (inaugural={o['inaugural']}, trading_day={o['trading_day']}) ===")
    print("brain:", "ok" if o["brain"].get("ok") else o["brain"].get("error", "skipped"),
          "| decided:", o.get("decided"), "| holdings:", o.get("holdings"))
    print("executed:", len(o.get("executed") or []), "trades | skipped:", o.get("skipped_unpriceable"))
    print("nav:", o.get("nav"), "| paths:", (o.get("paths") or {}).get("hub"))
