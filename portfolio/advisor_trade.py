"""Advisor-initiated ad-hoc paper trades — the chat Brain conducting an add / cut.

A confirmed evaluation in the live chat can ADD a name to the paper book (or TRIM / EXIT
one). This executes a REAL paper fill (cash-funded, in account.json + fills.jsonl) plus a
single-key ledger open/close, so it surfaces in the Trades view and live P&L — but it
NEVER disturbs the rest of the book (single-name, unlike the daily rebalance). The engine
still owns the daily book in latest.json; this is the advisor's own sleeve. Paper-only,
reversible.
"""
from __future__ import annotations

from datetime import date

from portfolio import paper_account, position_log

SLEEVE = "advisor"
NAME_CAP = 0.08          # mirror the conviction sleeve's per-name cap
STARTER = 0.03           # default starter weight for an advisor add

_ADD = {"add", "buy", "increase", "open"}
_CUT = {"trim", "reduce"}
_EXIT = {"exit", "sell", "close"}


def execute(ticker: str, action: str, *, weight: float | None = None,
            price: float | None = None, asof: str | None = None,
            thesis: str | None = None) -> dict:
    """Conduct one advisor paper trade. Returns a result dict:
       {ok, ticker, action, side, shares, price, value, weight, cash_after, note}
       or {ok: False, error}. `action`: add/buy/increase | trim/reduce | exit/sell/close.
    """
    ticker = ticker.upper()
    action = (action or "").lower().strip()
    asof = asof or date.today().isoformat()

    if action in _ADD:
        side, w = "buy", min(float(weight) if weight else STARTER, NAME_CAP)
        res = paper_account.execute_fill(ticker, "buy", weight=w, price=price, asof=asof)
        ledger_event, ledger_weight = "open", w
    elif action in _CUT:
        side, w = "sell", weight
        # trim ~half the position by share count when no explicit size is given
        held = _held_shares(ticker)
        sh = float(weight) if weight else (held * 0.5 if held else None)
        res = paper_account.execute_fill(ticker, "sell", shares=sh, price=price, asof=asof)
        ledger_event, ledger_weight = "trim", None
    elif action in _EXIT:
        side, w = "sell", None
        res = paper_account.execute_fill(ticker, "sell", price=price, asof=asof)  # full exit
        ledger_event, ledger_weight = "close", 0
    else:
        return {"ok": False, "ticker": ticker, "error": f"unknown action '{action}'"}

    if not res.get("ok"):
        return {"ok": False, "ticker": ticker, "action": action, **res}

    try:
        position_log.record_manual(ticker, SLEEVE, event=ledger_event,
                                   weight=ledger_weight, price=res.get("price"), asof_iso=asof)
    except Exception:                                   # ledger is a view; never fail the trade on it
        pass

    verb = {"buy": "Bought", "sell": "Sold"}[side]
    note = (f"{verb} {res['shares']:.4g} {ticker} @ ${res['price']:.2f} "
            f"(${res['value']:,.0f}); cash now ${res['cash_after']:,.0f}. Paper.")
    return {"ok": True, "ticker": ticker, "action": action, "side": side,
            "shares": res["shares"], "price": res["price"], "value": res["value"],
            "weight": ledger_weight, "cash_after": res["cash_after"], "note": note}


def _held_shares(ticker: str) -> float:
    try:
        acct = paper_account._load_account()
        return float((acct.get("positions") or {}).get(ticker.upper(), {}).get("shares") or 0.0)
    except Exception:
        return 0.0
