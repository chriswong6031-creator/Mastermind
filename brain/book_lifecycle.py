"""THE BOOK LIFECYCLE — probation / retire as CIO recommendations (W6 / T2; architecture §2).

The problem this kills (verified in the audits): the US books are ONE BET WEARING FOUR HATS.
Heavyweight was hard-gated to Flagship's universe (an amplifier), Autonomous's active returns
correlate ~0.87 with Flagship, and *nothing measures or polices book orthogonality*. Meanwhile the one
winning book (the user's Self-Directed defensive basket) sits outside the firm's accounting. The
architecture's answer (§2 kill/promote hooks) is a lifecycle: each book earns its capital, and a book
that is a noisy mirror of another — or that loses to its bogey with statistical reliability — is
recommended for retirement.

Three ingredients, all made HONEST AT PAPER-N:

  (a) PER-BOOK GRADES — each book's rolling active return (book minus its bogey) vs three bogeys:
        · SPY (the market),
        · the defensive basket (XLU/XLV/XLF/XLP — the thing that beat us),
        · the regime-conditional max(SPY, defensive) — the bogey a cash/defensive book can win but a
          cash-hoarder in a calm up-tape still loses to,
      plus the book's rolling max-drawdown (surfaced on the card, never an auto-trigger).

  (b) THE ORTHOGONALITY MATRIX — pairwise Pearson correlation of ACTIVE returns (book minus its bogey)
      across the US books over a rolling window. Autonomous persistently >= noisy_mirror_corr vs
      Flagship is the architecture's 'noisy mirror' flag (P9 — a book that is a noisy mirror of another
      is dead weight with extra API costs).

  (c) LIFECYCLE STATES — active -> probation -> retired-recommendation. A book enters probation on
      EITHER (i) `losing_reviews_to_probation` consecutive HAC-significant losing weekly reviews vs its
      regime-conditional bogey, OR (ii) the noisy-mirror flag. A book already on probation that keeps
      failing earns a RETIRED-RECOMMENDATION.

HONEST STATISTICS AT PAPER-N (charter P3/P8) — the load-bearing discipline, mirrored from the posture
governor and cio EXACTLY: a losing-review call requires HAC significance (Newey-West |t| >= hac_t_min)
over an active-return series with `effective_n >= min_effective_n` INDEPENDENT weekly observations. Below
that n the module says `insufficient-n` in the artifact and makes NO recommendation. Same for the
correlation matrix: a pair with fewer than `noisy_mirror_min_pairs` overlapping observations is reported
`insufficient-n`, never a flag.

KILL IS NEVER AUTOMATED (charter P8; architecture §2). This module is display/advisory ONLY: it writes
``data/lifecycle/<date>.json``, feeds the cio weekly note (cio consumes ``lifecycle_summary``), and emits
ONE ``{owner: fable-review}`` improvement-agenda item per probation/retire recommendation. A human/Fable
executes any retirement — nothing here flips a flag, resizes, or trades.

SELF-DIRECTED IS CONSTITUTIONALLY EXEMPT (architecture §2 / charter P6). It is the yardstick — never
killed, never put on probation, never in the orthogonality matrix as a candidate for retirement. This is
hard-coded (``EXEMPT_BOOKS``) and asserted in the tests.

    from brain import book_lifecycle as BL
    rep = BL.review(review_history=...)        # pure, deterministic, injectable
    BL.write(review_history=...)               # + persist data/lifecycle/<date>.json
    summ = BL.lifecycle_summary()              # the compact dict cio + the agenda read

Every input is INJECTED (fixture-driven in tests; derived from the persisted benchmark ledgers in prod).
Nothing here pins a live market state. Best-effort throughout — missing data degrades a book to
`insufficient-n`, never fabricates a grade or a recommendation (charter P2)."""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_OUT = _ROOT / "data" / "lifecycle"
_BENCH_DIR = _ROOT / "data" / "benchmark"

# The US books the lifecycle grades + polices for orthogonality. Flagship is the REFERENCE book (the
# orthogonality matrix measures every other US book's active-return correlation *to it*). Regional
# books (china/hk) run their own regional lifecycle and are out of the US orthogonality frame.
US_BOOKS = ["flagship", "heavyweight", "autonomous", "etf"]
REFERENCE_BOOK = "flagship"

# CONSTITUTIONALLY EXEMPT — never killed, never on probation, never a retirement candidate. The user's
# manual defensive book is the yardstick the whole program is graded against (architecture §2, charter
# P6). Hard-coded here and asserted in the tests so no future edit can quietly make it killable.
EXEMPT_BOOKS = frozenset({"self_directed"})

# lifecycle states (append-only progression; a human resets a book to active by executing/declining)
STATE_ACTIVE = "active"
STATE_PROBATION = "probation"
STATE_RETIRE_REC = "retired-recommendation"

# ── doctrine fallbacks (mirror config/doctrine.yml book_lifecycle:) — all (unverified-prior) ──
_MIN_EFFECTIVE_N = 8
_HAC_T_MIN = 2.0
_HAC_LAG = 3
_LOSING_REVIEWS_TO_PROBATION = 2
_CORR_WINDOW = 8
_NOISY_MIRROR_CORR = 0.8
_NOISY_MIRROR_MIN_PAIRS = 6
_MAX_DD_WATCH = 0.20


# ─────────────────────────────────────────────────────────────────────────────
# doctrine
# ─────────────────────────────────────────────────────────────────────────────
def _cfg() -> dict:
    try:
        from bot.doctrine_config import load_doctrine
        b = load_doctrine().get("book_lifecycle")
        return b if isinstance(b, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _cf(key: str, default: float) -> float:
    v = _cfg().get(key)
    try:
        return float(v) if v is not None else default
    except (TypeError, ValueError):
        return default


def _ci(key: str, default: int) -> int:
    v = _cfg().get(key)
    try:
        return int(v) if v is not None else default
    except (TypeError, ValueError):
        return default


# ─────────────────────────────────────────────────────────────────────────────
# statistics — the SAME HAC/effective_n discipline as the posture governor + cio
# ─────────────────────────────────────────────────────────────────────────────
def _hac(series: list[float], lags: int) -> dict:
    """Newey-West mean/t over an active-return series, reusing the macro engine's validated helper.
    Degrades to a plain mean with t=0 when the helper is unavailable (guard then fails closed → no
    recommendation). Never raises."""
    s = [float(x) for x in (series or []) if x is not None]
    if len(s) < 2:
        return {"n": len(s), "mean": (s[0] if s else None), "t": 0.0, "se": None}
    try:
        from engine.validation import newey_west_tstat
        return newey_west_tstat(list(s), lags=lags)
    except Exception:  # noqa: BLE001
        n = len(s)
        mean = sum(s) / n
        return {"n": n, "mean": round(mean, 8), "t": 0.0, "se": None}


def _loss_significance(active_series: list[float]) -> dict:
    """Is this book RELIABLY LOSING to its (regime-conditional) bogey over the review series?

    `active_series` = per-review (book − bogey) returns. Returns {effective_n, mean, hac_t, significant,
    losing, status}. `status`='insufficient-n' below the effective_n gate — the honest paper-n answer,
    NEVER a recommendation. `losing` (a mean-negative flag) is only trusted once `significant` (HAC |t|
    over threshold AND mean < 0). Pure; never raises. Each weekly review is treated as ONE independent
    observation — the caller feeds non-overlapping per-review increments (mirrors the governor)."""
    s = [float(x) for x in (active_series or []) if x is not None]
    min_n = _ci("min_effective_n", _MIN_EFFECTIVE_N)
    t_min = _cf("hac_t_min", _HAC_T_MIN)
    lags = _ci("hac_lag", _HAC_LAG)
    eff_n = len(s)
    out = {"effective_n": eff_n, "min_effective_n": min_n, "mean": None,
           "hac_t": None, "hac_t_min": t_min, "significant": False, "losing": False,
           "status": "insufficient-n"}
    if eff_n < min_n:
        # honest paper-n answer: not enough independent reviews to test a loss (P3)
        if s:
            out["mean"] = round(sum(s) / len(s), 6)
        return out
    stat = _hac(s, lags)
    mean = stat.get("mean")
    t = float(stat.get("t") or 0.0)
    out["mean"] = round(mean, 6) if mean is not None else None
    out["hac_t"] = round(t, 4)
    losing = (mean is not None and mean < 0)
    # significance is DIRECTIONAL for a loss: the NEGATIVE-mean t must clear the threshold
    out["significant"] = bool(losing and abs(t) >= t_min)
    out["losing"] = bool(losing)
    out["status"] = "scoring"
    return out


def _max_drawdown(curve: dict | None) -> float | None:
    """Rolling max-drawdown of a growth-of-$1 curve (as a positive fraction; 0.15 = −15%). None on an
    empty/degenerate curve. Display-only on the grade card — it never triggers probation by itself."""
    if not curve:
        return None
    try:
        vals = [float(curve[d]) for d in sorted(curve) if curve[d] is not None]
    except Exception:  # noqa: BLE001
        return None
    if len(vals) < 2:
        return None
    peak = vals[0]
    mdd = 0.0
    for v in vals:
        if v > peak:
            peak = v
        if peak > 0:
            dd = (peak - v) / peak
            if dd > mdd:
                mdd = dd
    return round(mdd, 6)


def _pearson(xs: list[float], ys: list[float]) -> float | None:
    """Pearson correlation over aligned pairs. None if <2 points or either side is constant (an
    undefined correlation is reported as None, never 0 — 0 would be a false 'orthogonal' claim)."""
    n = min(len(xs), len(ys))
    if n < 2:
        return None
    xs, ys = xs[:n], ys[:n]
    mx = sum(xs) / n
    my = sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx <= 0 or syy <= 0:
        return None
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    return round(sxy / (sxx ** 0.5 * syy ** 0.5), 6)


# ─────────────────────────────────────────────────────────────────────────────
# the review-history contract + active-return series extraction
# ─────────────────────────────────────────────────────────────────────────────
# A `review_history` is an oldest-first list of per-review snapshots. Each snapshot:
#   {"date": "YYYY-MM-DD",
#    "books":    {book_id: per_review_return_fraction, ...},          # each book's return THAT review
#    "bogeys":   {"spy": r, "defensive": r, "regime_max": r, ...}}    # each bogey's return THAT review
# Returns are PER-REVIEW increments (not cumulative), so each row is one independent observation. In
# prod these are derived by differencing consecutive benchmark-ledger curves (see `history_from_ledgers`);
# in tests they are injected directly.

def _active_series(review_history: list[dict], book: str, bogey: str) -> list[float]:
    """The per-review active-return series (book − bogey) for one book vs one bogey. Skips a review
    where either side is missing (P2 — never fabricate an observation)."""
    out: list[float] = []
    for snap in review_history or []:
        try:
            br = (snap.get("books") or {}).get(book)
            gr = (snap.get("bogeys") or {}).get(bogey)
            if br is None or gr is None:
                continue
            out.append(float(br) - float(gr))
        except Exception:  # noqa: BLE001
            continue
    return out


def _regime_bogey_id(review_history: list[dict]) -> str:
    """The bogey a book is GRADED against for the losing-review trigger: the regime-conditional
    max(SPY, defensive) when it is present in the history, else plain SPY (P2 — no defensive alibi
    without the bogey). This is the architecture's regime-conditional bogey."""
    for snap in reversed(review_history or []):
        if isinstance((snap.get("bogeys") or {}), dict) and "regime_max" in (snap.get("bogeys") or {}):
            return "regime_max"
    return "spy"


def history_from_ledgers(ledgers: list[dict] | None = None) -> list[dict]:
    """Derive a per-review `review_history` from the persisted benchmark ledgers (prod path).

    Each ledger snapshot carries CUMULATIVE window returns per book + per bogey (leaderboard
    `return_pct`). Consecutive snapshots are differenced into per-review increments so each row is one
    independent observation (matching the injected-fixture contract). Best-effort: a snapshot missing a
    leaderboard is skipped; a book/bogey absent in a snapshot simply has no increment that review.

    NOTE: differencing cumulative % returns is a first-order approximation of the per-review increment
    (exact only for small returns); it is honest enough for the HAC loss test and the correlation matrix
    at paper-n, and it is the single source of truth already on disk (charter P7). Never raises."""
    hist = ledgers if ledgers is not None else _bench_history()
    # cumulative return_pct per (date, id)
    rows: list[tuple[str, dict]] = []
    for lg in hist:
        if not isinstance(lg, dict):
            continue
        lb = lg.get("leaderboard") or []
        d = str(lg.get("as_of") or "")[:10]
        cum = {}
        for r in lb:
            rid = r.get("id")
            rp = r.get("return_pct")
            if rid is None or rp is None:
                continue
            try:
                cum[rid] = float(rp) / 100.0
            except (TypeError, ValueError):
                continue
        if cum:
            rows.append((d, cum))
    rows.sort(key=lambda x: x[0])
    out: list[dict] = []
    prev = {}
    for d, cum in rows:
        books, bogeys = {}, {}
        for rid, cr in cum.items():
            inc = cr - prev.get(rid, 0.0) if rid in prev else cr
            if rid in US_BOOKS or rid in EXEMPT_BOOKS:
                books[rid] = inc
            else:
                bogeys[rid] = inc
        if books or bogeys:
            out.append({"date": d, "books": books, "bogeys": bogeys})
        prev = cum
    return out


def _bench_history() -> list[dict]:
    try:
        out = []
        for f in sorted(_BENCH_DIR.glob("*.json")):
            try:
                out.append(json.loads(f.read_text()))
            except Exception:  # noqa: BLE001
                continue
        return out
    except Exception:  # noqa: BLE001
        return []


# ─────────────────────────────────────────────────────────────────────────────
# the orthogonality matrix
# ─────────────────────────────────────────────────────────────────────────────
def orthogonality_matrix(review_history: list[dict], *, books: list[str] | None = None) -> dict:
    """Pairwise correlation of ACTIVE returns (book − SPY) across the US books over the rolling window.

    Active returns are taken vs SPY (a stable common yardstick so the correlation reflects the books'
    *strategy* overlap, not a shared regime-bogey artifact). Returns:

      {as_of_reviews, window, books:[...], matrix:{a:{b:{corr, n_pairs, status}}},
       noisy_mirror_flags:[{book, corr, n_pairs}]}

    A pair with < `noisy_mirror_min_pairs` overlapping observations is `insufficient-n` (corr=None),
    never a flag (charter P3). The noisy-mirror flags are the pairs where a NON-reference US book's
    active-return correlation to `REFERENCE_BOOK` is >= `noisy_mirror_corr`. Pure; never raises."""
    books = [b for b in (books or US_BOOKS)]
    window = _ci("corr_window_reviews", _CORR_WINDOW)
    min_pairs = _ci("noisy_mirror_min_pairs", _NOISY_MIRROR_MIN_PAIRS)
    corr_thr = _cf("noisy_mirror_corr", _NOISY_MIRROR_CORR)

    # per-book active-return series vs SPY over the last `window` reviews (aligned by review index so
    # pairs are drawn from the SAME reviews — a book missing a review drops that pair, P2)
    recent = (review_history or [])[-window:]
    per_book_by_date: dict[str, dict] = {}
    for b in books:
        m = {}
        for snap in recent:
            d = str(snap.get("date") or "")
            br = (snap.get("books") or {}).get(b)
            sp = (snap.get("bogeys") or {}).get("spy")
            if br is None or sp is None:
                continue
            try:
                m[d] = float(br) - float(sp)
            except Exception:  # noqa: BLE001
                continue
        per_book_by_date[b] = m

    matrix: dict = {}
    for a in books:
        matrix[a] = {}
        for b in books:
            if a == b:
                matrix[a][b] = {"corr": 1.0, "n_pairs": len(per_book_by_date.get(a) or {}),
                                "status": "self"}
                continue
            da, db = per_book_by_date.get(a) or {}, per_book_by_date.get(b) or {}
            common = sorted(set(da) & set(db))
            n_pairs = len(common)
            if n_pairs < min_pairs:
                matrix[a][b] = {"corr": None, "n_pairs": n_pairs, "status": "insufficient-n"}
                continue
            corr = _pearson([da[d] for d in common], [db[d] for d in common])
            matrix[a][b] = {"corr": corr, "n_pairs": n_pairs,
                            "status": ("scoring" if corr is not None else "undefined")}

    noisy: list[dict] = []
    for b in books:
        if b == REFERENCE_BOOK or b in EXEMPT_BOOKS:
            continue
        cell = matrix.get(b, {}).get(REFERENCE_BOOK) or {}
        c = cell.get("corr")
        if cell.get("status") == "scoring" and c is not None and c >= corr_thr:
            noisy.append({"book": b, "vs": REFERENCE_BOOK, "corr": c,
                          "n_pairs": cell.get("n_pairs")})
    return {"window": window, "books": books, "matrix": matrix,
            "noisy_mirror_flags": noisy, "reference_book": REFERENCE_BOOK}


# ─────────────────────────────────────────────────────────────────────────────
# per-book grades
# ─────────────────────────────────────────────────────────────────────────────
_BOGEY_LABELS = {"spy": "SPY", "defensive": "defensive basket", "regime_max": "regime-conditional max"}


def _book_curve(book_curves: dict | None, book: str) -> dict:
    return (book_curves or {}).get(book) or {}


def grade_book(book: str, review_history: list[dict], *, book_curves: dict | None = None) -> dict:
    """The grade card for one book: active-return HAC test vs the regime-conditional bogey (the
    lifecycle trigger), the advisory active means vs SPY and the defensive basket, and the rolling
    max-drawdown. EXEMPT books are graded for DISPLAY but carry `exempt=True` and can never be
    recommended for retirement. Pure; never raises."""
    exempt = book in EXEMPT_BOOKS
    reg_bogey = _regime_bogey_id(review_history)
    reg_series = _active_series(review_history, book, reg_bogey)
    loss = _loss_significance(reg_series)

    # advisory means vs the two named yardsticks (display; not the trigger)
    def _mean(bogey: str) -> float | None:
        s = _active_series(review_history, book, bogey)
        return round(sum(s) / len(s), 6) if s else None

    mdd = _max_drawdown(_book_curve(book_curves, book))
    mdd_watch = (mdd is not None and mdd >= _cf("max_drawdown_watch", _MAX_DD_WATCH))
    return {
        "book": book,
        "exempt": exempt,
        "graded_vs": reg_bogey,
        "graded_vs_label": _BOGEY_LABELS.get(reg_bogey, reg_bogey),
        "loss_test": loss,                      # {effective_n, mean, hac_t, significant, losing, status}
        "active_vs_spy": _mean("spy"),
        "active_vs_defensive": _mean("defensive"),
        "active_vs_regime_max": _mean("regime_max"),
        "max_drawdown": mdd,
        "max_drawdown_watch": bool(mdd_watch),
    }


# ─────────────────────────────────────────────────────────────────────────────
# lifecycle-state transition (never automated — writes a RECOMMENDATION)
# ─────────────────────────────────────────────────────────────────────────────
def _load_states() -> dict:
    """Persisted per-book lifecycle state {book: {state, losing_streak, since, history:[...]}}."""
    try:
        d = json.loads((_OUT / "states.json").read_text())
        return d if isinstance(d, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _save_states(states: dict) -> None:
    try:
        _OUT.mkdir(parents=True, exist_ok=True)
        (_OUT / "states.json").write_text(json.dumps(states, indent=2, default=str))
    except Exception:  # noqa: BLE001
        pass


def _decide(book: str, grade: dict, noisy: bool, prior: dict, asof: str) -> dict:
    """The lifecycle transition for ONE book (pure). Returns the new per-book state dict + the
    recommendation (or None). EXEMPT books are pinned to active forever (the yardstick). A book:

      · books a HAC-significant losing review → increment `losing_streak`; a non-significant/winning
        review resets it to 0 (a single good/insufficient review clears the streak — no persecution).
      · enters PROBATION when losing_streak >= losing_reviews_to_probation OR noisy_mirror is set.
      · already on PROBATION and STILL failing (another significant loss, or still-noisy) → the
        RETIRED-RECOMMENDATION.
      · recovers (streak reset AND not noisy) while on probation → recommendation to RESTORE to active
        (still human-executed; the module never flips the state itself past writing the record).

    The returned `state` is the module's BOOKKEEPING (it tracks streaks + the current recommendation);
    it is NOT an executed kill. `recommendation` is what cio/the agenda surface for a human."""
    if book in EXEMPT_BOOKS:
        return {"state": {"state": STATE_ACTIVE, "losing_streak": 0, "since": prior.get("since") or asof,
                          "exempt": True}, "recommendation": None}

    prev_state = str(prior.get("state") or STATE_ACTIVE)
    streak = int(prior.get("losing_streak") or 0)

    loss = grade.get("loss_test") or {}
    sig_loss = bool(loss.get("significant"))          # HAC-significant losing review (n gate already passed)
    insufficient = loss.get("status") != "scoring"    # below the effective_n gate → NEVER counts

    if sig_loss:
        streak += 1
    elif not insufficient:
        streak = 0                                    # a scored, non-losing review clears the streak
    # if insufficient-n: DO NOT touch the streak — honest paper-n hold (P3)

    to_probation = _ci("losing_reviews_to_probation", _LOSING_REVIEWS_TO_PROBATION)
    trigger_reasons: list[str] = []
    if streak >= to_probation:
        trigger_reasons.append(f"{streak} consecutive HAC-significant losing weekly reviews vs "
                               f"{grade.get('graded_vs_label')}")
    if noisy:
        trigger_reasons.append(f"noisy-mirror: active-return corr to {REFERENCE_BOOK} >= "
                               f"{_cf('noisy_mirror_corr', _NOISY_MIRROR_CORR)}")

    recommendation = None
    new_state = prev_state
    if trigger_reasons:
        if prev_state == STATE_PROBATION:
            new_state = STATE_RETIRE_REC
            recommendation = {
                "book": book, "recommend": STATE_RETIRE_REC,
                "reasons": trigger_reasons,
                "note": ("STILL failing on probation — recommend retirement. KILL IS NOT AUTOMATED: a "
                         "human/Fable executes this. Self-Directed remains the yardstick."),
            }
        elif prev_state == STATE_RETIRE_REC:
            new_state = STATE_RETIRE_REC              # already recommended; keep the standing rec
            recommendation = {
                "book": book, "recommend": STATE_RETIRE_REC, "reasons": trigger_reasons,
                "note": "retirement recommendation STANDING (awaiting human decision).",
            }
        else:
            new_state = STATE_PROBATION
            recommendation = {
                "book": book, "recommend": STATE_PROBATION, "reasons": trigger_reasons,
                "note": ("recommend PROBATION (watch, do not yet retire). CIO recommends; a human "
                         "confirms."),
            }
    elif prev_state in (STATE_PROBATION, STATE_RETIRE_REC):
        # was flagged, now clean → recommend restoring (human-executed)
        recommendation = {
            "book": book, "recommend": STATE_ACTIVE, "reasons": ["no active trigger this review"],
            "note": "recommend RESTORE to active (the trigger has cleared).",
        }
        # bookkeeping stays on the prior state until a human executes the restore — we do NOT
        # auto-clear a probation (the recommendation is the deliverable, not the mutation)
        new_state = prev_state

    st = {"state": new_state, "losing_streak": streak, "since": prior.get("since") or asof,
          "last_review": asof, "exempt": False}
    if recommendation is not None:
        # Stamp the state transition onto the rec so the governance emitter (MW2 c)
        # can populate before/after — the emitter has no other access to these.
        recommendation.setdefault("prev_state", prev_state)
        recommendation.setdefault("new_state", new_state)
    return {"state": st, "recommendation": recommendation}


# ─────────────────────────────────────────────────────────────────────────────
# the review — the one public entry point (pure, deterministic, READ-ONLY of prices)
# ─────────────────────────────────────────────────────────────────────────────
def review(review_history: list[dict] | None = None, *, asof: date | None = None,
           book_curves: dict | None = None, states: dict | None = None,
           persist: bool = False) -> dict:
    """Grade every US book, compute the orthogonality matrix, decide each book's lifecycle
    recommendation, and assemble the artifact. PURE, deterministic, READ-ONLY of prices — it NEVER
    trades, resizes, or flips a flag; it writes a JSON artifact + returns the recommendations for cio
    and the agenda to surface. `states` may be injected (tests); when None the persisted bookkeeping is
    read. `persist=True` writes states.json AND data/lifecycle/<asof>.json. Never raises."""
    asof = asof or date.today()
    asof_iso = asof.isoformat()
    hist = review_history if review_history is not None else history_from_ledgers()
    states = dict(states if states is not None else _load_states())

    ortho = orthogonality_matrix(hist, books=US_BOOKS)
    noisy_books = {f["book"] for f in ortho.get("noisy_mirror_flags") or []}

    grades: list[dict] = []
    new_states: dict = {}
    recommendations: list[dict] = []
    n_reviews = len(hist)

    graded_books = list(US_BOOKS) + [b for b in EXEMPT_BOOKS]
    for book in graded_books:
        grade = grade_book(book, hist, book_curves=book_curves)
        grades.append(grade)
        prior = states.get(book) or {}
        dec = _decide(book, grade, book in noisy_books, prior, asof_iso)
        new_states[book] = dec["state"]
        if dec.get("recommendation"):
            recommendations.append({**dec["recommendation"],
                                    "prev_state": prior.get("state") or STATE_ACTIVE,
                                    "new_state": dec["state"]["state"]})

    # honest paper-n banner: how many US books even have enough reviews to be tested
    scored = sum(1 for g in grades
                 if not g["exempt"] and (g["loss_test"] or {}).get("status") == "scoring")
    insufficient = sum(1 for g in grades
                       if not g["exempt"] and (g["loss_test"] or {}).get("status") == "insufficient-n")

    result = {
        "as_of": asof_iso,
        "n_reviews": n_reviews,
        "us_books": US_BOOKS,
        "exempt_books": sorted(EXEMPT_BOOKS),
        "grades": grades,
        "orthogonality": ortho,
        "recommendations": recommendations,
        "states": new_states,
        "paper_n": {
            "scored_books": scored, "insufficient_n_books": insufficient,
            "min_effective_n": _ci("min_effective_n", _MIN_EFFECTIVE_N),
            "note": ("At paper-n, honesty is the deliverable: a book below the effective_n gate is "
                     "'insufficient-n' and gets NO recommendation. KILL IS NEVER AUTOMATED — a human "
                     "executes any retirement. Self-Directed is exempt (the yardstick)."),
        },
    }
    if persist:
        try:
            _OUT.mkdir(parents=True, exist_ok=True)
            (_OUT / f"{asof_iso}.json").write_text(json.dumps(result, indent=2, default=str))
        except Exception:  # noqa: BLE001
            pass
        _save_states(new_states)
    return result


def write(review_history: list[dict] | None = None, *, asof: date | None = None,
          book_curves: dict | None = None) -> dict:
    """Build the review and persist data/lifecycle/<asof>.json + states.json. Returns
    {ok, as_of, json_path, n_recommendations}. Never raises."""
    asof = asof or date.today()
    try:
        rep = review(review_history, asof=asof, book_curves=book_curves, persist=True)
    except Exception as e:  # noqa: BLE001
        rep = {"as_of": asof.isoformat(), "recommendations": [], "error": str(e)}

    # MW2 emitter (c): emit ``book_lifecycle_recommendation`` governance events for each
    # probation/retire/restore recommendation.  Advisory-only — no authority changes, but the
    # governance docket requires these visible as governance events.  Never raises.
    try:
        _emit_lifecycle_recommendations(rep.get("recommendations") or [], asof_iso=rep.get("as_of") or asof.isoformat())
    except Exception:  # noqa: BLE001
        pass

    return {"ok": "error" not in rep, "as_of": rep.get("as_of"),
            "json_path": str(_OUT / f"{asof.isoformat()}.json"),
            "n_recommendations": len(rep.get("recommendations") or [])}


def _emit_lifecycle_recommendations(recommendations: list[dict], *, asof_iso: str) -> None:
    """MW2 emitter (c): one ``book_lifecycle_recommendation`` governance event per recommendation.
    Recommendation-only — no authority change, but the docket requires these visible as governance
    events.  Never raises."""
    try:
        from control_plane import governance as _gov
        for rec in (recommendations or []):
            book = str(rec.get("book") or "")
            recommend = str(rec.get("recommend") or "")
            reasons = rec.get("reasons") or []
            prev_state = str(rec.get("prev_state") or "")
            new_state = str(rec.get("new_state") or "")
            reason_str = "; ".join(str(r) for r in reasons) if reasons else recommend
            _gov.append({
                "event_type": "book_lifecycle_recommendation",
                "target": book,
                "actor": "book_lifecycle",
                "reason": reason_str,
                "before": prev_state,
                "after": recommend,
                "rollback": "decline recommendation in the CIO review; human executes any state change",
                "source_artifact": f"brain.book_lifecycle.write:{asof_iso}",
            })
    except Exception:  # noqa: BLE001 — governance emit must never abort the review
        pass


def load(asof: str) -> dict:
    try:
        return json.loads((_OUT / f"{str(asof)[:10]}.json").read_text())
    except Exception:  # noqa: BLE001
        return {}


def latest() -> dict:
    """The most recent lifecycle artifact on disk (for cio / the agenda). {} if none."""
    try:
        files = sorted(f for f in _OUT.glob("20*.json"))
        return json.loads(files[-1].read_text()) if files else {}
    except Exception:  # noqa: BLE001
        return {}


# ─────────────────────────────────────────────────────────────────────────────
# consumers — cio weekly note + the improvement agenda (one source of truth, charter P7)
# ─────────────────────────────────────────────────────────────────────────────
def lifecycle_summary(rep: dict | None = None) -> dict:
    """A compact, display-only summary for the cio weekly note. Reads the latest artifact unless one is
    injected. Returns {as_of, n_reviews, states:{book:state}, noisy_mirror_flags, recommendations,
    scored_books, insufficient_n_books}. Never raises."""
    rep = rep if rep is not None else latest()
    if not rep:
        return {"as_of": None, "states": {}, "recommendations": [], "noisy_mirror_flags": [],
                "scored_books": 0, "insufficient_n_books": 0, "n_reviews": 0}
    states = {b: (s or {}).get("state") for b, s in (rep.get("states") or {}).items()}
    paper = rep.get("paper_n") or {}
    return {
        "as_of": rep.get("as_of"),
        "n_reviews": rep.get("n_reviews", 0),
        "states": states,
        "noisy_mirror_flags": (rep.get("orthogonality") or {}).get("noisy_mirror_flags") or [],
        "recommendations": rep.get("recommendations") or [],
        "scored_books": paper.get("scored_books", 0),
        "insufficient_n_books": paper.get("insufficient_n_books", 0),
    }


def agenda_items(rep: dict | None = None) -> list[dict]:
    """One improvement-agenda item per probation/retire recommendation, {owner: fable-review} (a
    lifecycle change is a boundary call — never self-applied, charter P8). Reads the latest artifact
    unless injected. Returns raw item dicts the agenda's fusion layer wraps; empty when there is nothing
    to recommend (P2 no-op). RESTORE recommendations are surfaced too (a book earning its way back is
    also a human decision). Never raises."""
    rep = rep if rep is not None else latest()
    recs = (rep or {}).get("recommendations") or []
    out: list[dict] = []
    for r in recs:
        book = r.get("book")
        rec = r.get("recommend")
        reasons = r.get("reasons") or []
        if rec == STATE_RETIRE_REC:
            title = f"Book '{book}' — CIO recommends RETIREMENT (still failing on probation)"
            impact = "a noisy-mirror / persistently-losing book stops consuming capital + API cost"
        elif rec == STATE_PROBATION:
            title = f"Book '{book}' — CIO recommends PROBATION (watch)"
            impact = "the book is watched; a second failing review escalates to a retirement recommendation"
        elif rec == STATE_ACTIVE:
            title = f"Book '{book}' — CIO recommends RESTORE to active (trigger cleared)"
            impact = "a recovered book returns to full standing"
        else:
            continue
        out.append({
            "id": f"lifecycle:{book}",
            "title": title,
            "evidence": [f"lifecycle {rep.get('as_of')}: {book} {r.get('prev_state')} → recommend {rec}"]
                        + [f"trigger: {rn}" for rn in reasons],
            "suggested_fix": ("Review the lifecycle card and EXECUTE or DECLINE the recommendation. "
                              "KILL IS NOT AUTOMATED — this is a human/Fable decision (charter P8). "
                              "Self-Directed is exempt and never appears here."),
            "expected_impact": impact,
            "owner": "fable-review",
            "recommend": rec,
            "book": book,
        })
    return out
