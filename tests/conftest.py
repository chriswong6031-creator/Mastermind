"""Shared test fixtures.

Several tests exercise the real book build (bot.phase2.run) and armed-session
paths, which call brain.runlog.start_run(...) and would otherwise append real
run-log entries into data/brain/runs/ on every `pytest` invocation — cluttering
the live "Brain Activity" feed. Isolate the run-log to a tmp dir for all tests.
"""
import sys
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# engine.canon hermetic injection
# ---------------------------------------------------------------------------
# portfolio.distribution_tells imports ``from engine import canon`` — a module
# that lives in vendor/macro_src (a gitignored sparse checkout present on the
# production Mac but absent in fresh worktrees and CI).  Without it the entire
# test_distribution_tells.py suite fails with ImportError before any test runs.
#
# Fix: inject the hermetic stub from tests/fixtures/engine_stub/ whenever the
# real vendor/macro_src is absent.  The stub contains ONLY the functions
# distribution_tells uses (resample_sessions, _resample_weekly, rsi_macd, and
# their primitives), snapshotted from vendor/macro_src/engine/canon.py on
# 2026-07-06.  Module code is NOT changed (ruling L4).
#
# If vendor/macro_src is present, its canon.py is used as-is (no override).
_ROOT = Path(__file__).resolve().parent.parent
_MACRO_SRC = _ROOT / "vendor" / "macro_src"
_STUB_DIR = Path(__file__).resolve().parent / "fixtures" / "engine_stub"


def _ensure_engine_canon() -> None:
    """Inject the hermetic engine.canon stub into sys.modules when macro_src is absent.

    Only ``engine.canon`` is injected — NOT the full engine package.  This avoids
    shadowing the real ``engine.*`` modules that other tests need (e.g. engine.signal_archive
    in test_self_tune, engine.lib.store in test_single_name_factor).  If vendor/macro_src
    is present it is added to sys.path so the real canon is found naturally.
    """
    if "engine.canon" in sys.modules:
        return  # already imported (real or stub) — do nothing
    if _MACRO_SRC.exists():
        # Real vendor checkout is present — add it to sys.path so all engine.* imports work
        if str(_MACRO_SRC) not in sys.path:
            sys.path.insert(0, str(_MACRO_SRC))
        return  # the real canon.py will be found via normal import machinery
    # vendor/macro_src absent: inject ONLY engine.canon from the hermetic stub.
    # The full engine package is NOT registered here; other engine.* submodules remain
    # importable only if vendor/macro_src or vendor/macro is on sys.path (not our job).
    import importlib.util
    import types
    stub_canon = _STUB_DIR / "canon.py"
    if not stub_canon.exists():
        return
    spec = importlib.util.spec_from_file_location("engine.canon", stub_canon)
    if not (spec and spec.loader):
        return
    mod = importlib.util.module_from_spec(spec)
    # Only register an "engine" package stub if none exists yet.
    # If another conftest or test already registered engine (e.g. via vendor/macro_src),
    # we must NOT overwrite it — that would shadow the real engine package.
    if "engine" not in sys.modules:
        pkg = types.ModuleType("engine")
        pkg.__path__ = [str(_STUB_DIR)]  # type: ignore[assignment]
        pkg.__package__ = "engine"
        sys.modules["engine"] = pkg
    sys.modules["engine.canon"] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]


_ensure_engine_canon()


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
def _disable_bot_response_ledger(monkeypatch):
    """Keep the AI response/thinking ledger (brain/thinking_log) OFF in tests.

    reason()/chat_stream/call_model log every turn to data/response_logs — and, when
    R2 creds are in the loaded .env, PUT rows into the shared admin corpus. A pytest
    run must never pollute either sink. Tests that assert on the ledger re-enable it
    by delenv-ing this var and pointing MASTERMIND_BOT_RESPONSE_LOG_DIR at tmp_path
    (see tests/test_thinking_log.py::ledger_dir)."""
    monkeypatch.setenv("MASTERMIND_BOT_RESPONSE_LOG_DISABLED", "1")


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
def _isolate_book_state(tmp_path, monkeypatch):
    """Isolate EVERY paper book's settled state (account/nav_history/fills/pending) to a tmp copy.

    This was the last unguarded live-write path in the suite, and it cost us a book: on
    2026-07-26 the Heavyweight account was wiped to ``{cash:0, positions:{}}`` with a
    backdated ``nav:0`` row in nav_history — the dashboard read $0 (−100%) for a book that
    had never lost a cent. Cause: ``tests/test_overnight.py`` called ``run_heavyweight()``
    without the file-local ``iso`` fixture, and ``bot/heavyweight.py`` marks the book
    UNCONDITIONALLY (outside the do_trade branch), so stubbing the brain did not stop it.

    Two distinct roots have to be redirected, because ``paper_account._paths()`` resolves
    them differently:
      * non-default books (heavyweight/autonomous/etf/china/hk/self_directed) →
        ``registry.data_dir()`` → ``registry._ROOT/data/portfolios/<id>``. Patching
        ``paper_account._DATA`` does NOTHING for these — only ``registry._ROOT`` works.
      * the legacy flagship book → the ``paper_account`` module-global path constants.

    The tmp root is SEEDED WITH A COPY of the live tree (like _isolate_experiment_registry)
    rather than left empty, so tests that legitimately READ a real book — e.g. heavyweight
    reading flagship's latest.json — still see realistic state, while every WRITE lands in
    tmp and is discarded. Per-test copy of ~90 small JSON/JSONL files; cheap. Tests wanting
    a pristine/empty book still patch ``registry._ROOT`` themselves and win (their patch
    applies after this autouse one). Idempotent + safe."""
    try:
        import shutil
        from portfolio import registry
        iso_root = tmp_path / "_bookroot"
        for rel in ("portfolio", "portfolios"):
            src, dst = _ROOT / "data" / rel, iso_root / "data" / rel
            if src.exists():
                shutil.copytree(src, dst, dirs_exist_ok=True)
            else:
                dst.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(registry, "_ROOT", iso_root, raising=False)
        try:
            from portfolio import paper_account as pa
            legacy = iso_root / "data" / "portfolio"
            monkeypatch.setattr(pa, "_DATA", legacy, raising=False)
            monkeypatch.setattr(pa, "_ACCOUNT_PATH", legacy / "account.json", raising=False)
            monkeypatch.setattr(pa, "_FILLS_PATH", legacy / "fills.jsonl", raising=False)
            monkeypatch.setattr(pa, "_NAV_PATH", legacy / "nav_history.jsonl", raising=False)
        except Exception:
            pass
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


@pytest.fixture(autouse=True)
def _reset_operator_rate_buckets():
    """MW6: operator rate-limit buckets are module-global mutable state — reset to
    full capacity before every test so collection ORDER is never load-bearing
    (reproduced: mw6 tests draining the LLM bucket failed a later auth test)."""
    try:
        from app import auth as _auth
        _auth.reset_rate_buckets()
    except Exception:
        pass
    yield
