"""Entry-Quality annotator — an ADVISORY read on whether NOW is a good price to start a position.

This is the minimal, safe first slice of the Entry-Discipline program
(``research/ENTRY_DISCIPLINE_PLAN.md``). It answers the one question the confluence gate never
asks: *"is this a good ENTRY, on the timescale a chase actually happens?"* The existing gates ask
"is this a real leader?" (confluence) and "is it broken?" (downtrend / falling-knife / parabolic),
and the only extension brake keys on the slow 200-day MA — so a name that just ripped vertically to
the top of its own recent range clears every brake. This module stamps a verdict that MAKES THAT
VISIBLE.

CONTRACT — read this before wiring it anywhere:
  * PURE + READ-ONLY. It computes from price history the bot already loads and from published
    fields. It sizes NOTHING, gates NOTHING, and touches NO trading state.
  * ADVISORY. ``annotate()`` returns a note dict; the caller stamps it on the decision/shadow record
    for forward grading (§5 of the plan). It NEVER changes a weight, a fill, or a gate outcome.
  * FAIL-OPEN (charter P2). Every input is optional. Missing price history / stockdata / sector_pulse
    → verdict ``'unknown'`` with empty notes. A missing signal can never manufacture a veto or a
    withhold — it can only fail to inform. (This is the common case in a fresh checkout / CI, where
    the per-name stores live on the R2 data plane and are absent.)
  * INJECTABLE. Every external read (`series`, `stockdata`, `pulse`) can be passed in, so tests and
    replays run fully offline with no vendor/macro engine and no network.

The verdict vocabulary (all advisory):
    clean          — near neither a range extreme nor a fresh rip; a reasonable place to start.
    extended       — stretched vs its own fast MA / high in its recent range, but not a fresh spike.
    chase          — at the TOP of its recent range AND freshly ripped (the buy-high failure mode).
    cooling_sector — its theme's sector_pulse heat is cooling/broken (an entry into a fading tailwind).
    wait           — poor entry for a composed reason (extended + cooling, or extended + no clean-entry).
    unknown        — not enough data to judge (fail-open; informs nothing, blocks nothing).
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)

# ── thresholds — (unverified-prior); tuned on the outcome ledger before anything BINDS (plan §5) ──
# Fast-extension: the up-side twin of the falling-knife veto, on the timescale the 200dma can't see.
_RANGE_HIGH_PCTILE = 90.0        # >= 90th pctile of its own 60d high-low range → at the top of the range
_RANGE_LOW_PCTILE = 15.0         # <= 15th pctile → near the bottom of the range (a pullback, not a chase)
_HOT_PCT_VS_20DMA = 12.0         # >= 12% above the 20d MA → stretched on the fast timescale
_RIP_RET_10D = 0.18              # >= +18% over 10 sessions → a fresh vertical move (the "rip")
_EXTENDED_PCT_VS_200DMA = 30.0   # mirrors the existing extension/timing brake (lenses / watchlist)
_NEAR_HIGH_OFF_52W = -6.0        # within 6% of the 52w high → pinned at the top (F1's blind spot)

_COOLING_HEAT = {"cooling", "broken"}
_HEATING_HEAT = {"heating", "hot"}


def _pctile_in_range(last: float, lo: float, hi: float) -> float | None:
    """Where `last` sits in the [lo, hi] band, as a 0..100 percentile (0 = at the low, 100 = at the
    high). None on a degenerate/zero-width band. Pure."""
    try:
        if hi is None or lo is None or hi <= lo:
            return None
        return round(max(0.0, min(1.0, (last - lo) / (hi - lo))) * 100.0, 1)
    except Exception:  # noqa: BLE001
        return None


def _fast_metrics(series) -> dict:
    """Fast-timescale extension metrics from an ascending close-price Series (or None).

    Returns {pct_vs_20dma, range_pctile_60d, ret_10d, last} — every value nullable. A too-short or
    absent series yields all-None (fail-open). Never raises."""
    out = {"pct_vs_20dma": None, "range_pctile_60d": None, "ret_10d": None, "last": None}
    try:
        if series is None or len(series) < 21:
            return out
        last = float(series.iloc[-1])
        out["last"] = last
        ma20 = float(series.iloc[-20:].mean())
        if ma20 > 0:
            out["pct_vs_20dma"] = round((last / ma20 - 1.0) * 100.0, 2)
        window = series.iloc[-60:] if len(series) >= 60 else series
        out["range_pctile_60d"] = _pctile_in_range(last, float(window.min()), float(window.max()))
        if len(series) > 10:
            base10 = float(series.iloc[-11])
            if base10 > 0:
                out["ret_10d"] = round(last / base10 - 1.0, 4)
    except Exception:  # noqa: BLE001
        return out
    return out


def _pulse_heat_for(theme_id: str | None, pulse: dict | None) -> dict:
    """The sector_pulse heat + clean_entry read for a theme id, {}-on-miss. Fail-open: absent pulse
    or unknown theme → {} (informs nothing). Guards the schema so a future shape can't raise."""
    if not theme_id or not isinstance(pulse, dict):
        return {}
    if pulse.get("schema") not in (1, "1", None):     # only the known schema; unknown → ignore
        return {}
    tid = str(theme_id).lower()
    for th in (pulse.get("themes") or []):
        if not isinstance(th, dict):
            continue
        if str(th.get("id") or "").lower() == tid or str(th.get("label") or "").lower() == tid:
            ce = th.get("clean_entry") if isinstance(th.get("clean_entry"), dict) else {}
            return {"heat": th.get("heat"), "clean_entry_flag": ce.get("flag"),
                    "clean_entry_quality": ce.get("quality"), "rank_delta_5d": th.get("rank_delta_5d"),
                    "reco": th.get("reco")}
    return {}


def annotate(ticker: str, *, series=None, stockdata: dict | None = None,
             theme_id: str | None = None, pulse: dict | None = None,
             as_of: str | None = None) -> dict:
    """Return an ADVISORY entry-quality note for a candidate BUY. Sizes/gates NOTHING.

    Inputs (all optional; injected for tests/replays, else read via the engine store in production):
      * series    — ascending close-price Series; falls back to ``lenses._closes(ticker)``.
      * stockdata — the vendored per-name stockdata dict; falls back to ``site/stockdata/{t}.json``.
                    Only ``tech.pct_vs_200dma`` and ``tech.off_52w_high_pct`` are consulted.
      * theme_id  — the name's theme slug (for the sector_pulse join); optional.
      * pulse     — a parsed ``sector_pulse.json`` dict; optional (product is new / may be absent).

    Returns {ticker, verdict, notes:[...], metrics:{...}, sources:[...], as_of}. FAIL-OPEN: any
    missing input degrades the verdict toward 'unknown' and never produces a veto. Never raises."""
    t = (ticker or "").upper().strip()
    notes: list[str] = []
    sources: list[str] = []

    # ── price series (fast-extension metrics) — injected or read via lenses (may be None) ──
    if series is None:
        try:
            from portfolio import lenses
            series = lenses._closes(t)
        except Exception:  # noqa: BLE001
            series = None
    fm = _fast_metrics(series)
    if fm.get("last") is not None:
        sources.append("price_series")

    # ── published slow fields (200dma distance + off-52w-high) — injected or read defensively ──
    if stockdata is None:
        try:
            from portfolio import lenses
            stockdata = lenses._load(f"site/stockdata/{t}.json")
        except Exception:  # noqa: BLE001
            stockdata = None
    pct_vs_200dma = off_52w_high = None
    if isinstance(stockdata, dict):
        tech = stockdata.get("tech") or {}
        pct_vs_200dma = tech.get("pct_vs_200dma")
        off_52w_high = tech.get("off_52w_high_pct")
        if pct_vs_200dma is not None or off_52w_high is not None:
            sources.append("stockdata")

    # ── sector_pulse heat (the new shared data product; may be absent — fail-open) ──
    # theme_id / pulse stay injectable for tests, but in production BOTH are derived here:
    # the per-name stockdata block carries the strongest theme's id, and the vendored
    # basketdata/sector_pulse.json is the whole-board pulse. Without this derivation the
    # phase2 call site (which passes neither) left this leg permanently dead — the
    # cooling_sector verdict could never fire.
    if theme_id is None and isinstance(stockdata, dict):
        sp = stockdata.get("sector_pulse")
        if isinstance(sp, dict):
            theme_id = sp.get("theme_id")
    if pulse is None and theme_id:
        try:
            from portfolio import lenses
            pulse = lenses._load("site/basketdata/sector_pulse.json")
        except Exception:  # noqa: BLE001
            pulse = None
    heat = _pulse_heat_for(theme_id, pulse)
    if heat:
        sources.append("sector_pulse")

    metrics = {
        "pct_vs_20dma": fm.get("pct_vs_20dma"),
        "range_pctile_60d": fm.get("range_pctile_60d"),
        "ret_10d": fm.get("ret_10d"),
        "pct_vs_200dma": pct_vs_200dma,
        "off_52w_high_pct": off_52w_high,
        "sector_heat": heat.get("heat"),
        "sector_clean_entry": heat.get("clean_entry_flag"),
    }

    # ── derive the boolean entry-quality tells (each guarded; a None field never fires) ──
    rp = fm.get("range_pctile_60d")
    p20 = fm.get("pct_vs_20dma")
    r10 = fm.get("ret_10d")
    p200 = pct_vs_200dma if isinstance(pct_vs_200dma, (int, float)) else None
    off = off_52w_high if isinstance(off_52w_high, (int, float)) else None

    at_range_top = isinstance(rp, (int, float)) and rp >= _RANGE_HIGH_PCTILE
    at_range_low = isinstance(rp, (int, float)) and rp <= _RANGE_LOW_PCTILE
    fast_stretched = (isinstance(p20, (int, float)) and p20 >= _HOT_PCT_VS_20DMA)
    fresh_rip = isinstance(r10, (int, float)) and r10 >= _RIP_RET_10D
    slow_extended = isinstance(p200, (int, float)) and p200 >= _EXTENDED_PCT_VS_200DMA
    pinned_at_high = isinstance(off, (int, float)) and off >= _NEAR_HIGH_OFF_52W

    sector_cooling = heat.get("heat") in _COOLING_HEAT
    sector_not_clean = heat.get("clean_entry_flag") is False

    # CHASE = at the top of its own recent range AND a fresh vertical move (the buy-high failure).
    is_chase = at_range_top and (fresh_rip or fast_stretched)
    # EXTENDED = stretched (fast or slow, or pinned at the 52w high) but NOT a fresh spike.
    is_extended = (fast_stretched or slow_extended or pinned_at_high) and not is_chase

    if is_chase:
        bits = []
        if fresh_rip:
            bits.append(f"+{r10 * 100:.0f}% in 10d")
        if fast_stretched:
            bits.append(f"+{p20:.0f}% vs 20dma")
        if at_range_top:
            bits.append(f"{rp:.0f}th pctile of 60d range")
        notes.append("CHASE — buying the top of the range on a fresh rip (" + ", ".join(bits) + ")")
    elif is_extended:
        if pinned_at_high:
            notes.append(f"extended — pinned near the 52w high ({off:.0f}% off)")
        if slow_extended:
            notes.append(f"extended — +{p200:.0f}% vs 200dma")
        if fast_stretched and not slow_extended and not pinned_at_high:
            notes.append(f"extended — +{p20:.0f}% vs 20dma")
    elif at_range_low:
        notes.append(f"pullback — {rp:.0f}th pctile of 60d range (a dip, not a chase)")

    if sector_cooling:
        notes.append(f"sector {heat.get('heat')} — entering a fading tailwind")
    if sector_not_clean and not sector_cooling:
        notes.append("sector_pulse: not a clean entry for this theme")
    if heat.get("heat") in _HEATING_HEAT and heat.get("clean_entry_flag") is True:
        notes.append(f"sector {heat.get('heat')} + clean entry — the entry the doctrine wants")

    # ── the composed verdict (advisory) ──
    have_any = bool(sources)
    if not have_any:
        verdict = "unknown"
    elif is_chase:
        verdict = "chase"
    elif is_extended and (sector_cooling or sector_not_clean):
        verdict = "wait"                 # extended AND no sector tailwind → the worst entry, composed
    elif sector_cooling:
        verdict = "cooling_sector"
    elif is_extended:
        verdict = "extended"
    elif fm.get("last") is None and pct_vs_200dma is None and off_52w_high is None and not heat:
        verdict = "unknown"              # only a source that carried no usable field
    else:
        verdict = "clean"

    return {"ticker": t, "verdict": verdict, "notes": notes, "metrics": metrics,
            "sources": sorted(set(sources)), "as_of": as_of}
