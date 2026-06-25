"""Desk-lever A/B harness — the cheap, deterministic test of whether a multi-seat LLM desk
would add alpha BEFORE we pay to build it.

The Flagship gate (engine confluence + research Conviction Index >= 60 → auto-buy) earned its
keep on day one less well than the holistic US Brain. The hypothesis is that passing a fixed gate
with NO entry-timing / market-context / concentration / no-ETF discipline is the leak. This module
A/B-tests that hypothesis with FOUR deterministic proxies for the proposed desk levers — each a
pure function over prod's stored decision-input records (``data/shadow/inputs/<asof>.json``), so the
whole experiment is leakage-free and re-runnable with no LLM and no network:

  * L1  ``apply_no_etf``           — drop broad sector/index ETFs from the alpha sleeve.
  * L2  ``apply_concentrated_topN`` — keep only the top-N names by Conviction Index, renormalize.
  * L3  ``apply_timing_gated``     — withhold names with poor entry technicals (extended / weak RS /
                                      'avoid' urgency) read leakage-free from the stockdata JSONs.
  * L4  ``apply_autonomous_clone`` — replay the US Brain's ACTUAL submitted book for the day.
  * combo ``apply_desk_proxy``     — L1 ∘ L2 ∘ L3 composed (the full deterministic desk).

ISOLATION CONTRACT (mirrors portfolio.shadow_books): this module is NEVER imported by the live
run-path. It only ever READS ``data/shadow/inputs/<asof>.json`` (read-only), the autonomous book's
``decisions.jsonl`` (read-only), and the vendored stockdata JSONs (read-only). It writes nothing on
import; the runner writes only under ``data/shadow/desk_ab/``. It reuses ``shadow_books`` primitives
(``read_inputs``, ``apply_policy``, the isolated paper-account + forward grader) so the books are
graded EXACTLY like the existing shadow books and stay directly comparable. Best-effort throughout —
every function tolerates missing fields / prices / files and never raises.
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from portfolio import shadow_books as S

_ROOT = Path(__file__).resolve().parent.parent
_DESK_AB = _ROOT / "data" / "shadow" / "desk_ab"
_AUTONOMOUS_DECISIONS = _ROOT / "data" / "portfolios" / "autonomous" / "decisions.jsonl"
_AUTONOMOUS_LATEST = _ROOT / "data" / "portfolios" / "autonomous" / "latest.json"
_STOCKDATA = _ROOT / "vendor" / "macro" / "site" / "stockdata"

_DEFAULT_TOP_N = 8   # the pre-registered concentration cap (AB_EXPERIMENT.md §2.3, N=8)


# ─────────────────────────────────────────────────────────────────────────────
# the desk-lever policies (display order; 'prod' first as the baseline)
# ─────────────────────────────────────────────────────────────────────────────
# Each carries the same {committee, calibration, sizing} knobs shadow_books uses for SIZING — the
# desk levers are FILTERS layered on top of prod sizing (committee+calibration on, research sizing),
# so any forward edge is attributable to the lever, not to a sizing change.
_PROD_SIZING = {"committee": True, "calibration": True, "sizing": "research"}

POLICIES = [
    {"id": "prod", "label": "Prod — full stack (baseline)",
     "desc": "The live Flagship gate: every FORGE-confirmed name at prod size. The control arm.",
     "filter": None, **_PROD_SIZING},
    {"id": "no_etf", "label": "L1 — No broad ETFs",
     "desc": "Prod sizing minus broad sector/index/factor ETFs (etf_universe allowlist).",
     "filter": "no_etf", **_PROD_SIZING},
    {"id": "concentrated_topN", "label": f"L2 — Concentration top-{_DEFAULT_TOP_N}",
     "desc": f"Keep only the top-{_DEFAULT_TOP_N} names by Conviction Index, renormalized to gross<=1.",
     "filter": "top_n", "n": _DEFAULT_TOP_N, **_PROD_SIZING},
    {"id": "timing_gated", "label": "L3 — Entry-timing gate",
     "desc": "Withhold names with poor entry technicals (extended / weak RS / 'avoid' urgency).",
     "filter": "timing", **_PROD_SIZING},
    {"id": "autonomous_clone", "label": "L4 — Autonomous-clone",
     "desc": "Replay the US Brain's actual submitted book for the day (its weights, verbatim).",
     "filter": "clone", **_PROD_SIZING},
    {"id": "desk_proxy", "label": "Desk proxy (L1+L2+L3)",
     "desc": "The full deterministic desk: no-ETF, then top-N, then timing gate, composed.",
     "filter": "desk_proxy", "n": _DEFAULT_TOP_N, **_PROD_SIZING},
]

_POLICY_BY_ID = {p["id"]: p for p in POLICIES}


# ─────────────────────────────────────────────────────────────────────────────
# L1 — no broad ETFs
# ─────────────────────────────────────────────────────────────────────────────
def _is_etf(ticker: str) -> bool:
    """True iff the ticker is a broad sector/index/factor ETF (the etf_universe allowlist). Best-
    effort: if the universe module can't load, nothing is treated as an ETF (fail-open, never raise)."""
    try:
        from portfolio import etf_universe
        return bool(etf_universe.is_etf(ticker))
    except Exception:  # noqa: BLE001
        return False


def apply_no_etf(inputs: list, policy: dict | None = None) -> dict:
    """L1: prod weights with every broad ETF dropped (zero weight), then renormalized to gross<=1.
    Long-only, leakage-free (membership is a static allowlist). Returns {ticker: target_weight}."""
    pol = policy or _PROD_SIZING
    kept = [r for r in (inputs or []) if not _is_etf((r.get("ticker") or "").upper())]
    return S.apply_policy(pol, kept)


# ─────────────────────────────────────────────────────────────────────────────
# L2 — concentration (top-N by Conviction Index)
# ─────────────────────────────────────────────────────────────────────────────
def _conviction(r: dict) -> float:
    """The Conviction Index used to rank names: the blended `combined` score, falling back to
    research_score, then engine_score, then 0 (so a record missing the field sorts last, never raises)."""
    for k in ("combined", "research_score", "engine_score"):
        v = r.get(k)
        if isinstance(v, (int, float)):
            return float(v)
    return 0.0


def apply_concentrated_topN(inputs: list, n: int = _DEFAULT_TOP_N, policy: dict | None = None) -> dict:
    """L2: keep only the top-`n` FORGE-confirmed names by Conviction Index, drop the rest, then size
    with prod and renormalize to gross<=1. Ties are broken deterministically by ticker. Returns
    {ticker: target_weight}."""
    pol = policy or _PROD_SIZING
    confirmed = [r for r in (inputs or []) if r.get("forge_confirmed")]
    # sort by conviction desc, ticker asc (stable, deterministic) and keep the head
    ranked = sorted(confirmed, key=lambda r: (-_conviction(r), (r.get("ticker") or "").upper()))
    keep = ranked[: max(0, int(n))]
    return S.apply_policy(pol, keep)


# ─────────────────────────────────────────────────────────────────────────────
# L3 — entry-timing gate (poor entry technicals → withhold)
# ─────────────────────────────────────────────────────────────────────────────
# Thresholds for the timing veto. Read leakage-free from the per-name stockdata JSON, whose `asof`
# is the build date (<= the decision asof). If a name has NO stockdata file (the common case in a
# fresh checkout / test sandbox), the gate FAILS OPEN — the name is kept — so L3 degrades to prod
# rather than silently dropping the whole book. See GT3 for the field-availability matrix.
_EXT_PCT_VS_200DMA = 30.0    # >= 30% above the 200dma → extended
_WEAK_RS_PCTILE = 50.0       # RS vs SPY below the median → weak relative strength
_BAD_EXT_GRADES = {"stretched", "parabolic", "extended"}
_BAD_URGENCY = {"avoid"}     # ladder.entry.urgency that says "do not chase"


def _stockdata(ticker: str) -> dict:
    """The vendored per-name stockdata JSON, or {} if absent/corrupt. Read-only; its `asof` is the
    build date so every field is computed from prices <= asof (leakage-free)."""
    t = (ticker or "").upper().strip()
    if not t:
        return {}
    try:
        d = json.loads((_STOCKDATA / f"{t}.json").read_text())
        return d if isinstance(d, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _timing_ok(ticker: str, sd: dict | None = None) -> bool:
    """True iff the name's entry technicals are acceptable (not extended, not weak-RS, not 'avoid').
    FAILS OPEN (returns True) when no stockdata is available — a missing signal must never silently
    drop a name. Pure read over leakage-free fields; never raises.

    NOTE / approximation (GT3): the `distribution` sub-lever from the design doc is DEFERRED — it
    requires running ``lenses.full()`` (not a raw stockdata field), which is out of scope for a
    pure, network-free replay. We gate on the directly-available fields only:
      * conviction.ext.grade ∈ {stretched, parabolic, extended}  → extended
      * conviction.ext.parabolic == True                         → extended
      * tech.pct_vs_200dma >= 30                                 → extended
      * ladder.entry.urgency == 'avoid'                          → bad timing
      * alpha.rs < 50  (RS pctile vs SPY below median)           → weak RS
    """
    sd = sd if sd is not None else _stockdata(ticker)
    if not sd:
        return True   # no timing data → keep the name (fail-open)
    try:
        conv = sd.get("conviction") or {}
        ext = conv.get("ext") or conv.get("extension") or {}
        grade = str(ext.get("grade") or "").lower()
        if grade in _BAD_EXT_GRADES or bool(ext.get("parabolic")):
            return False
        tech = sd.get("tech") or {}
        pv200 = tech.get("pct_vs_200dma")
        if isinstance(pv200, (int, float)) and pv200 >= _EXT_PCT_VS_200DMA:
            return False
        ladder = sd.get("ladder") or {}
        entry = ladder.get("entry") or {}
        if str(entry.get("urgency") or "").lower() in _BAD_URGENCY:
            return False
        alpha = sd.get("alpha") or {}
        rs = alpha.get("rs")
        if isinstance(rs, (int, float)) and rs < _WEAK_RS_PCTILE:
            return False
    except Exception:  # noqa: BLE001
        return True
    return True


def apply_timing_gated(inputs: list, policy: dict | None = None) -> dict:
    """L3: drop names whose entry technicals are poor (extended / weak RS / 'avoid'), size the rest
    with prod, renormalize. Names with no stockdata file are KEPT (fail-open). Returns
    {ticker: target_weight}."""
    pol = policy or _PROD_SIZING
    kept = [r for r in (inputs or []) if _timing_ok((r.get("ticker") or "").upper())]
    return S.apply_policy(pol, kept)


# ─────────────────────────────────────────────────────────────────────────────
# L4 — autonomous-clone (replay the US Brain's actual submitted book)
# ─────────────────────────────────────────────────────────────────────────────
def _clone_book_for(asof: str) -> dict:
    """{ticker: weight} the US Brain actually submitted for `asof`, read leakage-free from the
    autonomous book's decisions.jsonl (the per-day, dated submission log). Falls back to latest.json
    ONLY when its `as_of` matches `asof` exactly (never replays a future book onto a past date).
    Returns {} when no matching submission exists. Never raises."""
    asof = str(asof)[:10]
    out: dict = {}
    # primary: the dated decisions ledger (one row per submitted book)
    try:
        rows = [json.loads(l) for l in _AUTONOMOUS_DECISIONS.read_text().splitlines() if l.strip()]
        for row in rows:
            if str(row.get("asof") or "")[:10] != asof:
                continue
            for h in row.get("holdings") or []:
                tk = (h.get("ticker") or "").upper().strip()
                w = h.get("weight")
                if tk and isinstance(w, (int, float)) and w > 0:
                    out[tk] = out.get(tk, 0.0) + float(w)
    except Exception:  # noqa: BLE001
        pass
    # fallback: latest.json, but ONLY if it is the book for exactly this asof (no look-ahead)
    if not out:
        try:
            d = json.loads(_AUTONOMOUS_LATEST.read_text())
            if str(d.get("as_of") or "")[:10] == asof:
                for h in d.get("positions") or []:
                    tk = (h.get("ticker") or "").upper().strip()
                    w = h.get("weight")
                    if tk and isinstance(w, (int, float)) and w > 0:
                        out[tk] = out.get(tk, 0.0) + float(w)
        except Exception:  # noqa: BLE001
            pass
    return out


def apply_autonomous_clone(inputs: list, asof: str = "", policy: dict | None = None) -> dict:
    """L4: the US Brain's submitted book for `asof`, verbatim (its own weights), long-only and
    renormalized to gross<=1. `inputs` is unused (the clone does not depend on prod's candidate set)
    but kept in the signature so every policy is callable uniformly. Returns {ticker: target_weight}."""
    raw = _clone_book_for(asof)
    raw = {k: round(v, 4) for k, v in raw.items() if v and v > 0}
    total = sum(raw.values())
    if total > 1.0:
        raw = {k: round(v / total, 4) for k, v in raw.items()}
    return raw


# ─────────────────────────────────────────────────────────────────────────────
# combo — desk proxy (L1 ∘ L2 ∘ L3)
# ─────────────────────────────────────────────────────────────────────────────
def apply_desk_proxy(inputs: list, n: int = _DEFAULT_TOP_N, policy: dict | None = None) -> dict:
    """The full deterministic desk: filter ETFs (L1), then poor-timing names (L3), THEN keep the
    top-`n` by conviction (L2), size with prod and renormalize. Order matters — the concentration cap
    is applied LAST so it caps the surviving (non-ETF, well-timed) names, not the raw set. Returns
    {ticker: target_weight}."""
    pol = policy or _PROD_SIZING
    kept = [
        r for r in (inputs or [])
        if r.get("forge_confirmed")
        and not _is_etf((r.get("ticker") or "").upper())
        and _timing_ok((r.get("ticker") or "").upper())
    ]
    ranked = sorted(kept, key=lambda r: (-_conviction(r), (r.get("ticker") or "").upper()))
    keep = ranked[: max(0, int(n))]
    return S.apply_policy(pol, keep)


# ─────────────────────────────────────────────────────────────────────────────
# dispatch — {ticker: weight} for any policy over one day's inputs
# ─────────────────────────────────────────────────────────────────────────────
def apply_desk_policy(policy: dict, inputs: list, asof: str = "") -> dict:
    """Route a policy dict (from POLICIES) to its filter and return {ticker: target_weight}.
    Long-only, gross<=1, leakage-free, never raises."""
    f = (policy or {}).get("filter")
    try:
        if f == "no_etf":
            return apply_no_etf(inputs, policy)
        if f == "top_n":
            return apply_concentrated_topN(inputs, int(policy.get("n", _DEFAULT_TOP_N)), policy)
        if f == "timing":
            return apply_timing_gated(inputs, policy)
        if f == "clone":
            return apply_autonomous_clone(inputs, asof, policy)
        if f == "desk_proxy":
            return apply_desk_proxy(inputs, int(policy.get("n", _DEFAULT_TOP_N)), policy)
        return S.apply_policy(policy, inputs)   # filter is None → plain prod sizing (the baseline)
    except Exception:  # noqa: BLE001
        return {}


# ─────────────────────────────────────────────────────────────────────────────
# forward grading — reuse brain.outcomes.label_thesis (leakage-free, same as prod)
# ─────────────────────────────────────────────────────────────────────────────
def grade_forward(ticker: str, entry_iso: str, entry_px: float | None,
                  asof: date | str | None = None, horizon_d: int = 21,
                  vs: str = "SPY") -> dict | None:
    """Grade ONE name forward vs SPY by delegating to brain.outcomes.label_thesis — the same
    leakage-free, triple-barrier labeler prod uses. Builds the minimal thesis dict outcomes expects
    (a rel_return falsifier anchored at `entry_iso`) and returns its label, or None on bad input.

    The look-ahead guard lives in label_thesis (`req_end = min(final_end, asof)`): no price with a
    date past `asof` is ever read. Never raises."""
    try:
        from brain import outcomes
        t = (ticker or "").upper().strip()
        if not t or not entry_iso:
            return None
        if isinstance(asof, str):
            asof = date.fromisoformat(str(asof)[:10])
        thesis = {
            "id": f"desk_ab:{t}:{str(entry_iso)[:10]}",
            "subject": t,
            "state_asof": str(entry_iso)[:10],
            "horizon_d": int(horizon_d),
            "entry_levels": ({"ticker": t, "price": entry_px} if entry_px else {"ticker": t}),
            "falsifier": {"check": {"kind": "rel_return", "subject_ticker": t,
                                    "vs": vs, "horizon_d": int(horizon_d), "op": "<",
                                    "threshold": -0.05}},
        }
        return outcomes.label_thesis(thesis, asof)
    except Exception:  # noqa: BLE001
        return None


# ─────────────────────────────────────────────────────────────────────────────
# driver — re-derive every desk book for `asof`, mark + grade forward, isolated under desk_ab/
# ─────────────────────────────────────────────────────────────────────────────
def run(asof: str, prices: dict | None = None, inputs: list | None = None) -> dict:
    """Re-derive every desk-lever book for `asof`, mark each forward, label its theses, and return a
    leaderboard. Reuses the shadow_books isolated paper-account + grader BUT redirects its book dir to
    ``data/shadow/desk_ab/books/`` so the desk books never collide with the attribution shadow books.
    Best-effort; never raises. Writes nothing — the runner persists the result."""
    asof = str(asof)[:10]
    inputs = inputs if inputs is not None else S.read_inputs(asof)
    targets_by_book = {p["id"]: apply_desk_policy(p, inputs, asof) for p in POLICIES}

    # Reuse the shadow_books isolated paper-account + grader, but redirect its book dir to the
    # desk_ab sandbox so the desk books never collide with the attribution shadow books. The swap is
    # held for the whole loop and ALWAYS restored in `finally` (even on a hard failure).
    _orig_books = S._BOOKS
    S._BOOKS = _DESK_AB / "books"
    try:
        needed: set = {"SPY"}
        for tw in targets_by_book.values():
            needed |= set(tw)
        for p in POLICIES:                      # also re-mark names still held from prior runs
            needed |= set(S._load_account(p["id"]).get("positions", {}))
        px = S._gather_prices(needed, prices)

        # Same empty-inputs LIQUIDATION guard as shadow_books.run: on a carried/quiet day (inputs==[])
        # the input-derived books (prod/L1/L2/L3/desk_proxy) all target {}, and an unguarded rebalance
        # would liquidate them. HOLD + re-mark instead. The autonomous-clone book is NOT input-derived
        # (its targets come from the US Brain's own submission for `asof`), so when it legitimately
        # changed its targets are non-empty and `targets or _has_inputs` rebalances it normally.
        _has_inputs = bool(inputs)
        books = {}
        for p in POLICIES:
            bid = p["id"]
            targets = targets_by_book[bid]
            try:
                if targets or _has_inputs:
                    S._rebalance(bid, targets, px, asof)
                nav_rows = S._nav_rows(bid)
                row = S._mark(bid, px, asof)
                nav_rows = [x for x in nav_rows if x.get("date") != asof] + [row]
                theses = _update_theses(bid, p, inputs, targets, asof)
                books[bid] = S._book_summary(p, theses, S._load_account(bid), nav_rows)
            except Exception:  # noqa: BLE001 — one book failing must not sink the others
                continue
    finally:
        S._BOOKS = _orig_books
    result = {"as_of": asof, "books": books, "leaderboard": S.leaderboard(books)}
    try:                                            # persist the leaderboard (AB_EXPERIMENT.md §6.2)
        _DESK_AB.mkdir(parents=True, exist_ok=True)
        (_DESK_AB / "leaderboard.json").write_text(json.dumps(result, indent=2, default=str))
    except Exception:  # noqa: BLE001 — persistence is best-effort; never raise
        pass
    return result


def _update_theses(book_id: str, policy: dict, inputs: list, targets: dict, asof: str) -> list:
    """Open one thesis per newly-bought name and grade every open thesis forward — delegates to the
    shadow_books grader (brain.outcomes under the hood). For the autonomous-clone book the candidate
    `inputs` don't carry the clone's tickers, so we synthesize minimal records (ticker + entry price)
    from the marks so each cloned name still opens and grades a thesis."""
    eff_inputs = inputs
    if policy.get("filter") == "clone":
        by_ticker = {(r.get("ticker") or "").upper(): r for r in inputs or []}
        eff_inputs = []
        acct_pos = S._load_account(book_id).get("positions", {})
        for tk in targets:
            r = by_ticker.get(tk)
            if r is None:
                # synthesize a minimal, schema-compatible record so the grader can open a thesis
                px = (acct_pos.get(tk) or {}).get("avg_cost")
                r = {"ticker": tk, "forge_confirmed": True, "price": px,
                     "raw_prob_correct": 0.55, "horizon_d": 21}
            eff_inputs.append(r)
    return S._update_theses(book_id, policy, eff_inputs, targets, asof)
