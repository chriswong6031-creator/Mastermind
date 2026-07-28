"""The suite must never be able to write a LIVE paper book.

Regression cover for 2026-07-26, when a test that called run_heavyweight() without
isolation wiped data/portfolios/heavyweight/account.json to {cash:0, positions:{}} and
appended a backdated nav:0 row — the dashboard showed $0 (-100%) for a book that had
never lost a cent. Guarded by the autouse _isolate_book_state fixture in conftest.py.
"""
from pathlib import Path

import pytest

from portfolio import paper_account, registry

REPO = Path(__file__).resolve().parent.parent
BRAIN_BOOKS = ["heavyweight", "autonomous", "etf", "china", "hk"]


def _is_live(p: Path) -> bool:
    """True when p resolves inside the repo's real data/ tree."""
    try:
        p.resolve().relative_to((REPO / "data").resolve())
        return True
    except ValueError:
        return False


@pytest.mark.parametrize("pid", BRAIN_BOOKS)
def test_registry_data_dir_is_not_live(pid):
    """registry._ROOT is the ONLY lever for per-id books — patching paper_account._DATA
    does nothing for them (see paper_account._paths)."""
    assert not _is_live(registry.data_dir(pid))


@pytest.mark.parametrize("pid", BRAIN_BOOKS + ["flagship", None])
def test_account_and_nav_paths_are_not_live(pid):
    paths = paper_account._paths(pid)
    for key in ("account", "nav", "fills", "pending", "data"):
        assert not _is_live(paths[key]), f"{pid}:{key} still points at the live tree"


def test_seeded_from_live_so_reads_still_work():
    """Isolation SEEDS from the real tree — a book that exists must still be readable
    (heavyweight reads flagship's book, etc.), otherwise the fixture would break read paths.

    Only meaningful in a checkout that HAS book state. A worktree's data/ tree is typically
    empty, where _load_account would fall through to its fresh-$1M default and this would
    pass vacuously — so skip rather than pretend the seeding path was exercised.
    """
    live = REPO / "data" / "portfolios" / "heavyweight" / "account.json"
    if not live.exists():
        pytest.skip(f"no live book state at {live} — seeding path not exercisable here")
    acct = paper_account._load_account("heavyweight")
    assert isinstance(acct.get("positions"), dict)
    assert acct.get("starting_nav"), "seeded copy lost starting_nav"


def test_writing_a_book_does_not_touch_the_live_file():
    """The exact failure mode: save on a Brain book stays in tmp.

    Uses a throwaway book id, NOT a real one. The isolated root is session-scoped (tests
    share it on purpose — see conftest._book_state_root), so mutating `heavyweight` here
    would leak a bogus balance into every later test in the run.
    """
    pid = "_isolation_probe"
    live = REPO / "data" / "portfolios" / pid
    assert not live.exists(), f"{live} should never exist — pick another probe id"

    state = paper_account._load_account(pid)
    state["cash"] = 123.45
    paper_account._save_account(state, pid)

    assert paper_account._load_account(pid)["cash"] == 123.45          # the write landed
    assert not _is_live(paper_account._paths(pid)["account"])           # ...but not live
    assert not live.exists(), "a test created a book directory in the LIVE data tree"
