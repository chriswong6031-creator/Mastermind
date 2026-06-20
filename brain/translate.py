"""Cache-backed EN->Simplified-Chinese translator for the Mastermind dashboard.

Design:
- Cache file: data/brain/translations.json = { "<en text>": "<zh text>" }
  Crash-safe: written atomically via a temp file.
- cached_zh(text) -> str | None
  Pure cache lookup — NEVER calls the LLM; safe in the hot request path.
- translate_and_cache(texts) -> dict
  For any text not already cached, calls Claude at the Haiku tier (role="scout")
  in a single batched request returning a JSON object mapping each en -> zh.
  On any failure (no creds, network, parse error) the missing strings stay
  uncached and we return the en text for those; never crashes.
- translate_book(book) -> None
  Warms the cache for every translatable string in a portfolio.v1 dict.
- translate_notes(notes_dir) -> None
  Warms the cache for every research note body + title.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent.parent
_CACHE_PATH = _ROOT / "data" / "brain" / "translations.json"

# ---------------------------------------------------------------------------
# Cache helpers
# ---------------------------------------------------------------------------

def _load_cache() -> dict[str, str]:
    """Load the translations cache from disk. Returns {} on any error."""
    try:
        if _CACHE_PATH.exists():
            data = json.loads(_CACHE_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return {}


def _save_cache(cache: dict[str, str]) -> None:
    """Atomically write the cache to disk. Silently ignores errors."""
    try:
        _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _CACHE_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(_CACHE_PATH)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def cached_zh(text: str) -> str | None:
    """Return the cached Chinese translation for *text*, or None if not cached.

    NEVER calls the LLM. Safe to call in the FastAPI request path.
    """
    if not text or not text.strip():
        return None
    try:
        cache = _load_cache()
        return cache.get(text)
    except Exception:
        return None


def translate_and_cache(texts: list[str]) -> dict[str, str]:
    """Translate any uncached *texts* to Simplified Chinese via Claude Haiku.

    Sends ONE batched API request that returns a JSON object mapping each input
    string to its Chinese translation. Updates the cache, then returns the full
    en->zh map (both newly translated and previously cached entries).

    For any string where translation fails (network/creds unavailable, parse
    error, etc.) the result map contains the original English text so callers
    always get a value.  The cache is never left in a corrupt state.
    """
    if not texts:
        return {}

    cache = _load_cache()

    # Deduplicate + filter what's already cached
    unique: list[str] = []
    seen: set[str] = set()
    for t in texts:
        t = t.strip() if t else t
        if t and t not in seen and t not in cache:
            unique.append(t)
            seen.add(t)

    result: dict[str, str] = {}

    # Fill already-cached values
    for t in texts:
        t_stripped = (t or "").strip()
        if t_stripped in cache:
            result[t_stripped] = cache[t_stripped]

    if unique:
        translated = _call_haiku(unique)
        # Merge into cache and result
        for en_text in unique:
            zh = translated.get(en_text)
            if zh and isinstance(zh, str) and zh.strip():
                cache[en_text] = zh
                result[en_text] = zh
            else:
                # Translation missing for this string — fall back to en
                result[en_text] = en_text
        _save_cache(cache)

    return result


def _call_haiku(texts: list[str]) -> dict[str, str]:
    """Translate a batch via Claude Haiku through the SUBSCRIPTION cli bridge.

    The bot authenticates with CLAUDE_CODE_OAUTH_TOKEN (subscription), not a
    metered ANTHROPIC_API_KEY — so route through cli_bridge (role 'scout' = haiku
    per config/agents.yml). Returns {} on any failure (caller falls back to en).
    """
    try:
        from brain import cli_bridge
        if not cli_bridge.available():
            return {}

        # Index by number so identical prefixes can't collide as JSON keys.
        index = {str(i): t for i, t in enumerate(texts)}
        index_json = json.dumps(index, ensure_ascii=False)

        prompt = (
            "You are a professional financial translator. Translate each English string to fluent, "
            "natural Simplified Chinese (NOT literal word-for-word). Keep all ticker symbols "
            "(e.g. AVGO, SMH), numbers, percentages and markdown formatting (**, ##, -, |) unchanged. "
            "Output ONLY a valid JSON object mapping each input key (the number string) to its Chinese "
            "translation — no commentary, no markdown code fences.\n\n"
            f"Input:\n{index_json}"
        )
        out = cli_bridge.reason_sync(prompt, role="scout")
        raw = (out.get("text") or "").strip()

        # tolerate code fences / surrounding prose — extract the outermost JSON object
        if raw.startswith("```"):
            raw = "\n".join(l for l in raw.splitlines() if not l.startswith("```")).strip()
        if not raw.startswith("{"):
            i, j = raw.find("{"), raw.rfind("}")
            if i >= 0 and j > i:
                raw = raw[i:j + 1]

        parsed: dict[str, str] = json.loads(raw)
        # Remap back from index keys to original en text
        return {index[k]: v for k, v in parsed.items() if k in index}
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# Warm-up helpers for the batch populate script
# ---------------------------------------------------------------------------

def translate_book(book: dict[str, Any]) -> None:
    """Gather every translatable string from a portfolio.v1 dict and warm the cache.

    Translatable strings:
    - positions[].thesis_full.{summary, why_now, sizing_rationale}
    - positions[].thesis_full.bull[]  (each item)
    - positions[].thesis_full.bear[]  (each item)
    - rejected[].reason
    - rejected[].bear[]  (each item)
    - top-level disclaimer
    """
    texts: list[str] = []

    # disclaimer
    d = book.get("disclaimer")
    if d:
        texts.append(d)

    # positions
    for pos in book.get("positions", []):
        tf = pos.get("thesis_full") or {}
        for field in ("summary", "why_now", "sizing_rationale"):
            v = tf.get(field)
            if v:
                texts.append(v)
        for item in tf.get("bull", []) or []:
            if item:
                texts.append(item)
        for item in tf.get("bear", []) or []:
            if item:
                texts.append(item)

    # rejected
    for rej in book.get("rejected", []):
        r = rej.get("reason")
        if r:
            texts.append(r)
        for item in rej.get("bear", []) or []:
            if item:
                texts.append(item)

    if texts:
        translate_and_cache(texts)


def translate_notes(notes_dir: str | Path) -> None:
    """Warm the cache for all research note bodies + titles found in *notes_dir*."""
    notes_dir = Path(notes_dir)
    if not notes_dir.exists():
        return

    texts: list[str] = []
    for path in notes_dir.glob("*.md"):
        try:
            raw = path.read_text(encoding="utf-8")
            lines = raw.splitlines()
            if not lines:
                continue
            # title
            title = lines[0].lstrip("# ").strip() if lines[0].startswith("#") else path.stem
            if title:
                texts.append(title)
            # body = everything from line 1 onward
            body = "\n".join(lines[1:]).strip()
            if body:
                texts.append(body)
        except Exception:
            continue

    if texts:
        translate_and_cache(texts)
