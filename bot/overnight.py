"""The overnight watch loop — let the Brain books re-decide on the LIVE overnight tape before the open.

A human trader watches the overnight tape (US index futures, the Asia→Europe handoff, vol) and adjusts
their open position as it develops. The Brain books, by contrast, decide ONCE post-close and queue a
target (``bot/settle.py``) that settles at the open. This module lets them KEEP WATCHING: on a
scheduled overnight tick it reads the live tape (``data_layer.overnight``); if the tape is MATERIAL —
a deterministic tripwire (overnight risk elevated/stressed; free, no LLM) — it re-prompts the book's
Brain (Opus) with an overnight directive built from the tape, which REVISES the queued target. The
revised target then settles at the open via the existing queue→settle machinery.

Cheap by construction: the deterministic tripwire gates the Opus call, so the Brain fires only on a
material overnight move (~a couple nights a week), never on a calm tape. Never raises into the scheduler.
"""
from __future__ import annotations

import importlib
from datetime import date

import bot  # noqa: F401  -> vendor/macro onto sys.path

# pid → (module, daily-entrypoint) — each accepts a `directive=` (the ad-hoc overnight instruction)
_RUNNERS = {
    "etf": ("bot.etf", "run_etf"),
    "autonomous": ("bot.autonomous", "run_autonomous"),
    "china": ("bot.china", "run_china"),
    "hk": ("bot.hk", "run_hk"),
}


def _overnight_directive(pid: str, t: dict) -> str:
    """Build the Brain's overnight reconsideration prompt from the live tape."""
    groups = t.get("groups") or {}
    risk = t.get("risk") or {}

    def _fmt(g):
        return ", ".join(f"{r['name']} {r['change_pct']:+.2f}%"
                         for r in groups.get(g, []) if r.get("change_pct") is not None) or "n/a"

    return (
        "OVERNIGHT REVIEW — the LIVE overnight tape has moved materially since you last decided, and the "
        f"in-house dashboard is STALE and can't see it. Live tape now — risk_state {risk.get('state')}: "
        f"US index futures [{_fmt('us_futures')}]; international [{_fmt('international')}]; "
        f"vol [{_fmt('vol')}]; FX/rates [{_fmt('fx_rates')}]. "
        "You currently have a target QUEUED to settle at the next open. Review your current book (get_my_book) "
        "and this live tape (US books can also call get_overnight_tape for the full picture), then reconsider "
        "that queued target with fresh eyes: HOLD it as-is, or REVISE it — de-risk toward cash / safer assets "
        "if the overnight tape is selling off, or lean back in if it has stabilized. Submit your updated book "
        "via submit_book; it settles at the OPEN, not now. Cite what the live overnight tape is actually doing."
    )


def watch(pid: str, asof: str | None = None, *, force: bool = False) -> dict:
    """One overnight watch tick for book `pid`. No-op (and free) unless the market is CLOSED, a target
    is QUEUED, and the live tape is MATERIAL — only then does it fire the Opus re-decision that revises
    the queued target. Never raises. `force` bypasses the tripwire (manual re-prompt)."""
    from bot import settle
    from data_layer import overnight
    from portfolio import paper_account
    asof = asof or date.today().isoformat()
    out: dict = {"pid": pid, "asof": asof}
    try:
        if settle.is_open(pid):
            return {**out, "skipped": "market_open"}        # in-session: the normal daily run handles it
        if not paper_account.load_pending_target(pid):
            return {**out, "skipped": "nothing_queued"}     # no queued decision to refine
        t = overnight.tape(force=force)
        out["overnight_risk"] = (t.get("risk") or {}).get("state")
        out["tape_brief"] = overnight.brief(t)
        if not (force or overnight.is_material(t)):
            return {**out, "skipped": "tape_calm"}          # deterministic tripwire → free, no LLM
        # MATERIAL overnight move → re-prompt the Brain (Opus) to refine the queued target
        mod_name, fn_name = _RUNNERS.get(pid, (None, None))
        if not mod_name:
            return {**out, "skipped": "unknown_book"}
        fn = getattr(importlib.import_module(mod_name), fn_name)
        res = fn(asof=asof, directive=_overnight_directive(pid, t)) or {}
        out["refined"] = {k: res.get(k) for k in ("decided", "queued_for_open", "holdings", "brain")}
        return out
    except Exception as e:                                   # noqa: BLE001 — a watch miss must never kill the scheduler
        return {**out, "error": repr(e)[:200]}


def watch_us(asof: str | None = None) -> dict:
    """Overnight watch tick for the US Brain books (scheduler job)."""
    return {pid: watch(pid, asof) for pid in ("autonomous", "etf")}


def watch_asia(asof: str | None = None) -> dict:
    """Overnight watch tick for the Greater-China Brain books (scheduler job)."""
    return {pid: watch(pid, asof) for pid in ("china", "hk")}
