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
- translate_decisions() -> None
  Warms the cache for the autonomous daily-decision log write-ups (Daily Decision Log).
- translate_runs() -> None
  Warms the cache for run-log titles + summaries (the Brain Activity log's AI write-ups).
"""
from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

# Matches the "*tickers: AVGO, SMH · 2026-06-20T...*" metadata line a research
# note carries on its second line. Kept in sync with app.web._parse_note so the
# body string we cache here is byte-identical to the body the /api/research route
# looks up — otherwise the zh translation never matches and the feed stays English.
_TICKER_LINE_RE = re.compile(r"\*tickers:\s*(.*?)\s*·\s*([\d\-T:.+Z]+)\*")

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


# How many strings to send per Haiku request. One giant batch (e.g. all 30
# research notes at once) makes the model emit a ~100 KB JSON object whose
# escaping/length frequently breaks json.loads -> the WHOLE batch silently falls
# back to English. Small chunks keep each response parseable; the cache is saved
# after every chunk so partial progress survives an interruption.
_CHUNK_SIZE = 6


def translate_and_cache(texts: list[str]) -> dict[str, str]:
    """Translate any uncached *texts* to Simplified Chinese via Claude Haiku.

    Translates the uncached strings in small batched requests (see _CHUNK_SIZE),
    each returning a JSON object mapping its inputs to Chinese. Updates the cache
    after each chunk, then returns the full en->zh map (newly translated AND
    previously cached entries).

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

    def _store(en_text: str, zh: str | None) -> None:
        if zh and isinstance(zh, str) and zh.strip():
            cache[en_text] = zh
            result[en_text] = zh
        else:
            result[en_text] = en_text   # translation missing -> fall back to en

    # Long documents (note bodies, thesis reports) translate one-at-a-time via the
    # direct, segment-split path; short strings batch through the JSON envelope.
    short = [t for t in unique if len(t) <= _LONG_THRESHOLD]
    long_docs = [t for t in unique if len(t) > _LONG_THRESHOLD]

    for start in range(0, len(short), _CHUNK_SIZE):
        chunk = short[start:start + _CHUNK_SIZE]
        translated = _call_haiku(chunk)
        for en_text in chunk:
            _store(en_text, translated.get(en_text))
        _save_cache(cache)

    for en_text in long_docs:
        _store(en_text, _translate_long(en_text))
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
        # role 'scout' = haiku via the subscription OAuth (NOT a metered key / DeepSeek);
        # log_run=False so these utility translations never appear in the activity log.
        # allowed_tools=[] + add_dirs=[] keep it a lightweight single-shot text call —
        # translation needs no repo/tool access, and mounting vendor/macro on every call
        # makes each one many times slower.
        out = cli_bridge.reason_sync(prompt, role="scout", log_run=False,
                                     allowed_tools=[], add_dirs=[])
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


# Strings longer than this are translated as whole documents (direct prompt,
# segment-split) rather than batched into the JSON envelope of _call_haiku — a
# big string inside that JSON reliably truncates/breaks json.loads, dropping the
# WHOLE batch back to English (the research-note bodies and ~10 KB thesis reports
# are exactly this case).
_LONG_THRESHOLD = 1200
# Target characters per translation segment when splitting a long document, so no
# single response is large enough to hit the model's output-token cap and truncate.
_SEGMENT_TARGET = 1000


def _call_haiku_direct(text: str) -> str | None:
    """Translate ONE string via a direct (non-JSON) prompt; return raw Chinese or None.

    No JSON envelope means no escaping/parse fragility — the model just emits the
    translation. Used for long documents where _call_haiku's batched JSON breaks.
    """
    try:
        from brain import cli_bridge
        if not cli_bridge.available():
            return None
        prompt = (
            "You are a professional financial translator. Translate the text below to "
            "fluent, natural Simplified Chinese (NOT literal word-for-word). Keep ALL "
            "ticker symbols (e.g. AVGO, SMH), numbers, percentages and markdown "
            "formatting (#, ##, ###, **, -, |, tables) EXACTLY as in the source. Output "
            "ONLY the Chinese translation — no preamble, no commentary, no code fences.\n\n"
            "--- TEXT TO TRANSLATE ---\n" + text
        )
        # lightweight single-shot (no tools / no vendor/macro mount) — see _call_haiku
        out = cli_bridge.reason_sync(prompt, role="scout", log_run=False,
                                     allowed_tools=[], add_dirs=[])
        raw = (out.get("text") or "").strip()
        if raw.startswith("```"):
            raw = "\n".join(l for l in raw.splitlines() if not l.startswith("```")).strip()
        return raw or None
    except Exception:
        return None


def _split_segments(text: str) -> list[str]:
    """Split a long markdown document into ~_SEGMENT_TARGET-sized segments on blank-line
    (paragraph) boundaries, so each translation request stays small and structure is kept."""
    paras = text.split("\n\n")
    segments: list[str] = []
    cur: list[str] = []
    cur_len = 0
    for p in paras:
        if cur and cur_len + len(p) > _SEGMENT_TARGET:
            segments.append("\n\n".join(cur))
            cur, cur_len = [], 0
        cur.append(p)
        cur_len += len(p) + 2
    if cur:
        segments.append("\n\n".join(cur))
    return segments


def _translate_long(text: str) -> str | None:
    """Translate a long document by segmenting it, translating each segment via the
    direct path, and rejoining. Returns None if ANY segment fails (so we never cache
    a half-English document)."""
    segments = _split_segments(text)
    out: list[str] = []
    for seg in segments:
        zh = _call_haiku_direct(seg)
        if not zh:
            return None
        out.append(zh)
    return "\n\n".join(out)


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
    - decisions[].thesis  (each — backs the Brain Log decision events)
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
        for field in ("summary", "why_now", "sizing_rationale", "what_would_prove_wrong"):
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

    # decision theses — these back the Brain Log (/api/activity) decision events;
    # the full thesis is cached so the zh detail can be built and sliced client-side
    for d in book.get("decisions", []):
        th = d.get("thesis")
        if th:
            texts.append(th)

    if texts:
        translate_and_cache(texts)


def translate_notes(notes_dir: str | Path) -> None:
    """Warm the cache for all research note bodies + titles found in *notes_dir*.

    The title + body strings are extracted exactly as app.web._parse_note builds
    them (title = first # heading; body = everything AFTER the title AND the
    `*tickers: … · date*` metadata line) so the cached keys match what the
    /api/research route looks up via cached_zh(). A mismatch here silently leaves
    body_md_zh None and the feed renders English.
    """
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
            # title from the first # heading (matches _parse_note)
            title = lines[0].lstrip("# ").strip() if lines[0].startswith("#") else path.stem
            if title:
                texts.append(title)
            # find the *tickers: … · date* line; body starts after it (or after the
            # title if there is no ticker line)
            ticker_idx = None
            for i, ln in enumerate(lines[1:], 1):
                if _TICKER_LINE_RE.match(ln):
                    ticker_idx = i
                    break
            body_start = (ticker_idx + 1) if ticker_idx is not None else 1
            body = "\n".join(lines[body_start:]).strip()
            if body:
                texts.append(body)
        except Exception:
            continue

    if texts:
        translate_and_cache(texts)


def translate_papers(papers_dir: str | Path) -> None:
    """Warm the cache for every research paper's summary + full markdown report.

    These back the Research page thesis modal: /api/research_papers injects
    summary_zh and report_md_zh via cached_zh(). Each report can be ~17 KB, so we
    translate one paper at a time (its own batch) — a single oversized batched
    request is far likelier to come back as malformed JSON and fall back to English.
    """
    papers_dir = Path(papers_dir)
    if not papers_dir.exists():
        return

    for path in sorted(papers_dir.glob("*.json")):
        try:
            paper = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        batch: list[str] = []
        for field in ("summary", "report_md"):
            v = paper.get(field)
            if v and isinstance(v, str):
                batch.append(v)
        if batch:
            # one paper per call so a parse failure on a huge report can't poison
            # the other papers' translations
            translate_and_cache(batch)


def translate_decisions(limit: int = 60) -> None:
    """Warm the cache for the autonomous daily-decision log write-ups.

    These back the dashboard's Daily Decision Log: /api/decisions injects summary_zh /
    sold_note_zh / brain_text_zh and per-holding rationale_zh via cached_zh(). Without
    this warm-up the AI's decision write-up stays English even when zh is toggled.
    translate_and_cache() skips already-cached strings, so the daily call only translates
    text from new decisions. Best-effort — never raises.
    """
    try:
        from bot import autonomous
    except Exception:
        return

    texts: list[str] = []
    try:
        for d in autonomous.load_decisions(limit):
            for field in ("summary", "sold_note", "brain_text"):
                v = d.get(field)
                if v and isinstance(v, str):
                    texts.append(v)
            for h in (d.get("holdings") or []):
                r = h.get("rationale")
                if r and isinstance(r, str):
                    texts.append(r)
    except Exception:
        return

    if texts:
        translate_and_cache(texts)


def translate_runs() -> None:
    """Warm the cache for run-log titles + summaries.

    These back the dashboard's Brain Activity ("Full Trace") log: /api/runs injects
    title_zh / summary_zh via cached_zh(), so without this warm-up the AI's run write-up
    stays English even when zh is toggled. Reads the run index via brain.runlog;
    translate_and_cache() skips already-cached strings, so the daily call only translates
    text from new runs. Best-effort — never raises.
    """
    try:
        from brain import runlog
    except Exception:
        return

    texts: list[str] = []
    try:
        for r in runlog.list_runs():
            # title defaults to the run_id (a bare timestamp) when none was given — skip
            # those; they're not prose. summary is the AI's narrative write-up of the run.
            title = r.get("title")
            if title and isinstance(title, str) and title != r.get("run_id"):
                texts.append(title)
            summary = r.get("summary")
            if summary and isinstance(summary, str):
                texts.append(summary)
    except Exception:
        return

    if texts:
        translate_and_cache(texts)
