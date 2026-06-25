"""Strategy-label / posture deriver for the Brain books.

The dashboard already shows each Brain book's free-form daily rationale. This module distils
that rationale + the book's structured state into a small, glance-able **posture signal**:

    {book, posture_label, sub_strategy, favored[], avoided[], driver, detail, ...}

so a human can see at a glance what playbook a book is running — "Offensive", "Defensive",
"Risk-off", "Neutral / Cash-heavy" or "Rotating" — plus what's favored / avoided today and the
macro driver behind it.

Design rules (mirrors the rest of app/web.py's read-only API layer):
  * **Pure + offline.** Reads only the book's on-disk state (``latest.json`` +
    ``decisions.jsonl``) through ``registry.data_dir`` — never a network call, never the LLM.
  * **Deterministic where possible, extracted where not.** The posture LABEL and the macro DRIVER
    are computed from hard numbers (cash level, gross, net trade flow, the regime read). The
    favored / avoided THEMES are read off the structured trades + holdings, then enriched with a
    high-precision keyword scan over the Brain's own write-up.
  * **Never raises.** Every accessor is best-effort; an absent / malformed book degrades to an
    honest ``available: False`` stub rather than an exception.

Works uniformly for the gated *flagship* (whose "decision" lives inside ``latest.json``) and the
five free-form Brain books (autonomous / heavyweight / china / hk / etf), each of which appends a
``decisions.jsonl``.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# canonical posture vocabulary
# ---------------------------------------------------------------------------
# The five postures a book can read as, each with a chip tone (reuses the dashboard's
# --up/--down/--warn/--info/--muted design tokens) and a Chinese label for zh mode.
_LABELS = {
    "Offensive":            {"tone": "up",    "zh": "进攻"},
    "Defensive":            {"tone": "warn",  "zh": "防御"},
    "Risk-off":             {"tone": "down",  "zh": "避险"},
    "Rotating":             {"tone": "info",  "zh": "轮动"},
    "Neutral / Cash-heavy": {"tone": "muted", "zh": "中性 / 高现金"},
}

# Short playbook phrase per posture (the chip's second line).
_BASE_PLAY = {
    "Offensive":            "Pressing confirmed leadership",
    "Defensive":            "Holding a cash cushion, quality tilt",
    "Risk-off":             "De-risking — raising cash",
    "Rotating":             "Rotating within confirmed leadership",
    "Neutral / Cash-heavy": "Leadership core, cash discipline",
}

# ---------------------------------------------------------------------------
# lexicons — keyword → canonical human label. Matched case-insensitively as substrings
# against the book's text (rationales + write-up) and ticker symbols. Ordered by priority.
# ---------------------------------------------------------------------------
_THEMES: list[tuple[str, tuple[str, ...]]] = [
    ("AI infrastructure",       ("ai infra", "ai-infra", "data center", "data-center", "datacenter",
                                 "networking", "switching fabric", "interconnect", "connector",
                                 " aph", "amphenol", " anet", "arista")),
    ("Semis / AI-capex",        ("semicap", "semiconductor", "memory", "hbm", " wfe", " smh", " soxx",
                                 "chips", "foundry", "smic")),
    ("Electrification / grid",  ("electrification", "power grid", "power_grid", "power-grid", "grid-scale",
                                 " bess", "new power system", "new-energy", "new energy", " eme",
                                 "emcor", "hubbell", " hubb")),
    ("Megacap growth",          ("megacap", "mega-cap", "megacap-growth", " qqq", " xlk", "growth leadership",
                                 "large-cap growth", "quality growth")),
    ("Momentum",                ("momentum", " mtum")),
    ("Small caps",              ("small cap", "small-cap", " iwm")),
    ("Aerospace / defense",     ("aerospace", "defense", "defence", "howmet", " hwm")),
    ("Industrials / reshoring", ("reshoring", "industrial-capex", "industrial capex", "united rentals",
                                 " uri", " xli", " wab", " ame", " fss", " chrw", " tile")),
    ("Healthcare",              ("healthcare", "health care", "medical", "pharma", "biotech", " xlv",
                                 "zhende", "振德")),
    ("Staples / defensives",    ("staples", "beverage", "consumer staples", " xlp", "yangyuan", "养元")),
    ("Insurance",               ("insurance", "insurer", "p&c", "mercury general", " mcy")),
    ("Energy",                  (" energy", "oil & gas", " xle")),
    ("Financials",              ("financials", " banks", " xlf")),
    ("Gold / metals",           (" gold", " gld", " silver", " miners")),
    ("Duration / bonds",        ("duration", "treasur", " tlt", " ief", "long bond")),
    ("Front-end cash / T-bills", (" sgov", "t-bill", "tbill", "front-end bill", "front end bill",
                                  "money-market", "money market")),
]

# High-precision avoid flags — verbatim phrasing the Brain write-ups actually use when they
# DECLINE an exposure. Matched as substrings; absent → simply not surfaced.
_AVOID_FLAGS: list[tuple[str, tuple[str, ...]]] = [
    ("Chasing extension",         ("refuse to chase", "not chase", "don't chase", "won't chase",
                                   "without chasing", "chase extension", "chasing extension",
                                   "chasing gross", "won't press", "not press", "extended entries",
                                   "at a disciplined weight")),
    ("Extended / blocked entries", ("cycle_blocked", "cycle-blocked", "wait for a pullback",
                                    "wait for the pullback", "extended — wait", "extended - wait")),
    ("Duration",                  ("did not touch duration", "duration stays sidelined", "sidelined",
                                   "aren't paid", "isn't paid", "no duration", "tlt/ief")),
    ("Laggards",                  ("reach for laggards", "did not reach for laggards", "laggards")),
    ("Crowded AI trade",          ("crowded ai", "ai trade under", "crowding", "concentrated ai",
                                   "'concentrated'")),
    ("Double-counted exposure",   ("double-count", "double count")),
]

# Macro driver cues — appended after the regime read when the write-up names them.
_DRIVER_CUES: list[tuple[str, tuple[str, ...]]] = [
    ("hawkish Fed",          ("hawkish", "restrictive policy", "restrictive,")),
    ("GDP/PCE prints",       ("gdp", "pce", " prints")),
    ("rising real yields",   ("real yield", "real 10y", "rising real")),
    ("stagflation",          ("stagflation",)),
    ("PBoC easing watch",    ("pboc",)),
    ("memory / AI unwind",   ("memory unwind", "memory-unwind", "ai trade under")),
    ("event risk midweek",   ("event risk", "midweek")),
]

# Cushion / rotation language for the sub-strategy modifier.
_CUSHION_KW = ("dry powder", "dry-powder", "cushion", "buffer", "cash kept", "keep cash",
               "keeping dry", "high by design", "dry-powder buffer")


# ---------------------------------------------------------------------------
# small best-effort accessors
# ---------------------------------------------------------------------------
def _load_json(path: Path) -> dict[str, Any]:
    try:
        if path.exists():
            d = json.loads(path.read_text())
            return d if isinstance(d, dict) else {}
    except Exception:  # noqa: BLE001
        return {}
    return {}


def _read_jsonl_tail(path: Path, max_lines: int = 80) -> list[dict[str, Any]]:
    """Parse the last ``max_lines`` rows of a decisions.jsonl (oldest→newest), skipping junk."""
    try:
        lines = path.read_text().splitlines()
    except Exception:  # noqa: BLE001
        return []
    out: list[dict[str, Any]] = []
    for ln in lines[-max_lines:]:
        ln = ln.strip()
        if not ln:
            continue
        try:
            rec = json.loads(ln)
            if isinstance(rec, dict):
                out.append(rec)
        except Exception:  # noqa: BLE001
            continue
    return out


def _latest_meaningful(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Newest decision row that actually CARRIES content — a skipped / feed-gated run appends a
    summary-less, holding-less row that shouldn't define the posture (mirrors web.py's banner
    fallback)."""
    for rec in reversed(records):
        if rec.get("summary") or rec.get("brain_text") or rec.get("holdings") or rec.get("executed"):
            return rec
    return records[-1] if records else {}


def _num(v: Any) -> float | None:
    try:
        f = float(v)
        return f if f == f else None  # drop NaN
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# lexicon matching
# ---------------------------------------------------------------------------
def _match_lexicon(text: str, lexicon: list[tuple[str, tuple[str, ...]]],
                   cap: int = 4) -> list[str]:
    """Canonical labels whose keywords appear in ``text`` (deduped, priority-ordered, capped)."""
    blob = (" " + (text or "") + " ").lower()
    out: list[str] = []
    for label, kws in lexicon:
        if any(kw in blob for kw in kws) and label not in out:
            out.append(label)
            if len(out) >= cap:
                break
    return out


# Regime read fallbacks — when the book's structured ``regime`` block is null (some Brain books
# leave it blank and rely on the LLM's narrative), recover the quad / liquidity from the write-up.
_QUAD_FROM_TEXT = [("goldilocks", "Goldilocks"), ("reflation", "Reflation"),
                   ("stagflation", "Stagflation"), ("growth scare", "Growth Scare"),
                   ("growth-scare", "Growth Scare"), ("deflation", "Deflation")]
_LIQ_FROM_TEXT = [("contracting liquidity", "contracting"), ("expanding liquidity", "expanding"),
                  ("contracting", "contracting"), ("expanding", "expanding")]


def _from_text(blob: str, table: list[tuple[str, str]]) -> str | None:
    for kw, disp in table:
        if kw in blob:
            return disp
    return None


def _is_opaque(rec: dict[str, Any]) -> bool:
    """A name whose ticker/display is not self-describing — a numeric A-share / HK code or a CJK
    name. Only these earn a rationale-PROSE fallback for theme extraction; a Latin ETF/stock ticker
    is its own identity, so we never mine its prose (which may name a theme only to reject it)."""
    s = f"{rec.get('ticker') or ''}{rec.get('name') or ''}"
    return any(ch.isdigit() or ord(ch) > 127 for ch in s)


def _pos_text(pos: dict[str, Any]) -> str:
    """All the text attached to a holding (ticker + name + rationale + thesis) for theme matching."""
    bits = [str(pos.get("ticker") or ""), str(pos.get("name") or ""), str(pos.get("rationale") or "")]
    tf = pos.get("thesis_full") or {}
    if isinstance(tf, dict):
        for k in ("summary", "why_now", "sizing_rationale"):
            if tf.get(k):
                bits.append(str(tf[k]))
        for k in ("bull", "bear"):
            v = tf.get(k)
            if isinstance(v, list):
                bits.extend(str(x) for x in v)
    return " ".join(bits)


# ---------------------------------------------------------------------------
# the deriver
# ---------------------------------------------------------------------------
def _stub(book_id: str, note: str | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {
        "book": book_id, "as_of": None, "available": False,
        "posture_label": "—", "posture_label_zh": "—", "posture_tone": "muted",
        "sub_strategy": None, "favored": [], "avoided": [], "driver": None, "detail": None,
        "cash_pct": None, "invested_pct": None,
    }
    if note:
        out["note"] = note
    return out


def posture(portfolio_id: str | None, *, data_dir: Path | None = None) -> dict[str, Any]:
    """Derive the posture signal for one book. Never raises — returns an honest stub on any gap."""
    book_id = portfolio_id or "flagship"
    try:
        if data_dir is None:
            from portfolio import registry
            data_dir = registry.data_dir(book_id)
        data_dir = Path(data_dir)

        book = _load_json(data_dir / "latest.json")
        decision = _latest_meaningful(_read_jsonl_tail(data_dir / "decisions.jsonl"))
        if not book and not decision:
            return _stub(book_id, "no book yet")

        # ---- structured facts -------------------------------------------------
        holdings = book.get("positions") or decision.get("holdings") or []
        # The live latest.json often carries weights ONLY (the rich per-name rationale lives in the
        # decision row). Enrich each position with the matching decision-log rationale / name /
        # thesis, keyed by ticker, so theme extraction sees the Brain's actual words.
        _dec_h = {str(h.get("ticker") or "").upper(): h for h in (decision.get("holdings") or [])}
        for p in holdings:
            src = _dec_h.get(str(p.get("ticker") or "").upper())
            if src:
                for fld in ("rationale", "name", "thesis_full"):
                    if not p.get(fld) and src.get(fld):
                        p[fld] = src[fld]
        weights = [w for w in (_num(p.get("weight")) for p in holdings) if w]
        gross = _num(book.get("gross"))
        cash = _num(book.get("cash"))
        if cash is None and weights:
            cash = max(0.0, 1.0 - sum(weights))
        if gross is None and weights:
            gross = sum(weights)
        cash_pct = round(cash * 100, 1) if cash is not None else None
        invested_pct = round((gross if gross is not None else (1.0 - (cash or 0))) * 100, 1)

        nav = _num(book.get("nav")) or 1_000_000.0
        trades = book.get("executed_today") or decision.get("executed") or []
        buy_val = sum(_num(t.get("value")) or 0 for t in trades if t.get("side") == "buy")
        sell_val = sum(_num(t.get("value")) or 0 for t in trades if t.get("side") == "sell")
        turnover_pct = (buy_val + sell_val) / nav * 100 if nav else 0.0
        net_pct = (buy_val - sell_val) / nav * 100 if nav else 0.0
        net_sell = net_pct < -1.5

        regime = book.get("regime") or {}
        quad_name = regime.get("quad_name") or regime.get("quad")
        liq = (regime.get("liquidity_overlay") or "").lower() or None
        risk_state = (decision.get("risk_state") or book.get("risk_state") or "").lower() or None

        # full text blob for keyword extraction (the Brain's own words)
        text_bits = [str(book.get("summary") or decision.get("summary") or ""),
                     str(book.get("sold_note") or decision.get("sold_note") or ""),
                     str(decision.get("brain_text") or "")]
        text_bits.extend(_pos_text(p) for p in holdings)
        text = " ".join(text_bits)
        blob = text.lower()

        # recover a null structured regime from the Brain's own narrative (best-effort)
        if not quad_name:
            quad_name = _from_text(blob, _QUAD_FROM_TEXT)
        if not liq:
            liq = _from_text(blob, _LIQ_FROM_TEXT)

        # ---- regime risk read -------------------------------------------------
        qn = (quad_name or "").lower()
        if risk_state in ("stressed", "stress") or "deflation" in qn or "growth scare" in qn or "growth-scare" in qn:
            regime_risk = "stress"
        elif "stagflation" in qn or liq == "contracting" or risk_state == "elevated":
            regime_risk = "caution"
        else:
            regime_risk = "benign"

        # ---- posture label (deterministic cascade) ----------------------------
        cp = cash_pct if cash_pct is not None else 0.0
        ip = invested_pct if invested_pct is not None else (100.0 - cp)
        if regime_risk == "stress" and (cp >= 30 or net_sell):
            label = "Risk-off"
        elif cp >= 50:
            label = "Risk-off"
        elif cp >= 33:
            label = "Defensive"
        elif ip >= 88:
            label = "Offensive"
        elif turnover_pct >= 5 and abs(net_pct) < 3:
            label = "Rotating"
        elif cp >= 18:
            label = "Neutral / Cash-heavy"
        else:
            label = "Offensive"

        held = {str(p.get("ticker") or "").upper() for p in holdings if (_num(p.get("weight")) or 0) > 0}

        def _id_themes(rec: dict[str, Any]) -> list[str]:
            """Theme(s) keyed off a name's IDENTITY (ticker + display name) — precise, and free of
            the incidental themes a rationale paragraph might *mention while rejecting them*."""
            return _match_lexicon(f" {rec.get('ticker') or ''} {rec.get('name') or ''} ", _THEMES, cap=2)

        sold = {str(t.get("ticker") or "").upper() for t in trades if t.get("side") == "sell"}

        # ---- favored: themes of bought names, backfilled with the biggest holdings ----
        favored: list[str] = []
        for t in (x for x in trades if x.get("side") == "buy"):
            for th in _id_themes(t):
                if th not in favored:
                    favored.append(th)
        if len(favored) < 2:
            for p in sorted(holdings, key=lambda q: _num(q.get("weight")) or 0, reverse=True)[:4]:
                if str(p.get("ticker") or "").upper() in sold:
                    continue  # a sleeve trimmed today is not a "favored" tilt
                # identity first; only mine the rationale prose when the ticker/name is opaque
                # (A-share / HK code) so we never pull in negatively-mentioned themes.
                cand = _id_themes(p)
                if not cand and _is_opaque(p):
                    cand = _match_lexicon(_pos_text(p), _THEMES, cap=2)
                for th in cand:
                    if th not in favored:
                        favored.append(th)
        favored = favored[:4]

        # ---- avoided: themes of names FULLY EXITED (a trim of a still-held name is a trim, NOT an
        # avoidance), plus the high-precision decline flags mined from the write-up ----
        avoided: list[str] = []
        for t in (x for x in trades if x.get("side") == "sell"):
            if str(t.get("ticker") or "").upper() in held:
                continue  # still held → trimmed, not avoided
            for th in _id_themes(t):
                if th not in avoided and th not in favored:
                    avoided.append(th)
        for flag in _match_lexicon(text, _AVOID_FLAGS, cap=4):
            if flag not in avoided:
                avoided.append(flag)
        avoided = avoided[:4]

        # ---- driver: regime read + named macro cues ---------------------------
        dbits = [b for b in (quad_name, (liq + " liquidity" if liq else None)) if b]
        driver = ", ".join(dbits) if dbits else "regime read unavailable"
        cues: list[str] = []
        if risk_state:
            cues.append(f"risk {risk_state}")
        for cue in _match_lexicon(text, _DRIVER_CUES, cap=4):
            if cue.lower() not in driver.lower() and cue not in cues:
                cues.append(cue)
        if cues:
            driver += " · " + " · ".join(cues[:3])

        # ---- sub-strategy + composed hover detail -----------------------------
        play = _BASE_PLAY[label]
        top_weight = max(weights) if weights else 0.0
        n_pos = len([w for w in weights if w > 0])
        concentrated = top_weight >= 0.18 or (0 < n_pos <= 6)
        cushion = any(kw in blob for kw in _CUSHION_KW)
        # For a cautious book that explicitly names its cash buffer, "dry powder" is the more
        # telling modifier than "concentrated"; otherwise flag a concentrated book.
        if cushion and label in ("Defensive", "Neutral / Cash-heavy"):
            play += " · dry powder"
        elif concentrated and label != "Neutral / Cash-heavy":
            play += " · concentrated book"

        parts = [play + "."]
        if favored:
            parts.append("Favoring " + ", ".join(favored) + ".")
        if avoided:
            parts.append("Cutting / avoiding " + ", ".join(avoided) + ".")
        if cash_pct is not None:
            parts.append(f"Cash {cash_pct:.0f}% / invested {ip:.0f}%.")
        parts.append("Driver: " + driver + ".")
        detail = " ".join(parts)

        return {
            "book": book_id,
            "as_of": str(book.get("as_of") or decision.get("asof") or "")[:10] or None,
            "available": True,
            "posture_label": label,
            "posture_label_zh": _LABELS[label]["zh"],
            "posture_tone": _LABELS[label]["tone"],
            "sub_strategy": play,
            "favored": favored,
            "avoided": avoided,
            "driver": driver,
            "detail": detail,
            "cash_pct": cash_pct,
            "invested_pct": ip,
        }
    except Exception as exc:  # noqa: BLE001 — never raise; degrade to an honest stub
        return _stub(book_id, f"posture unavailable: {exc}")
