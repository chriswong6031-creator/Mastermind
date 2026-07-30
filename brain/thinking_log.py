"""Bot-side AI response + thinking-trace ledger — `mastermind.response_log.v1`, surface "bot".

The macro repo's PR #3781 (lib/mastermind_response_log.py + brain_gateway thinking
capture) gave BOTH chat surfaces ("macro", "terminal") a reasoning-trace corpus so the
operator can assess whether the AI wrestles contradictory site signals — split
our-data-is-wrong from genuine market splits. This module extends that program to the
trading bot's LLM seats (research desk, PM/flagship, strategist, gate/risk officers,
sentinel, CIO, the per-book brains): every turn through brain/cli_bridge or the
brain/client Messages-API fallback logs one immutable row under a THIRD surface:

    mastermind_response_logs/bot/<YYYY-MM-DD>/<id>.json      (R2, admin-visible)
    data/response_logs/bot/<YYYY-MM-DD>/<id>.json            (local mirror, always)

The admin "AI Response Logs" tab ingests the whole R2 prefix, tolerates any surface
value and preserves extra keys — so bot rows appear under its existing surface filter
with no macro-side change. Bot rows add attribution keys the chat surfaces don't have:
`seat`, `book`, `role`, `armed`, `backend`, `run_id`, `key_id`.

SCHEMA MIRROR, NOT AN IMPORT: the source of truth is the macro repo's
lib/mastermind_response_log.py (squash 92c341e345d). The vendored sparse checkout is
force-reset every build and its pin can lag main (it currently predates the `thinking`
field), so importing it would tie a live-bot write path to vendor freshness. The row
builder, thinking sanitizer (`_clean_segment`/`_clean_thinking`, caps 6000 chars/segment,
24 segments kept FIRST N-1 + LAST so the synthesis survives truncation) and R2 sink are
mirrored here verbatim-in-shape; keep them in sync with upstream when the schema revs.

LEAK LAW (mirrors brain_gateway's `thinking_out`): thinking text is LOG-ONLY. It flows
capture-site → this module → sinks, and must NEVER enter a returned result dict, an SSE
event, or any trade-decision path. Capture sites pass it via side-channel lists.

FAIL-SOFT: every public function is best-effort and never raises into the caller. No R2
creds → local mirror only. Logging is off only via MASTERMIND_RESPONSE_LOG_DISABLED /
MASTERMIND_BOT_RESPONSE_LOG_DISABLED.
"""
from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA = "mastermind.response_log.v1"
R2_PREFIX = "mastermind_response_logs"
SURFACE = "bot"

_ROOT = Path(__file__).resolve().parent.parent

# Upstream caps (lib/mastermind_response_log.py) — do not drift.
_THINKING_TEXT_CAP = 6000
_THINKING_MAX_SEGMENTS = 24
_QUESTION_CAP = 8000
_ANSWER_CAP = 24000


# ---------------------------------------------------------------------------
# Config / gating
# ---------------------------------------------------------------------------

def _truthy(v: str | None) -> bool:
    return str(v or "").strip().lower() in ("1", "true", "yes", "on")


def enabled() -> bool:
    """On by default (the local mirror is always a valid sink); the upstream
    kill-switch and a bot-only kill-switch both disable."""
    if _truthy(os.environ.get("MASTERMIND_RESPONSE_LOG_DISABLED")):
        return False
    if _truthy(os.environ.get("MASTERMIND_BOT_RESPONSE_LOG_DISABLED")):
        return False
    return True


def _local_dir() -> Path:
    d = os.environ.get("MASTERMIND_BOT_RESPONSE_LOG_DIR", "").strip()
    return Path(d) if d else (_ROOT / "data" / "response_logs")


def _r2_creds() -> bool:
    return bool(
        os.environ.get("R2_ENDPOINT")
        and os.environ.get("R2_ACCESS_KEY_ID")
        and os.environ.get("R2_SECRET_ACCESS_KEY")
        and os.environ.get("R2_BUCKET")
    )


# ---------------------------------------------------------------------------
# Row building (upstream mirror + bot extras)
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _clip(s: Any, n: int) -> str:
    t = "" if s is None else str(s)
    return t if len(t) <= n else t[:n] + " …[truncated]"


def _clean_segment(seg: Any) -> dict | None:
    """Sanitize ONE reasoning segment, or None if it carries nothing worth keeping.

    Clips text to _THINKING_TEXT_CAP; coerces round→int and phase/model→str; drops an
    empty-text segment UNLESS it is redacted (a redacted_thinking block is evidence the
    model reasoned even though the text is unavailable). Upstream-verbatim."""
    if not isinstance(seg, dict):
        return None
    redacted = bool(seg.get("redacted"))
    text = _clip(seg.get("text") or "", _THINKING_TEXT_CAP)
    if not text and not redacted:
        return None
    try:
        rnd = int(seg.get("round") or 0)
    except (TypeError, ValueError):
        rnd = 0
    item: dict[str, Any] = {
        "round": rnd,
        "phase": str(seg.get("phase") or ""),
        "model": str(seg.get("model") or ""),
        "text": text,
    }
    if redacted:
        item["redacted"] = True
    return item


def _clean_thinking(thinking: Any) -> list[dict]:
    """Cap the trace at _THINKING_MAX_SEGMENTS keeping FIRST (N-1) + LAST — the
    synthesis segment rides last and is the decision the corpus exists to show, so
    head-truncation would drop the most valuable segment. Upstream-verbatim."""
    out: list[dict] = []
    if not isinstance(thinking, list):
        return out
    tail: dict | None = None
    for seg in thinking:
        item = _clean_segment(seg)
        if item is None:
            continue
        if len(out) < _THINKING_MAX_SEGMENTS - 1:
            out.append(item)
        else:
            tail = item
    if tail is not None:
        out.append(tail)
    return out


def build_row(
    *,
    question: str,
    answer: str,
    model: str = "",
    seat: str | None = None,
    book: str | None = None,
    role: str | None = None,
    mode: str | None = None,
    backend: str | None = None,
    armed: bool = False,
    run_id: str | None = None,
    key_id: str | None = None,
    thread_id: str | None = None,
    latency_ms: int | None = None,
    input_tokens: int = 0,
    output_tokens: int = 0,
    tools: list | None = None,
    thinking: list | None = None,
    flags: dict | None = None,
    row_id: str | None = None,
) -> dict:
    """One `mastermind.response_log.v1` row, surface "bot". Pure — no I/O, never raises.

    Core keys match the upstream contract field-for-field (lane is null on this
    surface; user_ref is the constant "bot" — no human, nothing to hash; citations
    stay []). seat/book/role/armed/backend/run_id/key_id are additive bot keys the
    admin preserves and can filter on."""
    provider = "claude_api" if str(model).startswith("claude") else (
        "openai" if str(model).startswith(("gpt-", "o")) else (
            "deepseek" if "deepseek" in str(model).lower() else None))
    if backend == "codex":
        provider = "openai_codex"
    elif backend in ("sdk", "cli"):
        provider = "claude_code"
    row: dict[str, Any] = {
        "id": row_id or uuid.uuid4().hex,
        "schema": SCHEMA,
        "ts": _now_iso(),
        "surface": SURFACE,
        "lane": None,
        "mode": mode or None,
        "model": str(model or "unknown"),
        "provider": provider,
        "thread_id": str(thread_id) if thread_id else None,
        "user_ref": "bot",
        "question": _clip(question, _QUESTION_CAP),
        "answer": _clip(answer, _ANSWER_CAP),
        "input_tokens": int(input_tokens or 0),
        "output_tokens": int(output_tokens or 0),
        "latency_ms": int(latency_ms) if latency_ms is not None else None,
        "context": {},
        "citations": [],
        "tools": [str(t) for t in tools] if isinstance(tools, list) else [],
        "lang": None,
        "flags": {
            "filtered": bool((flags or {}).get("filtered")),
            "degraded": bool((flags or {}).get("degraded")),
            "error": bool((flags or {}).get("error")),
            "screened": bool((flags or {}).get("screened")),
        },
        "thinking": _clean_thinking(thinking),
        # --- bot-surface additive keys (admin tolerates + preserves extras) ---
        "seat": str(seat) if seat else None,
        "book": str(book) if book else None,
        "role": str(role) if role else None,
        "armed": bool(armed),
        "backend": str(backend) if backend else None,
        "run_id": str(run_id) if run_id else None,
        "key_id": str(key_id) if key_id else None,
    }
    return row


# ---------------------------------------------------------------------------
# Sinks
# ---------------------------------------------------------------------------

def object_key(row: dict) -> str:
    ts = str(row.get("ts") or "")
    date = ts[:10] if len(ts) >= 10 and ts[4] == "-" else _today()
    rid = row.get("id") or uuid.uuid4().hex
    return f"{R2_PREFIX}/{SURFACE}/{date}/{rid}.json"


def _write_local(row: dict) -> None:
    p = _local_dir() / Path(object_key(row)).relative_to(R2_PREFIX)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(row, ensure_ascii=False), encoding="utf-8")
    tmp.replace(p)


def _client():
    """Boto3 S3 client for R2, or None when creds/boto3 are absent (graceful no-op,
    upstream-mirror)."""
    ep = os.environ.get("R2_ENDPOINT")
    ak = os.environ.get("R2_ACCESS_KEY_ID")
    sk = os.environ.get("R2_SECRET_ACCESS_KEY")
    if not (ep and ak and sk):
        return None
    try:
        import boto3  # noqa: PLC0415
        from botocore.config import Config  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return None
    kw = dict(region_name="auto", signature_version="s3v4",
              retries={"max_attempts": 2, "mode": "standard"},
              connect_timeout=5, read_timeout=10)
    try:
        cfg = Config(**kw, request_checksum_calculation="when_required",
                     response_checksum_validation="when_required")
    except TypeError:
        cfg = Config(**kw)
    try:
        return boto3.client("s3", endpoint_url=ep, aws_access_key_id=ak,
                            aws_secret_access_key=sk, config=cfg)
    except Exception:  # noqa: BLE001
        return None


def _write_r2(row: dict) -> bool:
    s3 = _client()
    if s3 is None:
        return False
    try:
        s3.put_object(
            Bucket=os.environ["R2_BUCKET"],
            Key=object_key(row),
            Body=json.dumps(row, ensure_ascii=False).encode("utf-8"),
            ContentType="application/json",
        )
        return True
    except Exception:  # noqa: BLE001
        return False


def write_row(row: dict) -> bool:
    """Persist to every configured sink (local mirror always; R2 when creds exist).
    True if at least one sink accepted it. Never raises."""
    if not enabled():
        return False
    ok = False
    try:
        _write_local(row)
        ok = True
    except Exception:  # noqa: BLE001
        pass
    try:
        if _r2_creds() and _write_r2(row):
            ok = True
    except Exception:  # noqa: BLE001
        pass
    return ok


def log_turn(**kwargs: Any) -> bool:
    """Build + write one turn row. Best-effort; never raises into the caller."""
    try:
        if not enabled():
            return False
        return write_row(build_row(**kwargs))
    except Exception:  # noqa: BLE001
        return False


def log_turn_async(**kwargs: Any) -> None:
    """Fire-and-forget — the R2 PUT rides a daemon thread so a slow network call
    never delays a reasoning turn. Never raises."""
    if not enabled():
        return
    try:
        t = threading.Thread(target=log_turn, kwargs=kwargs, daemon=True)
        t.start()
    except Exception:  # noqa: BLE001
        log_turn(**kwargs)
