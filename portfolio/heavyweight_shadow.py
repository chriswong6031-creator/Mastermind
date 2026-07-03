"""Heavyweight mirror-shadow A/B — the comparison arm for the W6 T1 firm-universe flag.

W6 T1 re-points Heavyweight from a Flagship AMPLIFIER (hard-gated to Flagship's universe → it could
only concentrate Flagship's names, so it doubled Flagship's bets) to the FIRM'S BEST-IDEAS
CONCENTRATOR (universe = the union of every published book, one name per fragility cluster). The
falsifiable claim behind the flip: the firm-universe book should be MORE ORTHOGONAL to Flagship than
the old flagship-mirror book — i.e. its cluster-overlap-with-Flagship should FALL. If it does not,
the firm universe bought nothing and the flag should be killed (registry experiment 'hw-firm-universe-ab').

This module is the MIRROR SHADOW: given the Brain's raw submission for a day, it deterministically
re-derives the enforced book under BOTH arms —
  * ``live``   — the firm-universe universe + one-per-cluster (what ships when the flag is ON), and
  * ``mirror`` — the OLD flagship-only gate (no cross-book union), the flag-OFF behaviour,
and measures each arm's cluster-overlap-with-Flagship. The delta is the A/B signal the registry gate
reads: overlap(live) < overlap(mirror) over the 2–4wk window ⇒ the flag earned its keep.

ISOLATION CONTRACT (mirrors portfolio.shadow_books / portfolio.desk_ab): NEVER imported by the live
run-path; it only READS published ``latest.json`` books + the Brain's raw submission (both passed in
or read read-only) and WRITES nothing on import. The runner writes only under
``data/shadow/heavyweight_ab/``. Pure enforcement re-uses ``bot.heavyweight`` primitives so the two
arms are graded EXACTLY like the live book. Best-effort throughout — never raises.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent.parent
_SHADOW = _ROOT / "data" / "shadow" / "heavyweight_ab"


# ─────────────────────────────────────────────────────────────────────────────
# cluster-overlap-with-Flagship — the orthogonality metric the A/B turns on
# ─────────────────────────────────────────────────────────────────────────────

def _clusters_of(tickers: Any) -> set[str]:
    """The set of fragility-chain cluster ids a set of tickers occupies. Degrades to per-name
    singletons when the resolver is absent (so two distinct un-clustered names never collapse).
    Pure; never raises."""
    from portfolio import fragility_chain
    out: set[str] = set()
    for t in (tickers or []):
        tk = str(t or "").upper().strip()
        if not tk:
            continue
        try:
            out.add(str(fragility_chain.cluster_id(tk)))
        except Exception:  # noqa: BLE001 — resolver absent → singleton identity
            out.add(f"name:{tk}")
    return out


def cluster_overlap(book_tickers: Any, flagship_tickers: Any) -> dict:
    """Cluster-overlap-with-Flagship for a proposed book. Returns
    ``{overlap_clusters, book_clusters, flagship_clusters, jaccard, overlap_frac}`` where:
      * ``overlap_frac`` = |book∩flagship clusters| / |book clusters|  (of MY clusters, how many does
        Flagship already own — the amplifier metric: 1.0 = a pure Flagship mirror at the cluster level),
      * ``jaccard``      = |∩| / |∪|  (symmetric similarity).
    An empty book → zeros (an empty book is trivially non-overlapping). Pure; never raises."""
    bc = _clusters_of(book_tickers)
    fc = _clusters_of(flagship_tickers)
    inter = bc & fc
    union = bc | fc
    return {
        "overlap_clusters": sorted(inter),
        "book_clusters": sorted(bc),
        "flagship_clusters": sorted(fc),
        "n_book_clusters": len(bc),
        "n_overlap": len(inter),
        "jaccard": round(len(inter) / len(union), 4) if union else 0.0,
        "overlap_frac": round(len(inter) / len(bc), 4) if bc else 0.0,
    }


# ─────────────────────────────────────────────────────────────────────────────
# the two arms — enforce the SAME raw submission under firm-union vs flagship-mirror
# ─────────────────────────────────────────────────────────────────────────────

def _enforce_arm(holdings: list[dict], allowed: set[str], *, one_per_cluster: bool) -> dict:
    """Run the Brain's raw submission through the deterministic rails (bot.heavyweight._enforce), with
    one-per-cluster collapse applied first for the firm-universe arm only. Returns
    ``{weights, tickers, notes}``. Pure; never raises (returns empty book on any failure)."""
    try:
        from bot import heavyweight as hw
        rows = list(holdings or [])
        cluster_notes: dict = {}
        if one_per_cluster:
            rows, cluster_notes = hw._one_per_cluster(rows)
        weights, _kept, notes = hw._enforce(rows, allowed)
        if cluster_notes.get("dropped_same_cluster"):
            notes["dropped_same_cluster"] = cluster_notes["dropped_same_cluster"]
        return {"weights": weights, "tickers": sorted(weights), "notes": notes}
    except Exception:  # noqa: BLE001 — a helper failure degrades to an empty arm, never raises
        return {"weights": {}, "tickers": [], "notes": {}}


def compare(submission: dict | None, *, asof: str | None = None) -> dict:
    """The A/B comparison for one day. Re-derives BOTH arms from the SAME raw Brain submission and the
    CURRENTLY-published books, then measures each arm's cluster-overlap-with-Flagship.

    ``submission`` — the Brain's raw ``{holdings:[{ticker, weight, conviction, …}], summary}`` (what it
        proposed before any universe/cluster enforcement). None/empty → an inert stub.

    Returns::

        {asof, live:{tickers, overlap_frac, jaccard, n_book_clusters},
               mirror:{tickers, overlap_frac, jaccard, n_book_clusters},
               overlap_delta,          # live.overlap_frac − mirror.overlap_frac  (< 0 ⇒ flag earns keep)
               flag_favored: bool,     # overlap_delta < 0  (firm universe is MORE orthogonal)
               universe:{firm_size, mirror_size, source}}

    Pure; NEVER raises (degrades to an inert stub on any failure)."""
    asof = asof or date.today().isoformat()
    stub = {"asof": asof, "live": {}, "mirror": {}, "overlap_delta": None,
            "flag_favored": None, "universe": {}}
    holdings = (submission or {}).get("holdings") if isinstance(submission, dict) else None
    if not holdings:
        return stub
    try:
        from bot import heavyweight as hw
        firm_allowed, uni_meta = hw._firm_universe_arms()          # (firm_union_set, mirror_set, meta)
    except Exception:  # noqa: BLE001
        return stub

    firm_union, mirror_set, meta = firm_allowed
    flagship_tk = mirror_set                                       # the mirror universe IS Flagship's names

    live = _enforce_arm(holdings, firm_union, one_per_cluster=True)
    mirror = _enforce_arm(holdings, mirror_set, one_per_cluster=False)

    live_ov = cluster_overlap(live["tickers"], flagship_tk)
    mirror_ov = cluster_overlap(mirror["tickers"], flagship_tk)
    delta = round(live_ov["overlap_frac"] - mirror_ov["overlap_frac"], 4)

    return {
        "asof": asof,
        "live": {"tickers": live["tickers"], "overlap_frac": live_ov["overlap_frac"],
                 "jaccard": live_ov["jaccard"], "n_book_clusters": live_ov["n_book_clusters"],
                 "overlap_clusters": live_ov["overlap_clusters"]},
        "mirror": {"tickers": mirror["tickers"], "overlap_frac": mirror_ov["overlap_frac"],
                   "jaccard": mirror_ov["jaccard"], "n_book_clusters": mirror_ov["n_book_clusters"],
                   "overlap_clusters": mirror_ov["overlap_clusters"]},
        "overlap_delta": delta,
        "flag_favored": bool(delta < 0),
        "universe": {"firm_size": len(firm_union), "mirror_size": len(mirror_set),
                     "source": meta.get("source"), "mirror_fallback": meta.get("mirror_fallback")},
    }


# ─────────────────────────────────────────────────────────────────────────────
# runner — persist the daily A/B row under data/shadow/heavyweight_ab/ (write-isolated)
# ─────────────────────────────────────────────────────────────────────────────

def record(submission: dict | None, *, asof: str | None = None) -> dict:
    """Compute the A/B row for ``asof`` and append it to the isolated shadow ledger. Idempotent per
    date (a re-run replaces the day's row). Returns the computed row. Never raises."""
    row = compare(submission, asof=asof)
    row["recorded_at"] = datetime.now(timezone.utc).isoformat()
    try:
        _SHADOW.mkdir(parents=True, exist_ok=True)
        p = _SHADOW / "ledger.jsonl"
        rows: list[dict] = []
        if p.exists():
            for line in p.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except Exception:  # noqa: BLE001
                    continue
                if r.get("asof") != row.get("asof"):
                    rows.append(r)
        rows.append(row)
        p.write_text("\n".join(json.dumps(r, default=str, ensure_ascii=False) for r in rows) + "\n")
    except Exception:  # noqa: BLE001 — a write failure must never break the (already-computed) A/B
        pass
    return row


def window_verdict(days: int = 28) -> dict:
    """Roll up the ledger into the registry-gate read: over the most recent ``days`` A/B rows, does the
    firm-universe arm's mean cluster-overlap-with-Flagship fall BELOW the mirror arm's? Returns
    ``{n, mean_live_overlap, mean_mirror_overlap, mean_delta, flag_favored, kill_flag}`` where
    ``kill_flag`` is True when there is enough data (n≥5) AND the firm universe is NOT more orthogonal
    (mean_delta ≥ 0) — the pre-committed 'overlap doesn't fall vs the mirror → kill the flag' gate.
    Inert (kill_flag=False) below the data floor. Never raises."""
    out = {"n": 0, "mean_live_overlap": None, "mean_mirror_overlap": None,
           "mean_delta": None, "flag_favored": None, "kill_flag": False}
    try:
        p = _SHADOW / "ledger.jsonl"
        if not p.exists():
            return out
        rows: list[dict] = []
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except Exception:  # noqa: BLE001
                continue
        rows.sort(key=lambda r: r.get("asof") or "")
        rows = [r for r in rows[-days:]
                if isinstance(r.get("live"), dict) and isinstance(r.get("mirror"), dict)
                and r["live"].get("overlap_frac") is not None
                and r["mirror"].get("overlap_frac") is not None]
        n = len(rows)
        if n == 0:
            return out
        lv = sum(float(r["live"]["overlap_frac"]) for r in rows) / n
        mr = sum(float(r["mirror"]["overlap_frac"]) for r in rows) / n
        delta = lv - mr
        out.update({"n": n, "mean_live_overlap": round(lv, 4), "mean_mirror_overlap": round(mr, 4),
                    "mean_delta": round(delta, 4), "flag_favored": bool(delta < 0),
                    # kill only with enough evidence AND the firm universe failing to reduce overlap
                    "kill_flag": bool(n >= 5 and delta >= 0)})
        return out
    except Exception:  # noqa: BLE001
        return out
