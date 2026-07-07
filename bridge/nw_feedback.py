"""NW feedback artifact v2 — machine-readable governance summary for NW.

Writes site/mastermind/nw_feedback.json as a sibling of mastermind_snapshot.json.
The file is pushed to the PUBLIC macro repo and treated as fully public — it MUST
NOT contain dollar amounts, position sizes, API keys, or secrets of any kind.

Schema: mastermind_nw_feedback.v2

Contents (per-book governance snapshot):
  - gate_failures: guardrail event counts (by severity/guard) from
    data/governance/run_events.jsonl for the last N days
  - rejected_count: number of rejected candidate decisions from the
    book's latest.json (count only — no names, no text, no context)
  - lock_conflicts: count of lock_conflict events for the book
  - stale_freeze_count: count of FREEZE-severity events for the book
  - thesis_counts: open/closed counts from brain/ledger.py (read-only)
  - run_counts: success/failure counts by job from run_events.jsonl

v2 additions (counts-only, FB-R11):
  - decision_flow: packet_accepted/packet_rejected counts per book + top
    rejection error classes (sanitised field labels only, ≤10 classes)
  - outcome_mix: resolved-outcome counts by band from outcome_ledger.jsonl;
    n_resolved; counts only — no thesis_ids, subjects, or returns
  - context_audit: context engagement counts from nw_context_audit.jsonl
    (present/stale/absent runs), context_seen_rate, n_runs_total
  - metric_families: live + blocked family registry (FB-R9)

Public-surface hard constraints (tested in tests/test_nw_feedback.py):
  1. No dollar amounts / numeric values that look like $ amounts
  2. No API-key-shaped strings (long hex/base64 tokens)
  3. No MASTERMIND_* env variable names or values
  4. No position sizes (only counts of decisions, not weights/notional)
  5. No ticker strings, IDs (rejection_id/packet_id/thesis_id), raw prose

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
SCHEMA = "mastermind_nw_feedback.v2"

# How many days of run_events to include in the gate-failure window.
_EVENT_WINDOW_DAYS = 14

# Maximum number of rejection error classes emitted in decision_flow.
_MAX_REJECTION_CLASSES = 10

# Maximum number of by_outcome keys emitted in outcome_mix.
_MAX_OUTCOME_KEYS = 12

# Public-surface guard patterns — values matching these must never appear in the output.
_SECRET_PATTERNS = [
    re.compile(r'\bMASTERMIND_[A-Z_]+\b'),          # env var names
    re.compile(r'\$[\d,]+'),                          # dollar amounts
    re.compile(r'\b\d{4,}\.?\d*\s*(USD|usd)\b'),     # large numeric USD values
    re.compile(r'(?i)\b[A-Za-z0-9+/]{40,}\b'),       # API-key-shaped long tokens
]


def _run_events_path() -> Path:
    return _ROOT / "data" / "governance" / "run_events.jsonl"


def _packet_rejections_path() -> Path:
    return _ROOT / "data" / "governance" / "packet_rejections.jsonl"


def _outcome_ledger_path() -> Path:
    return _ROOT / "data" / "brain" / "outcome_ledger.jsonl"


def _nw_context_audit_path() -> Path:
    return _ROOT / "data" / "brain" / "nw_context_audit.jsonl"


def _parse_ts(ts_raw: str) -> datetime | None:
    """Parse an ISO-8601 timestamp; return None on failure."""
    try:
        if ts_raw.endswith("Z"):
            ts_raw = ts_raw[:-1] + "+00:00"
        ts = datetime.fromisoformat(ts_raw)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return ts
    except Exception:
        return None


def _cutoff(window_days: int) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=window_days)


def _load_events(window_days: int = _EVENT_WINDOW_DAYS) -> list[dict]:
    """Load run_events.jsonl; return events within the look-back window. Never raises."""
    try:
        p = _run_events_path()
        if not p.exists():
            return []
        cut = _cutoff(window_days)
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
            ts = _parse_ts(ts_raw)
            if ts is not None and ts < cut:
                continue  # exclude old events
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


# ---------------------------------------------------------------------------
# v2: decision_flow — packet_accepted/rejected per book + error class counts
# ---------------------------------------------------------------------------

def _classify_error(error_str: str) -> str:
    """Classify a rejection error string by its leading field name.

    Error strings have the form '<field_name>: <prose>' or '<field_name>[n]: <prose>'.
    We extract the leading identifier (up to the first colon or bracket) and sanitize
    it as a key. Returns 'other' if no leading field name is found.

    Counts only — the error prose itself never appears in the output.
    """
    s = str(error_str).strip()
    # Extract the portion before the first colon or bracket
    m = re.match(r'^([A-Za-z_][A-Za-z0-9_]*)', s)
    if m:
        return _sanitize_key(m.group(1))
    return "other"


def _decision_flow(events: list[dict], window_days: int) -> dict:
    """Build decision_flow block from run_events.jsonl and packet_rejections.jsonl.

    Per book: packet_accepted count, packet_rejected count (from run_events).
    rejection_error_classes: top ≤10 sanitised field-name classes from
      packet_rejections.jsonl rows in-window (counts only — no prose, no IDs).
    Never raises — missing/corrupt ledger yields an absent block signal.
    """
    try:
        # Per-book packet accepted/rejected counts from run_events
        book_accepted: dict[str, int] = defaultdict(int)
        book_rejected: dict[str, int] = defaultdict(int)
        for ev in events:
            kind = ev.get("kind", "")
            book = ev.get("book", "")
            if kind == "packet_accepted":
                book_accepted[book] += 1
            elif kind == "packet_rejected":
                book_rejected[book] += 1

        by_book: list[dict] = []
        all_books = sorted(set(list(book_accepted.keys()) + list(book_rejected.keys())))
        for book in all_books:
            by_book.append({
                "book_id": book,
                "packet_accepted": book_accepted.get(book, 0),
                "packet_rejected": book_rejected.get(book, 0),
            })

        # Rejection error classes from packet_rejections.jsonl
        error_classes: dict[str, int] = defaultdict(int)
        try:
            p = _packet_rejections_path()
            if p.exists():
                cut = _cutoff(window_days)
                for line in p.read_text().splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except Exception:
                        continue
                    # Window filter
                    ts = _parse_ts(row.get("ts", ""))
                    if ts is not None and ts < cut:
                        continue
                    # Classify each error string by leading field name only
                    for err in (row.get("errors") or []):
                        cls = _classify_error(str(err))
                        error_classes[cls] += 1
        except Exception:
            pass  # fail-soft: missing rejections ledger yields empty classes

        # Top ≤10 classes
        top_classes = dict(
            sorted(error_classes.items(), key=lambda kv: -kv[1])[:_MAX_REJECTION_CLASSES]
        )

        return {
            "by_book": by_book,
            "rejection_error_classes": top_classes,
        }
    except Exception:
        return {"by_book": [], "rejection_error_classes": {}}


# ---------------------------------------------------------------------------
# v2: outcome_mix — resolved-outcome counts from outcome_ledger.jsonl
# ---------------------------------------------------------------------------

def _outcome_mix(window_days: int) -> dict:
    """Build outcome_mix block from data/brain/outcome_ledger.jsonl.

    Counts rows with asof_resolved in-window by the 'outcome' field value.
    Returns {by_outcome: {str(value): count}, n_resolved}.
    Counts only — no thesis_ids, subjects, or realized returns.
    Never raises — missing/corrupt ledger yields absent block.
    """
    try:
        p = _outcome_ledger_path()
        if not p.exists():
            return {"state": "absent", "n_resolved": 0, "by_outcome": {}}
        cut = _cutoff(window_days)
        by_outcome: dict[str, int] = defaultdict(int)
        n_resolved = 0
        for line in p.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except Exception:
                continue
            asof_raw = row.get("asof_resolved", "")
            if not asof_raw:
                continue
            ts = _parse_ts(str(asof_raw) + "T00:00:00+00:00" if "T" not in str(asof_raw) else str(asof_raw))
            if ts is not None and ts < cut:
                continue
            outcome_val = row.get("outcome")
            if outcome_val is None:
                continue
            # Sanitize outcome value to a safe key before storing (MAJOR-1).
            safe_key = _sanitize_key(str(outcome_val))
            by_outcome[safe_key] += 1
            n_resolved += 1
        # Cap to _MAX_OUTCOME_KEYS: keep top by count, break ties lexicographically.
        if len(by_outcome) > _MAX_OUTCOME_KEYS:
            top_keys = sorted(by_outcome, key=lambda k: (-by_outcome[k], k))[:_MAX_OUTCOME_KEYS]
            by_outcome = defaultdict(int, {k: by_outcome[k] for k in top_keys})
        return {
            "state": "ok",
            "n_resolved": n_resolved,
            "by_outcome": dict(by_outcome),
        }
    except Exception:
        return {"state": "absent", "n_resolved": 0, "by_outcome": {}}


# ---------------------------------------------------------------------------
# v2: context_audit — context engagement from nw_context_audit.jsonl sidecar
# ---------------------------------------------------------------------------

def _context_audit(window_days: int) -> dict:
    """Build context_audit block from data/brain/nw_context_audit.jsonl.

    Counts runs in-window by nw_context status (present/stale/absent).
    Returns {n_present, n_stale, n_absent, n_runs_total, context_seen_rate}.
    If the sidecar doesn't exist yet, returns {state: 'accruing', n_runs_total: 0}.
    Never fabricates data. Never raises.
    """
    try:
        p = _nw_context_audit_path()
        if not p.exists():
            return {"state": "accruing", "n_runs_total": 0}
        cut = _cutoff(window_days)
        counts: dict[str, int] = defaultdict(int)
        n_total = 0
        for line in p.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except Exception:
                continue
            ts = _parse_ts(row.get("ts", ""))
            if ts is not None and ts < cut:
                continue
            status = str(row.get("status", "absent"))
            # MINOR-2: unknown status → bucket into n_absent (conservative: unusable context).
            if status not in {"present", "stale", "absent"}:
                status = "absent"
            counts[status] += 1
            n_total += 1
        if n_total == 0:
            return {"state": "accruing", "n_runs_total": 0}
        n_present = counts.get("present", 0)
        seen_rate = round(n_present / n_total, 3) if n_total > 0 else 0.0
        return {
            "state": "ok",
            "n_present": n_present,
            "n_stale": counts.get("stale", 0),
            "n_absent": counts.get("absent", 0),
            "n_runs_total": n_total,
            "context_seen_rate": seen_rate,
        }
    except Exception:
        return {"state": "accruing", "n_runs_total": 0}


# ---------------------------------------------------------------------------
# v2: metric_families registry (FB-R9, frozen)
# ---------------------------------------------------------------------------

_METRIC_FAMILIES: dict = {
    "live": ["context_engagement", "decision_flow", "outcome_mix"],
    "blocked": [
        {
            "name": "fill_slippage_by_context",
            "reason": "no execution model (paper fills at close/open)",
        },
        {
            "name": "warning_outcome_delta",
            "reason": "needs >=60 context-audit sessions (FB-R10)",
        },
    ],
}


def build(window_days: int = _EVENT_WINDOW_DAYS) -> dict:
    """Return the mastermind_nw_feedback.v2 payload. Never raises."""
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
            # v2 additions — counts-only (FB-R11)
            "decision_flow": _decision_flow(events, window_days),
            "outcome_mix": _outcome_mix(window_days),
            "context_audit": _context_audit(window_days),
            "metric_families": _METRIC_FAMILIES,
            "note": (
                "Machine-readable governance summary for NW integration. "
                "Contains counts only — no position sizes, no notional values, no secrets. "
                "v2 adds decision_flow, outcome_mix, context_audit (all counts-only, FB-R11)."
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
    """Scan the payload for secret-shaped strings and redact matching content.

    Walks the payload recursively (returns the modified structure).  Covers both
    string VALUES and dict KEYS: any string that matches a _SECRET_PATTERNS pattern
    is replaced with "<redacted>" for values, or the key is rewritten to "<redacted>"
    for keys.  On a key collision after rewriting, numeric values are summed; all other
    values keep the last-written value (keys are rare, counts are the common case).

    Pattern-limited — this is a backstop against accidental leakage; producer-side
    sanitization (counts-only ledger reads, _sanitize_key) remains the primary guarantee.

    Emits a GuardrailResult(ADVISORY_ONLY) run-event for each offending path so the
    incident is logged without ever blocking the publish.
    """
    def _redact_str(s: str, path: str) -> str:
        for pat in _SECRET_PATTERNS:
            if pat.search(s):
                try:
                    from control_plane.guardrail import GuardrailResult, Severity
                    result = GuardrailResult.failed(
                        guard="nw_feedback_redaction",
                        severity=Severity.ADVISORY_ONLY,
                        detail=f"secret-shaped string redacted at {path!r}",
                        action_taken="string replaced with <redacted>",
                        extra={"path": path, "pattern": pat.pattern},
                    )
                    result.log(job="export_macro_snapshot")
                except Exception:
                    pass  # logging failure must never prevent the redaction
                return "<redacted>"
        return s

    def _redact_value(v: object, path: str) -> object:
        if isinstance(v, str):
            return _redact_str(v, path)
        if isinstance(v, dict):
            out: dict = {}
            for k, val in v.items():
                # Redact the key itself (MAJOR-2).
                safe_k = _redact_str(str(k), f"{path}.{k}[key]") if isinstance(k, str) else k
                redacted_val = _redact_value(val, f"{path}.{k}")
                if safe_k in out:
                    # Collision after key redaction — merge by summing numeric values.
                    existing = out[safe_k]
                    if isinstance(existing, (int, float)) and isinstance(redacted_val, (int, float)):
                        out[safe_k] = existing + redacted_val
                    else:
                        out[safe_k] = redacted_val
                else:
                    out[safe_k] = redacted_val
            return out
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
