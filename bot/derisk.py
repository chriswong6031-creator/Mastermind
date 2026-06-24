"""FAST DE-RISK TRIGGER — react to a CONFIRMED unwind off-schedule, not at the next post-close run.

The desk decides once per day, post-close. On 2026-06-23 that was too slow: the AI-buildout/semis
chain cracked intraday (SMH −7%, DRAM −14%) and the held book sat in it until the once-daily run. This
module is the fast reflex the desk lacked. A DETERMINISTIC, free (no-LLM) ``tripwire`` fuses three
confirmations of an unwind — a −X% theme day on the book's fragile chain, a SPY dealer-gamma FLIP, a
credit gap — together with the Macro Risk Officer's risk state and the live overnight tape. When it
fires, the desk de-risks IMMEDIATELY, subtract-only:

  * ``derisk_flagship`` — cuts the held Flagship conviction book down to the risk-off gross cap
    (cracking-chain + worst-loser first), realizing REAL paper exits (``paper_account.execute_fill``)
    so cash is actually freed, with the Risk Officer's never-blow-to-cash guard enforced. Never adds.
  * ``derisk_brain``    — revises a Brain book's queued ``pending_target`` down to the gross cap and
    away from the cracking chains, so it settles defensively at the next open. No LLM needed (it fires
    even when the model is down — the whole point).

Flag-gated behind ``MASTERMIND_FAST_DERISK`` (default OFF → the scheduler hooks are inert and the books
are byte-identical to today). The macro-risk teeth themselves are gated on ``MASTERMIND_MACRO_RISK``.
Additive + reversible; NEVER raises into the scheduler.
"""
from __future__ import annotations

import json
import os
from datetime import date
from pathlib import Path

import bot  # noqa: F401  -> vendor/macro onto sys.path

_ROOT = Path(__file__).resolve().parent.parent
_V = _ROOT / "vendor" / "macro"
_ARTIFACTS = _ROOT / "data" / "macro_risk"

_US_BOOKS = ("flagship", "autonomous", "etf")
_ASIA_BOOKS = ("china", "hk")


def enabled() -> bool:
    """The fast de-risk trigger runs only when explicitly armed. Default OFF → the scheduler hooks are
    no-ops and every book is byte-identical to today."""
    return os.environ.get("MASTERMIND_FAST_DERISK", "0").strip().lower() in ("1", "true", "yes", "on")


def _envf(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _regime() -> dict:
    try:
        p = _V / "data" / "regime" / "latest.json"
        return json.loads(p.read_text()) if p.exists() else {}
    except Exception:  # noqa: BLE001
        return {}


def _load(rel: str):
    try:
        p = _V / rel
        return json.loads(p.read_text()) if p.exists() else None
    except Exception:  # noqa: BLE001
        return None


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


# ─────────────────────────────────────────────────────────────────────────────
# the three deterministic UNWIND-CONFIRMATION reads (free; no LLM). Each → (bool, reason).
# ─────────────────────────────────────────────────────────────────────────────
def _gex_flip() -> tuple[bool, str]:
    """SPY dealer-gamma FLIP — dealers short gamma (amplify moves) OR spot below the gamma flip."""
    g = _load("site/gex/SPY.json") or {}
    summ = g.get("summary") or {}
    regime = str(summ.get("regime") or "").lower()
    d2f = _f(summ.get("dist_to_flip_pct"))
    if regime == "short":
        return True, "SPY dealers SHORT gamma (amplifies the move)"
    if d2f is not None and d2f <= -0.25:
        return True, f"SPY {d2f:+.1f}% below the gamma-flip"
    return False, ""


def _credit_gap() -> tuple[bool, str]:
    """A credit GAP — HYG/TLT credit gapping down on the day, or a VIX spike, in the ETF risk block."""
    pulse = _load("site/basketdata/etf_pulse.json") or {}
    legs = (pulse.get("risk") or {}).get("legs") or []
    for leg in legs:
        if not isinstance(leg, dict):
            continue
        pair = str(leg.get("pair") or "")
        c1 = _f(leg.get("chg_1d"))
        if pair == "HYG/TLT" and c1 is not None and c1 <= -1.0:
            return True, f"credit gap: HYG/TLT {c1:+.1f}% on the day"
        if pair == "_VIX" and c1 is not None and c1 >= 10.0:
            return True, f"VIX spike {c1:+.0f}% on the day"
    return False, ""


def _theme_drop(drivers: list | None) -> tuple[bool, str]:
    """A −X% THEME DAY on the book's fragile-chain proxies (live). Uses the same batched yfinance read
    as the overnight tape (last vs prior session) against the proxy ETFs of the cracking chains."""
    drop = _envf("MASTERMIND_DERISK_THEME_DROP", -4.0)
    proxies: list[str] = []
    try:
        from portfolio import fragility_chain
        chains = fragility_chain.all_chains()
        for d in (drivers or []):
            cid = d.get("id") if isinstance(d, dict) else d
            for p in (chains.get(str(cid), {}) or {}).get("proxies", []):
                if p not in proxies:
                    proxies.append(p)
    except Exception:  # noqa: BLE001
        proxies = []
    if not proxies:
        return False, ""
    try:
        from data_layer import overnight
        changes = overnight._fetch_changes(proxies)
    except Exception:  # noqa: BLE001
        return False, ""
    worst = None
    for sym, c in (changes or {}).items():
        pct = _f((c or {}).get("change_pct"))
        if pct is not None and (worst is None or pct < worst[1]):
            worst = (sym, pct)
    if worst and worst[1] <= drop:
        return True, f"theme day: {worst[0]} {worst[1]:+.1f}% (≤ {drop:.0f}%)"
    return False, ""


def tripwire(pid: str, asof: str, *, regime: dict | None = None) -> dict:
    """DETERMINISTIC, free (no-LLM) tripwire — is an unwind CONFIRMED right now? Fuses the Macro Risk
    Officer state, the live overnight tape, a SPY GEX flip, a credit gap, and a −X% theme day on the
    book's fragile chains. ``trigger`` is True on a HARD confirmation (severity ≥ 2): macro risk_off, a
    stressed overnight tape, a gamma flip, or a theme-day drop — caution alone (severity 1) does not
    auto-cut. Returns ``{trigger, severity, reasons, state, gross_cap, risk_state}``. Never raises."""
    regime = regime if regime is not None else _regime()
    reasons: list[str] = []
    severity = 0
    try:
        from brain import macro_risk
        rs = macro_risk.risk_state(asof, regime)
    except Exception:  # noqa: BLE001
        rs = {"state": "risk_on", "gross_cap": 1.0, "drivers": []}

    state = rs.get("state")
    if state == "risk_off":
        reasons.append("macro RISK-OFF state")
        severity = max(severity, 2)
    elif state == "caution":
        reasons.append("macro CAUTION state")
        severity = max(severity, 1)

    try:
        from data_layer import overnight
        tp = overnight.tape()
        ostate = (tp.get("risk") or {}).get("state")
        if ostate == "stressed":
            reasons.append("overnight tape STRESSED")
            severity = max(severity, 2)
        elif ostate == "elevated":
            reasons.append("overnight tape elevated")
            severity = max(severity, 1)
    except Exception:  # noqa: BLE001
        pass

    for fn in (_gex_flip, _credit_gap):
        try:
            hit, why = fn()
            if hit:
                reasons.append(why)
                severity = max(severity, 2 if fn is _gex_flip else 1)
        except Exception:  # noqa: BLE001
            pass
    try:
        hit, why = _theme_drop(rs.get("drivers"))
        if hit:
            reasons.append(why)
            severity = max(severity, 2)
    except Exception:  # noqa: BLE001
        pass

    return {
        "trigger": severity >= 2,
        "severity": severity,
        "reasons": reasons,
        "state": state,
        "gross_cap": rs.get("gross_cap"),
        "risk_state": rs,
    }


def _write_artifact(asof: str, pid: str, payload: dict) -> None:
    try:
        d = _ARTIFACTS / (str(asof)[:10] or date.today().isoformat())
        d.mkdir(parents=True, exist_ok=True)
        (d / f"derisk_{pid}.json").write_text(json.dumps(payload, indent=2, default=str))
    except Exception:  # noqa: BLE001
        pass


# ─────────────────────────────────────────────────────────────────────────────
# FLAGSHIP — off-cycle subtract-only cut of the held conviction book to the gross cap.
# ─────────────────────────────────────────────────────────────────────────────
def derisk_flagship(asof: str | None = None, *, regime: dict | None = None, force: bool = False) -> dict:
    """When the tripwire fires, cut the held Flagship conviction book down to the risk-off gross cap —
    cracking-chain + worst-loser first — realizing REAL paper exits so cash is freed, with the Risk
    Officer's never-blow-to-cash guard enforced. Subtract-only; never adds; never raises. No-op (and
    free) when disabled, no trigger, or already under the cap. ``force`` bypasses the trigger gate."""
    asof = asof or date.today().isoformat()
    out: dict = {"pid": "flagship", "asof": asof}
    if not (enabled() or force):
        return {**out, "skipped": "disabled"}
    regime = regime if regime is not None else _regime()
    tw = tripwire("flagship", asof, regime=regime)
    out["tripwire"] = {k: tw.get(k) for k in ("trigger", "severity", "reasons", "state")}
    if not (force or tw["trigger"]):
        return {**out, "skipped": "no_trigger"}

    rs = tw["risk_state"]
    gcap = _f(rs.get("gross_cap")) or 1.0
    try:
        from portfolio import position_log, paper_account, fragility_chain
        from brain import risk_officer, ledger
    except Exception as e:  # noqa: BLE001
        return {**out, "error": f"import: {e!r}"[:160]}

    held = [p for p in position_log.open_positions() if (p or {}).get("sleeve") == "conviction"]
    if not held:
        _write_artifact(asof, "flagship", {**out, "action": "flat"})
        return {**out, "skipped": "flat"}

    # which chains are cracking (so we exit those names first)
    try:
        fr = fragility_chain.assess_book(
            [{"ticker": p["ticker"], "weight": _f(p.get("current_weight")) or 0.0} for p in held], rs)
        blocked = set(fr.get("blocked_chains") or [])
    except Exception:  # noqa: BLE001
        blocked = set()

    rows = []
    for p in held:
        t = str(p.get("ticker") or "").upper().strip()
        if not t:
            continue
        w = _f(p.get("current_weight")) or 0.0
        entry, cur = p.get("entry_price"), None
        try:
            cur = paper_account._current_price(t)
        except Exception:  # noqa: BLE001
            cur = None
        rel = ((cur / float(entry)) - 1.0) if (cur and entry and float(entry) > 0) else None
        try:
            in_crack = bool(fragility_chain.chains_of(t) & blocked)
        except Exception:  # noqa: BLE001
            in_crack = False
        rows.append({"ticker": t, "sleeve": "conviction", "current_weight": w,
                     "rel_return_since_entry": rel, "in_crack": in_crack})

    gross = round(sum(r["current_weight"] for r in rows), 4)
    if gross <= gcap + 1e-9 and not blocked:
        _write_artifact(asof, "flagship", {**out, "action": "hold", "gross": gross, "gross_cap": gcap})
        return {**out, "action": "hold", "gross": gross, "gross_cap": gcap,
                "reasons": tw["reasons"]}

    # order: cracking-chain first, then worst rel-return first; exit until gross ≤ cap.
    def _rel_key(r):
        rr = r.get("rel_return_since_entry")
        return rr if isinstance(rr, (int, float)) else 0.0

    order = sorted(rows, key=lambda r: (0 if r["in_crack"] else 1, _rel_key(r)))
    decisions, running = [], gross
    for r in order:
        if running <= gcap + 1e-9:
            break
        decisions.append({"ticker": r["ticker"], "action": "exit", "scale": 0.0,
                          "rel_return": r["rel_return_since_entry"]})
        running -= r["current_weight"]

    # never-blow-to-cash guard (worst-loser-first cap + invested floor) — reuse the Risk Officer's pure helper.
    guarded = risk_officer.apply_exits(rows, decisions, max_exits=len(decisions),
                                       min_invested=risk_officer._MIN_INVESTED_NAMES)
    exited, realized = [], []
    for d in guarded["exits"]:
        t = d["ticker"]
        try:
            res = paper_account.execute_fill(t, "sell", asof=asof)  # REAL paper exit → frees cash
            if res.get("ok"):
                realized.append(t)
        except Exception:  # noqa: BLE001
            pass
        try:
            position_log.close_position("conviction", t, asof, reason="fast_derisk")
        except Exception:  # noqa: BLE001
            pass
        try:
            ledger.close(t, f"exited (fast de-risk: {'; '.join(tw['reasons'])[:120]})")
        except Exception:  # noqa: BLE001
            pass
        exited.append(t)

    result = {**out, "action": "cut", "exited": exited, "realized": realized,
              "gross_before": gross, "gross_cap": gcap, "reasons": tw["reasons"]}
    _write_artifact(asof, "flagship", result)
    return result


# ─────────────────────────────────────────────────────────────────────────────
# BRAIN books — revise the queued pending target down to the gross cap (settles defensively at open).
# ─────────────────────────────────────────────────────────────────────────────
def derisk_brain(pid: str, asof: str | None = None, *, regime: dict | None = None,
                 force: bool = False) -> dict:
    """When the tripwire fires and the Brain book has a target QUEUED to settle at the next open, revise
    that target subtract-only: scale it down to the risk-off gross cap and drop net-new names sitting in
    a cracking fragile chain. Deterministic — no LLM (it fires even when the model is down). The revised
    target settles defensively through the existing queue→settle machinery. Never adds; never raises."""
    asof = asof or date.today().isoformat()
    out: dict = {"pid": pid, "asof": asof}
    if not (enabled() or force):
        return {**out, "skipped": "disabled"}
    try:
        from portfolio import paper_account, fragility_chain
    except Exception as e:  # noqa: BLE001
        return {**out, "error": f"import: {e!r}"[:160]}

    pt = paper_account.load_pending_target(pid)
    if not pt or not (pt.get("target")):
        return {**out, "skipped": "nothing_queued"}

    regime = regime if regime is not None else _regime()
    tw = tripwire(pid, asof, regime=regime)
    out["tripwire"] = {k: tw.get(k) for k in ("trigger", "severity", "reasons", "state")}
    if not (force or tw["trigger"]):
        return {**out, "skipped": "no_trigger"}

    rs = tw["risk_state"]
    gcap = _f(rs.get("gross_cap")) or 1.0
    target = {str(k).upper(): _f(v) or 0.0 for k, v in (pt["target"] or {}).items()}

    try:
        fr = fragility_chain.assess_book([{"ticker": k, "weight": v} for k, v in target.items()], rs)
        blocked = set(fr.get("blocked_chains") or [])
    except Exception:  # noqa: BLE001
        blocked = set()

    # (1) drop names in a cracking chain when adds are off (risk_off)
    if rs.get("allow_adds") is False and blocked:
        kept = {}
        for k, v in target.items():
            try:
                if fragility_chain.chains_of(k) & blocked:
                    continue
            except Exception:  # noqa: BLE001
                pass
            kept[k] = v
        target = kept
    # (2) scale gross down to the cap (the cash floor)
    gross = round(sum(target.values()), 4)
    scaled = False
    if gross > gcap > 0:
        sc = gcap / gross
        target = {k: round(v * sc, 4) for k, v in target.items()}
        scaled = True

    try:
        paper_account.save_pending_target(target, asof, portfolio_id=pid)
    except Exception as e:  # noqa: BLE001
        return {**out, "error": f"save: {e!r}"[:160]}

    result = {**out, "action": "revised_pending_target", "gross_before": gross, "gross_cap": gcap,
              "scaled": scaled, "dropped_chains": sorted(blocked), "reasons": tw["reasons"],
              "n_names": len(target)}
    _write_artifact(asof, pid, result)
    return result


# ─────────────────────────────────────────────────────────────────────────────
# scheduler entrypoints
# ─────────────────────────────────────────────────────────────────────────────
def sweep_us(asof: str | None = None) -> dict:
    """Intraday/overnight de-risk sweep for the US books (scheduler job). Flagship cuts held; the US
    Brain books revise their queued targets. No-op unless armed + a confirmation fires. Never raises."""
    out: dict = {}
    if not enabled():
        return {"skipped": "disabled"}
    try:
        out["flagship"] = derisk_flagship(asof)
    except Exception as e:  # noqa: BLE001
        out["flagship"] = {"error": repr(e)[:160]}
    for pid in ("autonomous", "etf"):
        try:
            out[pid] = derisk_brain(pid, asof)
        except Exception as e:  # noqa: BLE001
            out[pid] = {"error": repr(e)[:160]}
    return out


def sweep_asia(asof: str | None = None) -> dict:
    """De-risk sweep for the Greater-China Brain books (scheduler job). Revises their queued targets.
    No-op unless armed + a confirmation fires. Never raises."""
    if not enabled():
        return {"skipped": "disabled"}
    out: dict = {}
    for pid in _ASIA_BOOKS:
        try:
            out[pid] = derisk_brain(pid, asof)
        except Exception as e:  # noqa: BLE001
            out[pid] = {"error": repr(e)[:160]}
    return out
