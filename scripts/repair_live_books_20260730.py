#!/usr/bin/env python3
"""Repair the 2026-07-30 Flagship/China publishing incidents.

This is an intentionally narrow, invariant-checked operational recovery:

* Flagship: undo exactly five false peer-sentinel liquidation fills, restore the
  pre-incident lots, preserve the legitimate July-30 cash-sweep accrual, mark the
  restored holdings live, and republish the actual account.
* China: mark the untouched account through the restored Yahoo A-share route and
  replace the feed-aborted empty contract with a transparent carry-forward record.

The script defaults to a read-only dry run. ``--apply`` first creates timestamped
copies of every state file it may replace. It never calls an LLM or creates trades.
"""
from __future__ import annotations

import argparse
import json
import math
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import bot  # noqa: F401  # make the vendored Macro package importable

from bridge import build_portfolio
from portfolio import fx, paper_account, registry


ASOF = "2026-07-30"
FLAGSHIP_NAMES = {"SMH", "XLK", "MTUM", "XLV", "LNG"}
INVALID_SELL_SHARES = {
    "SMH": 88.619549,
    "XLK": 332.0,
    "MTUM": 188.360324,
    "XLV": 311.0,
    "LNG": 225.0,
}
STATE_FILES = (
    "account.json",
    "fills.jsonl",
    "nav_history.jsonl",
    "latest.json",
    "portfolio.json",
    "pending_orders.json",
    "pending_target.json",
    "decisions.jsonl",
)


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError(f"{path} is not a JSON object")
    return value


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise RuntimeError(f"{path} contains a non-object row")
        rows.append(value)
    return rows


def _write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False, default=str) + "\n",
                    encoding="utf-8")


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    text = "".join(json.dumps(row, ensure_ascii=False, default=str) + "\n" for row in rows)
    path.write_text(text, encoding="utf-8")


def _backup(base: Path, label: str) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest = base / f"_incident_backup_{label}_{stamp}"
    dest.mkdir(parents=True, exist_ok=False)
    for name in STATE_FILES:
        src = base / name
        if src.exists():
            shutil.copy2(src, dest / name)
    return dest


def _live_prices(tickers: set[str]) -> dict[str, float]:
    from data_layer import yahoo_feed

    yahoo_feed.warm(sorted(tickers))
    prices: dict[str, float] = {}
    for ticker in sorted(tickers):
        value = paper_account._current_price(ticker)
        if value is not None and float(value) > 0:
            prices[ticker] = float(value)
    return prices


def _invalid_flagship_sells(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    matched: list[dict[str, Any]] = []
    for row in rows:
        ticker = str(row.get("ticker") or "").upper()
        if (
            row.get("date") == ASOF
            and row.get("side") == "sell"
            and row.get("from_pending") is True
            and ticker in INVALID_SELL_SHARES
            and math.isclose(float(row.get("shares") or 0.0),
                             INVALID_SELL_SHARES[ticker], abs_tol=1e-6)
        ):
            matched.append(row)
    got = {str(row.get("ticker") or "").upper() for row in matched}
    if got != FLAGSHIP_NAMES or len(matched) != len(FLAGSHIP_NAMES):
        raise RuntimeError(
            "Flagship fill invariant failed: expected exactly the five false "
            f"2026-07-30 sells, found {sorted(got)} ({len(matched)} rows)"
        )
    return matched


def _restore_flagship(source_path: Path, metadata_path: Path, *, apply: bool) -> dict[str, Any]:
    base = registry.data_dir("flagship")
    current = _read_json(base / "account.json")
    source = _read_json(source_path)
    published = _read_json(base / "latest.json")
    metadata = _read_json(metadata_path)
    fills = _read_jsonl(base / "fills.jsonl")
    invalid = _invalid_flagship_sells(fills)

    current_material = {
        ticker for ticker, lot in (current.get("positions") or {}).items()
        if abs(float(lot.get("shares") or 0.0) * float(lot.get("avg_cost") or 0.0)) >= 1.0
    }
    source_material = {
        ticker for ticker, lot in (source.get("positions") or {}).items()
        if abs(float(lot.get("shares") or 0.0) * float(lot.get("avg_cost") or 0.0)) >= 1.0
    }
    if current_material:
        raise RuntimeError(f"Flagship account is no longer the incident state: {current_material}")
    if source_material != FLAGSHIP_NAMES:
        raise RuntimeError(f"Flagship source lots mismatch: {sorted(source_material)}")

    proceeds = round(sum(float(row.get("value") or 0.0) for row in invalid), 2)
    recovered_cash = round(float(current["cash"]) - proceeds, 2)
    source_cash = float(source["cash"])
    if recovered_cash < source_cash or recovered_cash - source_cash > 500.0:
        raise RuntimeError(
            f"Recovered cash {recovered_cash:.2f} is inconsistent with source {source_cash:.2f}"
        )

    restored = dict(current)
    restored["cash"] = recovered_cash
    restored["positions"] = source["positions"]
    kept_fills = [row for row in fills if row not in invalid]

    prices = _live_prices(FLAGSHIP_NAMES | {"SPY"})
    missing = FLAGSHIP_NAMES - prices.keys()
    if missing:
        raise RuntimeError(f"Cannot repair Flagship without live marks: {sorted(missing)}")
    nav = recovered_cash + sum(
        float(lot["shares"]) * prices.get(ticker, float(lot["avg_cost"]))
        for ticker, lot in restored["positions"].items()
    )

    metadata_by_ticker = {
        str(row.get("ticker") or "").upper(): row
        for row in (metadata.get("positions") or [])
    }
    missing_metadata = FLAGSHIP_NAMES - metadata_by_ticker.keys()
    if missing_metadata:
        raise RuntimeError(f"Flagship metadata is missing {sorted(missing_metadata)}")

    positions: list[dict[str, Any]] = []
    sleeve_weights = {"leadership": 0.0, "conviction": 0.0}
    for ticker in sorted(FLAGSHIP_NAMES):
        row = dict(metadata_by_ticker[ticker])
        lot = restored["positions"][ticker]
        market_value = float(lot["shares"]) * prices[ticker]
        weight = market_value / nav
        row.pop("pending", None)
        row.pop("status", None)
        row.pop("est_price", None)
        row.pop("fill_after", None)
        row.update({
            "weight": round(weight, 4),
            "verdict": "hold",
            "cost_basis": round(float(lot["avg_cost"]), 4),
            "current_price": round(prices[ticker], 4),
            "market_value": round(market_value, 2),
            "unrealized_pnl": round(
                (prices[ticker] - float(lot["avg_cost"])) * float(lot["shares"]), 2),
            "unrealized_pct": round(
                (prices[ticker] / float(lot["avg_cost"]) - 1.0) * 100.0, 2),
        })
        sleeve = str(row.get("sleeve") or "conviction")
        sleeve_weights[sleeve] = sleeve_weights.get(sleeve, 0.0) + weight
        positions.append(row)
    positions.sort(key=lambda row: float(row.get("weight") or 0.0), reverse=True)

    gross = sum(sleeve_weights.values())
    corrected = dict(published)
    corrected.update({
        "as_of": ASOF,
        "gross": round(gross, 4),
        "cash": round(recovered_cash / nav, 4),
        "sleeves": {
            "leadership": round(sleeve_weights.get("leadership", 0.0), 4),
            "conviction": round(sleeve_weights.get("conviction", 0.0), 4),
            "cash": round(recovered_cash / nav, 4),
        },
        "positions": positions,
        "market_status": "closed",
        "pending_orders": [],
        "operational_correction": {
            "as_of": ASOF,
            "reason": (
                "Restored the real account after an empty peer-sentinel snapshot "
                "incorrectly queued a full liquidation. No investment decision changed."
            ),
            "reversed_fill_count": len(invalid),
        },
    })

    backup = None
    if apply:
        backup = _backup(base, "peer_sentinel_liquidation")
        _write_json(base / "account.json", restored)
        _write_jsonl(base / "fills.jsonl", kept_fills)
        paper_account.mark(prices, ASOF, portfolio_id="flagship")
        build_portfolio.write(corrected, portfolio_id="flagship")
        _write_json(backup / "INCIDENT.json", {
            "incident": "empty peer snapshot caused false full liquidation",
            "reversed_fills": invalid,
            "restored_from": str(source_path),
            "metadata_from": str(metadata_path),
            "recovered_cash": recovered_cash,
            "live_prices": prices,
        })

    return {
        "apply": apply,
        "backup": str(backup) if backup else None,
        "reversed_fills": len(invalid),
        "reversed_proceeds": proceeds,
        "cash": recovered_cash,
        "nav": round(nav, 2),
        "positions": {ticker: round(prices[ticker], 4) for ticker in sorted(FLAGSHIP_NAMES)},
    }


def _latest_rationales() -> tuple[dict[str, dict[str, Any]], dict[str, Any] | None]:
    from bot import china

    by_ticker: dict[str, dict[str, Any]] = {}
    latest_good: dict[str, Any] | None = None
    for decision in china.load_decisions(500):
        holdings = decision.get("holdings") or []
        if holdings and latest_good is None:
            latest_good = decision
        for holding in holdings:
            ticker = str(holding.get("ticker") or "").upper()
            if ticker and ticker not in by_ticker:
                by_ticker[ticker] = holding
    return by_ticker, latest_good


def _repair_china(*, apply: bool) -> dict[str, Any]:
    from bot import china
    from data_layer import feed_health, yahoo_feed

    base = registry.data_dir("china")
    state = paper_account._load_account("china")
    held = set((state.get("positions") or {}).keys())
    if not held:
        raise RuntimeError("China account unexpectedly has no positions")
    if any(not ticker.upper().endswith((".SS", ".SZ")) for ticker in held):
        raise RuntimeError(f"China account contains an off-venue name: {sorted(held)}")

    yahoo_feed.warm(sorted(held))
    prices: dict[str, float] = {}
    for ticker in sorted(held | {registry.benchmark("china")}):
        usd = paper_account._current_price(ticker)
        local = fx.usd_to(usd, "CNY")
        if local is not None and local > 0:
            prices[ticker] = float(local)
    missing = held - prices.keys()
    if missing:
        raise RuntimeError(f"Cannot repair China without live marks: {sorted(missing)}")

    health = feed_health.status("A-share", ASOF)
    if health.get("status") != "up":
        raise RuntimeError(f"A-share feed is not healthy after fallback deployment: {health}")

    rationale_by_ticker, prior = _latest_rationales()
    holdings: list[dict[str, Any]] = []
    for ticker in sorted(held):
        old = rationale_by_ticker.get(ticker) or {}
        holdings.append({
            "ticker": ticker,
            "weight": None,
            "conviction": old.get("conviction"),
            "rationale": old.get("rationale"),
            "venue": "A-share",
        })
    submission = {
        "summary": (
            "CARRY UNCHANGED — the July 30 A-share decision was feed-gated because the "
            "VPS could not route to Tushare. No trades were executed. The existing account "
            "is now marked live through the restored Yahoo A-share fallback."
        ),
        "sold_note": "No July 30 buys or sells; this is an operational quote-route recovery.",
        "holdings": holdings,
    }
    payload = china._build_payload(
        ASOF, submission, prices, [], [], {"model": "ops-repair/no-ai"}, feed_health=health)

    backup = None
    if apply:
        backup = _backup(base, "ashare_quote_route")
        paper_account.mark(prices, ASOF, portfolio_id="china")
        # Rebuild after mark so the contract and account carry the exact same live prices.
        payload = china._build_payload(
            ASOF, submission, prices, [], [], {"model": "ops-repair/no-ai"}, feed_health=health)
        build_portfolio.write(payload, portfolio_id="china")
        china._append_decision_log(
            ASOF,
            submission,
            [],
            [],
            {"text": submission["summary"], "model": "ops-repair/no-ai"},
            feed_health=health,
        )
        _write_json(backup / "INCIDENT.json", {
            "incident": "Tushare route failure blanked live A-share marks",
            "prior_decision_asof": (prior or {}).get("asof"),
            "held": sorted(held),
            "live_prices_cny": prices,
            "feed_health": health,
        })

    return {
        "apply": apply,
        "backup": str(backup) if backup else None,
        "feed_health": health,
        "positions": {ticker: round(prices[ticker], 4) for ticker in sorted(held)},
        "nav": payload.get("nav"),
        "cash": payload.get("cash_usd"),
        "missing_rationales": sorted(
            ticker for ticker in held if not (rationale_by_ticker.get(ticker) or {}).get("rationale")
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="Back up and replace affected state")
    parser.add_argument("--flagship-source", type=Path, required=True)
    parser.add_argument("--flagship-metadata", type=Path, required=True)
    args = parser.parse_args()
    if not args.flagship_source.is_file():
        parser.error(f"missing Flagship source: {args.flagship_source}")
    if not args.flagship_metadata.is_file():
        parser.error(f"missing Flagship metadata: {args.flagship_metadata}")

    result = {
        "asof": ASOF,
        "mode": "apply" if args.apply else "dry-run",
        "flagship": _restore_flagship(
            args.flagship_source, args.flagship_metadata, apply=args.apply),
        "china": _repair_china(apply=args.apply),
    }
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
