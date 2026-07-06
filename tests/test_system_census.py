"""Smoke tests for scripts/system_census.py (L4 deliverable).

Verifies that the census script:
  1. Runs without error and produces both output files.
  2. Contains >= 10 scheduled jobs (actual count 18).
  3. Contains == 7 books (the full registry).
  4. Contains no secret values in the flags.set dict (PASSWORD/TOKEN/KEY names
     must have value "<set>", never the real value).
  5. Generated-at timestamp and git-sha header are present.
  6. Both files are non-empty and valid JSON / non-empty Markdown.
  7. Auth routes (GET /login, POST /login, GET /logout) are present and open-path.
  8. No cron_spec leaks a bare lowercase identifier (unresolved variable name).
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_CENSUS_JSON = _ROOT / "data" / "census" / "latest.json"
_CENSUS_MD = _ROOT / "data" / "census" / "CENSUS.md"

_SECRET_PATTERN = re.compile(r"(PASSWORD|TOKEN|KEY)", re.IGNORECASE)


@pytest.fixture(scope="module")
def census_data(tmp_path_factory):
    """Run system_census.py once and return the parsed JSON census."""
    # Run the script in a subprocess so it resolves imports from the repo root.
    result = subprocess.run(
        [sys.executable, str(_ROOT / "scripts" / "system_census.py")],
        cwd=str(_ROOT),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"system_census.py exited with code {result.returncode}.\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert _CENSUS_JSON.exists(), "data/census/latest.json not written"
    assert _CENSUS_MD.exists(), "data/census/CENSUS.md not written"
    return json.loads(_CENSUS_JSON.read_text())


# ---------------------------------------------------------------------------
# tests
# ---------------------------------------------------------------------------

def test_census_files_non_empty(census_data):
    """Both output files are non-empty."""
    assert _CENSUS_JSON.stat().st_size > 0, "latest.json is empty"
    assert _CENSUS_MD.stat().st_size > 0, "CENSUS.md is empty"


def test_census_meta_header(census_data):
    """The _meta block carries a generated_at timestamp and git_sha."""
    meta = census_data.get("_meta") or {}
    assert meta.get("generated_at"), "_meta.generated_at missing"
    assert meta.get("git_sha"), "_meta.git_sha missing"
    assert "GENERATED" in (meta.get("notice") or ""), "_meta.notice missing"


def test_census_has_at_least_10_jobs(census_data):
    """At least 10 scheduled jobs are emitted (actual baseline: 18)."""
    jobs = census_data.get("jobs") or []
    assert len(jobs) >= 10, (
        f"Expected >= 10 jobs, got {len(jobs)}. "
        "Check that app/scheduler.py cron registrations are being parsed."
    )


def test_census_all_jobs_have_required_fields(census_data):
    """Every job entry has id, cron_spec, hour, minute, timezone."""
    jobs = census_data.get("jobs") or []
    for j in jobs:
        assert j.get("id"), f"Job missing id: {j}"
        assert j.get("cron_spec"), f"Job {j.get('id')} missing cron_spec"
        assert j.get("timezone"), f"Job {j.get('id')} missing timezone"


def test_census_has_7_books(census_data):
    """Exactly 7 books from portfolio.registry.PORTFOLIOS."""
    books = [b for b in (census_data.get("books") or []) if "error" not in b]
    assert len(books) == 7, (
        f"Expected 7 books, got {len(books)}. "
        "Check portfolio/registry.py PORTFOLIOS list."
    )


def test_census_books_have_required_fields(census_data):
    """Every book entry has id, kind, manager, benchmark, currency."""
    books = [b for b in (census_data.get("books") or []) if "error" not in b]
    for b in books:
        assert b.get("id"), f"Book missing id: {b}"
        assert b.get("kind"), f"Book {b.get('id')} missing kind"
        assert b.get("manager"), f"Book {b.get('id')} missing manager"
        assert b.get("benchmark"), f"Book {b.get('id')} missing benchmark"
        assert b.get("currency"), f"Book {b.get('id')} missing currency"


def test_census_no_secret_values_in_flags(census_data):
    """No secret flag value leaks into the census output.

    Any flag whose NAME matches PASSWORD/TOKEN/KEY must have value '<set>',
    never the real credential.
    """
    flags_set = (census_data.get("flags") or {}).get("set") or {}
    for name, value in flags_set.items():
        if _SECRET_PATTERN.search(name):
            assert value == "<set>", (
                f"Secret flag {name!r} has value {value!r} — must be masked to '<set>'"
            )


def test_census_flags_known_not_set_is_list(census_data):
    """flags.known_not_set is a list (possibly empty if all flags are set)."""
    flags = census_data.get("flags") or {}
    assert isinstance(flags.get("known_not_set"), list), (
        "flags.known_not_set must be a list"
    )


def test_census_endpoints_present(census_data):
    """At least the well-known routes are present, including auth routes."""
    endpoints = census_data.get("endpoints") or []
    paths = {(ep["method"], ep["path"]) for ep in endpoints}
    assert ("GET", "/health") in paths, "/health route not found"
    assert ("POST", "/daily") in paths, "POST /daily not found"
    assert ("POST", "/chat") in paths, "POST /chat not found"
    # auth routes from app/auth.py
    assert ("GET", "/login") in paths, "GET /login route not found"
    assert ("POST", "/login") in paths, "POST /login route not found"
    assert ("GET", "/logout") in paths, "GET /logout route not found"


def test_census_auth_routes_are_open_path(census_data):
    """Auth routes (GET /login, POST /login, GET /logout) must be tagged open=True."""
    endpoints = census_data.get("endpoints") or []
    ep_map = {(e["method"], e["path"]): e for e in endpoints}
    for method, path in [("GET", "/login"), ("POST", "/login"), ("GET", "/logout")]:
        ep = ep_map.get((method, path))
        assert ep is not None, f"{method} {path} not found in census endpoints"
        assert ep.get("open") is True, (
            f"{method} {path} must be tagged open=True (it is in _OPEN_PATHS)"
        )


def test_census_cron_specs_no_identifier_leak(census_data):
    """No cron_spec field contains an unresolved lowercase Python identifier.

    Legitimate day abbreviations (mon/tue/wed/thu/fri/sat/sun) are allowed.
    Any other lowercase alpha token indicates an unresolved variable name leaked
    from the static parser (e.g. 'agenda_hour').
    """
    _DAY_ABBREVS = {"sun", "mon", "tue", "wed", "thu", "fri", "sat"}
    # Match sequences of 2+ lowercase letters/underscores (identifiers, not day abbrevs)
    _IDENT_RE = re.compile(r'\b([a-z_]{2,})\b')
    jobs = census_data.get("jobs") or []
    leaks = []
    for j in jobs:
        spec = j.get("cron_spec", "")
        for token in _IDENT_RE.findall(spec):
            # Allow day range tokens like "mon-fri" (split on hyphen)
            parts = re.split(r'[-,]', token)
            bad = [p for p in parts if p and p not in _DAY_ABBREVS]
            if bad:
                leaks.append((j["id"], spec, bad))
    assert not leaks, (
        "cron_spec identifier leak(s) — unresolved variable names in cron specs:\n"
        + "\n".join(f"  job={jid!r} spec={spec!r} tokens={tokens}" for jid, spec, tokens in leaks)
    )


def test_census_llm_triggering_routes_marked(census_data):
    """Known LLM-triggering routes are tagged llm_triggering=True."""
    endpoints = census_data.get("endpoints") or []
    ep_map = {(e["method"], e["path"]): e for e in endpoints}
    for method, path in [
        ("POST", "/reason"),
        ("POST", "/research"),
        ("POST", "/chat"),
    ]:
        ep = ep_map.get((method, path))
        assert ep is not None, f"{method} {path} not in census endpoints"
        assert ep.get("llm_triggering") is True, (
            f"{method} {path} should be marked llm_triggering=True"
        )


def test_census_health_is_open_path(census_data):
    """/health must be tagged open (no auth required)."""
    endpoints = census_data.get("endpoints") or []
    health = next((e for e in endpoints if e["path"] == "/health"), None)
    assert health is not None, "/health not found in census endpoints"
    assert health.get("open") is True, "/health should be marked open=True"


def test_census_artifacts_section_present(census_data):
    """Artifacts section covers the known reader modules."""
    artifacts = census_data.get("artifacts") or []
    modules = {a["module"] for a in artifacts}
    expected = {
        "portfolio.lenses",
        "brain.intake",
        "data_layer.macro_refresh",
        "brain.regime_frame",
    }
    missing = expected - modules
    assert not missing, f"Missing artifact modules: {missing}"


def test_census_guardrails_section_is_list(census_data):
    """Guardrails section is a list (may be empty at this wave — L2 not merged yet)."""
    guards = census_data.get("guardrails")
    assert isinstance(guards, list), "guardrails section must be a list"


def test_census_markdown_contains_headings(census_data):
    """CENSUS.md contains the expected section headings."""
    md = _CENSUS_MD.read_text()
    for heading in ("## A. Scheduled Jobs", "## B. Portfolio Books",
                     "## C. MASTERMIND_* Flags", "## D. API Endpoints",
                     "## E. External Artifact Read Paths",
                     "## F. GuardrailResult Construction Sites"):
        assert heading in md, f"CENSUS.md missing heading: {heading!r}"
