"""NW feedback artifact v1 — machine-readable governance summary for NW.

Writes site/mastermind/nw_feedback.json as a sibling of mastermind_snapshot.json.
The file is pushed to the PUBLIC macro repo and treated as fully public — it MUST
NOT contain dollar amounts, position sizes, API keys, or secrets of any kind.

Schema: mastermind_nw_feedback.v1

Contents (per-book governance snapshot):
  - gate_failures: guardrail event counts (by severity/guard) from
    data/governance/run_events.jsonl for the last N days
  - rejected_count: number of rejected candidate decisions from the
    book's latest.json (count only — no names, no text, no context)
  - lock_conflicts: count of lock_conflict events for the book
  - stale_freeze_count: count of FREEZE-severity events for the book
  - thesis_counts: open/closed counts from brain/ledger.py (read-only)
  - run_counts: success/failure counts by job from run_events.jsonl

Public-surface hard constraints (tested in tests/test_nw_feedback.py):
  1. No dollar amounts / numeric values that look like $ amounts
  2. No API-key-shaped strings (long hex/base64 tokens)
  3. No MASTERMIND_* env variable names or values
  4. No position sizes (only counts of decisions, not weights/notional)

Integration:
  bridge/macro_snapshot.write() calls nw_feedback.build() best-effort (never-raise).
  The write() call writes BOTH JSONs in one pass.

Lane B owns this file. Do NOT add a new scheduler job — the snapshot scheduler
(app/scheduler.py publish_macro_snapshot -> scripts/export_macro_snapshot) already
covers the build+push path via bridge/macro_snapshot.write().
"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent.parent
SCHEMA = "mastermind_nw_feedback.v1"

# How many days of run_events to include in the gate-failure window.
_EVENT_WINDOW_DAYS = 14

# Public-surface guard patterns — values matching these must never appear in the output.
_SECRET_PATTERNS = [
    re.compile(r'\bMASTERMIND_[A-Z_]+\b'),          # env var names
    re.compile(r'\$[\d,]+'),                          # dollar amounts
    re.compile(r'\b\d{4,}\.?\d*\s*(USD|usd)\b'),     # large numeric USD values
    re.compile(r'(?i)\b[A-Za-z0-9+/]{40,}\b'),       # API-key-shaped long tokens
]


def _run_events_path() -> Path:
    return _ROOT / "data" / "governance" / "run_events.jsonl"


def _load_events(window_days: int = _EVENT_WINDOW_DAYS) -> list[dict]:
    """Load run_events.jsonl; return events within the look-back window. Never raises."""
    try:
        p = _run_events_path()
        if not p.exists():
            return []
        cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)
        rows: list[dict] = []
        for line in p.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except Exception:
                continue
            ts_raw = ev.get("ts", "")
            try:
                # parse ISO-8601 with or without timezone
                if ts_raw.endswith("Z"):
                    ts_raw = ts_raw[:-1] + "+00:00"
                ts = datetime.fromisoformat(ts_raw)
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                if ts < cutoff:
                    continue
            except Exception:
                pass  # include on parse error (conservative)
            rows.append(ev)
        return rows
    except Exception:  # never raise
        return []


_KEY_MAX_LEN = 64
_KEY_SAFE_RE = re.compile(r'^[a-z0-9_:.\-]+$')


def _sanitize_key(raw: str) -> str:
    """Sanitize a by_guard / step key for the public artifact.

    Applies two rules:
      1. Length-cap: truncate to _KEY_MAX_LEN characters.
      2. Charset: only [a-z0-9_:.-] allowed.  Any other character → replace the entire
         key with 'other' (length-cap is applied after downcasing so a mixed-case safe key
         stays readable; an unsafe key becomes 'other' regardless of length).
    Keys feed a public artifact; an injected colon-separated path or long token must not
    reach the output verbatim.
    """
    s = str(raw)[:_KEY_MAX_LEN].lower()
    return s if _KEY_SAFE_RE.match(s) else "other"


def _gate_failures(events: list[dict], book: str) -> dict:
    """Return gate failure counts for one book: by_severity and by_guard.

    Only guardrail events with ok=False (status='error') are counted.
    Guard/step keys are sanitized before inclusion (see _sanitize_key).
    """
    by_severity: dict[str, int] = defaultdict(int)
    by_guard: dict[str, int] = defaultdict(int)
    for ev in events:
        if ev.get("kind") != "guardrail":
            continue
        if ev.get("book", "") != book:
            continue
        if ev.get("status") != "error":
            continue
        sev = ev.get("severity") or "UNKNOWN"
        raw_guard = (ev.get("extra") or {}).get("guard") or ev.get("step") or "unknown"
        guard = _sanitize_key(str(raw_guard))
        by_severity[sev] += 1
        by_guard[guard] += 1
    return {
        "by_severity": dict(by_severity),
        "by_guard": dict(by_guard),
        "total": sum(by_severity.values()),
    }


def _rejected_count(book_id: str) -> int:
    """Count rejected decisions for a book from latest.json. Count only — no content."""
    try:
        from portfolio import registry
        latest_path = registry.data_dir(book_id) / "latest.json"
        if not latest_path.exists():
            return 0
        latest = json.loads(latest_path.read_text())
        return len(latest.get("rejected") or [])
    except Exception:
        return 0


def _lock_conflict_count(events: list[dict], book: str) -> int:
    """Count lock_conflict events for a book in the window."""
    return sum(
        1 for ev in events
        if ev.get("kind") == "lock_conflict" and ev.get("book", "") == book
    )


def _stale_freeze_count(events: list[dict], book: str) -> int:
    """Count FREEZE-severity events for a book in the window."""
    return sum(
        1 for ev in events
        if ev.get("severity") == "FREEZE" and ev.get("book", "") == book
    )


def _thesis_counts() -> dict:
    """Read brain/ledger.py theses — return open/closed counts. Never reads positions/sizes."""
    try:
        from brain import ledger
        theses = ledger.all_theses()
        open_n = sum(1 for t in theses if t.get("status", "open") == "open")
        closed_n = len(theses) - open_n
        return {"open": open_n, "closed_or_rebuilt": closed_n, "total": len(theses)}
    except Exception:
        return {"open": 0, "closed_or_rebuilt": 0, "total": 0}


def _run_counts(events: list[dict]) -> dict:
    """Success/failure counts by job, derived from run_finished events in the window."""
    by_job: dict[str, dict[str, int]] = defaultdict(lambda: {"ok": 0, "error": 0, "other": 0})
    for ev in events:
        if ev.get("kind") != "run_finished":
            continue
        job = ev.get("job") or "unknown"
        status = ev.get("status") or "other"
        bucket = "ok" if status == "ok" else "error" if status == "error" else "other"
        by_job[job][bucket] += 1
    return {job: dict(counts) for job, counts in by_job.items()}


def _book_entry(book_id: str, events: list[dict]) -> dict:
    """Assemble one book's governance entry. Only counts — no text, no sizes, no secrets."""
    return {
        "book_id": book_id,
        "gate_failures": _gate_failures(events, book_id),
        "rejected_decision_count": _rejected_count(book_id),
        "lock_conflict_count": _lock_conflict_count(events, book_id),
        "stale_freeze_count": _stale_freeze_count(events, book_id),
    }


def build(window_days: int = _EVENT_WINDOW_DAYS) -> dict:
    """Return the mastermind_nw_feedback.v1 payload. Never raises."""
    try:
        events = _load_events(window_days)

        try:
            from portfolio import registry
            book_ids = registry.ids()
        except Exception:
            book_ids = ["flagship"]

        books = [_book_entry(bid, events) for bid in book_ids]

        payload: dict[str, Any] = {
            "schema": SCHEMA,
            "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "window_days": window_days,
            "thesis_counts": _thesis_counts(),
            "run_counts": _run_counts(events),
            "books": books,
            "note": (
                "Machine-readable governance summary for NW integration. "
                "Contains counts only — no position sizes, no notional values, no secrets."
            ),
        }
        return payload
    except Exception as exc:
        # Never raise — return a minimal valid payload so the caller never breaks
        return {
            "schema": SCHEMA,
            "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "window_days": window_days,
            "error": f"build failed: {type(exc).__name__}",
            "books": [],
            "thesis_counts": {"open": 0, "closed_or_rebuilt": 0, "total": 0},
            "run_counts": {},
            "note": "partial build — see error field",
        }


def _redact_secrets(payload: dict) -> dict:
    """Scan the serialized payload for secret-shaped strings and redact matching values.

    Walks the payload in-place (returns the modified dict).  Any string value that
    matches a _SECRET_PATTERNS pattern is replaced with "<redacted>".  Emits a
    GuardrailResult(ADVISORY_ONLY) run-event for each offending key found so the
    incident is logged without ever blocking the publish.
    """
    def _redact_value(v: object, path: str) -> object:
        if isinstance(v, str):
            for pat in _SECRET_PATTERNS:
                if pat.search(v):
                    try:
                        from control_plane.guardrail import GuardrailResult, Severity
                        result = GuardrailResult.failed(
                            guard="nw_feedback_redaction",
                            severity=Severity.ADVISORY_ONLY,
                            detail=f"secret-shaped value redacted at {path!r}",
                            action_taken="value replaced with <redacted>",
                            extra={"path": path, "pattern": pat.pattern},
                        )
                        result.log(job="export_macro_snapshot")
                    except Exception:
                        pass  # logging failure must never prevent the redaction
                    return "<redacted>"
            return v
        if isinstance(v, dict):
            return {k: _redact_value(val, f"{path}.{k}") for k, val in v.items()}
        if isinstance(v, list):
            return [_redact_value(item, f"{path}[{i}]") for i, item in enumerate(v)]
        return v

    return _redact_value(payload, "root")  # type: ignore[return-value]


def write(dest_site_dir: Path | str | None = None) -> Path:
    """Build the feedback artifact and write it to <dest>/mastermind/nw_feedback.json.

    Mirrors the write() signature of bridge/macro_snapshot.py.
    Returns the written path. Never raises.

    Secret guard: before writing, the serialized JSON is scanned for secret-shaped
    values (_SECRET_PATTERNS). Any match is redacted (value → "<redacted>") and an
    ADVISORY_ONLY GuardrailResult run-event is emitted. The publish is never blocked
    by the guard — a partial redaction is better than a silent publish failure.
    """
    try:
        if dest_site_dir is None:
            # Default: same vendor/macro/site/ destination as macro_snapshot
            dest = _ROOT / "vendor" / "macro" / "site"
        else:
            dest = Path(dest_site_dir)
        out_dir = dest / "mastermind"
        out_dir.mkdir(parents=True, exist_ok=True)
        out = out_dir / "nw_feedback.json"
        payload = build()
        # Scan for and redact any secret-shaped values before writing to the public artifact.
        payload = _redact_secrets(payload)
        out.write_text(json.dumps(payload, indent=2, default=str, ensure_ascii=False))
        return out
    except Exception as exc:
        # If we can't write, log to stdout and return a dummy path — never raise into scheduler
        print(f"[nw_feedback] write failed (non-fatal): {exc}")
        return Path("/dev/null")
