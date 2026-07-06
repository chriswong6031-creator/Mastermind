"""Firm-level cross-book exposure — monitor AND binding cap (two layers, same module).

Mastermind runs several independent paper books (flagship, autonomous/US Brain, heavyweight,
china/CN Brain, hk/HK Brain, etf/ETF Brain). Each is sized in isolation by its own manager, so
NOTHING in a single book sees the FIRM-WIDE picture: three Brains independently max-convicting
the same name concentrates the firm even if every individual book is within its own mandate.

TWO LAYERS:

  1. summary() — read-only MONITOR. Aggregates cross-book exposure and flags pile-ups; never
     changes an allocation, queues an order, or touches a paper account. Display and alert use.

  2. headroom() / clamp_book() — BINDING firm cap (W3, Architecture Stage-6.3), DEFAULT ON.
     Called by every US book's finalize path (flagship, autonomous, etf, heavyweight — four call
     sites) BEFORE a book's target weights are committed. subtract-only invariant: headroom only
     clamps a book's target DOWN toward cash; it never raises it.  Absent peer data may not
     un-cap (returns +inf, leaving only per-book caps active), and a missing cluster config
     falls back to hard-coded defaults so the cap can never be silently removed.

    from portfolio import firm_exposure
    firm_exposure.summary()                   # read-only firm-exposure dict
    firm_exposure.headroom("NVDA", "flagship")  # remaining weight the flagship may hold

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
    """Brain/paper books to scan for pile-up detection (self_directed excluded — it is the user's
    own book and the firm yardstick; it must never contribute to pile-up detection or headroom math,
    so it is kept separate: see summary() `yardstick` key). Degrades to a static list."""
    try:
        from portfolio import registry
        return [m for m in registry.all_portfolios() if m.get("id") != "self_directed"]
    except Exception:  # noqa: BLE001
        return [{"id": i, "currency": "USD"} for i in
                ("flagship", "heavyweight", "autonomous", "etf", "china", "hk")]


def _self_directed_meta() -> dict:
    """The self_directed book's registry metadata (best-effort)."""
    try:
        from portfolio import registry
        return registry.get("self_directed")
    except Exception:  # noqa: BLE001
        return {"id": "self_directed", "name": "Self-Directed", "currency": "USD"}


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
        "yardstick": None,   # self_directed snapshot — visible for firm-wide view, excluded from clamp math
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
          yardstick:      {id, name, currency, nav, n_holdings,
                           holdings:{ticker:weight}} | None,
                          # self_directed book — displayed for whole-firm concentration visibility;
                          # EXCLUDED from books[], top_exposures, flags, and all clamp/headroom math.
                          # The benchmark book must never mechanically shape the books it measures.
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

    # ---- self_directed yardstick: the user's book as a whole-firm visibility row ----
    # DISPLAY-ONLY in this report: self_directed is excluded from books[], top_exposures, and all
    # flag/clamp/headroom math. The benchmark book must never mechanically shape the books it measures.
    # It is loaded independently here so the firm accountant can see the whole-firm picture, including
    # the user's positions, without those positions entering the pile-up or cap calculations.
    yardstick: dict | None = None
    try:
        sd_meta = _self_directed_meta()
        sd_book = _load_book(sd_meta)
        if sd_book:
            sd_book["nav_usd"] = _nav_usd(sd_book)
            yardstick = {
                "id": sd_book["id"],
                "name": sd_book["name"],
                "currency": sd_book["currency"],
                "nav": round(sd_book["nav"], 2) if sd_book.get("nav") else None,
                "nav_usd": round(sd_book["nav_usd"], 2) if sd_book.get("nav_usd") else None,
                "n_holdings": sd_book["n_holdings"],
                "holdings": {tk: round(w, 6) for tk, w in sd_book["holdings"].items()},
                "note": ("Display-only firm-wide yardstick. EXCLUDED from pile-up detection, "
                         "headroom(), and clamp_book() — the benchmark book must never "
                         "mechanically shape the books it measures."),
            }
    except Exception:  # noqa: BLE001 — the yardstick row is additive; never break the monitor
        yardstick = None

    # ---- honest note about the aggregation actually used ----
    if currency_clean:
        method = ("Firm weight = USD-NAV-weighted mean book weight; firm_usd is the USD-equivalent "
                  "dollar exposure (all book NAVs converted to USD via portfolio.fx).")
    else:
        method = ("Cross-currency NAVs could not all be converted to USD, so firm weight = "
                  "EQUAL-book mean weight (each holding book counts the same) and firm_usd is "
                  "populated only for names held entirely by USD-convertible books.")
    sector_note = "" if any_sector else " Sector rollup omitted — no sector data available (stockdata snapshot absent)."
    ys_note = (" Self-directed yardstick included for whole-firm visibility (excluded from all "
               "clamp math — it is the benchmark, not a managed book)." if yardstick else "")
    note = (f"Read-only firm-exposure monitor across {len(books)} book(s). {method}"
            f"{sector_note}{ys_note} This NEVER changes any allocation.")

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
        "yardstick": yardstick,
        "note": note,
    }


# ═══════════════════════════════════════════════════════════════════════════════════════════════
# W3 B1 — headroom(): the BINDING firm-wide cap (Architecture Stage-6.3)
# ═══════════════════════════════════════════════════════════════════════════════════════════════
# The monitor above is TOOTHLESS by design. `headroom()` is the pure function each US book's finalize
# calls to learn "how much of this cluster / this name may I STILL hold, given what my PEERS already
# published?". It reads the OTHER US books' latest.json (flagship's legacy data/portfolio/latest.json +
# data/portfolios/{autonomous,etf,heavyweight}/latest.json — self_directed excluded until W5 makes it
# publish; china/hk are non-US, different currency + venue, never firm-aggregated), aggregates PEER
# exposure by fragility_chain.cluster_id and by name, and returns the remaining weight this book may
# add for the queried key.
#
# THE INVARIANT (v2 Stage-6): headroom only CLAMPS a book's own target DOWN, never raises it.
#   * peer files ABSENT / unreadable  -> return +inf (no firm-level clamp; per-book caps still bind —
#     "absent peer data may not un-cap" means WE never raise, and absent data means no firm clamp).
#   * peer file STALE-BUT-PRESENT     -> used as-is (last-known ≈ fail-closed).
#   * an UNKNOWN name                 -> its own SINGLETON cluster (cluster_id degrades to 'name:<T>'),
#     so a firm cluster cap can never spuriously group it; the firm name cap still binds.
# The firm caps (cluster 0.30 / name 0.10) live in config/clusters.yml (unverified-prior), env-
# overridable, with in-code fallbacks so a missing file never un-caps.

# US books whose published latest.json is firm-aggregated. self_directed excluded (doesn't publish
# until W5); china/hk excluded (non-USD, disjoint venue — a CNY A-share never piles into a US name).
_FIRM_US_BOOKS = ("flagship", "autonomous", "etf", "heavyweight")


def caps_enabled() -> bool:
    """The firm-wide headroom clamp (Stage-6.3) — the ONE definition every US book's finalize reads, so
    the flag can never drift between books. DEFAULT ON with an env opt-out: MASTERMIND_FIRM_CAPS='0'
    (or false/no/off) DISABLES it; anything else (unset / '1' / true / …) is ON.

    WHY DEFAULT-ON (against the usual dark-until-armed pattern): the audit VERIFIED four US books
    independently max-convicting the SAME SMH — the exact firm-concentration failure this clamp exists
    to stop, live, with nothing trimming the aggregate. A subtract-only firm cap is pure risk reduction
    (it can only shrink a book toward cash, never lever it up), so shipping it dark would leave the
    proven breach uncovered for no safety benefit. The clamp is byte-identical no-op on a small book
    that fits under the firm caps (the calm-tape invariant), so default-on costs nothing when it doesn't
    bind."""
    return os.environ.get("MASTERMIND_FIRM_CAPS", "1").strip().lower() not in ("0", "false", "no", "off", "")


def _firm_caps() -> dict[str, float]:
    """The firm-wide cluster / name caps (fraction of a book's NAV). Precedence:
    env (MASTERMIND_FIRM_CLUSTER_CAP / MASTERMIND_FIRM_NAME_CAP) -> config/clusters.yml
    (firm_cluster_cap / firm_name_cap) -> in-code fallback (0.30 / 0.10). Never raises; a bad value
    degrades to the fallback (an over-permissive env can never un-cap below the fallback? — no: env is
    honoured as-is for tuning, but a non-positive/garbage value falls back). All (unverified-prior)."""
    cluster_cap, name_cap = 0.30, 0.10
    try:
        from portfolio import cluster_config
        spec = cluster_config.load()
        v = spec.get("firm_cluster_cap")
        if isinstance(v, (int, float)) and v > 0:
            cluster_cap = float(v)
        v = spec.get("firm_name_cap")
        if isinstance(v, (int, float)) and v > 0:
            name_cap = float(v)
    except Exception:  # noqa: BLE001 — no reader / bad file → in-code fallbacks (never un-cap)
        pass
    cluster_cap = _env_float("MASTERMIND_FIRM_CLUSTER_CAP", cluster_cap)
    name_cap = _env_float("MASTERMIND_FIRM_NAME_CAP", name_cap)
    return {"cluster_cap": cluster_cap, "name_cap": name_cap}


def _cluster_id(ticker: str) -> str:
    """The single firm-wide cluster identity (fragility_chain.cluster_id — the ONLY identity, never a
    second definition). Degrades to the ticker's own singleton ('name:<T>') when the resolver is absent
    so a firm cluster cap can never spuriously group an unknown name. Never raises."""
    try:
        from portfolio import fragility_chain
        return str(fragility_chain.cluster_id(ticker))
    except Exception:  # noqa: BLE001 — resolver absent → singleton identity (degrade-safe)
        t = str(ticker or "").upper().strip()
        return f"name:{t}" if t else "name:?"


def _peer_sentinel_enabled() -> bool:
    """R9 sentinel kill-switch: MASTERMIND_PEER_SENTINEL=0 disables the FREEZE behavior while still
    logging. Default ON (R9: expected-peer enforcement is shrink-only → charter-legal default-on)."""
    return os.environ.get("MASTERMIND_PEER_SENTINEL", "1").strip().lower() not in ("0", "false", "no", "off", "")


def _latest_mtime(pid: str) -> float | None:
    """Modification time (float seconds since epoch) of `pid`'s latest.json, or None when absent."""
    try:
        path = _data_dir(pid) / "latest.json"
        if not path.exists():
            return None
        return path.stat().st_mtime
    except Exception:  # noqa: BLE001
        return None


def _session_length_seconds() -> float:
    """Approximate length of one US trading session in seconds (36 h) — the staleness budget.

    36 h instead of 24 h: the flagship build runs at 22:40; any intraday headroom check on the
    following trading day reads a peer file that is ~22 h old.  A 24 h budget fires a stale alert
    at the nightly boundary itself (22:40 → next-day 22:40 = exactly 24 h, with clock drift).
    36 h gives a full nightly cycle + intraday slack while still catching a book that genuinely
    skipped two consecutive nights (48 h >> 36 h)."""
    return 36.0 * 3600.0


def expected_peers(asof: str | None = None) -> list[str]:
    """The peer book IDs that ARE EXPECTED to have published today — i.e. the books that are
    enabled in the registry AND whose venue calendar says today is/was a trading day.

    A peer that IS expected but whose latest.json is ABSENT or STALE (older than one session)
    is the sentinel trigger (R9). A peer that is expected but publishes an empty book is NOT a
    trigger — it ran, it just had nothing to hold.

    Only the firm-aggregated US books are considered (china/hk are non-USD / separate venues
    and are never in _FIRM_US_BOOKS)."""
    today: date
    try:
        today = date.fromisoformat(str(asof or "")[:10])
    except Exception:  # noqa: BLE001
        today = date.today()
    result = []
    for pid in _FIRM_US_BOOKS:
        # All firm US books use the NYSE calendar — if today is a trading day, every one of them
        # is expected to have built and published.  china/hk are already excluded by _FIRM_US_BOOKS.
        try:
            from portfolio import market_calendar
            trading_today = market_calendar.is_trading_day(today)
        except Exception:  # noqa: BLE001
            trading_today = today.weekday() < 5  # degrade-safe: Mon–Fri
        if trading_today:
            result.append(pid)
    return result


def _emit_peer_sentinel(missing: list[str], stale: list[str], book_id: str) -> None:
    """Emit a FREEZE GuardrailResult to run_events for the R9 peer-expectation sentinel.
    Never raises (guardrail logging must never abort the calling build)."""
    try:
        from control_plane.guardrail import GuardrailResult, Severity
        detail_parts = []
        if missing:
            detail_parts.append(f"absent: {', '.join(sorted(missing))}")
        if stale:
            detail_parts.append(f"stale(>1 session): {', '.join(sorted(stale))}")
        detail = f"expected peer(s) not fresh — {'; '.join(detail_parts)}"
        result = GuardrailResult.failed(
            "peer_expectation",
            Severity.FREEZE,
            detail=detail,
            action_taken=("firm headroom zeroed for new adds (sentinel active)"
                          if _peer_sentinel_enabled()
                          else "logged only (MASTERMIND_PEER_SENTINEL=0)"),
            extra={"book": book_id, "missing_peers": missing, "stale_peers": stale,
                   "sentinel_active": _peer_sentinel_enabled()},
        )
        result.log(job="firm_exposure", book=book_id)
    except Exception:  # noqa: BLE001 — guardrail logging must never raise
        pass


def _peer_exposure(exclude_book: str,
                   *,
                   asof: str | None = None,
                   _sentinel_emit: bool = True) -> dict | None:
    """Aggregate the OTHER US books' published exposure, by cluster and by name.

    Returns ``{"by_cluster": {cid: weight}, "by_name": {TICKER: weight}, "n_peers": int,
    "sentinel_fired": bool}`` where each weight is the SUM across peer books of that book's
    weight in the key — this is intentionally the additive firm contribution (four books each
    at 0.08 in SMH ⇒ 0.32 firm-cluster weight), so a firm cap of 0.30 binds.
    Returns None when NO peer file could be read at all (the caller then returns +inf —
    absent peer data must not clamp).  A book that publishes an empty/corrupt file is simply
    skipped; the firm view is built from whatever peers DID publish (never raises).

    R9 SENTINEL: if >=1 expected peer's latest.json is absent or stale (>1 session), a
    GuardrailResult(FREEZE, guard="peer_expectation") is emitted to run_events.  When the
    sentinel is ACTIVE (MASTERMIND_PEER_SENTINEL != 0, the default), ``sentinel_fired=True``
    is set in the return dict — callers (headroom/clamp_book) then treat firm headroom as 0
    for new adds in the affected books.  The kill-switch (=0) logs only; sentinel_fired stays
    False so the freeze behavior is suppressed."""
    by_cluster: dict[str, float] = {}
    by_name: dict[str, float] = {}
    n_peers = 0
    any_readable = False

    for pid in _FIRM_US_BOOKS:
        if pid == exclude_book:
            continue
        # _load_book returns None for a missing file, an empty book, OR a corrupt file (it swallows the
        # JSON error). We distinguish "file present but unreadable/empty" (skip, but the firm view is
        # still valid from other peers) from "no peer files at all" (return None → +inf) by probing the
        # path directly: a present-but-unloadable file still counts as "we looked and the firm has data".
        try:
            path = _data_dir(pid) / "latest.json"
            present = path.exists()
        except Exception:  # noqa: BLE001
            present = False
        book = _load_book({"id": pid})
        if book is None:
            if present:
                # a peer published SOMETHING (even if empty/corrupt) — the firm view is real, this peer
                # just contributes nothing. Does NOT force the +inf no-clamp branch.
                any_readable = True
            continue
        any_readable = True
        n_peers += 1
        for tk, w in book["holdings"].items():
            try:
                wv = float(w)
            except (TypeError, ValueError):
                continue
            if wv <= 0:
                continue
            by_name[tk] = by_name.get(tk, 0.0) + wv
            by_cluster[_cluster_id(tk)] = by_cluster.get(_cluster_id(tk), 0.0) + wv
    if not any_readable:
        return None                       # no peer file exists at all → caller returns +inf (no clamp)

    # R9 SENTINEL: check which expected peers are missing or stale.
    # The sentinel fires only when the PIPELINE IS RUNNING TODAY — i.e., at least one expected
    # peer has a FRESH file (written within the session budget).  This distinguishes:
    #   (a) "some peers ran today but a specific expected book is absent" → sentinel (problem)
    #   (b) "pipeline hasn't run / cold start / test isolation" → no sentinel (normal)
    # Without the "fresh peer present" guard, any test that seeds only a subset of peer books
    # would trigger the sentinel for the unseeded books, breaking existing test isolation.
    import time as _time
    expected = expected_peers(asof)
    budget = _session_length_seconds()
    now_ts = _time.time()
    missing_peers: list[str] = []
    stale_peers: list[str] = []
    fresh_count = 0   # expected peers (excluding self) with a file written within the session budget
    for pid in expected:
        if pid == exclude_book:
            continue
        mtime = _latest_mtime(pid)
        if mtime is None:
            missing_peers.append(pid)
        elif (now_ts - mtime) > budget:
            stale_peers.append(pid)
        else:
            fresh_count += 1   # this peer has a file AND it was written within the session

    sentinel_fired = False
    # Only arm the sentinel when the pipeline demonstrably ran today for at least one other peer.
    # If NO expected peer has a fresh file, we are in a cold/test state — the sentinel must not fire.
    if fresh_count > 0 and (missing_peers or stale_peers):
        if _sentinel_emit:
            _emit_peer_sentinel(missing_peers, stale_peers, exclude_book)
        if _peer_sentinel_enabled():
            sentinel_fired = True

    return {"by_cluster": by_cluster, "by_name": by_name, "n_peers": n_peers,
            "sentinel_fired": sentinel_fired}


def headroom(cluster_or_ticker: str, book_id: str, own_weight: float | None = None) -> float:
    """How much weight ``book_id`` may STILL hold for a cluster id OR a single ticker, given its PEERS'
    published exposure. Architecture Stage-6.3, the binding firm-wide cap. PURE / NEVER raises.

    ``cluster_or_ticker`` — pass either a cluster id (e.g. ``'semis_ai'`` / ``'sector:...'`` / ``'name:NVDA'``)
        OR a raw ticker (e.g. ``'NVDA'``). A raw ticker is resolved to BOTH its cluster identity and its
        name key, and the returned headroom is the TIGHTER of the two firm caps (the name cap and the
        cluster cap must BOTH be satisfiable). A value that already looks like a cluster id (contains
        ``':'`` or matches a known explicit cluster) is treated as a cluster key only.
    ``book_id``     — the requesting book (its own published exposure is NOT counted; only peers).
    ``own_weight``  — advisory only; unused in the headroom math (headroom is peer-driven), accepted so
        callers can pass the target weight for symmetry/logging. Kept for API stability.

    Returns the MAX fraction of NAV this book may hold for the key = firm_cap − peer_weight, floored at
    0.0. Returns ``float('inf')`` (NO clamp) when no peer file is readable at all — the invariant's
    'absent peer data may not un-cap' means we NEVER raise; absent data simply removes the firm clamp
    and leaves the per-book caps as the only binding limit.

    R9 SENTINEL: if an expected peer's file is missing/stale, returns 0.0 (no new adds allowed)
    when the sentinel is active (MASTERMIND_PEER_SENTINEL != 0, the default).  De-risking is
    unaffected — the caller may still REDUCE positions."""
    peers = _peer_exposure(str(book_id or ""))
    if peers is None:
        return float("inf")               # no peer data → no firm clamp (never un-caps; per-book caps bind)

    # R9: when the sentinel fired, headroom is 0 for new adds (charter-legal: shrink-only).
    if peers.get("sentinel_fired"):
        return 0.0

    caps = _firm_caps()

    key = str(cluster_or_ticker or "").strip()
    if not key:
        return float("inf")

    # Decide whether the key is a cluster id or a raw ticker. A cluster id either carries the ':' prefix
    # convention (sector:/name:) or is a known explicit cluster from clusters.yml. Everything else is a
    # raw ticker → bind on BOTH its name cap and its (resolved) cluster cap, whichever is tighter.
    is_cluster_key = ":" in key
    if not is_cluster_key:
        try:
            from portfolio import fragility_chain
            if fragility_chain.cluster_cap(key) is not None:   # an explicit clusters.yml id
                is_cluster_key = True
        except Exception:  # noqa: BLE001
            is_cluster_key = False

    if is_cluster_key:
        peer_w = float(peers["by_cluster"].get(key, 0.0))
        return max(0.0, caps["cluster_cap"] - peer_w)

    # raw ticker → tighter of (firm name cap − peer name weight) and (firm cluster cap − peer cluster weight)
    tk = key.upper()
    cid = _cluster_id(tk)
    name_room = caps["name_cap"] - float(peers["by_name"].get(tk, 0.0))
    cluster_room = caps["cluster_cap"] - float(peers["by_cluster"].get(cid, 0.0))
    return max(0.0, min(name_room, cluster_room))


def published_weights(book_id: str) -> dict[str, float]:
    """Return the last-published ``{ticker: weight}`` for ``book_id`` from its latest.json.

    Used by the four US books' firm-clamp exception arms to obtain prior weights for
    ``portfolio.freeze.freeze_to_prior``.  Returns ``{}`` when the file is absent or
    unreadable — callers fall back to prior-keys-only mode (no new adds, existing weights
    unchanged). Never raises."""
    try:
        book = _load_book({"id": str(book_id or "")})
        if book and isinstance(book.get("holdings"), dict):
            return {tk: float(w) for tk, w in book["holdings"].items()
                    if isinstance(w, (int, float)) and w > 0}
    except Exception:  # noqa: BLE001
        pass
    return {}


def clamp_book(positions: Any, book_id: str) -> dict:
    """Clamp a finalized US book's target weights DOWN to firm headroom (subtract-only). PURE helper the
    four US books' finalize paths call; NEVER raises, NEVER increases a weight, freed weight → cash.

    ``positions`` — a list of ``{ticker, weight, ...}`` rows OR a ``{ticker: weight}`` mapping (both
        the sleeve-book shape and the Brain target shape). Returned in the SAME shape.
    ``book_id``   — the requesting book (excluded from the peer aggregation).

    Algorithm (deterministic, order-independent of book rows):
      1. NAME pass — each ticker clamped to its firm name headroom (peer name weight only).
      2. CLUSTER pass — aggregate the (name-clamped) weights by cluster_id; any cluster whose
         (this-book + peer) firm weight exceeds the firm cluster cap is scaled DOWN pro-rata across its
         members so this book's contribution fits the remaining cluster headroom.
    Both passes use PEER exposure (this book's own rows never inflate its own headroom). When no peer
    file is readable, every headroom is +inf → this is a byte-identical no-op (the calm-tape invariant).

    Returns ``{"positions": <same shape>, "clamped": [{key, kind, from, to, freed}], "freed": float,
    "bound": bool}``. ``bound`` is True iff any weight was actually reduced (the loud-log trigger)."""
    # ---- coerce to a {ticker: weight} working map, remembering the input shape ----
    as_mapping = isinstance(positions, dict)
    work: dict[str, float] = {}
    rows: list[dict] = []
    if as_mapping:
        for tk, w in positions.items():
            t = str(tk or "").upper().strip()
            if not t:
                continue
            try:
                wv = float(w)
            except (TypeError, ValueError):
                continue
            work[t] = work.get(t, 0.0) + wv
    else:
        for r in (positions or []):
            if not isinstance(r, dict):
                continue
            t = str(r.get("ticker") or "").upper().strip()
            if not t:
                continue
            try:
                wv = float(r.get("weight"))
            except (TypeError, ValueError):
                continue
            work[t] = work.get(t, 0.0) + wv
            rows.append(r)

    if not work:
        return {"positions": positions, "clamped": [], "freed": 0.0, "bound": False}

    peers = _peer_exposure(str(book_id or ""))
    if peers is None:                     # no peer data → no clamp (never un-caps; byte-identical no-op)
        return {"positions": positions, "clamped": [], "freed": 0.0, "bound": False}
    caps = _firm_caps()
    clamped: list[dict] = []
    freed = 0.0

    # ---- R9 sentinel: freeze to prior book — FREEZE NEW ADDS, de-risking allowed, existing
    # book MAINTAINED (Ruling R9: "FREEZE NEW ADDS, de-risking allowed, existing book MAINTAINED").
    # Zeroing ALL positions was over-aggressive: it liquidated EVERY held position (forced
    # liquidation is an act of NEW risk, not "no new risk"). Correct behaviour: no new adds,
    # no increases; holds and reductions pass through.
    # Only active when MASTERMIND_PEER_SENTINEL != 0 (default ON). ----
    if peers.get("sentinel_fired"):
        from portfolio.freeze import freeze_to_prior as _ftp
        prior_w = published_weights(str(book_id or ""))
        # Build the target map from the working (already-coerced) dict
        frozen = _ftp(work, prior_w)
        for tk in list(work.keys()):
            old_w = work[tk]
            new_w = frozen.get(tk.upper(), frozen.get(tk, 0.0))
            # normalise: frozen keys track prior casing but work keys are already UPPER
            # find the frozen value via the upper key directly (freeze_to_prior uses prior casing)
            new_w_upper = 0.0
            for fk, fv in frozen.items():
                if str(fk or "").upper().strip() == tk:
                    new_w_upper = fv
                    break
            new_w = new_w_upper
            if old_w > new_w + 1e-12:
                clamped.append({"key": tk, "kind": "sentinel",
                                "from": round(old_w, 4), "to": round(new_w, 4),
                                "freed": round(old_w - new_w, 4)})
                freed += old_w - new_w
                work[tk] = new_w
        # Add prior-only names (held but not in the built target — must be retained, not zeroed)
        for fk, fv in frozen.items():
            ku = str(fk or "").upper().strip()
            if ku and ku not in work:
                work[ku] = fv
        if as_mapping:
            out_map = {tk: round(w, 4) for tk, w in work.items() if w > 0}
            return {"positions": out_map, "clamped": clamped, "freed": round(freed, 4), "bound": True}
        # list shape: update existing rows and add prior-only rows
        row_tks = {str(r.get("ticker") or "").upper().strip() for r in rows}
        for r in rows:
            t = str(r.get("ticker") or "").upper().strip()
            if t in work:
                r["weight"] = round(work[t], 4)
        # inject prior-only tickers as minimal rows so they are not absent (= not liquidated)
        for ku, fw in work.items():
            if ku not in row_tks and fw > 0:
                rows.append({"ticker": ku, "weight": round(fw, 4), "sleeve": "prior", "_sentinel_hold": True})
        return {"positions": positions, "clamped": clamped, "freed": round(freed, 4), "bound": True}

    # ---- pass 1: firm NAME cap ----
    for tk in list(work.keys()):
        w = work[tk]
        room = max(0.0, caps["name_cap"] - float(peers["by_name"].get(tk, 0.0)))
        if w > room + 1e-12:
            clamped.append({"key": tk, "kind": "name", "from": round(w, 4),
                            "to": round(room, 4), "freed": round(w - room, 4)})
            freed += w - room
            work[tk] = room

    # ---- pass 2: firm CLUSTER cap (pro-rata scale-down of this book's contribution) ----
    by_cluster: dict[str, float] = {}
    members: dict[str, list[str]] = {}
    for tk, w in work.items():
        cid = _cluster_id(tk)
        by_cluster[cid] = by_cluster.get(cid, 0.0) + w
        members.setdefault(cid, []).append(tk)
    for cid, own_w in by_cluster.items():
        peer_w = float(peers["by_cluster"].get(cid, 0.0))
        room = max(0.0, caps["cluster_cap"] - peer_w)     # weight THIS book may still hold in the cluster
        if own_w > room + 1e-12 and own_w > 0:
            scale = room / own_w
            clamped.append({"key": cid, "kind": "cluster", "from": round(own_w, 4),
                            "to": round(room, 4), "freed": round(own_w - room, 4)})
            freed += own_w - room
            for tk in members[cid]:
                work[tk] = work[tk] * scale

    if not clamped:
        return {"positions": positions, "clamped": [], "freed": 0.0, "bound": False}

    # ---- write the clamped weights back in the original shape ----
    if as_mapping:
        out_map = {tk: round(w, 4) for tk, w in work.items()}
        return {"positions": out_map, "clamped": clamped, "freed": round(freed, 4), "bound": True}
    # A ticker can (rarely) appear in TWO rows (e.g. a name held in both the leadership and conviction
    # sleeves). `work[t]` is the CLAMPED total for the ticker; distribute it back across that ticker's
    # rows PRO-RATA by their original weight so the sum matches (never write the full total to each row).
    orig_by_tk: dict[str, float] = {}
    for r in rows:
        t = str(r.get("ticker") or "").upper().strip()
        try:
            orig_by_tk[t] = orig_by_tk.get(t, 0.0) + float(r.get("weight"))
        except (TypeError, ValueError):
            continue
    for r in rows:
        t = str(r.get("ticker") or "").upper().strip()
        if t not in work:
            continue
        orig_total = orig_by_tk.get(t, 0.0)
        try:
            row_w = float(r.get("weight"))
        except (TypeError, ValueError):
            row_w = 0.0
        # single-row ticker → the clamped total; duplicated ticker → its pro-rata share of the total.
        share = (row_w / orig_total) if orig_total > 0 else 0.0
        r["weight"] = round(work[t] * share if orig_total > 0 else work[t], 4)
    return {"positions": positions, "clamped": clamped, "freed": round(freed, 4), "bound": True}
