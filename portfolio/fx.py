"""Currency conversion for the multi-currency China book — everything marked in USD.

The all-China book holds three venues that quote in three currencies:
  * mainland A-shares (``*.SS`` / ``*.SZ``) quote in **CNY**
  * Hong Kong (``*.HK``) quotes in **HKD**
  * US-listed China ADRs (no suffix) quote in **USD**

The paper account keeps ONE NAV, so every local price is converted to USD before it ever
touches cash/positions. This module owns that conversion: it resolves a ticker's currency
from its suffix and the live USD rate from (1) the vendored macro yahoo FX store, (2) the
macro forex snapshot, then (3) a static peg/recent fallback so marking never fails offline.

Rates are quoted the market way (USD-per-1-USD in the foreign unit, i.e. CNY-per-USD ≈ 7),
and ``to_usd`` divides by them. Pure-ish + degrade-never-raise; results are memoised per
process (FX moves slowly relative to a once-daily mark)."""
from __future__ import annotations

import json
import logging
from datetime import date
from pathlib import Path

log = logging.getLogger(__name__)

_ROOT = Path(__file__).resolve().parent.parent
_V = _ROOT / "vendor" / "macro"

# Static fallbacks — foreign units PER 1 USD. Used only when no live rate is available.
# CNH offshore yuan and the HKD peg (band 7.75–7.85) are both stable enough that a constant
# is accurate to ~1% for a paper mark; live values override.
_FALLBACK = {"CNY": 7.2, "HKD": 7.80, "USD": 1.0}

# Per-process memo, keyed by currency → (rate, day-it-was-fetched). DATE-keyed so a long-lived
# server (the china_daily cron runs in one persistent process) picks up the nightly FX refresh
# instead of freezing the first rate forever, while a single build still reads a stable rate.
_RATE_CACHE: dict[str, tuple[float, str]] = {}


def currency_of(ticker: str) -> str:
    """The quote currency implied by a ticker's venue suffix."""
    t = (ticker or "").upper().strip()
    if t.endswith(".SS") or t.endswith(".SZ"):
        return "CNY"
    if t.endswith(".HK"):
        return "HKD"
    return "USD"


def market_of(ticker: str) -> str:
    """'A' (mainland A-share), 'HK', or 'US' (incl. ADRs) — by suffix."""
    cur = currency_of(ticker)
    return {"CNY": "A", "HKD": "HK"}.get(cur, "US")


def _from_yahoo_store(symbol: str) -> float | None:
    try:
        from lib import store  # vendored macro lib
        df = store.read("yahoo", symbol)
        if df is not None and "close" in df.columns and len(df) > 0:
            v = float(df["close"].astype(float).dropna().iloc[-1])
            return v if v > 0 else None
    except Exception as e:  # noqa: BLE001
        log.debug("fx: yahoo %s failed (%s)", symbol, e)
    return None


def _from_forex_snapshot(key: str) -> float | None:
    """Read pairs[KEY].quote from the macro forex snapshot (e.g. 'USDCNH')."""
    for rel in ("data/forex/latest.json", "site/forexdata/latest.json"):
        p = _V / rel
        try:
            if p.exists():
                pairs = (json.loads(p.read_text()) or {}).get("pairs") or {}
                rec = pairs.get(key)
                if isinstance(rec, dict):
                    q = rec.get("quote")
                    if q and float(q) > 0:
                        return float(q)
        except Exception as e:  # noqa: BLE001
            log.debug("fx: forex snapshot %s/%s failed (%s)", rel, key, e)
    return None


def rate_per_usd(currency: str) -> float:
    """Foreign units per 1 USD for `currency` (CNY≈7.2, HKD≈7.8, USD=1.0). Degrade-safe."""
    cur = (currency or "USD").upper()
    if cur == "USD":
        return 1.0
    try:
        today = date.today().isoformat()
    except Exception:
        today = ""
    cached = _RATE_CACHE.get(cur)
    if cached is not None and cached[1] == today:
        return cached[0]
    val: float | None = None
    if cur == "CNY":
        # offshore yuan tracks onshore closely enough to mark A-shares
        val = _from_yahoo_store("CNH=X") or _from_yahoo_store("CNY=X") or _from_forex_snapshot("USDCNH")
    elif cur == "HKD":
        val = (_from_yahoo_store("HKD=X") or _from_forex_snapshot("USDHKD")
               or _from_forex_snapshot("HKDUSD"))
    if not val or val <= 0:
        val = _FALLBACK.get(cur, 1.0)
    _RATE_CACHE[cur] = (val, today)
    return val


def to_usd(price_local: float | None, ticker: str) -> float | None:
    """Convert a LOCAL-currency price for `ticker` into USD. None passes through."""
    if price_local is None:
        return None
    try:
        px = float(price_local)
    except (TypeError, ValueError):
        return None
    if px <= 0:
        return None
    cur = currency_of(ticker)
    if cur == "USD":
        return px
    rate = rate_per_usd(cur)
    return px / rate if rate and rate > 0 else None


def clear_cache() -> None:
    """Drop the per-process rate memo (tests / a fresh FX read)."""
    _RATE_CACHE.clear()
