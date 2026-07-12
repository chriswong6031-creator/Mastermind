"""The daily loop + scheduler wiring."""
from pathlib import Path

import bot  # noqa: F401

_DB = Path(__file__).resolve().parent.parent / "data" / "bot.db"
_SCHED = Path(__file__).resolve().parent.parent / "data" / "scheduler.sqlite"


def _clean():
    for p in (_DB, _SCHED):
        if p.exists():
            p.unlink()


def test_daily_loop_deterministic():
    _clean()
    from bot import daily
    out = daily.run_daily(armed=False)            # offline: book only, no Claude bridge
    assert out["book"]["ran"] is True
    assert out["book"]["sleeves"]["cash"] >= 0.05
    # armed steps are skipped without armed=True
    assert "research" not in out
    # 0d perception organs run UNCONDITIONALLY and fail-soft — they must record a status into
    # `out` without ever breaking the daily flow, even absent vendor data (no exception, no error key
    # required, but the key is always present).
    assert "universe_triage" in out
    assert "divergence_clue" in out
    _clean()


def test_scheduler_registers_daily_job():
    _clean()
    from app import scheduler
    s = scheduler.start()
    if s is None:                                  # apscheduler not installed -> graceful no-op
        return
    assert any(j.id == "daily_loop" for j in s.get_jobs())
    s.shutdown(wait=False)
    _clean()
