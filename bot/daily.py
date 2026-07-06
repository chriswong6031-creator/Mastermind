"""The daily loop — one entrypoint for cron / the scheduler.

Runs, in order, each step degrading gracefully (a missing credential never breaks the loop):
  0a. Deploy-lag tripwire: alert (LOUD) when production trails master >24h. Never raises.
  0b. Freshen the vendored macro analyzer data before the engine reads it.
  1.  Gated multi-name paper book (phase2, material-change gated).
  2.  Armed Claude regime/theme research -> proposals gated into the falsifiable ledger.
  3.  Competitor desk — pull Quiver's AI strategies + an armed "where's our edge" note.

Run:  python -m bot.daily        (or POST /daily, or the APScheduler job in app/scheduler.py)
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timezone

import bot  # noqa: F401

_log = logging.getLogger(__name__)


def run_daily(asof: str | None = None, *, force: bool = False, armed: bool = True) -> dict:
    asof = asof or date.today().isoformat()
    out = {"asof": asof, "ran_at": datetime.now(timezone.utc).isoformat()}

    # 0a. DEPLOY-LAG TRIPWIRE — alert when production trails master >24h (W-I Task 4b).
    #     The 2026-07-02 incident was worsened because 4 merged fix-waves sat on master while
    #     production ran a pre-W0 branch through the entire episode. This check runs first so
    #     the alert appears at the TOP of the daily runlog, not buried. Never raises; degrades
    #     silently when git is unavailable (e.g. a fully detached CI checkout).
    try:
        from scripts.check_deploy_lag import check as _deploy_lag_check
        lag = _deploy_lag_check()
        out["deploy_lag"] = lag
        if lag.get("warn"):
            _log.warning(lag["message"])
    except Exception as e:  # noqa: BLE001 — deploy-lag check must NEVER kill the build
        out["deploy_lag"] = {"error": str(e)[:200]}

    # 0b. FRESHEN the vendored macro analyzer data BEFORE the engine reads it. A stale vendored
    #     tree is how the book once bought NVDA off a days-old "Constructive" read after the live
    #     analyzer had already flipped it to "avoid / wait for a base". Pulls origin/main (== the
    #     live site); the staleness tripwire warns (or refuses via MACRO_STALE_BLOCK=1). Never raises.
    try:
        from data_layer import macro_refresh
        out["macro_data"] = macro_refresh.refresh_and_check()
    except Exception as e:  # noqa: BLE001 — freshness must never kill the build
        out["macro_data"] = {"error": str(e)[:200]}

    # 1. the gated paper book (deterministic; always runs)
    try:
        from bot import phase2
        out["book"] = phase2.run(asof=asof, force=force)
    except Exception as e:
        out["book"] = {"error": str(e)[:200]}

    # 1b. MW3 R3 STALE-ANCHOR FREEZE: when a FREEZE-class macro anchor is stale beyond its
    #     contract budget, freeze the flagship targets to the prior published book — no new adds,
    #     no weight increases; de-risk and existing holds are untouched.  Runs only when the
    #     macro_data refresh found a freeze condition AND the book built successfully.
    #     Kill-switch: MASTERMIND_STALE_FREEZE=0 suppresses application (still logs).
    #     Reuses the firm_clamp_freeze seam in phase2 — same freeze_to_prior helper, same
    #     GuardrailResult(FREEZE) log pattern, same stale_freeze key in out for runlog surfacing.
    try:
        _macro_data = out.get("macro_data") or {}
        _book_data = out.get("book") or {}
        if (isinstance(_macro_data, dict) and _macro_data.get("freeze")
                and isinstance(_book_data, dict) and _book_data.get("ran")
                and not _book_data.get("error")):
            from data_layer.macro_refresh import _freeze_enabled
            _positions = _book_data.get("positions") or []
            if _freeze_enabled() and _positions:
                from portfolio.freeze import freeze_to_prior as _ftp
                from portfolio import firm_exposure as _firm_exp
                _prior = _firm_exp.published_weights("flagship")
                # Build target map from the built positions list
                _target_map: dict[str, float] = {}
                for _p in _positions:
                    if isinstance(_p, dict) and _p.get("ticker"):
                        _tk = str(_p["ticker"]).upper().strip()
                        try:
                            _target_map[_tk] = float(_p.get("weight") or 0.0)
                        except (TypeError, ValueError):
                            pass
                _frozen_map = _ftp(_target_map, _prior)
                # Rebuild positions list with frozen weights
                _frozen_positions = []
                _seen: set[str] = set()
                for _p in _positions:
                    _tk = str((_p or {}).get("ticker") or "").upper().strip()
                    if not _tk or _tk in _seen:
                        continue
                    _fw = _frozen_map.get(_tk)
                    if _fw is not None:
                        _seen.add(_tk)
                        _frozen_positions.append({**_p, "weight": round(_fw, 4)})
                # Inject prior-only names (must not be absent = liquidated)
                for _fk, _fw in _frozen_map.items():
                    _ku = str(_fk or "").upper().strip()
                    if _ku and _ku not in _seen and _fw > 0:
                        _frozen_positions.append({"ticker": _fk, "weight": round(_fw, 4),
                                                   "sleeve": "prior", "_sentinel_hold": True})
                _book_data["positions"] = _frozen_positions
                _book_data["stale_freeze"] = {
                    "applied": True,
                    "reasons": _macro_data.get("freeze_reasons", []),
                    "asof": _macro_data.get("asof"),
                }
                out["stale_freeze"] = _book_data["stale_freeze"]
                # GuardrailResult(FREEZE) → run_events
                from control_plane.guardrail import GuardrailResult, Severity
                GuardrailResult.failed(
                    "stale_anchor",
                    Severity.FREEZE,
                    detail=(f"Stale FREEZE-class anchor(s): "
                            f"{_macro_data.get('freeze_reasons', [])}"),
                    action_taken="freeze_to_prior applied to flagship targets "
                                 "(no new adds, no weight increases)",
                    extra={"asof": _macro_data.get("asof"),
                           "freeze_reasons": _macro_data.get("freeze_reasons", [])},
                ).log(job="daily_loop", book="flagship")
                _log.warning(
                    "[daily] STALE-ANCHOR FREEZE applied to flagship: %s",
                    _macro_data.get("freeze_reasons", []))
            elif not _freeze_enabled():
                # Kill-switch: log but do not apply
                out["stale_freeze"] = {
                    "applied": False,
                    "kill_switch": True,
                    "reasons": _macro_data.get("freeze_reasons", []),
                }
                _log.warning(
                    "[daily] STALE-ANCHOR FREEZE suppressed (MASTERMIND_STALE_FREEZE=0): %s",
                    _macro_data.get("freeze_reasons", []))
    except Exception as _e:  # noqa: BLE001 — freeze helper must never kill the build
        out["stale_freeze"] = {"error": str(_e)[:200]}

    # NOTE: the flagship book's safety scorecard is computed + CONSUMED inside phase2 (it
    # de-grosses a fragile book before sizing cash) and persisted to data/portfolio/safety.json.
    # Other books' safety is computed on demand by the /api/risk endpoint (cached). So there is
    # no separate safety step here — safety is part of the book build, not a bolt-on display pass.

    # 2. armed regime/theme research -> gated ledger (needs a Claude credential)
    if armed:
        try:
            from brain import research_desk
            out["research"] = research_desk.daily_research_and_ingest(asof)
        except Exception as e:
            out["research"] = {"error": str(e)[:200]}

        # 3. competitor desk — Quiver pull + edge note
        try:
            from brain import competitor_desk
            out["competitor"] = competitor_desk.analyze(asof)
        except Exception as e:
            out["competitor"] = {"error": str(e)[:200]}

        # 4. warm the EN->ZH translation cache for the freshly-written book, research
        #    notes and papers. This is what lets the dashboard render Chinese (Brain
        #    Log, Research Feed, the thesis reports) WITHOUT a live LLM call in the
        #    request path — the API only does cache lookups via cached_zh(). Gated on
        #    `armed` because it needs the Claude bridge, same as steps 2-3 (so the
        #    offline/deterministic path stays LLM-free). Incremental (skips
        #    already-cached strings) and best-effort: a missing bridge or slow call
        #    never breaks the loop; the UI just falls back to English until warmed.
        try:
            import json
            from pathlib import Path
            from brain import translate as _translate
            _root = Path(__file__).resolve().parent.parent
            latest_p = _root / "data" / "portfolio" / "latest.json"
            if latest_p.exists():
                _translate.translate_book(json.loads(latest_p.read_text(encoding="utf-8")))
            _translate.translate_notes(_root / "data" / "research" / "notes")
            _translate.translate_papers(_root / "data" / "research" / "papers")
            _translate.translate_decisions()   # Daily Decision Log write-ups (summary / rationale / brain_text)
            _translate.translate_runs()        # Brain Activity log titles + summaries (the run write-ups)
            out["translate"] = {"ok": True}
        except Exception as e:
            out["translate"] = {"error": str(e)[:200]}
    return out


if __name__ == "__main__":
    o = run_daily()
    print(f"=== daily loop {o['asof']} ===")
    b = o.get("book", {})
    print("book:", "ran" if b.get("ran") else b.get("reason", b.get("error")),
          "| sleeves:", b.get("sleeves"))
    _sf = (b or {}).get("safety") or {}
    _ov = (b or {}).get("safety_overlay") or {}
    print("safety:", f"score={_sf.get('safety_score')}({_sf.get('grade')})",
          f"gross_mult={_ov.get('gross_mult')}", f"reasons={_ov.get('reasons')}")
    r = o.get("research", {})
    print("research:", (r.get("ingest") or {}).get("ingested", r.get("error")), "theses ingested")
    c = o.get("competitor", {})
    ca = (c or {}).get("analysis", {})
    print("competitor:", "note written" if ca.get("ok") else ca.get("error"),
          "| quiver pull:", (c or {}).get("pull", {}).get("ok"))
    tx = o.get("translate", {})
    print("translate:", "cache warmed" if tx.get("ok") else tx.get("error"))
