"""Conviction sleeve — a name takes paper size only when ALL sides confirm.

Closes the loop: candidate names (Claude's open proposals + the leadership universe) are
each run through the multi-sided decision matrix; a name is sized ONLY if its synthesis
says size_authority == 'up' AND it trips no hard veto (parabolic / Altman distress /
cycle-blocked). Size is confluence-weighted, subtract-only, capped per name. Everything
else is shown but held at 0 — discipline over enthusiasm.
"""
from __future__ import annotations

import json
from pathlib import Path

import bot  # noqa: F401

from portfolio import lenses

# liquid leadership/AI-complex names that carry a full stockdata lens read
_SHORTLIST = ["AVGO", "NVDA", "AMD", "MU", "GEV", "PLTR", "DELL", "TSM", "AMAT", "MRVL",
              "ORCL", "VST", "BWXT", "ANET", "LRCX", "KLAC", "MSFT", "GOOGL", "META", "AAPL"]

# the fed-in candidate universe: top names from the us_stocks standout board + the top
# stock picks across the thematic baskets. The engine gate (build) filters this down — a
# broad feed in, discipline at the gate.
TOP_US = 100        # top-N from us_stocks.html's standout BUY board (ranked by alpha)
TOP_BASKET = 100    # top-N single-name picks across all thematic baskets (by 20d return)

_V = Path(__file__).resolve().parent.parent / "vendor" / "macro"


class _SizedBook(list):
    """A plain list of sized-position dicts that ALSO carries a `data_health` attribute.

    build() historically returns (sized_list, rejected_list); every caller unpacks that tuple and
    iterates `sized` as a list. To surface the build-wide data-health / fail-closed record WITHOUT
    breaking that contract (add fields, never rename — house rule), `sized` is this list subclass:
    `isinstance(sized, list)` and all list behaviour is unchanged, and `sized.data_health` exposes
    the coverage record for the runlog."""
    data_health: dict | None = None


def _load(rel: str):
    p = _V / rel
    try:
        return json.loads(p.read_text()) if p.exists() else None
    except Exception:
        return None


def _us_standouts(n: int = TOP_US) -> list[str]:
    """Top-N tickers from the us_stocks standout BUY board (already rank-ordered by alpha)."""
    d = _load("site/factordata/us_standouts.json") or {}
    buy = d.get("buy") or d.get("standouts") or []
    return [r.get("ticker") for r in buy[:n] if isinstance(r, dict) and r.get("ticker")]


# sector-concentration firebreak — PERCENTAGE based (PM directive 2026-06-22, replacing the prior
# count-based SECTOR_MAX_NAMES which had been disabled). No single sector may hold more than
# SECTOR_MAX_FRACTION of the conviction-sleeve budget; an over-weight sector is scaled DOWN
# proportionally (subtract-only — names are down-sized, never churned out) and the freed weight is
# left in cash. This is the crowding / cohort de-gross control: a book that piles a whole homogeneous
# cohort (e.g. every AI-semis leader) into one sector is fragile even when each name scores well in
# isolation. The per-name + book/theme weight caps still bound individual position size on top of it.
SECTOR_MAX_FRACTION = 0.50

# MANUAL HOLD-OUT (operational, 2026-06-22) — names deliberately reversed out of the book by PM
# directive after the AVGO/NVDA forced-override post-mortem (see docs/case_studies). This is NOT a
# scoring penalty or a permanent ban: it is a do-not-AUTO-re-add guard so the daily rebalance does
# not silently re-buy a name the desk just deliberately exited. Remove a ticker here to let the
# engine consider it again on its own merits.
_MANUAL_EXCLUDE = {"NVDA", "AVGO"}

# EXIT HYSTERESIS — to ENTER, a new name must clear the full 'up' gate (confluence > 0.30). To be
# DROPPED, a name we ALREADY hold has to fall below this LOWER floor (or trip a hard exit). The
# asymmetric entry/exit bars stop a name being churned in and out across builds when it wobbles
# around the 0.30 entry line (the NVDA bought-then-immediately-closed problem). RESTORED toward
# entry parity (0.15 -> 0.25, 2026-06-22): the prior 0.15 floor was loosened under the AVGO/NVDA
# override and let a deteriorating held name ride too long; a tight 0.05 band still prevents churn.
_EXIT_CONFLUENCE_FLOOR = 0.25

# CATALYST/CONFIRMATION gates FULL size (doctrine §4.3 "catalyst gates full size" + "own leaders
# without chasing"). A name that clears the gate but lacks price+leadership confirmation (or a
# leading theme) takes only INITIAL size — this fraction of its confluence-weighted target.
_INITIAL_SIZE_FRACTION = 0.7


def _sector_of(t: str) -> str:
    """Normalised sector key for the concentration cap — collapses synonym labels
    ('Technology' / 'Information Technology' -> XLK) via the sector→ETF map so a cohort can't
    dodge the cap by sitting under two spellings of the same sector."""
    d = _load(f"site/stockdata/{t}.json")
    sec = (d or {}).get("sector") or "Unknown"
    return lenses._SECTOR_ETF.get(sec, sec)


def _basket_top_picks(n: int = TOP_BASKET) -> list[str]:
    """Top-N single-name picks across all thematic baskets, ranked by 20-day return.

    Union every basket's members, keep each name's best 20d return, take the top N. The
    extension veto at the gate handles parabolic momentum names, so a momentum-ranked feed
    is safe here."""
    d = _load("site/basketdata/baskets.json") or {}
    best: dict[str, float] = {}
    for b in (d.get("baskets") or []):
        for m in (b.get("members") or []):
            sym = (m.get("symbol") or m.get("ticker") or "").upper()
            if not sym:
                continue
            r = m.get("ret_20d")
            rr = float(r) if isinstance(r, (int, float)) else -1e9
            if sym not in best or rr > best[sym]:
                best[sym] = rr
    ranked = sorted(best.items(), key=lambda kv: kv[1], reverse=True)
    return [sym for sym, _ in ranked[:n]]


def universe() -> list[str]:
    """The fed-in candidate universe: top us_stocks standouts ∪ top thematic-basket picks."""
    return sorted(set(_us_standouts()) | set(_basket_top_picks()))


def candidates() -> list[str]:
    """Conviction candidate pool: the fed-in universe (top us_stocks + top basket picks)
    ∪ open ledger theses (Claude's proposals) ∪ the liquid leadership shortlist ∪ the unified
    intake queue (radar / alt-data / briefing-corroborated + divergent names the buy board
    alone misses). The engine gate (build) filters this down — broad feed in, discipline at
    the gate."""
    try:
        from brain import ledger
        proposed = {t["subject"].upper() for t in ledger.all_theses() if t.get("status") == "open"}
    except Exception:
        proposed = set()
    try:
        from brain import intake
        # only reasonably-corroborated names (score floor) so the gate isn't drowned in noise
        fed_in = set(intake.tickers(limit=60, min_score=0.4))
    except Exception:
        fed_in = set()
    return sorted((set(_SHORTLIST) | set(universe()) | proposed | fed_in) - _MANUAL_EXCLUDE)


def build(budget: float, name_cap: float = 0.08,
          held: set | None = None) -> tuple[list[dict], list[dict]]:
    """Return (sized_positions, rejected) where rejected contains every evaluated name
    that did NOT make the size gate, with the veto/bear detail that kept it out.

    `held` = tickers already open in the conviction book; they get priority in the sector cap
    (hysteresis) so a name isn't churned in/out across builds when a marginally-higher new name
    appears. Sizing behaviour is otherwise unchanged.
    """
    held = {h.upper() for h in (held or set())}
    passed = []
    rejected: list[dict] = []
    n_evaluated = 0
    n_degraded = 0

    for t in candidates():
        try:
            full = lenses.full(t, "name")
            syn = full["synthesis"]
            rows: list[dict] = full.get("rows", [])
        except Exception:
            continue

        vetoes: list[str] = syn.get("vetoes", [])
        confluence: float = syn.get("confluence", 0.0)
        sa = syn.get("size_authority")
        is_held = t.upper() in held
        # DATA-DEGRADED (fail-closed): the per-name stockdata was absent OR < 2 lenses voted, so the
        # synthesis flagged size_authority='insufficient_data'. Track coverage across the whole build
        # for the >80%-degraded circuit breaker (f). (Also read the explicit flag so a future
        # authority value can't silently bypass this.)
        degraded = (sa == "insufficient_data") or bool(syn.get("data_degraded"))
        n_evaluated += 1
        if degraded:
            n_degraded += 1

        # HARD exits — a held name is dropped IMMEDIATELY on any of these (no hysteresis for a
        # genuinely broken name): a hard veto (parabolic / Altman / cycle-blocked), a CONFIRMED
        # structural downtrend, or size_authority blocked. A fresh falling-knife or a softened
        # sector is NOT a hard exit, so a name we already own rides through a rough week.
        # CRITICAL FREEZE SEMANTICS: a data outage is NOT a hard exit. Missing data must NEVER
        # liquidate the book (the inverse disaster of the fail-open bug). When degraded we suppress
        # price_downtrend as an exit trigger — there is no real price read to trust — so a held name
        # FREEZES (hold, don't churn) rather than being dropped on a phantom/stale signal.
        hard_exit = (bool(vetoes)
                     or (bool(syn.get("price_downtrend")) and not degraded)
                     or (sa == "blocked"))
        entry_ok = (sa == "up") and not vetoes and not degraded    # sa=='up' already implies not-degraded; belt-and-suspenders
        # FREEZE-ON-DEGRADE: a HELD name with degraded data is RETAINED as a hold regardless of the
        # confluence floor (a degraded confluence is untrustworthy — possibly the 1.0 mirage — so it
        # can neither justify nor deny the hold). Only a genuine hard exit (real veto) removes it.
        held_frozen = is_held and degraded and not hard_exit
        hold_ok = held_frozen or (is_held and not hard_exit and confluence > _EXIT_CONFLUENCE_FLOOR)

        if entry_ok or hold_ok:
            # full-size confirmation: a confirmed leader (price + sector leadership) OR a genuine
            # leading theme. Everything else that clears the gate is sized at INITIAL only.
            _dirs = {r["lens"]: r.get("direction") for r in rows}
            confirmed = ((_dirs.get("trend") == "bull" and _dirs.get("sector_rs") == "bull")
                         or _dirs.get("narrative") == "bull")
            # A frozen (data-degraded) held name is confirmed=False — we have NO price/leadership read
            # to justify full size, so it can only carry its existing (initial-fraction) weight.
            if held_frozen:
                confirmed = False
            # FREEZE weight floor: a frozen held name's degraded confluence may be 0 (or the untrusted
            # 1.0 mirage) — either way it must SURVIVE the `weight > 0` filter so the freeze actually
            # HOLDS the position (a 0 weight would silently liquidate it — the very disaster we guard).
            # A tiny positive floor keeps it in the book at minimal size; the sector cap / vol sizing
            # still bound it. Non-frozen entries keep their real confluence unchanged.
            _conf = max(0.01, confluence) if held_frozen else max(0.0, confluence)
            _entry = {"ticker": t, "confluence": _conf,
                      "bull": syn["bull"], "bear": syn["bear"],
                      "retained": bool(hold_ok and not entry_ok), "confirmed": confirmed,
                      "divergences": [d["pattern"] for d in syn.get("divergences", [])]}
            if held_frozen:
                _entry["retained_reason"] = "data_degraded_freeze"
                _entry["data_degraded"] = True
            passed.append(_entry)
        else:
            # Determine a short human-readable rejection reason (most-specific first).
            if vetoes:
                reason = "Vetoed: " + ", ".join(vetoes)
            elif sa == "blocked":
                reason = "Blocked (size_authority=blocked)"
            elif syn.get("price_downtrend"):
                reason = "Downtrend — price rolling over (no falling knives)"
            elif syn.get("price_falling_fast"):
                reason = "Falling knife — sharp recent multi-day decline (await stabilization)"
            elif not syn.get("leadership_ok", True):
                reason = "Lagging sector/commodity — leadership gate (fighting the tape)"
            elif syn.get("weak_asymmetry"):
                _ar = syn.get("asym_ratio")
                reason = ("Weak asymmetry — upside/downside cone "
                          + (f"{_ar:.2f}" if isinstance(_ar, (int, float)) else "?")
                          + " (not asymmetric)")
            elif confluence <= -0.3:
                reason = f"Negative confluence ({confluence:+.2f})"
            else:
                reason = f"Insufficient confluence ({confluence:+.2f}, need >0.30)"

            # Extract bear bullets from the matrix rows (cap at 4).
            bear_pts: list[str] = []
            for r in rows:
                if r.get("direction") == "bear" and len(bear_pts) < 4:
                    lens_name = r.get("lens", "")
                    note = r.get("note") or ""
                    val = r.get("value") or {}
                    if lens_name == "extension":
                        pv2 = val.get("pct_vs_200dma")
                        para = val.get("parabolic")
                        bear_pts.append(
                            f"Extension: grade={val.get('grade')}"
                            + (", parabolic=True" if para else "")
                            + (f", +{pv2:.1f}% vs 200dma" if pv2 is not None else "")
                        )
                    elif lens_name == "valuation":
                        vz = val.get("value_z")
                        bear_pts.append(
                            f"Valuation stretched"
                            + (f" (value_z={vz:.2f})" if vz is not None else "")
                        )
                    elif lens_name == "flows_13f":
                        ns = val.get("n_selling")
                        nb = val.get("n_buying")
                        asof = val.get("as_of")
                        when = f" as of {asof}" if asof else ""
                        bear_pts.append(
                            f"13F distribution tilt ({ns} trimmed vs {nb} added last quarter{when}, lagged)"
                            if ns is not None else "13F smart-money net negative (lagged quarterly snapshot)"
                        )
                    elif lens_name == "quality":
                        acct = val.get("accounting")
                        bear_pts.append(
                            f"Quality / accounting flag: {acct}"
                            if acct else "Quality lens bearish"
                        )
                    elif lens_name == "solvency":
                        az = val.get("altman_zone")
                        bear_pts.append(
                            f"Solvency: Altman zone={az}" if az else "Solvency concern"
                        )
                    elif lens_name == "macro_risk":
                        score = val.get("score")
                        bear_pts.append(
                            f"Macro risk elevated (score={score:.2f})" if score is not None
                            else "Macro risk elevated"
                        )
                    elif lens_name == "conviction":
                        band = val.get("band")
                        bear_pts.append(
                            f"Engine conviction band={band}" if band else "Engine conviction bearish"
                        )
                    elif note:
                        bear_pts.append(f"{lens_name.replace('_', ' ').title()}: {note}")
                    else:
                        bear_pts.append(f"{lens_name.replace('_', ' ').title()} lens bearish")

            rejected.append({
                "ticker": t,
                "reason": reason,
                "vetoes": vetoes,
                "bear": bear_pts,
                "confluence": round(confluence, 3),
            })

    # ── BUILD-LEVEL DATA-HEALTH CIRCUIT BREAKER (fail-closed, book-wide) ─────────────────────────
    # If the OVERWHELMING majority of evaluated candidates are data-degraded (>80%), the feed is
    # broken system-wide — not a single-name gap. On the 2026-07-01 incident this was ~100%. In that
    # state we refuse EVERY new add this build (there is no trustworthy evidence to open on) but KEEP
    # existing holds (freeze, don't churn — missing data must never liquidate the book). A loud
    # data_health record rides out in the return so the runlog shows exactly WHY the book froze.
    _degraded_frac = (n_degraded / n_evaluated) if n_evaluated else 0.0
    _breaker_tripped = n_evaluated > 0 and _degraded_frac > 0.80
    data_health = {
        "degraded": _breaker_tripped,
        "n_evaluated": n_evaluated,
        "n_degraded": n_degraded,
        "degraded_fraction": round(_degraded_frac, 3),
        "threshold": 0.80,
        "action": ("NEW_ADDS_FROZEN — data feed degraded across the candidate universe; "
                   "holding existing book, refusing all new opens this build")
                  if _breaker_tripped else "ok",
    }
    if _breaker_tripped:
        # keep only names ALREADY in the book (a held name that still cleared the gate on its own real
        # data is retained too — key off `held`, not the retained flag, so healthy holds aren't
        # churned out by the breaker); drop every genuinely NEW add. A kept name becomes a HOLD.
        _kept = [p for p in passed if p["ticker"].upper() in held]
        for p in _kept:
            p.setdefault("data_degraded", True)
            p["retained"] = True                       # a breaker-kept name is a HOLD, not a fresh add
            p["retained_reason"] = p.get("retained_reason") or "data_health_freeze"
        passed = _kept

    # NOTE: the sector-concentration firebreak is now a PERCENTAGE cap applied AFTER sizing (see
    # _apply_sector_cap, called below). Every entry-gate passer stays in the book; no single sector
    # may exceed SECTOR_MAX_FRACTION of the budget, so an over-weight cohort is risk-trimmed (scaled
    # down) rather than demoted — held names are never churned out, just sized down.

    # confidence-weighted sizing, then the catalyst/confirmation FULL-vs-INITIAL size gate
    tot = sum(p["confluence"] for p in passed) or 1.0
    for p in passed:
        base = min(p["confluence"] / tot * budget, name_cap)
        mult = 1.0 if p.get("confirmed") else _INITIAL_SIZE_FRACTION
        p["weight"] = round(base * mult, 4)
        p["size_stage"] = "full" if p.get("confirmed") else "initial"
        p["sleeve"] = "conviction"
        # a name kept only by exit-hysteresis (retained, entry gate NOT re-cleared) is a HOLD, not a
        # fresh add — say so honestly so the book/thesis doesn't claim "all sides confirm".
        p["verdict"] = "hold" if p.get("retained") else "add"

    # `sized` is a list (unchanged for every existing caller) that ALSO carries the build-wide
    # data_health record as an attribute, so the runlog can surface WHY the book froze without
    # changing the (sized, rejected) tuple contract every caller already unpacks. Also mirrored onto
    # the first sized dict as a fallback for consumers that only iterate the positions.
    sized = _SizedBook(p for p in passed if p["weight"] > 0)
    sized.data_health = data_health
    if sized:
        sized[0].setdefault("data_health", data_health)
    # VOL-MANAGED RISK SIZING (the validated +0.1-0.15 Sharpe lever): re-weight the book
    # by inverse forecasted vol x the dispersion regime — bet less on high-vol names, more
    # on calm ones, de-gross when selection doesn't pay. Risk lever only; never changes
    # WHICH names are in. Additive + graceful (neutral until the macro field ships).
    try:
        from portfolio import risk_sizing
        risk_sizing.apply(sized, budget, name_cap)
    except Exception:  # noqa: BLE001 — additive, never breaks book construction
        pass
    # PERCENTAGE sector-concentration firebreak (applied LAST, after vol-managed sizing, so the
    # <=SECTOR_MAX_FRACTION-per-sector invariant holds in the FINAL book): scale any over-weight
    # sector down proportionally, leaving the freed weight in cash. Subtract-only; never churns.
    _apply_sector_cap(sized, budget)
    # sort rejected worst-confluence first so the most-bearish names surface at top
    rejected.sort(key=lambda x: x["confluence"])
    return sized, rejected


def _apply_sector_cap(sized: list[dict], budget: float,
                      frac: float = SECTOR_MAX_FRACTION) -> None:
    """Percentage sector-concentration firebreak (subtract-only, in place).

    No single sector may hold more than `frac` of the conviction-sleeve `budget`. Any sector over
    the cap has every one of its names scaled DOWN by the same factor so the sector lands exactly at
    the cap; the freed weight is left uninvested (cash), never redistributed (which would just
    re-concentrate elsewhere). Names are down-sized, never dropped — a held position is risk-trimmed,
    not churned out. The catch-all 'Unknown' bucket (untagged names — not a real cohort) is exempt."""
    if not sized or budget <= 0 or frac <= 0:
        return
    cap = frac * budget
    from collections import defaultdict
    by_sec: dict[str, list[dict]] = defaultdict(list)
    for p in sized:
        by_sec[_sector_of(p["ticker"])].append(p)
    for sec, names in by_sec.items():
        if sec == "Unknown":
            continue
        tot = sum(max(0.0, float(p.get("weight", 0.0))) for p in names)
        if tot > cap and tot > 0:
            scale = cap / tot
            for p in names:
                p["weight"] = round(float(p.get("weight", 0.0)) * scale, 4)
                p["sector_capped"] = {"sector": sec, "scaled_to_frac": round(frac, 3)}
