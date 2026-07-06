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
from typing import Any

import bot  # noqa: F401  -> vendor/macro onto sys.path

_ROOT = Path(__file__).resolve().parent.parent
_V = _ROOT / "vendor" / "macro"
_ARTIFACTS = _ROOT / "data" / "macro_risk"

_US_BOOKS = ("flagship", "autonomous", "etf")
_ASIA_BOOKS = ("china", "hk")

# Sleeves that participate in the held-position sweep — conviction (flagship-style) AND leadership
# (40-60% NAV in the brain books that hold both sleeves).  The leadership sleeve is the one that
# was exempt on 2026-07-01, so a severity-2 tripwire with a full-sized leadership sleeve did
# nothing.  Both sleeves are SUBTRACT-ONLY here: we only trim/exit, never add.
_DERISKED_SLEEVES: frozenset[str] = frozenset({"conviction", "leadership"})


def _severity_cap(severity: int) -> float | None:
    """Return the doctrine-configured gross cap for *severity*, or None when severity < 2 (caution
    alone is advisory — no automatic gross-cap override).  Reads config/doctrine.yml lazily so the
    threshold is one source of truth; falls back to hard-coded defaults on any parse error.

    The ladder (tagged unverified-prior in doctrine.yml):
      severity 2 → 0.70  (confirmed unwind: risk_off OR gex_flip OR theme_day)
      severity 3 → 0.55  (maximum severity: all channels lit)
    """
    if severity < 2:
        return None  # caution-only (severity 0/1) never overrides the gross cap
    _FALLBACK: dict[int, float] = {2: 0.70, 3: 0.55}
    try:
        import yaml  # lazy — not on the critical path; gracefully absent in CI without PyYAML
        _cfg_path = _ROOT / "config" / "doctrine.yml"
        _cfg: dict[str, Any] = yaml.safe_load(_cfg_path.read_text()) if _cfg_path.exists() else {}
        ladder: dict = _cfg.get("derisk_severity_caps") or {}
        # Walk from the exact severity down to 2 so a severity-4 gap is handled gracefully.
        for s in range(max(severity, 2), 1, -1):
            if s in ladder:
                return float(ladder[s])
    except Exception:  # noqa: BLE001
        pass
    return _FALLBACK.get(max(severity, 2), _FALLBACK[2])


def enabled() -> bool:
    """The fast de-risk trigger runs only when explicitly armed. Code default is OFF; production
    .env sets MASTERMIND_FAST_DERISK=1 (the trigger is armed in the live deployment). When unset
    the scheduler hooks are no-ops and every book is byte-identical to a non-derisk run."""
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


def _distribution_escalation(pid: str) -> tuple[int, str]:
    """W-I task 1 — DISTRIBUTION ESCALATOR. Read the pid's held book for distribution tells (crowding
    + 3D/weekly-MACD bear + defensive-RS crossover) and return ``(severity_bump, reason)``. When
    >= book_weight_escalate_frac of book weight sits in >=min_tells distributing names, bump +1.

    SHRINK-ONLY + degrade-safe: any failure (no holdings module, no price series, empty book) returns
    ``(0, "")`` so the tripwire is byte-identical to today. The bump is composed via max() by the
    caller — it can only ADD severity into the already-validated ladder, never un-cap. The reason
    string names the tells (spec 1b: 'distribution: SMH crowd99+3D-MACD-bear ...')."""
    try:
        from portfolio import position_log, distribution_tells
        held = position_log.open_positions(pid if pid != "flagship" else None)
    except Exception:  # noqa: BLE001
        return 0, ""
    if not held:
        return 0, ""
    try:
        sc = distribution_tells.score(held)
    except Exception:  # noqa: BLE001
        return 0, ""
    bump = int(sc.get("escalate_severity") or 0)
    return (bump, sc.get("reason") or "") if bump > 0 else (0, "")


def tripwire(pid: str, asof: str, *, regime: dict | None = None) -> dict:
    """DETERMINISTIC, free (no-LLM) tripwire — is an unwind CONFIRMED right now? Fuses the Macro Risk
    Officer state, the live overnight tape, a SPY GEX flip, a credit gap, a −X% theme day on the
    book's fragile chains, and (W-I) a DISTRIBUTION ESCALATION on the book's own held names.
    ``trigger`` is True on a HARD confirmation (severity ≥ 2): macro risk_off, a stressed overnight
    tape, a gamma flip, or a theme-day drop — caution alone (severity 1) does not auto-cut. The
    distribution escalator is SHRINK-ONLY: it composes +1 via max() (so a book already printing sev-2
    on short gamma + a distributing pile becomes sev-3 → eff_cap 0.55), NEVER additive beyond sev-3.
    Returns ``{trigger, severity, reasons, state, gross_cap, risk_state}``. Never raises."""
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

    # W-I DISTRIBUTION ESCALATOR — compose +1 via max() (SHRINK-ONLY; never un-caps). A book already
    # printing sev-2 on a hard confirmation whose held names are ALSO distributing becomes sev-3
    # (eff_cap 0.55). Clamped to the ladder ceiling (3): the escalation is a bump, never additive
    # stacking beyond the ladder's floor. A distribution read WITHOUT a hard confirmation lifts
    # severity to 1 (advisory) but does not on its own auto-cut — the hard-confirmation gate
    # (severity>=2) is unchanged, exactly as caution-alone never auto-cuts.
    try:
        dist_bump, dist_reason = _distribution_escalation(pid)
        if dist_bump > 0:
            severity = min(3, severity + dist_bump)
            if dist_reason:
                reasons.append(dist_reason)
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
    # BUG-A FIX (problem #22, 07-01 no-op): the original code took ONLY the macro-risk-state cap
    # (rs.gross_cap).  On 07-01 the state was risk_on so gross_cap=1.0, meaning a correctly-fired
    # severity-2 tripwire on a book at gross 0.75 saw gross ≤ 1.0 and concluded "hold".  The fix:
    # eff_cap = min(state_cap, severity_cap) so the cut ALWAYS bites once severity ≥ 2, regardless
    # of the macro-state scorer's instantaneous read.
    state_gross_cap = _f(rs.get("gross_cap")) or 1.0
    sev = tw.get("severity", 0)
    sev_cap = _severity_cap(sev)                             # None when severity < 2
    eff_cap = min(state_gross_cap, sev_cap) if sev_cap is not None else state_gross_cap
    # ── E2.2 SUBSUMPTION — the posture notch joins by MIN-composition (idempotent ceilings
    # can't double-cut; charter P7). Notch SOURCES carry {source, dedup_key}: the W-I
    # distribution escalator already bumps `sev` upstream (one seam), and any future
    # anticipation notch must arrive through the same _distribution_escalation-style bump —
    # capped at the sev-3 ladder ceiling via max()-composition there, never summed here.
    # Flag OFF ⇒ this block is dead and eff_cap is the W1 two-term min, byte-identical.
    posture_notch_cap = None
    try:
        from brain import posture_decider as _pd
        if _pd.posture_flag():
            _rec = _pd.latest() or {}
            _pn = _f(_rec.get("posture_notch_cap"))
            if _pn is not None and _pn > 0:
                posture_notch_cap = _pn
                eff_cap = min(eff_cap, _pn)
    except Exception:  # noqa: BLE001 — P2: degrade to the two-term min
        pass
    try:
        from portfolio import position_log, paper_account, fragility_chain
        from brain import risk_officer, ledger
    except Exception as e:  # noqa: BLE001
        return {**out, "error": f"import: {e!r}"[:160]}

    # BUG-B FIX (problem #22, leadership off-ramp): the original filter checked only
    # sleeve=='conviction'.  The flagship book also holds a 40-60% NAV leadership sleeve; those
    # positions had no derisk off-ramp at all, so the tripwire only ever targeted the smaller
    # conviction piece.  Extend to _DERISKED_SLEEVES = {conviction, leadership}.
    # Subtract-only invariant preserved: we only trim/exit, never add.
    held = [p for p in position_log.open_positions()
            if (p or {}).get("sleeve") in _DERISKED_SLEEVES]
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
        sleeve = str(p.get("sleeve") or "conviction")
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
        rows.append({"ticker": t, "sleeve": sleeve, "current_weight": w,
                     "rel_return_since_entry": rel, "in_crack": in_crack})

    gross = round(sum(r["current_weight"] for r in rows), 4)
    if gross <= eff_cap + 1e-9 and not blocked:
        artifact = {**out, "action": "hold", "gross": gross,
                    "gross_cap": eff_cap, "state_gross_cap": state_gross_cap,
                    "severity_cap": sev_cap, "posture_notch_cap": posture_notch_cap, "eff_cap": eff_cap,
                    "cut_scope": sorted(_DERISKED_SLEEVES)}
        _write_artifact(asof, "flagship", artifact)
        return {**out, "action": "hold", "gross": gross, "gross_cap": eff_cap,
                "eff_cap": eff_cap, "severity_cap": sev_cap,
                "cut_scope": sorted(_DERISKED_SLEEVES), "reasons": tw["reasons"]}

    # order: cracking-chain first, then worst rel-return first; exit until gross ≤ eff_cap.
    def _rel_key(r):
        rr = r.get("rel_return_since_entry")
        return rr if isinstance(rr, (int, float)) else 0.0

    order = sorted(rows, key=lambda r: (0 if r["in_crack"] else 1, _rel_key(r)))
    decisions: list[dict] = []
    running = gross
    for r in order:
        if running <= eff_cap + 1e-9:
            break
        decisions.append({"ticker": r["ticker"], "action": "exit", "scale": 0.0,
                          "sleeve": r["sleeve"],
                          "rel_return": r["rel_return_since_entry"]})
        running -= r["current_weight"]

    # risk_officer.apply_exits is CONVICTION-ONLY: its internal _held_map() filters to
    # sleeve=='conviction' and enforces the ½-Kelly minimum-invested-names floor.  Leadership
    # exits bypass it (the equal-weight sleeve has no such minimum; subtracting pro-rata is
    # correct).  Both paths are subtract-only — we never add.
    conv_decisions = [d for d in decisions if d.get("sleeve") != "leadership"]
    lead_decisions = [d for d in decisions if d.get("sleeve") == "leadership"]

    conv_rows = [r for r in rows if r.get("sleeve") == "conviction"]
    guarded_conv = risk_officer.apply_exits(
        conv_rows, conv_decisions,
        max_exits=len(conv_decisions),
        min_invested=risk_officer._MIN_INVESTED_NAMES,
    )

    _reason_str = f"exited (fast de-risk: {'; '.join(tw['reasons'])[:120]})"
    exited: list[str] = []
    realized: list[str] = []

    def _execute_exit(t: str, sleeve: str) -> None:
        try:
            res = paper_account.execute_fill(t, "sell", asof=asof)  # REAL paper exit → frees cash
            if res.get("ok"):
                realized.append(t)
        except Exception:  # noqa: BLE001
            pass
        try:
            position_log.close_position(sleeve, t, asof, reason="fast_derisk")
        except Exception:  # noqa: BLE001
            pass
        try:
            ledger.close(t, _reason_str)
        except Exception:  # noqa: BLE001
            pass
        exited.append(t)

    for d in guarded_conv["exits"]:         # conviction — guarded by invested floor
        _execute_exit(d["ticker"], "conviction")
    for d in lead_decisions:                # leadership — equal-weight, no minimum floor
        _execute_exit(d["ticker"], "leadership")

    result = {**out, "action": "cut", "exited": exited, "realized": realized,
              "gross_before": gross, "gross_cap": eff_cap,
              "state_gross_cap": state_gross_cap, "severity_cap": sev_cap, "posture_notch_cap": posture_notch_cap, "eff_cap": eff_cap,
              "cut_scope": sorted(_DERISKED_SLEEVES), "reasons": tw["reasons"]}
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
    # Apply the same severity-decoupled cap used in derisk_flagship so pending-target revisions
    # are also bounded by the ladder (sev2→0.70, sev3→0.55) even when state is risk_on/cap=1.0.
    state_gross_cap = _f(rs.get("gross_cap")) or 1.0
    sev = tw.get("severity", 0)
    sev_cap = _severity_cap(sev)
    eff_cap = min(state_gross_cap, sev_cap) if sev_cap is not None else state_gross_cap
    # E2.2: the posture notch composes into the Brain-book cutter identically (min(); charter P7).
    # Flag OFF ⇒ dead block, eff_cap byte-identical to W1.
    posture_notch_cap = None
    try:
        from brain import posture_decider as _pd
        if _pd.posture_flag():
            _rec = _pd.latest() or {}
            _pn = _f(_rec.get("posture_notch_cap"))
            if _pn is not None and _pn > 0:
                posture_notch_cap = _pn
                eff_cap = min(eff_cap, _pn)
    except Exception:  # noqa: BLE001 — P2
        pass
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
    # (2) scale gross down to the effective cap (severity-decoupled; subtract-only)
    gross = round(sum(target.values()), 4)
    scaled = False
    if gross > eff_cap > 0:
        sc = eff_cap / gross
        target = {k: round(v * sc, 4) for k, v in target.items()}
        scaled = True

    try:
        paper_account.save_pending_target(target, asof, portfolio_id=pid)
    except Exception as e:  # noqa: BLE001
        return {**out, "error": f"save: {e!r}"[:160]}

    result = {**out, "action": "revised_pending_target", "gross_before": gross,
              "gross_cap": eff_cap, "state_gross_cap": state_gross_cap,
              "severity_cap": sev_cap, "posture_notch_cap": posture_notch_cap, "eff_cap": eff_cap,
              "scaled": scaled, "dropped_chains": sorted(blocked), "reasons": tw["reasons"],
              "n_names": len(target), "cut_scope": sorted(_DERISKED_SLEEVES)}
    _write_artifact(asof, pid, result)
    return result


# ─────────────────────────────────────────────────────────────────────────────
# scheduler entrypoints
# ─────────────────────────────────────────────────────────────────────────────
def _log_sweep_advisory(pid: str, exc: Exception) -> None:
    """Emit an ADVISORY GuardrailResult when a sweep leg fails. Never raises."""
    try:
        from control_plane.guardrail import GuardrailResult, Severity
        GuardrailResult.failed(
            "derisk_sweep",
            Severity.ADVISORY_ONLY,
            detail=f"sweep({pid}) raised: {exc!r}"[:200],
            action_taken="sweep failure visible; de-risk not applied to this book",
        ).log(job="derisk_sweep", book=pid)
    except Exception:  # noqa: BLE001
        pass


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
        _log_sweep_advisory("flagship", e)
    for pid in ("autonomous", "etf"):
        try:
            out[pid] = derisk_brain(pid, asof)
        except Exception as e:  # noqa: BLE001
            out[pid] = {"error": repr(e)[:160]}
            _log_sweep_advisory(pid, e)
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
            _log_sweep_advisory(pid, e)
    return out
