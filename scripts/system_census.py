"""scripts/system_census.py — GENERATED system census for Mastermind.

Emits two files:
  data/census/latest.json   — machine-readable census (authoritative; R10)
  data/census/CENSUS.md     — human-readable summary

GENERATED — do not hand-edit; architecture docs must cite this file.
Ruling R10: generated state is authoritative over prose.

Sections
--------
  A  JOBS        — scheduler registrations (id, cron spec, timezone, day_of_week)
  B  BOOKS       — portfolio registry (id, kind, manager, benchmark, currency)
  C  FLAGS       — MASTERMIND_* env vars (set/known-not-set; secret values masked)
  D  ENDPOINTS   — FastAPI routes (method, path, open vs auth-gated, LLM-triggering); includes app/auth.py routes
  E  ARTIFACTS   — external artifact read paths per reader module
  F  GUARDRAILS  — GuardrailResult construction sites found in the codebase

Run:
  python3 scripts/system_census.py
  python3 scripts/system_census.py --json   (print latest.json to stdout)
"""
from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

_SECRET_PATTERN = re.compile(r"(PASSWORD|TOKEN|KEY)", re.IGNORECASE)


def _mask_value(name: str, value: str) -> str:
    """Mask the value when the flag name looks like a secret."""
    if _SECRET_PATTERN.search(name):
        return "<set>"
    return value


def _git_sha() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(_ROOT), stderr=subprocess.DEVNULL, text=True,
        ).strip()
    except Exception:
        return "unknown"


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# A. JOBS — parse app/scheduler.py by importing the module (guarded) or
#            static parsing as a fallback.
# ---------------------------------------------------------------------------

_CRON_JOB_PATTERN = re.compile(
    r'sch\.add_job\s*\(\s*\S+\s*,\s*CronTrigger\(([^)]+)\)\s*,\s*id=(["\'])([^"\']+)\2',
    re.DOTALL,
)


def _extract_cron_arg(args_str: str, key: str) -> str | None:
    """Extract a keyword argument value from a CronTrigger(…) argument string."""
    pat = re.compile(key + r'\s*=\s*([^\s,)]+|"[^"]*"|\'[^\']*\')')
    m = pat.search(args_str)
    if not m:
        return None
    v = m.group(1).strip("'\"")
    return v


def _collect_jobs() -> list[dict]:
    """Parse app/scheduler.py statically to enumerate all cron jobs.

    Static parsing is used (not live import) to avoid triggering the
    APScheduler background threads and SQLite jobstore during a census run.

    Hour values that reference Python variables (e.g. ``hour``, ``a_hour``) are
    resolved against the env-var defaults extracted from the scheduler source so
    the cron_spec is human-readable without running the scheduler.
    """
    sched_path = _ROOT / "app" / "scheduler.py"
    if not sched_path.exists():
        return []
    src = sched_path.read_text()

    # Extract env-var → int defaults from lines like:
    #   hour = int(os.environ.get("BOT_DAILY_UTC_HOUR", "22"))
    env_default_re = re.compile(
        r'(\w+)\s*=\s*int\s*\(os\.environ\.get\s*\(\s*["\'][^"\']+["\'],\s*["\'](\d+)["\']'
    )
    # Handle int(os.environ.get("FOO", str(other_var))) — default is a variable reference
    env_int_var_re = re.compile(
        r'(\w+)\s*=\s*int\s*\(os\.environ\.get\s*\(\s*["\'][^"\']+["\'],\s*str\s*\((\w+)\s*\)'
    )
    # Handle multi-value env vars like "2,6,11" and string env vars like "sun"
    # Covers: x = (os.environ.get("FOO", "val").strip() or "val")
    #         x = os.environ.get("FOO", "val")
    env_str_re = re.compile(
        r'(\w+)\s*=\s*\(?os\.environ\.get\s*\(\s*["\'][^"\']+["\'],\s*["\']([^"\']+)["\']'
    )
    var_defaults: dict[str, str] = {}
    for m in env_default_re.finditer(src):
        var_defaults[m.group(1)] = m.group(2)
    for m in env_int_var_re.finditer(src):
        name, ref = m.group(1), m.group(2)
        if name not in var_defaults and ref in var_defaults:
            var_defaults[name] = var_defaults[ref]
    for m in env_str_re.finditer(src):
        if m.group(1) not in var_defaults:
            var_defaults[m.group(1)] = m.group(2).strip().strip('"').strip("'")
    # Handle variable references like:
    #   agenda_dow = os.environ.get("AGENDA_WEEKLY_DAY", cio_dow)  → resolve via var_defaults
    var_ref_re = re.compile(
        r'(\w+)\s*=\s*\(?os\.environ\.get\s*\(\s*["\'][^"\']+["\'],\s*(\w+)\s*\)'
    )
    for m in var_ref_re.finditer(src):
        name, ref = m.group(1), m.group(2)
        if name not in var_defaults and ref in var_defaults:
            var_defaults[name] = var_defaults[ref]

    def _resolve(raw: str | None) -> str | None:
        """Replace a variable reference with its default value."""
        if raw is None:
            return None
        # Already a literal (digit, comma-sep, */N, range)
        if re.match(r'^[\d,*\-/]+$', raw):
            return raw
        # Python variable name — look up default
        return var_defaults.get(raw, raw)

    jobs = []
    for m in _CRON_JOB_PATTERN.finditer(src):
        args_str = m.group(1)
        job_id = m.group(3)
        # extract individual cron fields
        hour_raw = _extract_cron_arg(args_str, "hour")
        minute_raw = _extract_cron_arg(args_str, "minute")
        day_of_week_raw = _extract_cron_arg(args_str, "day_of_week")
        timezone_ = _extract_cron_arg(args_str, "timezone")
        hour = _resolve(hour_raw)
        minute = _resolve(minute_raw)
        day_of_week = _resolve(day_of_week_raw)
        # build a cron-spec string for display
        dow = day_of_week or "*"
        h = hour or "*"
        mi = minute or "0"
        cron_spec = f"{mi} {h} * * {dow}"
        jobs.append({
            "id": job_id,
            "cron_spec": cron_spec,
            "hour": hour,
            "minute": minute,
            "day_of_week": day_of_week,
            "timezone": timezone_ or "UTC",
        })
    return jobs


# ---------------------------------------------------------------------------
# B. BOOKS — from portfolio/registry.py PORTFOLIOS
# ---------------------------------------------------------------------------

def _collect_books() -> list[dict]:
    try:
        # Safe import: registry has no side effects
        sys.path.insert(0, str(_ROOT))
        from portfolio import registry  # type: ignore[import]
        books = []
        for p in registry.PORTFOLIOS:
            books.append({
                "id": p["id"],
                "name": p.get("name", ""),
                "kind": p.get("kind", ""),
                "manager": p.get("manager", ""),
                "benchmark": p.get("benchmark", "SPY"),
                "currency": p.get("currency", "USD"),
                "venues": p.get("venues") or [],
                "legacy": bool(p.get("legacy")),
            })
        return books
    except Exception as exc:
        return [{"error": str(exc)}]


# ---------------------------------------------------------------------------
# C. FLAGS — control_plane.flags.enumerate_flags() + KNOWN_FLAGS
# ---------------------------------------------------------------------------

def _collect_flags() -> dict:
    try:
        from control_plane.flags import enumerate_flags, KNOWN_FLAGS  # type: ignore[import]
    except ImportError:
        return {"error": "control_plane.flags not importable", "set": {}, "known_not_set": []}

    raw = enumerate_flags()
    masked: dict[str, str] = {}
    for name, value in raw.items():
        masked[name] = _mask_value(name, value)

    known_not_set = sorted(f for f in KNOWN_FLAGS if f not in raw)

    return {
        "set": masked,
        "known_not_set": known_not_set,
    }


# ---------------------------------------------------------------------------
# D. ENDPOINTS — static parse of app/main.py + app/web.py
#
# LLM-triggering paths (hardcoded; maintenance comment below).
# MAINTENANCE: update this list whenever a route is added/removed that
# triggers a Claude CLI call (cli_bridge.reason / research / chat_stream).
# ---------------------------------------------------------------------------

_LLM_TRIGGERING_PATHS = frozenset({
    "POST /reason",
    "POST /research",
    "POST /chat",
    "POST /api/autonomous/run",
    "POST /api/heavyweight/run",
    "POST /api/china/run",
    "POST /api/hk/run",
    "POST /api/etf/run",
    "POST /daily",
})

_ROUTE_PATTERN = re.compile(
    r'@(?:app|router)\.(get|post|put|delete|patch)\s*\(\s*(["\'])([^"\']+)\2',
    re.IGNORECASE,
)


def _collect_endpoints() -> list[dict]:
    """Static-parse app/main.py, app/web.py, and app/auth.py for route declarations."""
    try:
        from app import auth  # type: ignore[import]
        open_paths: set[str] = set(getattr(auth, "_OPEN_PATHS", {"/login", "/logout", "/health"}))
    except Exception:
        open_paths = {"/login", "/logout", "/health"}

    endpoints = []
    for src_file in (_ROOT / "app" / "main.py", _ROOT / "app" / "web.py", _ROOT / "app" / "auth.py"):
        if not src_file.exists():
            continue
        src = src_file.read_text()
        for m in _ROUTE_PATTERN.finditer(src):
            method = m.group(1).upper()
            path = m.group(3)
            key = f"{method} {path}"
            endpoints.append({
                "method": method,
                "path": path,
                "open": path in open_paths,
                "llm_triggering": key in _LLM_TRIGGERING_PATHS,
                "source": src_file.name,
            })

    # Deduplicate (main.py auth routes appear twice — once registered, once defined)
    seen: set[str] = set()
    deduped = []
    for ep in endpoints:
        key = f"{ep['method']} {ep['path']}"
        if key not in seen:
            seen.add(key)
            deduped.append(ep)
    return deduped


# ---------------------------------------------------------------------------
# E. EXTERNAL ARTIFACTS — static grep of vendor/macro read paths per module
# ---------------------------------------------------------------------------

_READER_MODULES: list[tuple[str, Path]] = [
    ("portfolio.lenses",           _ROOT / "portfolio" / "lenses.py"),
    ("brain.intake",               _ROOT / "brain" / "intake.py"),
    ("brain.china_intake",         _ROOT / "brain" / "china_intake.py"),
    ("brain.etf_board",            _ROOT / "brain" / "etf_board.py"),
    ("brain.gate_officer",         _ROOT / "brain" / "gate_officer.py"),
    ("brain.regime_frame",         _ROOT / "brain" / "regime_frame.py"),
    ("data_layer.macro_refresh",   _ROOT / "data_layer" / "macro_refresh.py"),
    ("brain.neural_web_context",   _ROOT / "brain" / "neural_web_context.py"),
]

# Patterns that indicate a vendor/macro artifact read.
# Pattern 1: string literals with a path prefix (used by lenses, macro_refresh,
# intake, china_intake, etf_board which pass relative paths to _read/_load helpers).
_ARTIFACT_PATH_PATTERN = re.compile(
    r'"((?:site|data|factordata|basketdata|altdata|stockdata|sectordata|neuralwebdata'
    r'|china_regime|regime|intelligence|news|vol|flow|transmission)/[a-zA-Z0-9_./\-]+'
    r'\.(?:json|jsonl|parquet|csv))"'
    r'|'
    r"'((?:site|data|factordata|basketdata|altdata|stockdata|sectordata|neuralwebdata"
    r"|china_regime|regime|intelligence|news|vol|flow|transmission)/[a-zA-Z0-9_./\-]+"
    r"\.(?:json|jsonl|parquet|csv))'"
)
# Pattern 2: Path-composition literals like:
#   _ROOT / "vendor" / "macro" / "data" / "regime" / "latest.json"
# Captures the path after "macro" /.
_PATH_COMPOSITION_PATTERN = re.compile(
    r'"macro"\s*/\s*"([^"]+)"(?:\s*/\s*"([^"]+)")*'
)


def _extract_path_composition(src: str) -> list[str]:
    """Extract vendor/macro artifact paths from Path-composition literals.

    Handles patterns like:
      _ROOT / "vendor" / "macro" / "data" / "regime" / "latest.json"
    by finding every run of string literals joined with / after "macro".
    """
    # Find all occurrences of "macro" followed by one or more / "segment" pairs
    pattern = re.compile(
        r'"macro"'                         # anchor on "macro"
        r'(?:\s*/\s*"([^"]+)")+',          # one or more / "segment"
        re.DOTALL
    )
    results = []
    for m in pattern.finditer(src):
        # re.findall gives only last group; use finditer over sub-matches instead
        # Walk the match string to extract each segment
        seg_pat = re.compile(r'/\s*"([^"]+)"')
        segments = seg_pat.findall(m.group(0))
        if segments:
            composed = "/".join(segments)
            # Only include if it ends with a known extension
            if re.search(r'\.(json|jsonl|parquet|csv)$', composed):
                results.append(composed)
    return results


def _collect_artifacts() -> list[dict]:
    """Grep reader modules for vendor/macro artifact paths."""
    results = []
    for module_name, module_path in _READER_MODULES:
        if not module_path.exists():
            results.append({"module": module_name, "error": "file not found", "paths": []})
            continue
        src = module_path.read_text()
        seen_paths: set[str] = set()
        paths: list[str] = []

        # Pattern 1: string literal relative paths
        for m in _ARTIFACT_PATH_PATTERN.finditer(src):
            rel = m.group(1) or m.group(2)
            if rel and rel not in seen_paths:
                seen_paths.add(rel)
                paths.append(rel)

        # Pattern 2: Path-composition paths (_ROOT / "vendor" / "macro" / ...)
        for composed in _extract_path_composition(src):
            if composed not in seen_paths:
                seen_paths.add(composed)
                paths.append(composed)

        results.append({"module": module_name, "artifact_paths": sorted(paths)})
    return results


# ---------------------------------------------------------------------------
# F. GUARDRAILS — grep for GuardrailResult construction sites
# ---------------------------------------------------------------------------

def _collect_guardrails() -> list[dict]:
    """Find GuardrailResult.passed() / GuardrailResult.failed() call sites.

    Skips: tests/, __pycache__, the guardrail definition module itself, and
    this census script (which only mentions GuardrailResult in its output strings).
    """
    this_script = Path(__file__).resolve()
    sites: list[dict] = []
    for py_file in _ROOT.rglob("*.py"):
        # Skip tests, __pycache__, the guardrail module, and this script
        parts = py_file.parts
        if "__pycache__" in parts or "tests" in parts:
            continue
        if py_file.name == "guardrail.py" and "control_plane" in str(py_file):
            continue
        if py_file.resolve() == this_script:
            continue
        try:
            src = py_file.read_text()
        except Exception:
            continue
        if "GuardrailResult" not in src:
            continue
        for lineno, line in enumerate(src.splitlines(), 1):
            if "GuardrailResult.passed" in line or "GuardrailResult.failed" in line:
                rel = str(py_file.relative_to(_ROOT))
                kind = "passed" if "GuardrailResult.passed" in line else "failed"
                sites.append({"file": rel, "line": lineno, "kind": kind,
                               "snippet": line.strip()[:120]})
    return sites


# ---------------------------------------------------------------------------
# assemble census
# ---------------------------------------------------------------------------

def build_census() -> dict[str, Any]:
    sha = _git_sha()
    ts = _now_iso()
    return {
        "_meta": {
            "generated_at": ts,
            "git_sha": sha,
            "notice": "GENERATED — do not hand-edit; architecture docs must cite this file.",
            "ruling": "R10",
        },
        "jobs": _collect_jobs(),
        "books": _collect_books(),
        "flags": _collect_flags(),
        "endpoints": _collect_endpoints(),
        "artifacts": _collect_artifacts(),
        "guardrails": _collect_guardrails(),
    }


# ---------------------------------------------------------------------------
# markdown renderer
# ---------------------------------------------------------------------------

def _render_markdown(census: dict[str, Any]) -> str:
    meta = census["_meta"]
    lines: list[str] = [
        "# Mastermind System Census",
        "",
        f"> GENERATED — do not hand-edit; architecture docs must cite this file. (R10)  ",
        f"> Generated at: `{meta['generated_at']}`  ",
        f"> Git SHA: `{meta['git_sha']}`",
        "",
    ]

    # A. JOBS
    lines += ["## A. Scheduled Jobs", ""]
    jobs = census.get("jobs") or []
    lines.append(f"**{len(jobs)} jobs registered** in `app/scheduler.py`")
    lines.append("")
    lines.append("| id | cron_spec | hour | minute | day_of_week | timezone |")
    lines.append("|---|---|---|---|---|---|")
    for j in jobs:
        lines.append(
            f"| `{j['id']}` | `{j['cron_spec']}` | {j.get('hour','*')} "
            f"| {j.get('minute','0')} | {j.get('day_of_week') or '*'} "
            f"| {j.get('timezone','UTC')} |"
        )
    lines.append("")

    # B. BOOKS
    lines += ["## B. Portfolio Books", ""]
    books = [b for b in (census.get("books") or []) if "error" not in b]
    lines.append(f"**{len(books)} books** in `portfolio/registry.py`")
    lines.append("")
    lines.append("| id | kind | manager | benchmark | currency |")
    lines.append("|---|---|---|---|---|")
    for b in books:
        lines.append(
            f"| `{b['id']}` | {b.get('kind','')} | {b.get('manager','')} "
            f"| {b.get('benchmark','SPY')} | {b.get('currency','USD')} |"
        )
    lines.append("")

    # C. FLAGS
    lines += ["## C. MASTERMIND_* Flags", ""]
    flags = census.get("flags") or {}
    set_flags = flags.get("set") or {}
    known_not_set = flags.get("known_not_set") or []
    lines.append(f"**{len(set_flags)} flags currently set** in environment:")
    lines.append("")
    if set_flags:
        lines.append("| Name | Value |")
        lines.append("|---|---|")
        for name, val in sorted(set_flags.items()):
            lines.append(f"| `{name}` | `{val}` |")
        lines.append("")
    lines.append(f"**{len(known_not_set)} known flags NOT set:**  ")
    lines.append(", ".join(f"`{f}`" for f in known_not_set) or "_(none)_")
    lines.append("")

    # D. ENDPOINTS
    lines += ["## D. API Endpoints", ""]
    endpoints = census.get("endpoints") or []
    lines.append(f"**{len(endpoints)} routes** across `app/main.py` + `app/web.py` + `app/auth.py`")
    lines.append("")
    lines.append("| method | path | open | LLM |")
    lines.append("|---|---|---|---|")
    for ep in endpoints:
        open_mark = "open" if ep.get("open") else "auth"
        llm_mark = "LLM" if ep.get("llm_triggering") else ""
        lines.append(
            f"| {ep['method']} | `{ep['path']}` | {open_mark} | {llm_mark} |"
        )
    lines.append("")

    # E. ARTIFACTS
    lines += ["## E. External Artifact Read Paths", ""]
    for entry in (census.get("artifacts") or []):
        if "error" in entry:
            lines.append(f"- **{entry['module']}**: _{entry['error']}_")
            continue
        paths = entry.get("artifact_paths") or []
        lines.append(f"### `{entry['module']}` ({len(paths)} paths)")
        if paths:
            for p in paths:
                lines.append(f"- `{p}`")
        else:
            lines.append("_(no artifact paths detected)_")
        lines.append("")

    # F. GUARDRAILS
    lines += ["## F. GuardrailResult Construction Sites", ""]
    guards = census.get("guardrails") or []
    if guards:
        lines.append(f"**{len(guards)} construction site(s):**")
        lines.append("")
        for g in guards:
            lines.append(
                f"- `{g['file']}:{g['line']}` — `{g['kind']}` — _{g.get('snippet','')}_"
            )
    else:
        lines.append(
            "_(none found — GuardrailResult is defined but not yet called outside tests "
            "and its own module. L2 lane will add call sites.)_"
        )
    lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# entrypoint
# ---------------------------------------------------------------------------

def main() -> None:
    census = build_census()

    out_dir = _ROOT / "data" / "census"
    out_dir.mkdir(parents=True, exist_ok=True)

    json_path = out_dir / "latest.json"
    md_path = out_dir / "CENSUS.md"

    json_path.write_text(json.dumps(census, indent=2, default=str))
    md_path.write_text(_render_markdown(census))

    print(f"census written -> {json_path}")
    print(f"markdown  written -> {md_path}")

    jobs = census.get("jobs") or []
    books = census.get("books") or []
    flags = census.get("flags") or {}
    print(f"  jobs={len(jobs)}  books={len(books)}  "
          f"flags_set={len(flags.get('set') or {})}  "
          f"endpoints={len(census.get('endpoints') or [])}")

    if "--json" in sys.argv:
        print(json_path.read_text())


if __name__ == "__main__":
    main()
