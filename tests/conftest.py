"""Shared test fixtures.

Several tests exercise the real book build (bot.phase2.run) and armed-session
paths, which call brain.runlog.start_run(...) and would otherwise append real
run-log entries into data/brain/runs/ on every `pytest` invocation — cluttering
the live "Brain Activity" feed. Isolate the run-log to a tmp dir for all tests.
"""
import pytest


@pytest.fixture(autouse=True)
def _isolate_runlog(tmp_path, monkeypatch):
    try:
        import brain.runlog as rl
        runs_dir = tmp_path / "_runlog"
        runs_dir.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(rl, "_RUNS_DIR", runs_dir, raising=False)
        monkeypatch.setattr(rl, "_INDEX", runs_dir / "index.jsonl", raising=False)
    except Exception:
        pass


@pytest.fixture(autouse=True)
def _isolate_positions_ledger(tmp_path, monkeypatch):
    """Isolate the positions ledger + fills blotter so the real book build tests (phase2 /
    tracking) don't write phantom ADD/TRIM history into the LIVE data/portfolio/*.json — which
    otherwise pollutes the dashboard activity feed on every `pytest` run."""
    try:
        import portfolio.position_log as pl
        monkeypatch.setattr(pl, "_LEDGER_PATH", tmp_path / "_positions_ledger.json", raising=False)
    except Exception:
        pass
    try:
        import portfolio.trade_history as th
        monkeypatch.setattr(th, "_FILLS_PATH", tmp_path / "_fills.jsonl", raising=False)
    except Exception:
        pass


@pytest.fixture(autouse=True)
def _isolate_bot_db(tmp_path, monkeypatch):
    """Isolate the system-of-record SQLite store (data_layer.store._DB) to a tmp file.

    Several tests exercise the real book build and deliberately WIPE the store to reset the
    gate (test_phase2 unlinks the DB via `if _DB.exists(): _DB.unlink()`). Because
    ``store._DB`` is an ABSOLUTE path resolved at import, those unlinks hit the LIVE
    data/bot.db — nuking the production ``runs`` table and, with it, the same-day dedup gate
    that stops every intraday call from triggering a full rebuild. ``store.connect()`` reads
    the ``_DB`` module global at call time, so redirecting it here points every test's
    connect() AND any per-test unlink at the isolated copy instead (mirrors how
    _isolate_positions_ledger / _isolate_runlog protect their files). Idempotent + safe: a
    test that never touches the store simply gets an unused tmp path."""
    try:
        from data_layer import store
        monkeypatch.setattr(store, "_DB", tmp_path / "bot.db", raising=False)
    except Exception:
        pass


@pytest.fixture(autouse=True)
def _isolate_macro_risk_state(tmp_path, monkeypatch):
    """Isolate the macro-risk fragility DWELL state machine (brain/macro_risk).

    ``risk_state`` now persists a dwell record to ``data/macro_risk/state_machine.json`` and reads the
    dated derisk_*.json artifacts to detect a recent severity>=2 tripwire. Both are ABSOLUTE paths
    resolved at import. Without isolation a test that escalates the state (e.g. a risk_off synthetic
    read) would WRITE the live state file and the NEXT test would load it — the escalation is sticky by
    design, so ``test_risk_on_state`` would inherit a leaked risk_off state and fail. Redirect the state
    file to a fresh tmp path per test (cold-start == stateless, today's behaviour) and point the
    artifacts dir at an empty tmp dir so the tripwire read finds nothing. Injection-based dwell tests
    (test_macro_risk_dwell) pass their own loader/saver and are unaffected. Idempotent + safe."""
    try:
        from brain import macro_risk as mr
        mr_data = tmp_path / "_macro_risk"
        mr_data.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(mr, "_STATE_MACHINE", mr_data / "state_machine.json", raising=False)
        monkeypatch.setattr(mr, "_ARTIFACTS", mr_data, raising=False)
    except Exception:
        pass


@pytest.fixture(autouse=True)
def _isolate_gate_severity_artifacts(tmp_path, monkeypatch):
    """Isolate the gate's severity-tripwire reader (brain/gate._default_severity_reader).

    The A6 wake-trigger reads ``gate._ROOT/data/macro_risk/<asof>/derisk_*.json`` off the LIVE
    filesystem to decide whether a severity>=2 tripwire should force a same-day rebuild. That path is
    resolved from an ABSOLUTE module root at call time. If any prior run (dev or another test) has left
    a severity>=2 ``derisk_*.json`` in the worktree's live data/macro_risk/<asof>/ dir, EVERY
    gate.should_run() at that asof would fire 'severity_tripwire' and the same-day dedup carry-forward
    (test_phase2::test_runs_table_written_and_dedup) breaks — a false rebuild. Redirect the gate's root
    at a fresh tmp dir per test so the tripwire reader finds no artifacts (severity 0 == today's default,
    no spurious wake). Tests that want the trigger inject their own severity_reader and are unaffected.
    Idempotent + safe."""
    try:
        from brain import gate
        monkeypatch.setattr(gate, "_ROOT", tmp_path / "_gate_root", raising=False)
    except Exception:
        pass


@pytest.fixture(autouse=True)
def _isolate_research_papers(tmp_path, monkeypatch):
    """Force the deterministic research-paper path (no armed Claude session during pytest) and
    redirect the papers/feed-note writes into a tmp dir, so the book build's research gate never
    hits the network nor pollutes the live research feed."""
    monkeypatch.setenv("MASTERMIND_RESEARCH_LLM", "0")
    # Never let the FastAPI startup hook fire a real (armed) Brain run during pytest, even when a
    # test mounts the app via TestClient on a machine with the Claude CLI present (which would
    # otherwise spawn a live Opus session + write a real book). Covers every Brain book.
    monkeypatch.setenv("AUTONOMOUS_FIRST_RUN", "0")
    monkeypatch.setenv("CHINA_FIRST_RUN", "0")
    monkeypatch.setenv("ETF_FIRST_RUN", "0")
    try:
        import brain.research_paper as rp
        papers = tmp_path / "_papers"
        papers.mkdir(parents=True, exist_ok=True)
        notes = tmp_path / "_papernotes"
        notes.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(rp, "_PAPERS", papers, raising=False)
        monkeypatch.setattr(rp, "_INDEX", papers / "index.jsonl", raising=False)
        monkeypatch.setattr(rp, "_NOTES", notes, raising=False)
    except Exception:
        pass


@pytest.fixture(autouse=True)
def _isolate_wl_marks_and_benchmark(tmp_path, monkeypatch):
    """Isolate the W-L / L1 artifacts (the ONE marking layer's carry store + per-day logs, the
    benchmark ledger's output dir + rolling series, and the Self-Directed NAV history) so a test that
    runs the daily-mark job or builds the ledger never writes into the LIVE data/ tree. All are
    ABSOLUTE module paths resolved at import; redirect them per test. Idempotent + safe."""
    try:
        from portfolio import marks
        md = tmp_path / "_marks"
        monkeypatch.setattr(marks, "_MARKS_DIR", md, raising=False)
        monkeypatch.setattr(marks, "_CARRY_PATH", md / "last_good.json", raising=False)
    except Exception:
        pass
    try:
        from brain import benchmark_ledger as bl
        monkeypatch.setattr(bl, "_BENCH_DIR", tmp_path / "_benchmark", raising=False)
    except Exception:
        pass
    try:
        from portfolio import self_directed as sd
        sdd = tmp_path / "_self_directed"
        monkeypatch.setattr(sd, "_DATA", sdd, raising=False)
        monkeypatch.setattr(sd, "_ACCOUNT_PATH", sdd / "account.json", raising=False)
        monkeypatch.setattr(sd, "_NAV_PATH", sdd / "nav_history.jsonl", raising=False)
        monkeypatch.setattr(sd, "_FILLS_PATH", sdd / "fills.jsonl", raising=False)
        monkeypatch.setattr(sd, "_PENDING_PATH", sdd / "pending.json", raising=False)
        monkeypatch.setattr(sd, "_THESES_PATH", sdd / "theses.json", raising=False)
    except Exception:
        pass


@pytest.fixture(autouse=True)
def _no_tushare_network(monkeypatch):
    """Default the Tushare feed OFF in tests so the suite never makes a live network call (and
    _live_price falls back to the vendored snapshot); the tushare-specific tests re-enable it
    with a mocked _call."""
    try:
        from data_layer import tushare_feed
        tushare_feed.clear_cache()
        monkeypatch.setattr(tushare_feed, "_token", lambda: None)
        yield
        tushare_feed.clear_cache()
    except Exception:
        yield


@pytest.fixture(autouse=True)
def _no_yahoo_network(monkeypatch):
    """Default the Yahoo HK feed OFF in tests: stub the yfinance import so the real warm() degrades
    to a no-op and _live_price falls back to the vendored snapshot. HK-specific tests install a fake
    yfinance via sys.modules to exercise the parse path without a live call."""
    try:
        import sys
        from data_layer import yahoo_feed
        yahoo_feed.clear_cache()
        monkeypatch.setitem(sys.modules, "yfinance", None)
        yield
        yahoo_feed.clear_cache()
    except Exception:
        yield


@pytest.fixture(autouse=True)
def _clear_fx_cache():
    """The China book's FX rate memo (portfolio.fx) is a process-global; clear it around every
    test so an FX-dependent test is order-independent (a real rate read in one test can't leak a
    non-fallback rate into another)."""
    try:
        from portfolio import fx
        fx.clear_cache()
        yield
        fx.clear_cache()
    except Exception:
        yield


@pytest.fixture(autouse=True)
def _disable_peer_sentinel(monkeypatch):
    """Disable the R9 peer-expectation sentinel (MASTERMIND_PEER_SENTINEL) by default in all
    tests.  Existing firm_exposure tests seed only a subset of the 4 firm US books — that is
    intentional for the scenario under test, not a sign that the pipeline failed.  With the
    sentinel armed, those tests fail because an unseeded (but expected) peer has no latest.json.
    The sentinel tests in test_mw1_guardrails.py re-enable it explicitly with
    ``monkeypatch.setenv("MASTERMIND_PEER_SENTINEL", "1")`` which overrides this default."""
    monkeypatch.setenv("MASTERMIND_PEER_SENTINEL", "0")


@pytest.fixture(autouse=True)
def _clear_portfolios_cache():
    """/api/portfolios memoises its assembled status payload for a short TTL (a process-global);
    clear it around every test so a payload built in one test can't leak stale NAV/return chips
    into a later test that seeds a different book."""
    try:
        from app import web
        web._portfolios_cache.clear()
        yield
        web._portfolios_cache.clear()
    except Exception:
        yield


@pytest.fixture(autouse=True)
def _isolate_experiment_registry(tmp_path, monkeypatch):
    """Isolate the W-L / L6 experiment registry (brain/experiment_registry._REGISTRY_PATH) so
    that tests that call brain.experiment_registry.matured() (directly or via improvement_agenda)
    never promote items in the real data/experiments/registry.json — which is the long-lived seed
    for the programme and must not be mutated by the test suite.  Provides a COPY of the real seed
    so registry-shape tests still see a realistic corpus; the copy is discarded after each test.
    Idempotent + safe."""
    try:
        import brain.experiment_registry as er
        import shutil
        isolated_dir = tmp_path / "_experiments"
        isolated_dir.mkdir(parents=True, exist_ok=True)
        isolated_path = isolated_dir / "registry.json"
        real_path = er._REGISTRY_PATH
        if real_path.exists():
            shutil.copy2(real_path, isolated_path)
        monkeypatch.setattr(er, "_REGISTRY_PATH", isolated_path, raising=False)
    except Exception:
        pass
