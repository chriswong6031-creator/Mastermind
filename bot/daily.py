"""The daily loop — one entrypoint for cron / the scheduler.

Runs, in order, each step degrading gracefully (a missing credential never breaks the loop):
  1. Gated multi-name paper book (phase2, material-change gated).
  2. Armed Claude regime/theme research -> proposals gated into the falsifiable ledger.
  3. Competitor desk — pull Quiver's AI strategies + an armed "where's our edge" note.

Run:  python -m bot.daily        (or POST /daily, or the APScheduler job in app/scheduler.py)
"""
from __future__ import annotations

from datetime import date, datetime, timezone

import bot  # noqa: F401


def run_daily(asof: str | None = None, *, force: bool = False, armed: bool = True) -> dict:
    asof = asof or date.today().isoformat()
    out = {"asof": asof, "ran_at": datetime.now(timezone.utc).isoformat()}

    # 1. the gated paper book (deterministic; always runs)
    try:
        from bot import phase2
        out["book"] = phase2.run(asof=asof, force=force)
    except Exception as e:
        out["book"] = {"error": str(e)[:200]}

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
