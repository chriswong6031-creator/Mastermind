"""Firm-level cross-book exposure MONITOR — read-only concentration radar.

Mastermind runs several independent paper books (flagship, autonomous/US Brain, heavyweight,
china/CN Brain, hk/HK Brain, etf/ETF Brain). Each is sized in isolation by its own manager, so
NOTHING in the system sees the FIRM-WIDE picture: when three different Brains independently pile
into the same name — or the same sector — the firm is concentrated even though no single book
breached its own mandate. This module computes that cross-book view and FLAGS the pile-ups.

It is deliberately TOOTHLESS — a MONITOR, alert, and dashboard surface only. It NEVER changes an
allocation, queues an order, or touches a paper account; a hard "firm allocator" that overrides a
book's sizing is explicitly OUT OF SCOPE for v1 (too risky per the failure register). This is
ADDITIVE: it can only raise a flag, never a trade.

    from portfolio import firm_exposure
    firm_exposure.summary()      # the full read-only firm-exposure dict

Honest about currency. NAVs are per-book base currency (USD for the US books, CNY for china, HKD
for hk). Summing raw weights × NAV across books would silently add CNY to USD, so we aggregate
two HONEST, clearly-labelled ways:
  * ``by_weight``  — weight-share across books, currency-free (a name's firm weight = its
                     NAV-weighted mean book weight, where the NAV weights are converted to a common
                     USD basis ONLY when that conversion is trivially available via portfolio.fx;
                     otherwise it falls back to an equal-book mean and says so in ``note``).
  * ``firm_usd``   — total USD-equivalent dollars, populated per ticker ONLY when every holding
                     book's NAV could be expressed in USD; left None (and flagged in ``note``)
                     when a book's currency couldn't be converted, so a number is never a lie.

Pure / deterministic / NEVER raises — every read degrades to an honest stub on missing data.
Thresholds are env-configurable with sane defaults.
"""
from __future__ import annotations

import json
import os
from datetime import date
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# thresholds (env-configurable, sane defaults)
# ---------------------------------------------------------------------------
# A name is FLAGGED when it is held by at least FIRM_MIN_BOOKS books OR its firm-wide weight
# clears FIRM_NAME_MAX. The sector aggregate is flagged at FIRM_SECTOR_MAX. TOP_K bounds how many
# of the biggest concentrations we surface. All overridable via env for tuning; never raises on a
# bad value (falls back to the default).

def _env_int(name: str, default: int) -> int:
    try:
        v = int(float(os.environ.get(name, default)))
        return v if v > 0 else default
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        v = float(os.environ.get(name, default))
        return v if v > 0 else default
    except (TypeError, ValueError):
        return default


def _thresholds() -> dict[str, Any]:
    return {
        # number of distinct books holding a name before it's a pile-up (default 3)
        "min_books": _env_int("FIRM_MIN_BOOKS", 3),
        # firm-wide weight (fraction, 0..1) above which a single name is flagged (default 8%)
        "name_max": _env_float("FIRM_NAME_MAX", 0.08),
        # firm-wide sector weight (fraction) above which a sector is flagged (default 25%)
        "sector_max": _env_float("FIRM_SECTOR_MAX", 0.25),
        # how many of the largest firm-wide concentrations to surface
        "top_k": _env_int("FIRM_TOP_K", 12),
    }


# ---------------------------------------------------------------------------
# per-book holding load (read-only — latest.json published book, never the account writer)
# ---------------------------------------------------------------------------

def _book_ids() -> list[dict]:
    """The paper books to scan (registry-driven; self_directed is user-run and excluded — it has
    no published latest.json book and isn't a Brain pile-up). Degrades to a static list."""
    try:
        from portfolio import registry
        return [m for m in registry.all_portfolios() if m.get("id") != "self_directed"]
    except Exception:  # noqa: BLE001
        return [{"id": i, "currency": "USD"} for i in
                ("flagship", "heavyweight", "autonomous", "etf", "china", "hk")]


def _data_dir(pid: str) -> Path:
    try:
        from portfolio import registry
        return registry.data_dir(pid)
    except Exception:  # noqa: BLE001
        return _ROOT / "data" / "portfolios" / pid


def _book_currency(meta: dict) -> str:
    cur = meta.get("currency")
    if cur:
        return str(cur)
    try:
        from portfolio import registry
        return registry.currency(meta.get("id"))
    except Exception:  # noqa: BLE001
        return "USD"


def _load_book(meta: dict) -> dict | None:
    """Read one book's published latest.json → {id, currency, nav, currency, holdings:{ticker:weight}}.

    Weight is taken from the published ``positions[].weight`` when present; otherwise derived from
    ``market_value / nav`` so a book that omits weights still aggregates honestly. Returns None when
    the book has no state (skipped). Never raises."""
    pid = meta.get("id")
    if not pid:
        return None
    try:
        path = _data_dir(pid) / "latest.json"
        if not path.exists():
            return None
        doc = json.loads(path.read_text())
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(doc, dict):
        return None
    positions = doc.get("positions")
    if not isinstance(positions, list) or not positions:
        return None

    nav = doc.get("nav")
    try:
        nav = float(nav) if nav is not None else None
    except (TypeError, ValueError):
        nav = None

    holdings: dict[str, float] = {}
    for pos in positions:
        if not isinstance(pos, dict):
            continue
        tk = str(pos.get("ticker") or "").upper().strip()
        if not tk:
            continue
        w = pos.get("weight")
        try:
            w = float(w) if w is not None else None
        except (TypeError, ValueError):
            w = None
        # derive weight from market_value / nav when the book didn't publish an explicit weight
        if (w is None or w <= 0) and nav and nav > 0:
            mv = pos.get("market_value")
            try:
                mv = float(mv) if mv is not None else None
            except (TypeError, ValueError):
                mv = None
            if mv is not None:
                w = mv / nav
        if w is None or w <= 0:
            continue
        # a ticker can in principle appear twice (defensive) — sum
        holdings[tk] = holdings.get(tk, 0.0) + w

    if not holdings:
        return None
    return {
        "id": pid,
        "name": meta.get("name") or pid,
        "currency": _book_currency(meta),
        "nav": nav,
        "holdings": holdings,
        "n_holdings": len(holdings),
    }


# ---------------------------------------------------------------------------
# USD-equivalence (best-effort; honest about what couldn't convert)
# ---------------------------------------------------------------------------

def _nav_usd(book: dict) -> float | None:
    """A book's NAV expressed in USD when trivially available, else None.

    USD books pass through; CNY/HKD books convert their base NAV to USD via portfolio.fx (divide by
    the foreign-units-per-USD rate). Returns None when NAV is missing or the rate is unavailable, so
    a cross-currency sum can flag itself as incomplete rather than silently mixing currencies."""
    nav = book.get("nav")
    if nav is None or nav <= 0:
        return None
    cur = (book.get("currency") or "USD").upper()
    if cur == "USD":
        return float(nav)
    try:
        from portfolio import fx
        rate = fx.rate_per_usd(cur)          # foreign units per 1 USD
        if rate and rate > 0:
            return float(nav) / rate
    except Exception:  # noqa: BLE001
        return None
    return None


# ---------------------------------------------------------------------------
# sector lookup (best-effort from the vendored macro stockdata; omitted when absent)
# ---------------------------------------------------------------------------

def _sector_of(ticker: str) -> str | None:
    """Best-effort sector for a ticker from the vendored macro stockdata snapshot. None when the
    snapshot isn't present (common in a lean checkout) — the caller then omits the sector rollup."""
    t = (ticker or "").upper().strip()
    if not t:
        return None
    try:
        p = _ROOT / "vendor" / "macro" / "site" / "stockdata" / f"{t}.json"
        if not p.exists():
            return None
        sd = json.loads(p.read_text())
        sec = ((sd.get("factors") or {}).get("sector")) or None
        return str(sec) if sec else None
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# the public summary
# ---------------------------------------------------------------------------

def _empty(as_of: str, note: str) -> dict:
    return {
        "as_of": as_of,
        "books": [],
        "n_books": 0,
        "top_exposures": [],
        "flags": [],
        "by_sector": {},
        "by_chain": {},
        "thresholds": _thresholds(),
        "currency_clean": False,
        "note": note,
    }


def summary(asof: str | None = None) -> dict:
    """Firm-wide cross-book exposure — read-only concentration radar. NEVER raises.

    Returns::

        {
          as_of,
          books:          [{id, name, currency, nav, nav_usd, n_holdings}],
          n_books,
          top_exposures:  [{ticker, n_books, books_holding, firm_weight,
                            firm_weight_pct, firm_usd, sector, flagged}],
          flags:          [{kind:'name'|'sector', ticker|sector, n_books?, books_holding?,
                            firm_weight, reason}],
          by_sector:      {sector: {firm_weight, firm_usd, n_books, tickers}},
          thresholds:     {min_books, name_max, sector_max, top_k},
          currency_clean: bool,    # True iff every holding book's NAV converted to USD cleanly
          note:           str,     # honest description of the aggregation method actually used
        }

    Aggregation (honest about currency):
      * ``firm_weight`` — a name's NAV-weighted mean weight across the books holding it. The NAV
        weights use each book's USD-equivalent NAV when EVERY scanned book converts cleanly
        (``currency_clean`` True); otherwise they fall back to an EQUAL-book mean (each holding book
        counts the same) and ``note`` says so. Currency-free either way: a fraction, not dollars.
      * ``firm_usd``    — the USD-equivalent dollars in a name (Σ weight_in_book × book_nav_usd),
        populated ONLY when every book holding that name has a USD-convertible NAV; None otherwise.
    """
    try:
        as_of = (asof or date.today().isoformat())[:10]
    except Exception:  # noqa: BLE001
        as_of = ""
    th = _thresholds()

    # ---- load every book's holdings (skip empties) ----
    try:
        metas = _book_ids()
    except Exception:  # noqa: BLE001
        metas = []
    books: list[dict] = []
    for meta in metas:
        try:
            b = _load_book(meta)
        except Exception:  # noqa: BLE001
            b = None
        if b:
            b["nav_usd"] = _nav_usd(b)
            books.append(b)

    if not books:
        return _empty(as_of, "No published books to aggregate — run the book builds to populate.")

    # currency cleanliness: can EVERY scanned book express its NAV in USD?
    currency_clean = all(b.get("nav_usd") is not None for b in books)

    # ---- per-ticker firm aggregation ----
    # raw[ticker] = list of (book_id, weight_in_book, book_nav_usd_or_None)
    raw: dict[str, list[tuple[str, float, float | None]]] = {}
    for b in books:
        for tk, w in b["holdings"].items():
            raw.setdefault(tk, []).append((b["id"], w, b.get("nav_usd")))

    exposures: list[dict] = []
    for tk, entries in raw.items():
        books_holding = sorted({e[0] for e in entries})
        n_books = len(books_holding)

        # firm weight: NAV-(USD)-weighted mean book weight when clean, else equal-book mean.
        usable = [(w, nav) for (_bid, w, nav) in entries]
        if currency_clean and all(nav and nav > 0 for (_w, nav) in usable):
            denom = sum(nav for (_w, nav) in usable)
            firm_weight = (sum(w * nav for (w, nav) in usable) / denom) if denom > 0 else 0.0
        else:
            firm_weight = sum(w for (w, _nav) in usable) / max(1, len(usable))

        # firm USD: only when every holding book's NAV converts cleanly
        firm_usd: float | None = None
        if all(nav and nav > 0 for (_w, nav) in usable):
            firm_usd = round(sum(w * nav for (w, nav) in usable), 2)

        exposures.append({
            "ticker": tk,
            "n_books": n_books,
            "books_holding": books_holding,
            "firm_weight": round(firm_weight, 6),
            "firm_weight_pct": round(firm_weight * 100, 2),
            "firm_usd": firm_usd,
            "sector": _sector_of(tk),
            "flagged": False,
        })

    # ---- flags: many-book pile-ups OR over-weight single names ----
    flags: list[dict] = []
    for e in exposures:
        reasons = []
        if e["n_books"] >= th["min_books"]:
            reasons.append(f">= {th['min_books']} books hold it")
        if e["firm_weight"] >= th["name_max"]:
            reasons.append(f"firm weight {e['firm_weight_pct']:.1f}% >= {th['name_max'] * 100:.1f}%")
        if reasons:
            e["flagged"] = True
            flags.append({
                "kind": "name",
                "ticker": e["ticker"],
                "n_books": e["n_books"],
                "books_holding": e["books_holding"],
                "firm_weight": e["firm_weight"],
                "firm_weight_pct": e["firm_weight_pct"],
                "firm_usd": e["firm_usd"],
                "reason": " · ".join(reasons),
            })

    # ---- top-K concentrations (most-books first, then biggest firm weight) ----
    exposures.sort(key=lambda x: (x["n_books"], x["firm_weight"]), reverse=True)
    top_exposures = exposures[: th["top_k"]]

    # ---- per-sector rollup (best-effort; omitted when no sector resolved at all) ----
    by_sector: dict[str, dict] = {}
    any_sector = False
    for e in exposures:
        sec = e["sector"]
        if not sec:
            continue
        any_sector = True
        agg = by_sector.setdefault(sec, {"firm_weight": 0.0, "firm_usd": 0.0,
                                         "tickers": [], "books": set(), "_usd_clean": True})
        agg["firm_weight"] += e["firm_weight"]
        agg["tickers"].append(e["ticker"])
        agg["books"].update(e["books_holding"])
        if e["firm_usd"] is None:
            agg["_usd_clean"] = False
        else:
            agg["firm_usd"] += e["firm_usd"]
    # finalize the sector aggregates + flag the over-weight ones
    by_sector_out: dict[str, dict] = {}
    for sec, agg in by_sector.items():
        fw = round(agg["firm_weight"], 6)
        flagged = fw >= th["sector_max"]
        by_sector_out[sec] = {
            "firm_weight": fw,
            "firm_weight_pct": round(fw * 100, 2),
            "firm_usd": round(agg["firm_usd"], 2) if agg["_usd_clean"] else None,
            "n_books": len(agg["books"]),
            "tickers": sorted(agg["tickers"]),
            "flagged": flagged,
        }
        if flagged:
            flags.append({
                "kind": "sector",
                "sector": sec,
                "firm_weight": fw,
                "firm_weight_pct": round(fw * 100, 2),
                "n_books": len(agg["books"]),
                "reason": f"firm sector weight {fw * 100:.1f}% >= {th['sector_max'] * 100:.1f}%",
            })

    # ---- per-fragility-chain rollup (additive, read-only) — the FIRM-wide view of how exposed every
    # book together is to each leading-edge fragile theme-chain (memory→capex→buildout→power, …). A
    # ticker in two chains counts in both (honest: the firm carries both). Degrades to {} when the
    # chain map is unavailable. NEVER changes an allocation. ----
    by_chain: dict[str, dict] = {}
    try:
        from portfolio import fragility_chain
        chain_agg: dict[str, dict] = {}
        for e in exposures:
            for c in fragility_chain.classify(e["ticker"]):
                cid = c["chain"]
                agg = chain_agg.setdefault(cid, {"name": c["name"], "driver": c["driver"],
                                                 "firm_weight": 0.0, "tickers": [],
                                                 "leading_tickers": [], "books": set()})
                agg["firm_weight"] += e["firm_weight"]
                agg["tickers"].append(e["ticker"])
                if c["position"] == "leading_edge":
                    agg["leading_tickers"].append(e["ticker"])
                agg["books"].update(e["books_holding"])
        for cid, agg in chain_agg.items():
            fw = round(agg["firm_weight"], 6)
            by_chain[cid] = {
                "name": agg["name"], "driver": agg["driver"],
                "firm_weight": fw, "firm_weight_pct": round(fw * 100, 2),
                "tickers": sorted(agg["tickers"]),
                "leading_tickers": sorted(agg["leading_tickers"]),
                "n_books": len(agg["books"]),
                "flagged": fw >= th["sector_max"],
            }
        by_chain = dict(sorted(by_chain.items(), key=lambda kv: kv[1]["firm_weight"], reverse=True))
    except Exception:  # noqa: BLE001 — the chain rollup is additive; never break the monitor
        by_chain = {}

    # ---- honest note about the aggregation actually used ----
    if currency_clean:
        method = ("Firm weight = USD-NAV-weighted mean book weight; firm_usd is the USD-equivalent "
                  "dollar exposure (all book NAVs converted to USD via portfolio.fx).")
    else:
        method = ("Cross-currency NAVs could not all be converted to USD, so firm weight = "
                  "EQUAL-book mean weight (each holding book counts the same) and firm_usd is "
                  "populated only for names held entirely by USD-convertible books.")
    sector_note = "" if any_sector else " Sector rollup omitted — no sector data available (stockdata snapshot absent)."
    note = (f"Read-only firm-exposure monitor across {len(books)} book(s). {method}"
            f"{sector_note} This NEVER changes any allocation.")

    return {
        "as_of": as_of,
        "books": [{"id": b["id"], "name": b["name"], "currency": b["currency"],
                   "nav": round(b["nav"], 2) if b.get("nav") else None,
                   "nav_usd": round(b["nav_usd"], 2) if b.get("nav_usd") else None,
                   "n_holdings": b["n_holdings"]} for b in books],
        "n_books": len(books),
        "top_exposures": top_exposures,
        "flags": flags,
        "by_sector": by_sector_out,
        "by_chain": by_chain,
        "thresholds": th,
        "currency_clean": currency_clean,
        "note": note,
    }
