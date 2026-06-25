"""Discounted Thompson Sampling over the shadow-book / desk-A/B policies (#5) — faster, adaptive A/B.

The shadow leaderboard ranks policies by CUMULATIVE forward return: slow to converge, and blind to a
decayed edge (a policy that worked for two months then stopped still looks good on the cumulative line).
This treats each policy as a bandit ARM with a DISCOUNTED Beta posterior over its forward HIT-RATE —
recent resolved theses count fully, older ones decay by γ per observation, so the posterior tracks
regime breaks. It reports each arm's posterior mean + 95% credible interval + P(this arm is best)
(the probability-matching Thompson allocation), which reaches a confident verdict in fewer resolutions
than raw return and self-corrects when a policy stops working (Discounted-TS is near-optimal under
non-stationarity, where vanilla TS and a fixed even-split A/B cling to a stale winner).

ISOLATION: reads ONLY the isolated shadow ledgers (portfolio.shadow_books theses). It NEVER controls
real sizing — the desk's actual policy switch stays gated on the A/B experiment; this is a faster,
honest *read* of which arm is winning, not an auto-switch. PURE + DETERMINISTIC (seeded sampler, fixed
γ) so a re-run is byte-identical and testable. Best-effort: degrades to 'building' rather than raising.
"""
from __future__ import annotations

_GAMMA = 0.98          # per-observation discount — older resolved theses decay; tracks regime breaks
_N_SAMPLES = 20_000    # Monte-Carlo draws for P(best); fixed seed → deterministic
_SEED = 12345
_MIN_N = 8             # an arm needs this many resolved theses before the read leaves 'building'


def _disc_beta(outcomes: list[int], gamma: float = _GAMMA) -> tuple[float, float, float]:
    """Discounted Beta(α, β) posterior from a recency-ordered (oldest→newest) list of 0/1 outcomes.
    The most-recent observation carries full weight (γ^0=1); each step older decays by γ. Returns
    (α, β, n_eff) where n_eff = Σ weights (the effective, discounted sample size). Uniform prior (1,1)."""
    n = len(outcomes)
    a, b, n_eff = 1.0, 1.0, 0.0
    for i, o in enumerate(outcomes):
        w = gamma ** (n - 1 - i)
        n_eff += w
        if o:
            a += w
        else:
            b += w
    return a, b, n_eff


def rank(arms: dict[str, list], gamma: float = _GAMMA, n_samples: int = _N_SAMPLES,
         seed: int = _SEED, min_n: int = _MIN_N) -> dict:
    """Rank bandit arms by Discounted-TS. `arms` maps arm_id → a list of resolved outcomes, each either
    a 0/1 int OR a (date_iso, 0|1) pair (sorted oldest→newest by date if pairs). Returns a dict with a
    per-arm posterior (mean, 95% credible interval, P(best), n, n_eff) sorted by P(best) desc, plus a
    'status' ('building' until some arm clears `min_n`). Deterministic. Never raises."""
    out_arms: list[dict] = []
    try:
        import numpy as np
        rng = np.random.default_rng(seed)
        ids, posts, max_n = [], {}, 0
        for aid, raw in (arms or {}).items():
            seq = _to_outcomes(raw)
            a, b, n_eff = _disc_beta(seq, gamma)
            posts[aid] = (a, b, n_eff, len(seq))
            ids.append(aid)
            max_n = max(max_n, len(seq))
        if not ids:
            return {"status": "building", "gamma": gamma, "arms": []}
        samples = {aid: rng.beta(posts[aid][0], posts[aid][1], n_samples) for aid in ids}
        stack = np.vstack([samples[aid] for aid in ids])          # (n_arms, n_samples)
        winners = np.argmax(stack, axis=0)
        for i, aid in enumerate(ids):
            a, b, n_eff, n = posts[aid]
            s = samples[aid]
            out_arms.append({
                "id": aid, "posterior_mean": round(float(a / (a + b)), 3),
                "ci95": [round(float(np.quantile(s, 0.025)), 3), round(float(np.quantile(s, 0.975)), 3)],
                "p_best": round(float((winners == i).mean()), 3),
                "n": n, "n_eff": round(float(n_eff), 2)})
        out_arms.sort(key=lambda x: -x["p_best"])
        return {"status": "scoring" if max_n >= min_n else "building", "gamma": gamma, "arms": out_arms}
    except Exception as exc:  # noqa: BLE001 — analytics layer; never break the caller
        return {"status": "building", "gamma": gamma, "arms": out_arms, "error": str(exc)}


def _to_outcomes(raw: list) -> list[int]:
    """Normalize an arm's outcome list to a recency-ordered list of 0/1 ints. Accepts bare ints or
    (date, outcome) pairs (sorted by date). Non-conforming entries are dropped."""
    if not raw:
        return []
    if isinstance(raw[0], (list, tuple)):
        ordered = sorted((r for r in raw if isinstance(r, (list, tuple)) and len(r) == 2),
                         key=lambda r: str(r[0]))
        return [int(bool(o)) for _, o in ordered]
    return [int(bool(o)) for o in raw]


# ─────────────────────────────────────────────────────────────────────────────
# shadow-book adapter — build arms from the isolated shadow ledgers
# ─────────────────────────────────────────────────────────────────────────────
def _book_outcomes(book_id: str) -> list:
    """(state_asof, hit) for each RESOLVED thesis of a shadow book — hit ⇔ NOT falsified by its own
    threshold predicate (the exact grade shadow_books._book_summary / scorer.track_record use)."""
    try:
        from portfolio import shadow_books as S
        rows = []
        for t in S._load_theses(book_id):
            if t.get("status") != "resolved" or t.get("realized") is None:
                continue
            chk = (t.get("falsifier") or {}).get("check") or {}
            op, thr = chk.get("op", "<"), chk.get("threshold", -0.05)
            falsified = (t["realized"] < thr) if op == "<" else (t["realized"] > thr)
            rows.append((str(t.get("state_asof") or "")[:10], 0 if falsified else 1))
        return rows
    except Exception:  # noqa: BLE001
        return []


def rank_shadow_books(gamma: float = _GAMMA) -> dict:
    """Discounted-TS over the attribution shadow books (prod / no_committee / no_calibration /
    engine_only / risk_tilt). Each arm's outcomes are its resolved theses' hit/miss. Labels attached.
    Returns the rank() block. Honestly 'building' until a book accrues resolved theses (~first cohort
    2026-07-17). Never raises."""
    try:
        from portfolio import shadow_books as S
        arms = {p["id"]: _book_outcomes(p["id"]) for p in S.POLICIES}
        res = rank(arms, gamma=gamma)
        labels = {p["id"]: p.get("label") for p in S.POLICIES}
        for a in res.get("arms", []):
            a["label"] = labels.get(a["id"])
        res["note"] = ("Discounted Thompson over each shadow policy's resolved-thesis hit-rate; "
                       "p_best = probability the arm is best (Thompson allocation). Read-only — never "
                       "switches the live policy.")
        return res
    except Exception as exc:  # noqa: BLE001
        return {"status": "building", "arms": [], "error": str(exc)}
