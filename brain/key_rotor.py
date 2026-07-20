"""brain/key_rotor.py — OAuth key-pool rotation for the Mastermind trading bot.

PURPOSE
-------
Tracks estimated usage state for N OAuth tokens discovered from numbered env vars
(CLAUDE_CODE_OAUTH_TOKEN_1..7) plus a legacy bare CLAUDE_CODE_OAUTH_TOKEN.
When the active token hits a quota limit or is org-disabled, the rotation layer
picks the next healthy candidate automatically.

Ported and adapted from engine/neuralweb/key_pool.py in the Macro Dashboard repo
(credit: macro/engine/neuralweb/key_pool.py, cooling/ledger logic at lines ~545–795).

REDLINE (unforgivable):
    A token VALUE must NEVER appear in any log, ledger row, git artifact, exception
    message, or return value.  This module:
      - Discovers tokens via env-var NAMES (CLAUDE_CODE_OAUTH_TOKEN_N).
      - Stores only key_id strings (e.g. "claude_code_oauth_3", "legacy").
      - Never reads, logs, or compares token values.

COOLING HORIZONS
----------------
  window  — 5h quota 429 / "usage limit reached".  Cleared only by reset_hint passage.
  weekly  — weekly quota 429 / "weekly usage limit".  Cleared only by reset_hint passage.
  auth    — 401/403 / org-disabled.  Cleared by a later successful session OR reset_hint.

NEVER-RAISE CONTRACT
--------------------
All public functions catch ALL exceptions, log a warning, and return a safe fallback.
A pool failure must never abort a trading lane.

STATELESS-CATTLE
----------------
All state is in data/metabolism/key_ledger.jsonl (append-only).
No in-process mutable state between calls.
"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Slot → env name mapping (1..7)
# ---------------------------------------------------------------------------

SLOT_ENV: dict[int, str] = {n: f"CLAUDE_CODE_OAUTH_TOKEN_{n}" for n in range(1, 8)}
LEGACY_ENV = "CLAUDE_CODE_OAUTH_TOKEN"
LEGACY_KEY_ID = "legacy"

_ALL_NUMBERED_KEY_IDS = [f"claude_code_oauth_{n}" for n in range(1, 8)]


def key_id(n: int) -> str:
    """Return the canonical key_id for numbered slot N (1..7)."""
    return f"claude_code_oauth_{n}"


def _slot_from_key_id(kid: str) -> int | None:
    """Return the slot number from a key_id, or None for non-numbered keys."""
    m = re.fullmatch(r"claude_code_oauth_(\d+)", kid)
    return int(m.group(1)) if m else None


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_LEDGER_REL = "data/metabolism/key_ledger.jsonl"
_SCHEMA = "metabolism.key_ledger.v1"

# Default cooling TTLs (port of macro key_pool.py constants)
_WINDOW_SECONDS = 5 * 3600     # 5h window 429
_WEEK_SECONDS = 7 * 24 * 3600  # weekly quota


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

def _mm_root() -> Path:
    """Auto-detect Mastermind repo root from this file's location (brain/)."""
    return Path(__file__).resolve().parent.parent


def _ledger_path(root: Path | None = None) -> Path:
    base = root if root is not None else _mm_root()
    return base / _LEDGER_REL


# ---------------------------------------------------------------------------
# Enabled-key filter (METAB_KEYS_ENABLED)
# ---------------------------------------------------------------------------

def enabled_key_ids() -> set[str] | None:
    """Return the enabled key-id set from METAB_KEYS_ENABLED env var, or None.

    None means "all keys enabled" (fail-open default when env var is absent/empty).

    METAB_KEYS_ENABLED is a csv of numeric slots and/or "legacy":
        "3,5,6"     -> {"claude_code_oauth_3", "claude_code_oauth_5", "claude_code_oauth_6"}
        "legacy,3"  -> {"legacy", "claude_code_oauth_3"}
        unknown tokens are silently ignored.

    Returns None on error (NEVER-RAISE).

    Ported from macro key_pool.py:189-220.
    """
    try:
        raw = os.environ.get("METAB_KEYS_ENABLED", "").strip()
        if not raw:
            return None
        enabled: set[str] = set()
        for token in raw.split(","):
            token = token.strip()
            if not token:
                continue
            if token == "legacy":
                enabled.add("legacy")
            elif token.isdigit():
                enabled.add(f"claude_code_oauth_{token}")
            else:
                log.debug("key_rotor.enabled_key_ids: ignoring unknown token %r", token)
        # If we got tokens but none were valid, treat as absent (fail-open)
        return enabled if enabled else None
    except Exception as exc:  # noqa: BLE001
        log.warning("key_rotor.enabled_key_ids: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Ledger I/O
# ---------------------------------------------------------------------------

def _now_ts() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _parse_ts(ts: str) -> datetime | None:
    """Parse an ISO-8601 UTC timestamp string.  Returns None on error."""
    try:
        if ts.endswith("Z"):
            ts = ts[:-1] + "+00:00"
        return datetime.fromisoformat(ts).astimezone(timezone.utc)
    except Exception:  # noqa: BLE001
        return None


def _read_ledger(root: Path | None = None) -> list[dict[str, Any]]:
    """Read all ledger rows.  Returns [] on error or absent file (NEVER-RAISE)."""
    try:
        p = _ledger_path(root)
        if not p.exists():
            return []
        rows: list[dict[str, Any]] = []
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                pass  # corrupt row — skip gracefully
        return rows
    except Exception as exc:  # noqa: BLE001
        log.warning("key_rotor._read_ledger: %s", exc)
        return []


def _write_row(row: dict[str, Any], root: Path | None = None) -> bool:
    """Append one row to the ledger.  Returns True on success.  NEVER raises."""
    try:
        p = _ledger_path(root)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, separators=(",", ":")) + "\n")
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("key_rotor._write_row: %s", exc)
        return False


# ---------------------------------------------------------------------------
# Public ledger functions (ported from macro key_pool.py:545-725)
# ---------------------------------------------------------------------------

def record_session(
    key_id_str: str,
    est_tokens: int = 0,
    cycle_id: str = "",
    stage: str = "",
    outcome: str = "ok",
    root: Path | None = None,
) -> bool:
    """Append one session row to the key ledger.

    REDLINE: records only key_id (a name string, not a token value) + metadata.

    Returns True if the row was written; False on any error.  NEVER raises.
    """
    try:
        row: dict[str, Any] = {
            "schema": _SCHEMA,
            "ts": _now_ts(),
            "key_id": key_id_str,
            "cycle_id": cycle_id,
            "stage": stage,
            "est_tokens": int(est_tokens),
            "outcome": outcome,
        }
        return _write_row(row, root)
    except Exception as exc:  # noqa: BLE001
        log.warning("key_rotor.record_session(%s): %s", key_id_str, exc)
        return False


def mark_cooling(
    key_id_str: str,
    reset_hint: str | None = None,
    cool_kind: str = "window",
    root: Path | None = None,
) -> bool:
    """Mark a key as cooling by appending a cooling row to the ledger.

    Parameters
    ----------
    key_id_str : str
        The key_id to cool (e.g. "claude_code_oauth_3").
    reset_hint : str | None
        ISO-8601 UTC when the key is expected to recover.  Defaults:
          window → +5h, weekly → +7d, auth → +24h.
    cool_kind : str
        "window" (5h quota 429), "weekly" (weekly quota), "auth" (401/403 / org-disabled).

    Returns True if the row was written; False on any error.  NEVER raises.
    """
    try:
        now = datetime.now(timezone.utc)
        if reset_hint is None:
            if cool_kind == "weekly":
                reset_dt = now + timedelta(seconds=_WEEK_SECONDS)
            elif cool_kind == "auth":
                reset_dt = now + timedelta(hours=24)
            else:  # window (default)
                reset_dt = now + timedelta(seconds=_WINDOW_SECONDS)
            reset_hint = reset_dt.isoformat(timespec="seconds")

        row: dict[str, Any] = {
            "schema": _SCHEMA,
            "ts": _now_ts(),
            "key_id": key_id_str,
            "cycle_id": "",
            "stage": "cooling",
            "est_tokens": 0,
            "outcome": "auth_failed" if cool_kind == "auth" else "rate_limited",
            "cool_kind": cool_kind,
            "reset_hint": reset_hint,
        }
        wrote = _write_row(row, root)
        # Cooling state just changed → refresh the published pool view (federation UP).
        # Best-effort: a publish miss must never disturb the cooling write or the loop.
        if wrote:
            try:
                write_pool_status(root=root)
            except Exception:  # noqa: BLE001
                pass
        return wrote
    except Exception as exc:  # noqa: BLE001
        log.warning("key_rotor.mark_cooling(%s): %s", key_id_str, exc)
        return False


def is_cooling(key_id_str: str, root: Path | None = None) -> bool:
    """Return True if key_id is currently in a cooling period.

    Evaluates EVERY cooling horizon independently (not just the most recent):
      - auth: cleared by a later "ok" row (operator rotated the secret) OR reset_hint passage.
      - window/weekly: cleared ONLY by reset_hint passage — an ok row within the same
        quota window does NOT restore exhausted quota.

    Returns False on error (NEVER-RAISE).

    Ported from macro key_pool.py:655-725 (F4 horizon logic preserved verbatim).
    """
    try:
        rows = _read_ledger(root)

        cooling_rows = [
            r for r in rows
            if r.get("key_id") == key_id_str
            and r.get("outcome") in ("rate_limited", "auth_failed")
            and r.get("reset_hint")
        ]
        if not cooling_rows:
            return False

        def _ts_key(r: dict) -> datetime:
            t = _parse_ts(r.get("ts", ""))
            return t if t is not None else datetime.min.replace(tzinfo=timezone.utc)

        now = datetime.now(timezone.utc)

        ok_ts = [
            _ts_key(r) for r in rows
            if r.get("key_id") == key_id_str and r.get("outcome") == "ok"
        ]

        # Per kind, take the LATEST cooling row and apply the kind's clear rule.
        latest_by_kind: dict[str, dict] = {}
        for r in sorted(cooling_rows, key=_ts_key):
            latest_by_kind[r.get("cool_kind", "window")] = r

        for kind, cool_row in latest_by_kind.items():
            reset_dt = _parse_ts(cool_row.get("reset_hint", ""))
            if reset_dt is None:
                continue  # unparseable hint → treat this horizon as resolved
            if now >= reset_dt:
                continue  # horizon expired by time
            # Only 'auth' cooling can be cleared early by a later 'ok' row.
            # 'window'/'weekly' must wait for reset_hint (quota not restored by a success).
            if kind == "auth":
                cooling_ts = _ts_key(cool_row)
                if any(t >= cooling_ts for t in ok_ts):
                    continue  # auth horizon cleared by a later ok row
            return True  # at least one horizon still active

        return False
    except Exception as exc:  # noqa: BLE001
        log.warning("key_rotor.is_cooling(%s): %s", key_id_str, exc)
        return False


def window_load(key_id_str: str, root: Path | None = None) -> int:
    """Return the estimated number of sessions for key_id in the last 5h window.

    Returns 0 on error (NEVER-RAISE).
    """
    try:
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(seconds=_WINDOW_SECONDS)
        rows = _read_ledger(root)
        count = 0
        for row in rows:
            if row.get("key_id") != key_id_str:
                continue
            ts = _parse_ts(row.get("ts", ""))
            if ts is None:
                continue
            if ts >= cutoff:
                count += 1
        return count
    except Exception as exc:  # noqa: BLE001
        log.warning("key_rotor.window_load(%s): %s", key_id_str, exc)
        return 0


# ---------------------------------------------------------------------------
# Macro-side key health (FEDERATION DOWN) — read the Macro Dashboard's richer
# "Raw Key Usage" artifacts, fed by REAL anthropic-ratelimit-* response headers.
#
# The macro repo is vendored read-only under vendor/macro (a symlink to the
# build-managed sparse checkout vendor/macro_src; see data_layer/macro_refresh).
# The whole brain/ package resolves it as _ROOT/"vendor"/"macro" (brain/intake.py
# idiom) — we mirror that here.  Key identity is SHARED: macro key_id "1".."7"/
# "legacy" == bot slot key_ids "claude_code_oauth_N"/"legacy".
#
# FAIL-OPEN INVARIANT (charter P2): every helper here returns a SAFE empty value
# on any miss/staleness/corruption, so candidates() degrades to today's ordering.
# Federation may only REORDER or SKIP bot-side keys — never add, un-cool, or expand.
# ---------------------------------------------------------------------------

_MACRO_BUDGET_REL = "data/metabolism/key_budget_status.json"
_MACRO_LEDGER_REL = "data/metabolism/key_ledger.jsonl"

# Ignore macro budget data whose ts is older than this — a frozen macro pipeline
# must never silently steer the rotor on days-stale utilization (fail-open).
_MACRO_STALE_SECONDS = 36 * 3600
# Only the last ~300 ledger lines are read (the file can be large; one row per session).
_MACRO_LEDGER_TAIL = 300
# An uncleared macro auth-death older than this is ignored (the token may have been
# rotated server-side since; only a RECENT auth-death is trusted to drop a bot key).
_MACRO_AUTH_DEATH_SECONDS = 7 * 24 * 3600
# pct_weekly at/above this is soft-demoted to the back of the candidate list.
_MACRO_WEEKLY_DEMOTE_PCT = 95.0


def _macro_vendor_root() -> Path:
    """The vendored macro repo root (symlink-following happens at read time via Path).

    Mirrors the brain/ package convention (_ROOT/"vendor"/"macro").  Kept as its own
    function so tests can monkeypatch it to a tmp dir.
    """
    return _mm_root() / "vendor" / "macro"


def _macro_bud_to_key_id(k: str) -> str:
    """Map a macro budget/ledger key_id to the bot's canonical key_id.

    Macro uses "1".."7"/"legacy"; the bot uses "claude_code_oauth_N"/"legacy".  A key
    that is already in bot form (or unknown) is returned unchanged.
    """
    k = str(k)
    if k == "legacy":
        return "legacy"
    if k.isdigit():
        return f"claude_code_oauth_{k}"
    return k


def _read_macro_budget(vroot: Path) -> dict[str, Any] | None:
    """Read + freshness-gate the macro key_budget_status.json.

    Returns the parsed dict ONLY when its top-level ``ts`` is within _MACRO_STALE_SECONDS;
    returns None on absent/corrupt/unparseable/stale (fail-open).  NEVER raises.
    """
    try:
        p = vroot / _MACRO_BUDGET_REL
        if not p.exists():
            return None
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return None
        ts = _parse_ts(str(data.get("ts", "")))
        if ts is None:
            return None  # no/!parseable ts → cannot prove freshness → ignore
        age = (datetime.now(timezone.utc) - ts).total_seconds()
        if age > _MACRO_STALE_SECONDS:
            return None  # stale → fail-open to bot-only ordering
        return data
    except Exception as exc:  # noqa: BLE001
        log.warning("key_rotor._read_macro_budget: %s", exc)
        return None


def _read_macro_ledger_tail(vroot: Path) -> list[dict[str, Any]]:
    """Read the last ~_MACRO_LEDGER_TAIL rows of the macro key_ledger.jsonl.

    Returns [] on absent/corrupt/unreadable (fail-open).  NEVER raises.  Efficiency:
    reads the whole file then slices the tail — the macro ledger is bounded (one row
    per session, rotated) so this stays cheap; corrupt rows are skipped, not fatal.
    """
    try:
        p = vroot / _MACRO_LEDGER_REL
        if not p.exists():
            return []
        lines = p.read_text(encoding="utf-8").splitlines()
        rows: list[dict[str, Any]] = []
        for line in lines[-_MACRO_LEDGER_TAIL:]:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                if isinstance(obj, dict):
                    rows.append(obj)
            except json.JSONDecodeError:
                pass
        return rows
    except Exception as exc:  # noqa: BLE001
        log.warning("key_rotor._read_macro_ledger_tail: %s", exc)
        return []


def _macro_key_health(root: Path | None = None) -> dict[str, Any]:
    """Load macro-side key health, returning a compact per-bot-key view.

    Returns
    -------
    {
      "fresh": bool,                       # True only when a fresh budget file was read
      "pct_weekly": {bot_key_id: float},   # macro weekly-utilization %, keyed by BOT key_id
      "auth_dead": set[bot_key_id],        # keys whose last macro row is a RECENT, uncleared
                                           #   auth-death (auth_failed / cool_kind=auth, no later ok)
    }

    A missing/stale/corrupt macro dataset yields {"fresh": False, "pct_weekly": {}, "auth_dead": set()}
    — the fail-open sentinel that leaves candidates() ordering identical to today.  NEVER raises.

    ``root`` overrides the bot repo root (test injection): the macro vendor root becomes
    ``root/vendor/macro``.  When ``root`` is None (production) the seam is _macro_vendor_root()
    — monkeypatch either for tests/ops.
    """
    safe: dict[str, Any] = {"fresh": False, "pct_weekly": {}, "auth_dead": set()}
    try:
        vroot = (root / "vendor" / "macro") if root is not None else _macro_vendor_root()
        budget = _read_macro_budget(vroot)
        if budget is None:
            return safe  # stale/absent → fail-open

        per_key = budget.get("per_key")
        pct_weekly: dict[str, float] = {}
        if isinstance(per_key, dict):
            for raw_kid, rec in per_key.items():
                if not isinstance(rec, dict):
                    continue
                bot_kid = _macro_bud_to_key_id(raw_kid)
                pw = rec.get("pct_weekly")
                try:
                    if pw is not None:
                        pct_weekly[bot_kid] = float(pw)
                except (TypeError, ValueError):
                    pass

        # auth-death detection from the ledger tail: the LAST row per key_id that is an
        # auth failure (outcome=auth_failed OR a cooling row with cool_kind=auth), with no
        # later ok row, within the last 7 days → the token is dead server-side; drop it.
        auth_dead: set[str] = set()
        rows = _read_macro_ledger_tail(vroot)
        now = datetime.now(timezone.utc)

        def _ts_of(r: dict) -> datetime:
            t = _parse_ts(str(r.get("ts", "")))
            return t if t is not None else datetime.min.replace(tzinfo=timezone.utc)

        # group rows by bot key_id, preserving order
        by_key: dict[str, list[dict]] = {}
        for r in rows:
            kid = _macro_bud_to_key_id(r.get("key_id", ""))
            if kid:
                by_key.setdefault(kid, []).append(r)

        for kid, krows in by_key.items():
            krows_sorted = sorted(krows, key=_ts_of)
            last = krows_sorted[-1]
            is_auth_fail = (last.get("outcome") == "auth_failed" or
                            last.get("cool_kind") == "auth")
            if not is_auth_fail:
                continue
            last_ts = _ts_of(last)
            # a later ok row would clear it — but 'last' IS the latest row, so any ok is earlier;
            # still, guard explicitly in case of same-ts ordering ambiguity.
            later_ok = any(r.get("outcome") == "ok" and _ts_of(r) > last_ts for r in krows_sorted)
            if later_ok:
                continue
            if (now - last_ts).total_seconds() <= _MACRO_AUTH_DEATH_SECONDS:
                auth_dead.add(kid)

        return {"fresh": True, "pct_weekly": pct_weekly, "auth_dead": auth_dead}
    except Exception as exc:  # noqa: BLE001
        log.warning("key_rotor._macro_key_health: %s", exc)
        return safe


# ---------------------------------------------------------------------------
# candidates() — ordered list of healthy candidates
# ---------------------------------------------------------------------------

def candidates(root: Path | None = None) -> list[dict]:
    """Return ordered candidate pool entries for the rotation loop.

    Each entry: {"key_id": str, "env_name": str, "cooling": bool, "load": int}

    Ordering:
      1. Non-cooling numbered slots ordered by ascending window_load then slot number.
      2. Cooling numbered slots appended last (emergency fallback).
      3. Legacy bare token: ONLY when zero numbered slots are present (macro V11 rule).

    Enabled-set filtering: if METAB_KEYS_ENABLED filters out ALL present numbered slots,
    fall back to the unfiltered list (mirror of macro R-V10-3 — never strand the loop).

    FEDERATION (macro Raw Key Usage, when fresh — _macro_key_health):
      a. DROP a key whose macro-side state is a recent, uncleared auth-death (dead server-side),
         UNLESS dropping would empty the numbered pool (then keep them — never strand).
      b. Among non-cooling numbered keys, order by macro pct_weekly ASCENDING (least-utilized
         first); tie-break by the existing (window_load, slot) heuristic.
      c. SOFT-DEMOTE keys with macro pct_weekly >= 95 to the back of the non-cooling group
         (still tried if everything else is cooling — NOT dropped).
    FAIL-OPEN INVARIANT (charter P2): missing/stale/corrupt macro data → ordering IDENTICAL to
    the bot-only behaviour above.  Federation only reorders or skips keys already in the env pool;
    it never adds a key, never un-cools a cooling key, never expands beyond METAB_KEYS_ENABLED.

    REDLINE: only env-var names and key_ids are stored; token values are never read.

    Returns [] on error (NEVER-RAISE).
    """
    try:
        enabled = enabled_key_ids()  # None = all enabled

        # Numbered slots: check presence via env-var name (value presence only, not value)
        numbered: list[dict] = []
        for n in range(1, 8):
            env_name = SLOT_ENV[n]
            if not os.environ.get(env_name, ""):
                continue  # slot not present
            kid = key_id(n)
            numbered.append({
                "key_id": kid,
                "env_name": env_name,
                "cooling": is_cooling(kid, root),
                "load": window_load(kid, root),
            })

        # R-V10-3: filter to enabled set; fall back if filter zeroes all present keys
        if enabled is not None and numbered:
            filtered = [c for c in numbered if c["key_id"] in enabled]
            if not filtered:
                log.warning(
                    "key_rotor.candidates: enabled-set %r filtered all present numbered "
                    "slots — falling back to unfiltered list (R-V10-3)",
                    enabled,
                )
                # use unfiltered numbered
            else:
                numbered = filtered

        # Legacy token: include ONLY when zero numbered slots are present (macro V11 suppression)
        all_candidates: list[dict] = []
        if not numbered and os.environ.get(LEGACY_ENV, ""):
            # Legacy is the only path; include it regardless of enabled set
            all_candidates.append({
                "key_id": LEGACY_KEY_ID,
                "env_name": LEGACY_ENV,
                "cooling": is_cooling(LEGACY_KEY_ID, root),
                "load": window_load(LEGACY_KEY_ID, root),
            })
            return all_candidates

        # ── FEDERATION: consult macro-side key health (fail-open when absent/stale) ──
        health = _macro_key_health(root)
        macro_fresh = bool(health.get("fresh"))
        pct_weekly: dict[str, float] = health.get("pct_weekly", {}) if macro_fresh else {}
        auth_dead: set[str] = health.get("auth_dead", set()) if macro_fresh else set()

        # (a) DROP macro auth-dead keys — but never strand the loop: if the drop would
        #     remove every numbered candidate, keep them (a maybe-dead key beats no key).
        if macro_fresh and auth_dead:
            survivors = [c for c in numbered if c["key_id"] not in auth_dead]
            if survivors:
                dropped = [c["key_id"] for c in numbered if c["key_id"] in auth_dead]
                if dropped:
                    log.info("key_rotor.candidates: macro auth-death dropped %s", dropped)
                numbered = survivors
            else:
                log.warning(
                    "key_rotor.candidates: macro auth-death would empty the pool (%s) — "
                    "keeping all keys (never strand the loop)",
                    sorted(auth_dead),
                )

        # Sort non-cooling: when macro is fresh, primary key = pct_weekly ASC with a >=95%
        # soft-demotion bucket (0 = normal, 1 = demoted), tie-broken by the bot heuristic.
        # When macro is absent/stale, demote_bucket=0 and pct_weekly=1e9 for ALL keys, so the
        # sort collapses to the original (load, slot) ordering — byte-identical fail-open.
        def _noncool_sort_key(c: dict) -> tuple:
            kid = c["key_id"]
            pw = pct_weekly.get(kid)
            if macro_fresh and pw is not None:
                demote = 1 if pw >= _MACRO_WEEKLY_DEMOTE_PCT else 0
                primary = pw
            else:
                # No macro read (or no per-key datum): neutral — falls back to bot heuristic.
                demote = 0
                primary = float("inf")
            return (demote, primary, c["load"], _slot_from_key_id(kid) or 99)

        non_cooling = sorted([c for c in numbered if not c["cooling"]], key=_noncool_sort_key)
        cooling_ones = sorted(
            [c for c in numbered if c["cooling"]],
            key=lambda c: (_slot_from_key_id(c["key_id"]) or 99),
        )
        return non_cooling + cooling_ones

    except Exception as exc:  # noqa: BLE001
        log.warning("key_rotor.candidates: %s", exc)
        return []


# ---------------------------------------------------------------------------
# classify_failure() — map error text to a cool_kind
# ---------------------------------------------------------------------------

# STRICT phrases: distinctive, full error-banner wording that a legitimate model
# ANSWER would essentially never contain.  Safe to match against result TEXT.
_AUTH_PHRASES_STRICT = [
    "disabled claude subscription access",
    "invalid bearer token",
    "authentication_error",
    "oauth token has expired",
]
_WINDOW_PHRASES_STRICT = [
    "usage limit reached",
    "you've reached your usage limit",
    "limit will reset",
]
# LOOSE additions: short/generic substrings ("429", "quota", "overloaded", …) that
# are reliable inside an exception repr / error field but appear in ordinary
# English or numbers — matching them against a legitimate short trading answer
# would cool a HEALTHY key and discard the served result.  Applied ONLY when
# source="error".
_AUTH_PHRASES_LOOSE = _AUTH_PHRASES_STRICT + [
    "token expired",
    "revoked",
    "401",
]
_AUTH_403_PHRASES = ["forbidden", "permission"]  # only when combined with "403" (loose path)
_WINDOW_PHRASES_LOOSE = _WINDOW_PHRASES_STRICT + [
    "429",
    "rate_limit",
    "rate limit",
    "overloaded",
    "529",
    "quota",
]
# Indicator phrases for weekly (must also match "week").  The strict (text-path)
# list drops "rate limit": a legitimate answer like "weekly rate limit for visas"
# must not cool a key; the real weekly banner always says "usage limit".
_RATE_OR_LIMIT_LOOSE = ["usage limit", "limit reached", "rate limit"]
_RATE_OR_LIMIT_STRICT = ["usage limit", "limit reached"]

_SDK_ODDITY = "claude code returned an error result"


def classify_failure(text: str | None, source: str = "error") -> tuple[str, str] | None:
    """Classify an error text into a cooling kind.

    Parameters
    ----------
    text : str | None
        The error string or result text to inspect.
    source : str
        "error" — text came from an exception repr / error field / stderr;
        the loose substring lists apply ("429", "quota", "overloaded", …).
        "text" — text is a model-visible RESULT; only the strict anchored
        banner phrases apply, so a legitimate short answer that merely
        mentions "rate limit" or "quota" never cools a healthy key.

    Returns
    -------
    (cool_kind, matched_reason) | None
        None when text is None, too long (> 600 chars), or no match.

    Classification (evaluated in order: auth > weekly > window):
      auth   — subscription disabled / invalid token / (loose: 401 / 403+forbidden)
      weekly — (usage limit OR limit reached OR rate limit) AND "week"
      window — usage-limit banner phrases (loose adds 429/529/rate limit/quota/overloaded)

    The SDK oddity "Claude Code returned an error result" alone is NOT classifiable;
    it must co-occur with one of the above phrases.

    Matching is case-insensitive on COMPACT text only (truncated at 600 chars).
    """
    if text is None:
        return None
    if len(text) > 600:
        return None  # long legitimate analysis — don't cool on it

    t = text.lower()
    loose = source == "error"
    auth_phrases = _AUTH_PHRASES_LOOSE if loose else _AUTH_PHRASES_STRICT
    window_phrases = _WINDOW_PHRASES_LOOSE if loose else _WINDOW_PHRASES_STRICT

    # ── auth ────────────────────────────────────────────────────────────────
    for phrase in auth_phrases:
        if phrase in t:
            return ("auth", phrase)
    if loose and "403" in t and any(p in t for p in _AUTH_403_PHRASES):
        matched = next(p for p in _AUTH_403_PHRASES if p in t)
        return ("auth", f"403+{matched}")

    # ── weekly ──────────────────────────────────────────────────────────────
    rate_or_limit = _RATE_OR_LIMIT_LOOSE if loose else _RATE_OR_LIMIT_STRICT
    if "week" in t and any(p in t for p in rate_or_limit):
        matched = next(p for p in rate_or_limit if p in t)
        return ("weekly", f"{matched}+week")

    # ── window ──────────────────────────────────────────────────────────────
    for phrase in window_phrases:
        if phrase in t:
            return ("window", phrase)

    return None


# ---------------------------------------------------------------------------
# all_cooling_info() — freeze info for present candidates
# ---------------------------------------------------------------------------

def all_cooling_info(root: Path | None = None) -> dict[str, Any]:
    """Return freeze info for all present candidate keys.

    If NOT all cooling, returns {"all_cooling": False}.
    If all cooling, returns:
      {"all_cooling": True, "earliest_reset": "<ISO-8601 UTC>", "key_reset_hints": {...}}

    NEVER raises.  Port of macro key_pool.py:728-777.
    """
    try:
        cands = candidates(root)
        if not cands:
            return {"all_cooling": False}

        present_keys = [c["key_id"] for c in cands]
        cooling_status = {k: is_cooling(k, root) for k in present_keys}
        if not all(cooling_status.values()):
            return {"all_cooling": False}

        # All cooling — collect reset hints
        rows = _read_ledger(root)
        key_resets: dict[str, str] = {}
        for k in present_keys:
            cooling_rows = [
                r for r in rows
                if r.get("key_id") == k
                and r.get("outcome") in ("rate_limited", "auth_failed")
                and r.get("reset_hint")
            ]
            if cooling_rows:
                def _ts_key(r: dict) -> datetime:
                    t = _parse_ts(r.get("ts", ""))
                    return t if t is not None else datetime.min.replace(tzinfo=timezone.utc)
                last = sorted(cooling_rows, key=_ts_key)[-1]
                key_resets[k] = last.get("reset_hint", "")

        parsed_resets = [_parse_ts(v) for v in key_resets.values() if v]
        parsed_resets = [r for r in parsed_resets if r is not None]
        earliest = min(parsed_resets).isoformat(timespec="seconds") if parsed_resets else ""

        return {
            "all_cooling": True,
            "earliest_reset": earliest,
            "key_reset_hints": key_resets,
        }
    except Exception as exc:  # noqa: BLE001
        log.warning("key_rotor.all_cooling_info: %s", exc)
        return {"all_cooling": False}


# ---------------------------------------------------------------------------
# Bot key-pool status (FEDERATION UP) — the bot's CURRENT pool view, published to
# the macro/admin side as data/mastermind/key_pool_status.json.  Schema is EXACT
# (mastermind.key_pool_status.v1); the macro/admin reader is being extended in a
# parallel PR to consume it — keep the field names + shape stable.
# ---------------------------------------------------------------------------

_POOL_STATUS_SCHEMA = "mastermind.key_pool_status.v1"
_POOL_STATUS_REL = "data/mastermind/key_pool_status.json"


def _last_row_for(rows: list[dict], key_id_str: str) -> dict | None:
    """Return the newest ledger row for key_id_str (by ts), or None."""
    def _ts_key(r: dict) -> datetime:
        t = _parse_ts(str(r.get("ts", "")))
        return t if t is not None else datetime.min.replace(tzinfo=timezone.utc)
    krows = [r for r in rows if r.get("key_id") == key_id_str]
    return sorted(krows, key=_ts_key)[-1] if krows else None


def _last_cooling_row_for(rows: list[dict], key_id_str: str) -> dict | None:
    """Return the newest COOLING row (has reset_hint) for key_id_str, or None."""
    def _ts_key(r: dict) -> datetime:
        t = _parse_ts(str(r.get("ts", "")))
        return t if t is not None else datetime.min.replace(tzinfo=timezone.utc)
    krows = [
        r for r in rows
        if r.get("key_id") == key_id_str
        and r.get("outcome") in ("rate_limited", "auth_failed")
        and r.get("reset_hint")
    ]
    return sorted(krows, key=_ts_key)[-1] if krows else None


def pool_status_view(root: Path | None = None) -> dict[str, Any]:
    """Build the bot's current key-pool view (mastermind.key_pool_status.v1).

    A cheap rebuild from the bot's own ledger tail + the present env slots + the enabled set.
    Every key the bot knows about (present numbered slots, plus legacy when it is the only
    path — mirroring candidates()) is listed with its live cooling state and last outcome.

    Returns a dict with keys: schema, ts, keys[].  Each key entry:
      {key_id, enabled, cooling, cool_kind, reset_hint, last_outcome, last_ts}

    REDLINE: only key_id NAMES + metadata — never a token value.  NEVER raises (returns a
    minimal well-formed doc on error).
    """
    ts = _now_ts()
    try:
        enabled = enabled_key_ids()  # None = all enabled
        rows = _read_ledger(root)

        # Present numbered slots (by env-var NAME presence — never the value).
        present: list[str] = [key_id(n) for n in range(1, 8)
                              if os.environ.get(SLOT_ENV[n], "")]
        # Legacy is part of the pool view only when it is the sole path (mirror candidates()).
        if not present and os.environ.get(LEGACY_ENV, ""):
            present = [LEGACY_KEY_ID]

        keys: list[dict[str, Any]] = []
        for kid in present:
            cooling = is_cooling(kid, root)
            cool_row = _last_cooling_row_for(rows, kid) if cooling else None
            last_row = _last_row_for(rows, kid)
            # enabled: None → all enabled; else membership. (An enabled filter that names
            # only absent slots still reports the present keys as enabled=False honestly —
            # candidates() has its own R-V10-3 fallback for actual selection.)
            is_enabled = True if enabled is None else (kid in enabled)
            keys.append({
                "key_id": kid,
                "enabled": bool(is_enabled),
                "cooling": bool(cooling),
                "cool_kind": (cool_row.get("cool_kind") if cool_row else None),
                "reset_hint": (cool_row.get("reset_hint") if cool_row else None),
                "last_outcome": (last_row.get("outcome") if last_row else None),
                "last_ts": (last_row.get("ts") if last_row else None),
            })

        return {"schema": _POOL_STATUS_SCHEMA, "ts": ts, "keys": keys}
    except Exception as exc:  # noqa: BLE001
        log.warning("key_rotor.pool_status_view: %s", exc)
        return {"schema": _POOL_STATUS_SCHEMA, "ts": ts, "keys": []}


def write_pool_status(dest: Path | None = None, root: Path | None = None) -> Path | None:
    """Write the pool-status view to `dest` (default <root>/data/mastermind/key_pool_status.json).

    Uses a temp-file + atomic replace (temp+os.replace law) so a reader never sees a half-written
    file.  Returns the written path, or None on failure.  NEVER raises — a publish miss must never
    disturb a trading lane or the rotor.
    """
    try:
        base = root if root is not None else _mm_root()
        out = dest if dest is not None else (base / _POOL_STATUS_REL)
        out.parent.mkdir(parents=True, exist_ok=True)
        view = pool_status_view(root)
        tmp = out.with_name(f".{out.name}.tmp")
        tmp.write_text(json.dumps(view, separators=(",", ":")) + "\n", encoding="utf-8")
        tmp.replace(out)
        return out
    except Exception as exc:  # noqa: BLE001
        log.warning("key_rotor.write_pool_status: %s", exc)
        return None
