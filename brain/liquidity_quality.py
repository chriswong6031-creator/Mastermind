"""brain/liquidity_quality.py — the liquidity-QUALITY classifier (Incident Wave W-I, task 2).

WHY THIS MODULE EXISTS
----------------------
On 2026-07-01 every US book bought into a semis breakdown partly on the strength of a
``liquidity_overlay = "expanding"`` label.  That label is a PURE quantity read: the macro
engine computes ``net_liquidity = WALCL − RRP − TGA`` and takes its 20-day rate-of-change
(``engine/regime.py``); a +25bn RoC prints "expanding" with NO regard for *how* the
liquidity is expanding.  On 07-01 the +68.9bn RoC was one-day base-effect noise on a flat,
forward-filled WALCL, and its composition was Treasury-cash drawdown (−TGA) against an
RRP facility already drained to $6.4bn — a MECHANICAL, stress-flavoured expansion, not
durable Fed easing.  The benign-expansion label the bot traded on was wrong.

This module adds the QUALITY dimension the overlay lacks.  It is STANDALONE — it does NOT
wire into ``budget()`` here (task 6 wires it); it only classifies.

WHAT IT DECIDES
---------------
``classify(series_fn) -> {label, components, asof}`` with

    label ∈ {benign-expansion, stress-expansion, neutral-hollow, contracting, unknown}

from four component reads (each of which can independently degrade to ``unknown``):
  (a) QUANTITY   — net-liquidity (WALCL − RRP − TGA) 20-day RoC, in $bn.
  (b) COMPOSITION— share of the 20d RoC coming from dWALCL (Fed, BENIGN) vs
                   −dTGA / −dRRP (Treasury/plumbing, MECHANICAL).
  (c) RRP-BUFFER — RRPONTSYD level; < ~$100bn ⇒ the reserves cushion is exhausted (today
                   $6.4bn ⇒ TRUE) so further issuance drains reserves directly.
  (d) STRESS     — HY-OAS 20d change + z, and NFCI 4-week direction.  Credit CONFIRMING
                   (OAS widening / NFCI tightening) upgrades an expansion to stress.

CLASSIFICATION RULES (signals.md §2, verbatim)
----------------------------------------------
  * QUANTITY expanding (RoC ≥ +expand_bn) AND
        (composition MECHANICAL  OR  buffer EXHAUSTED  OR  credit CONFIRMING)
      ⇒ **stress-expansion**
  * QUANTITY expanding AND composition BENIGN AND credit CALM
      ⇒ **benign-expansion**
  * QUANTITY flat (|RoC| < expand_bn) AND buffer EXHAUSTED
      ⇒ **neutral-hollow**
  * QUANTITY contracting (RoC ≤ −expand_bn)
      ⇒ **contracting**
  * otherwise (flat, buffer NOT exhausted) ⇒ **neutral-hollow** (a plain flat read; the
    hollow qualifier is only meaningful when the buffer is out, but "flat" is not benign).

THE INVARIANT (governs every path here)
---------------------------------------
Missing / stale / wrong data may coarsen, freeze, or SHRINK — never un-cap, raise
authority, or flip direction.  Concretely for this classifier:

  * A missing series makes ITS component read ``unknown`` and NEVER benign.  If the
    QUANTITY read is unknown the whole label is ``unknown`` (a consumer that cannot even
    measure liquidity must no-op).  If only the stress/composition legs are unknown, an
    expansion degrades TOWARD stress — never toward benign — because the ONLY way to earn
    the benign label is to affirmatively prove benign composition AND calm credit.
  * The classifier is SHRINK-ONLY by construction: the honest failure of the incident was
    a FALSE benign; degrading toward ``unknown`` / ``stress`` / ``neutral-hollow`` is the
    conservative direction and is what task 6 will treat as no-op-or-shrink on offense.

ALL THRESHOLDS live in config/doctrine.yml ``liquidity_quality:`` (each an ``(unverified-
prior)``); the ``_FALLBACK`` dict below mirrors them for the PyYAML-absent / malformed-
block case, exactly as regime_frame's budget block does.

PUBLIC API
----------
* ``classify(series_fn) -> dict``      — the classifier; ``series_fn`` is a data-access
                                         callback (fixture-injectable) — see below.
* ``series_from_frames(...)``          — the DEFAULT ``series_fn`` for production: reads the
                                         vendored/Macro-Dashboard FRED+treasury stores.  Not
                                         used by tests (which inject a synthetic series_fn).

THE ``series_fn`` CONTRACT
--------------------------
``series_fn(name: str) -> Mapping[date-like, float] | pandas-Series | None``.  It is called
with the canonical names below; it returns a date→value mapping (a pandas Series, or a plain
dict keyed by ``YYYY-MM-DD``), or ``None`` when that series is unavailable.  Units:
  * ``"walcl_bn"``  Fed balance sheet, $bn      (weekly; ffilled to a daily grid here)
  * ``"rrp_bn"``    ON-RRP take-up, $bn         (daily)
  * ``"tga_bn"``    Treasury General Account, $bn (daily)
  * ``"hy_oas"``    ICE BofA HY OAS, percent    (daily)
  * ``"nfci"``      Chicago Fed NFCI, level     (weekly)
Injecting ``series_fn`` is what keeps the tests pure (the shared vendor store mutates live —
fixture-INJECT, never live-read in a test).
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

# The module lives at brain/liquidity_quality.py; the repo root is two levels up.
_ROOT: Path = Path(__file__).resolve().parent.parent
_DOCTRINE_PATH: Path = _ROOT / "config" / "doctrine.yml"

# Canonical series names the classifier requests from series_fn (see the contract above).
NET_LIQ_LEGS: tuple[str, str, str] = ("walcl_bn", "rrp_bn", "tga_bn")

# Fallback thresholds — MUST mirror config/doctrine.yml's `liquidity_quality:` block.
# Used ONLY when PyYAML is absent or the block is malformed; the yml is the source of truth.
_FALLBACK: dict[str, Any] = {
    "roc_window_d": 20,          # net-liquidity rate-of-change window (trading days)
    "expand_bn": 25.0,          # |RoC| >= this = expanding/contracting; below = flat
    "rrp_buffer_bn": 100.0,     # RRP level below this = cushion exhausted
    "benign_walcl_share": 0.50, # dWALCL share of the gross 20d move >= this = BENIGN composition
    "oas_widen_pp": 0.10,       # HY-OAS 20d change >= this (pp) = credit confirming stress
    "oas_z_hot": 1.0,           # OR HY-OAS 20d z >= this = credit confirming stress
    "nfci_window_d": 20,        # NFCI direction lookback (~4 weeks of weekly prints)
}


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------
def _cfg() -> dict[str, Any]:
    """Read config/doctrine.yml ``liquidity_quality:``, merged over the _FALLBACK prior.

    Lazy and defensive — a missing PyYAML / config / block leaves every key at its
    (unverified-prior) fallback so a bare CI or a malformed yml never breaks a build.
    """
    cfg = dict(_FALLBACK)
    try:
        import yaml  # lazy — not on the hot path; gracefully absent in bare CI
        if _DOCTRINE_PATH.exists():
            block = (yaml.safe_load(_DOCTRINE_PATH.read_text()) or {}).get("liquidity_quality")
            if isinstance(block, dict):
                cfg.update({k: v for k, v in block.items() if v is not None})
    except Exception:  # noqa: BLE001 — degrade to the fallback prior
        pass
    return cfg


# ---------------------------------------------------------------------------
# series handling — turn whatever series_fn returns into a sorted (date, value) list
# ---------------------------------------------------------------------------
def _as_pairs(raw: Any) -> Optional[list[tuple[date, float]]]:
    """Normalise a series_fn return into a date-sorted list of (date, float) pairs.

    Accepts a pandas Series (any date-like index), a plain dict keyed by ``YYYY-MM-DD``
    (or date objects), or None.  Returns None when the series is absent or unusable —
    which the caller MUST treat as an ``unknown`` component (never a benign default).
    """
    if raw is None:
        return None
    items: list[tuple[Any, Any]]
    # pandas Series (duck-typed to avoid a hard pandas import in this module).
    if hasattr(raw, "items") and hasattr(raw, "index") and hasattr(raw, "values"):
        items = list(raw.items())
    elif isinstance(raw, Mapping):
        items = list(raw.items())
    else:
        return None
    pairs: list[tuple[date, float]] = []
    for k, v in items:
        d = _to_date(k)
        if d is None:
            continue
        try:
            fv = float(v)
        except (TypeError, ValueError):
            continue
        if fv != fv:  # NaN
            continue
        pairs.append((d, fv))
    if not pairs:
        return None
    pairs.sort(key=lambda p: p[0])
    return pairs


def _to_date(k: Any) -> Optional[date]:
    """Coerce a mapping key / index label to a ``datetime.date``; None if impossible."""
    if isinstance(k, date):
        return k
    # pandas Timestamp / datetime expose .date()
    if hasattr(k, "date") and callable(getattr(k, "date")):
        try:
            return k.date()
        except Exception:  # noqa: BLE001
            return None
    try:
        return date.fromisoformat(str(k)[:10])
    except (ValueError, TypeError):
        return None


def _ffill_daily(
    pairs: list[tuple[date, float]], end: Optional[date] = None
) -> list[tuple[date, float]]:
    """Forward-fill *pairs* onto a dense BUSINESS-day grid (Mon–Fri) from first date to *end*.

    Weekly series (WALCL, NFCI) must be aligned to the daily RRP/TGA grid before a
    trading-day RoC can be taken.  The grid is business-day (weekends dropped) so a
    fixed-lag ``diff(window)`` counts ~calendar-weeks in trading-day steps — matching the
    engine's ``pandas.date_range(freq='B').diff(20)`` net-liquidity reconstruction (the
    exact method that produced the 07-01 +68.9bn "expanding" print).  Federal holidays are
    NOT modelled (a couple of extra grid steps per year shifts the 20d window by <1 print;
    the ±25bn threshold is far coarser than that noise).

    ``end`` (default = the series' own last date) lets a WEEKLY leg be ffilled PAST its last
    native print out to a common horizon — mirroring the engine carrying a stale weekly
    WALCL forward across the daily RRP/TGA grid.  ``end`` never precedes the series start.
    """
    from datetime import timedelta

    out: list[tuple[date, float]] = []
    cur = pairs[0][0]
    if end is None or end < pairs[-1][0]:
        end = pairs[-1][0]
    idx = 0
    last_val = pairs[0][1]
    while cur <= end:
        # advance the source pointer to the most recent point at or before `cur`
        while idx + 1 < len(pairs) and pairs[idx + 1][0] <= cur:
            idx += 1
        if pairs[idx][0] <= cur:
            last_val = pairs[idx][1]
        if cur.weekday() < 5:  # business-day grid only (drop Sat/Sun)
            out.append((cur, last_val))
        cur = cur + timedelta(days=1)
    return out


def _roc(daily: list[tuple[date, float]], window: int) -> Optional[float]:
    """Level change over the last *window* business-day grid steps; None if too short."""
    if len(daily) <= window:
        return None
    return daily[-1][1] - daily[-1 - window][1]


def _zscore(daily: list[tuple[date, float]], window: int) -> Optional[float]:
    """Trailing z of the last value vs a rolling mean/std of the level (min ~40 obs)."""
    import statistics

    lookback = max(60, window * 3)
    vals = [v for _, v in daily[-lookback:]]
    if len(vals) < 40:
        return None
    try:
        mu = statistics.fmean(vals)
        sd = statistics.pstdev(vals)
    except statistics.StatisticsError:
        return None
    if sd <= 0:
        return None
    return (vals[-1] - mu) / sd


# ---------------------------------------------------------------------------
# component reads — each returns a small dict and NEVER raises
# ---------------------------------------------------------------------------
def _net_liquidity_components(
    series_fn: Callable[[str], Any], cfg: dict[str, Any]
) -> dict[str, Any]:
    """QUANTITY (RoC) + COMPOSITION + RRP-BUFFER, in one pass over the three balance legs.

    Returns keys:
      roc_bn        float | None   net-liquidity 20d RoC ($bn); None ⇒ quantity unknown
      quantity      str            'expanding' | 'contracting' | 'flat' | 'unknown'
      dWALCL_bn, dnegRRP_bn, dnegTGA_bn  float | None  the 20d component moves
      walcl_share   float | None   |dWALCL| / (Σ|component moves|); None if any leg missing
      composition   str            'benign' | 'mechanical' | 'unknown'
      rrp_level_bn  float | None   latest RRP level
      buffer        str            'exhausted' | 'ample' | 'unknown'
      asof          date  | None   the daily-grid date the reads are anchored on
    """
    window = int(cfg["roc_window_d"])
    expand = float(cfg["expand_bn"])

    legs: dict[str, Optional[list[tuple[date, float]]]] = {}
    for name in NET_LIQ_LEGS:
        try:
            legs[name] = _as_pairs(series_fn(name))
        except Exception:  # noqa: BLE001 — a throwing series_fn is a missing series
            legs[name] = None

    out: dict[str, Any] = {
        "roc_bn": None, "quantity": "unknown",
        "dWALCL_bn": None, "dnegRRP_bn": None, "dnegTGA_bn": None,
        "walcl_share": None, "composition": "unknown",
        "rrp_level_bn": None, "buffer": "unknown", "asof": None,
    }

    # ---- RRP buffer (independent of the RoC; usable even if WALCL/TGA are missing) ----
    rrp_pairs = legs.get("rrp_bn")
    if rrp_pairs:
        lvl = rrp_pairs[-1][1]
        out["rrp_level_bn"] = lvl
        out["buffer"] = "exhausted" if lvl < float(cfg["rrp_buffer_bn"]) else "ample"

    # ---- QUANTITY + COMPOSITION need all three legs ffilled onto a common daily grid ----
    if not all(legs.get(n) for n in NET_LIQ_LEGS):
        return out  # quantity/composition stay 'unknown' — never benign

    # The grid runs to the LATEST date any leg publishes (the daily RRP/TGA legs lead the
    # weekly WALCL), with the weekly leg forward-filled OUT to that horizon — exactly the
    # engine's stale-WALCL ffill.  The grid starts at the latest leg START (so every leg has
    # a real value from day one; no leading NaN).
    end = max(p[-1][0] for p in legs.values() if p)          # type: ignore[index]
    start = max(p[0][0] for p in legs.values() if p)         # type: ignore[index]
    if start >= end:
        return out
    daily = {n: _ffill_daily(legs[n], end=end) for n in NET_LIQ_LEGS}  # type: ignore[arg-type]
    grid = {n: [(dt, v) for dt, v in daily[n] if start <= dt <= end] for n in NET_LIQ_LEGS}
    out["asof"] = end

    net = [(w[0], w[1] - r[1] - t[1])
           for w, r, t in zip(grid["walcl_bn"], grid["rrp_bn"], grid["tga_bn"])]
    roc = _roc(net, window)
    out["roc_bn"] = roc
    if roc is not None:
        out["quantity"] = (
            "expanding" if roc >= expand
            else "contracting" if roc <= -expand
            else "flat"
        )

    # composition of the SAME 20d window
    dW = _roc(grid["walcl_bn"], window)
    dR = _roc(grid["rrp_bn"], window)
    dT = _roc(grid["tga_bn"], window)
    if None not in (dW, dR, dT):
        d_walcl, d_neg_rrp, d_neg_tga = dW, -dR, -dT  # type: ignore[operator]
        out["dWALCL_bn"], out["dnegRRP_bn"], out["dnegTGA_bn"] = d_walcl, d_neg_rrp, d_neg_tga
        gross = abs(d_walcl) + abs(d_neg_rrp) + abs(d_neg_tga)
        if gross > 0:
            share = abs(d_walcl) / gross
            out["walcl_share"] = share
            out["composition"] = (
                "benign" if share >= float(cfg["benign_walcl_share"]) else "mechanical"
            )
    return out


def _credit_stress(series_fn: Callable[[str], Any], cfg: dict[str, Any]) -> dict[str, Any]:
    """STRESS overlay: HY-OAS 20d change + z, and NFCI direction.

    Returns keys:
      oas_chg_pp   float | None    HY-OAS 20d change (pp)
      oas_z        float | None    HY-OAS trailing z
      oas_level    float | None    latest HY-OAS level
      nfci_dir     str             'tightening' | 'easing' | 'flat' | 'unknown'
      credit       str             'confirming' | 'calm' | 'unknown'

    'confirming' fires on OAS widening (>= oas_widen_pp OR z >= oas_z_hot) OR NFCI
    tightening.  When NO stress series is available, credit is 'unknown' (NOT 'calm') —
    the classifier must not manufacture the calm read the benign label requires.
    """
    out: dict[str, Any] = {
        "oas_chg_pp": None, "oas_z": None, "oas_level": None,
        "nfci_dir": "unknown", "credit": "unknown",
    }
    have_any = False
    widening = False

    try:
        oas_pairs = _as_pairs(series_fn("hy_oas"))
    except Exception:  # noqa: BLE001
        oas_pairs = None
    if oas_pairs:
        have_any = True
        oas_daily = _ffill_daily(oas_pairs)
        out["oas_level"] = oas_daily[-1][1]
        chg = _roc(oas_daily, int(cfg["roc_window_d"]))
        z = _zscore(oas_daily, int(cfg["roc_window_d"]))
        out["oas_chg_pp"] = chg
        out["oas_z"] = z
        if (chg is not None and chg >= float(cfg["oas_widen_pp"])) or (
            z is not None and z >= float(cfg["oas_z_hot"])
        ):
            widening = True

    try:
        nfci_pairs = _as_pairs(series_fn("nfci"))
    except Exception:  # noqa: BLE001
        nfci_pairs = None
    tightening = False
    if nfci_pairs:
        have_any = True
        nfci_daily = _ffill_daily(nfci_pairs)
        dchg = _roc(nfci_daily, int(cfg["nfci_window_d"]))
        if dchg is not None:
            out["nfci_dir"] = (
                "tightening" if dchg > 0 else "easing" if dchg < 0 else "flat"
            )
            if dchg > 0:  # a RISING NFCI = tightening financial conditions
                tightening = True

    if have_any:
        out["credit"] = "confirming" if (widening or tightening) else "calm"
    return out


# ---------------------------------------------------------------------------
# the classifier
# ---------------------------------------------------------------------------
def classify(series_fn: Callable[[str], Any]) -> dict[str, Any]:
    """Classify liquidity QUALITY from *series_fn* — the sole public decision.

    ``series_fn(name)`` returns a date→value series (pandas Series / dict) or None per the
    contract in the module docstring.  Returns::

        {label, components, asof}

    with label ∈ {benign-expansion, stress-expansion, neutral-hollow, contracting, unknown}.

    INVARIANT: a missing QUANTITY read ⇒ label 'unknown' (cannot even measure liquidity).
    An expansion earns 'benign-expansion' ONLY when composition is affirmatively BENIGN and
    credit is affirmatively CALM and the buffer is not exhausted; any unknown among those
    leaves the expansion at 'stress-expansion' — degrade toward stress, never toward benign.
    """
    cfg = _cfg()
    liq = _net_liquidity_components(series_fn, cfg)
    stress = _credit_stress(series_fn, cfg)

    components = {**liq, **stress}
    quantity = liq["quantity"]
    composition = liq["composition"]
    buffer_state = liq["buffer"]
    credit = stress["credit"]

    label = _label(quantity, composition, buffer_state, credit)
    return {"label": label, "components": components, "asof": liq["asof"]}


def _label(quantity: str, composition: str, buffer_state: str, credit: str) -> str:
    """Apply the signals.md §2 rule table.  Pure — no I/O, fully unit-testable."""
    # 0) Cannot even measure liquidity → unknown (consumers no-op).
    if quantity == "unknown":
        return "unknown"

    # 1) Contracting is contracting regardless of quality legs.
    if quantity == "contracting":
        return "contracting"

    # 2) Expanding: benign ONLY if affirmatively benign composition AND calm credit AND an
    #    un-exhausted buffer; ANY stress/mechanical/exhausted/unknown → stress-expansion.
    if quantity == "expanding":
        mechanical = composition == "mechanical"
        exhausted = buffer_state == "exhausted"
        confirming = credit == "confirming"
        if mechanical or exhausted or confirming:
            return "stress-expansion"
        # earn benign only with POSITIVE benign+calm reads (unknowns fall through to stress)
        if composition == "benign" and credit == "calm":
            return "benign-expansion"
        return "stress-expansion"  # composition/credit unknown → conservative

    # 3) Flat quantity.  An exhausted buffer under a flat RoC is the "hollow" read; an
    #    un-exhausted flat is a plain neutral (still NOT benign).
    return "neutral-hollow"


# ---------------------------------------------------------------------------
# DEFAULT production series_fn — reads the FRED / treasury stores.  NOT used by tests.
# ---------------------------------------------------------------------------
_FRED_DIRS: tuple[Path, ...] = (
    _ROOT / "vendor" / "macro" / "data" / "fred",
    Path("/Users/chriswong/Documents/Cluade/Macro Dashboard/data/fred"),
)
_TREASURY_DIRS: tuple[Path, ...] = (
    _ROOT / "vendor" / "macro" / "data" / "treasury",
    Path("/Users/chriswong/Documents/Cluade/Macro Dashboard/data/treasury"),
)
# name -> (search dirs, filename, column, scale-to-$bn)
_SERIES_MAP: dict[str, tuple[tuple[Path, ...], str, str, float]] = {
    "walcl_bn": (_FRED_DIRS, "WALCL.parquet", "fed_balance_sheet", 1 / 1000.0),
    "rrp_bn":   (_FRED_DIRS, "RRPONTSYD.parquet", "on_rrp", 1.0),
    "tga_bn":   (_TREASURY_DIRS, "tga.parquet", "tga_mn", 1 / 1000.0),
    "hy_oas":   (_FRED_DIRS, "BAMLH0A0HYM2.parquet", "hy_oas", 1.0),
    "nfci":     (_FRED_DIRS, "NFCI.parquet", "nfci", 1.0),
}


def series_from_frames(name: str) -> Any:
    """DEFAULT production ``series_fn``: read a named series from the FRED/treasury store.

    Returns a pandas Series (date-indexed, scaled to $bn where relevant) or None on any
    missing file / column / pandas-absent condition — so a data gap degrades the matching
    component to 'unknown', per the invariant.  Tests do NOT call this (they inject a
    synthetic series_fn); it exists so task 6 can wire a zero-arg default.
    """
    spec = _SERIES_MAP.get(name)
    if spec is None:
        return None
    dirs, fname, col, scale = spec
    try:
        import pandas as pd  # lazy — the classifier core has no pandas dependency
    except Exception:  # noqa: BLE001
        return None
    for d in dirs:
        p = d / fname
        if not p.exists():
            continue
        try:
            df = pd.read_parquet(p)
            if col not in df.columns:
                continue
            return df[col].astype(float) * scale
        except Exception:  # noqa: BLE001 — a corrupt frame is a missing series
            continue
    return None


def series_from_json_fixtures(fixture_dir: str | Path) -> Callable[[str], Any]:
    """Build a ``series_fn`` that reads the trimmed JSON fixtures in *fixture_dir*.

    Fixture files are ``<name>.json`` mapping ``YYYY-MM-DD -> value`` (already scaled to the
    classifier's units).  Missing files return None (→ 'unknown').  This is the replay-test
    data path; keeping it in the module (not the test) lets task 6 reuse the exact fixtures.
    """
    fdir = Path(fixture_dir)
    _files = {
        "walcl_bn": "walcl_bn.json", "rrp_bn": "rrp_bn.json", "tga_bn": "tga_bn.json",
        "hy_oas": "hy_oas.json", "nfci": "nfci.json",
    }

    def _fn(name: str) -> Any:
        fname = _files.get(name)
        if fname is None:
            return None
        p = fdir / fname
        if not p.exists():
            return None
        try:
            return json.loads(p.read_text())  # dict[YYYY-MM-DD] -> float
        except Exception:  # noqa: BLE001
            return None

    return _fn
