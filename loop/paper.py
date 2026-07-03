"""Forward paper-trading judge — the REAL judge (the only thing that flips paper->live).

A promoted sleeve archives its live target weights via signal_archive (keep-FIRST PIT),
and a forward Brier accumulates against realized next-period sign. Until a forward window
that did not exist at training time resolves, the sleeve stays paper/display-only — no
in-sample DSR ever promotes a sleeve to live sizing.

W-L / L4 — `forward_brier` is now WIRED (it was a None-stub with zero callers). Each archived
snapshot carries `asof` + `prob_up` (the sleeve's forward up-probability at that PIT) + the sleeve
`weights`. Grading is leakage-free: for a snapshot dated `asof`, we read the sleeve's realized
forward return over the NEXT `horizon_sessions` sessions from the vendored yahoo parquet store —
using ONLY closes strictly after `asof` — and score the Brier against the up/down outcome. A
snapshot whose forward window has not fully resolved (the market has not printed `horizon_sessions`
sessions past `asof` yet) is SKIPPED, so the Brier only ever reflects realized, out-of-training data.
Returns None while no snapshot has resolved ('building') — the paper→live promise is honest, not a
zombie: a sleeve promotes only once real forward periods grade it.
"""
from __future__ import annotations

import bot  # noqa: F401
from engine import signal_archive as sa

_HORIZON_SESSIONS = 5   # forward sign horizon (one trading week) — the resolved-window unit


def enroll(spec_hash: str, weights: dict, asof: str, prob_up: float = 0.55) -> bool:
    """Archive the sleeve's live weights at `asof` (keep-first). Returns appended?"""
    return sa.archive_snapshot(label=f"sleeve_{spec_hash[:8]}",
                               snap={"spec_hash": spec_hash, "weights": weights, "prob_up": prob_up},
                               asof=asof)


def _default_read(symbol: str):
    """The vendored yahoo parquet close series for `symbol` (offline, dividend-adjusted). None on miss."""
    try:
        from lib import store
        return store.read("yahoo", symbol)
    except Exception:  # noqa: BLE001
        return None


def _sleeve_forward_up(weights: dict, asof: str, horizon: int, read_fn=None) -> bool | None:
    """Realized forward sign of a weighted sleeve over the `horizon` sessions AFTER `asof`.

    Leakage-free: reads ONLY the vendored yahoo parquet closes with date strictly > asof, takes the
    first `horizon` of them, and asks whether the weight-blended return over that window is >= 0.
    Returns True/False when the window has fully resolved, or None when it hasn't (or on missing data)
    — a None makes the caller SKIP that snapshot (never a fabricated outcome). `read_fn(symbol)->df`
    injects the price store for offline tests (default: the yahoo parquet). Never raises.
    """
    try:
        import pandas as pd
        rfn = read_fn if read_fn is not None else _default_read

        cutoff = pd.Timestamp(str(asof)[:10])
        w = {(k or "").upper(): float(v) for k, v in (weights or {}).items()
             if isinstance(v, (int, float)) and v}
        if not w:
            return None
        tot = sum(abs(x) for x in w.values()) or 1.0

        blended = 0.0
        resolved_any = False
        for sym, wt in w.items():
            df = rfn(sym)
            if df is None or "close" not in getattr(df, "columns", []) or len(df) == 0:
                return None                      # a missing leg voids the whole snapshot (P2)
            s = df["close"].astype(float).dropna()
            s.index = pd.to_datetime(s.index)
            fwd = s[s.index > cutoff]
            if len(fwd) < horizon:
                return None                      # window has not fully printed yet → unresolved
            entry = s[s.index <= cutoff]
            if len(entry) == 0:
                return None
            p0 = float(entry.iloc[-1])
            p1 = float(fwd.iloc[horizon - 1])
            if p0 <= 0:
                return None
            blended += (wt / tot) * (p1 / p0 - 1.0)
            resolved_any = True
        if not resolved_any:
            return None
        return bool(blended >= 0.0)
    except Exception:  # noqa: BLE001 — a grading miss degrades to 'unresolved', never a fake outcome
        return None


def forward_brier(spec_hash: str, horizon: int = _HORIZON_SESSIONS, *,
                  archive_fn=None, read_fn=None):
    """Rolling forward Brier from the archived snapshots vs realized next-period sign.

    Returns None ('building') until at least one forward window resolves — paper-only until then.
    Otherwise a dict: {brier, n_resolved, n_snapshots, mean_prob_up, horizon}. Leakage-free (only
    prices AFTER each snapshot's asof are read) and offline (yahoo parquet). `archive_fn(label)->df`
    and `read_fn(symbol)->df` inject the snapshot archive + price store for offline tests. Never raises.
    """
    afn = archive_fn if archive_fn is not None else sa.load_archive
    df = afn(f"sleeve_{spec_hash[:8]}")
    if df is None or len(df) < 1:
        return None

    n_snapshots = len(df)
    sq_err: list[float] = []
    probs: list[float] = []
    for _, row in df.iterrows():
        snap = row.get("snapshot") if hasattr(row, "get") else None
        if not isinstance(snap, dict):
            continue
        asof = row.get("asof") if hasattr(row, "get") else None
        if not asof:
            asof = snap.get("asof")
        prob_up = snap.get("prob_up")
        weights = snap.get("weights")
        if asof is None or not isinstance(prob_up, (int, float)) or not isinstance(weights, dict):
            continue
        outcome = _sleeve_forward_up(weights, str(asof), int(horizon), read_fn=read_fn)
        if outcome is None:                      # window unresolved → not yet gradable, skip
            continue
        p = min(1.0, max(0.0, float(prob_up)))
        sq_err.append((p - (1.0 if outcome else 0.0)) ** 2)
        probs.append(p)

    if not sq_err:
        return None                              # building — no forward window has resolved yet
    n = len(sq_err)
    return {
        "brier": round(sum(sq_err) / n, 6),
        "n_resolved": n,
        "n_snapshots": int(n_snapshots),
        "mean_prob_up": round(sum(probs) / n, 4),
        "horizon": int(horizon),
    }
