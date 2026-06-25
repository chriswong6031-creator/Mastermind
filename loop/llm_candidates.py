"""LLM-proposed factor hypotheses → the frozen loop/ gauntlet (#8, the proposer side ONLY).

The Brain can propose falsifiable, economically-grounded single-name factor hypotheses; this module
TRANSLATES each into a `FactorCandidate` (the loop's atomic, PIT-safe spec) and PRE-SCREENS it through
the FROZEN judge (`loop.harness.score`) on the IN-SAMPLE slice only — reporting the judge's own
per-candidate overfit stats (Deflated-Sharpe verdict, crisis-survival, 5-fold-purged-CV sign robustness,
Newey-West p) deflated by the batch size. It is READ-ONLY: it never burns the one-shot holdout, never
writes the trial store, and never promotes. Real promotion to paper stays in the DELIBERATE
`loop.single_name_iterate.iterate(dry_run=False)` path (one holdout peek per spec_hash, BH-FDR + PBO over
the whole batch, budget gate) — because auto-promoting LLM-proposed factors on a schedule is precisely
the multiple-testing / p-hacking trap the gauntlet exists to stop.

The judge is FROZEN and unchanged here. Only the PROPOSER (this translator) is new. Best-effort: a bad
hypothesis is dropped, an unavailable panel degrades to status 'unavailable'; never raises into a caller.
"""
from __future__ import annotations

# the kinds loop.single_name_candidates.FactorCandidate actually implements (others raise ValueError)
ALLOWED_KINDS = {"momentum", "reversal", "lowvol", "high52w"}
ALLOWED_SIDES = {"long", "ls"}
_HOLDOUT_START = "2022-01-01"     # in-sample = strictly before this (mirrors the loop's split convention)


def _valid_spec(spec: dict) -> bool:
    """A hypothesis is well-formed iff its kind is implemented and its params are sane + bounded (so an
    LLM can't request a degenerate / look-ahead-prone factor)."""
    try:
        if not isinstance(spec, dict):
            return False
        if spec.get("kind") not in ALLOWED_KINDS:
            return False
        lb = int(spec.get("lookback"))
        sk = int(spec.get("skip", 0))
        frac = float(spec.get("frac", 0.10))
        vw = int(spec.get("vol_win", 60))
        # lowvol's signal is vol-driven (lookback ignored) → lookback=0 is the canonical spec; the
        # other kinds need a real lookback window (lookback=0 is degenerate for momentum/reversal/high52w).
        lb_min = 0 if spec.get("kind") == "lowvol" else 1
        if not (lb_min <= lb <= 504 and 0 <= sk <= 63 and 0.02 <= frac <= 0.50 and 20 <= vw <= 252):
            return False
        if spec.get("side", "long") not in ALLOWED_SIDES:
            return False
        return bool(str(spec.get("name") or "").strip())
    except (TypeError, ValueError):
        return False


def materialize_hypothesis(spec: dict, mem_df):
    """Translate one LLM hypothesis dict → a FactorCandidate, or None if invalid. mem_df is the PIT
    membership table from single_name_panel.load_panel()['mem']."""
    if not _valid_spec(spec):
        return None
    try:
        from loop.single_name_candidates import FactorCandidate
        return FactorCandidate(
            name=str(spec["name"]), kind=spec["kind"], lookback=int(spec["lookback"]),
            skip=int(spec.get("skip", 0)), side=spec.get("side", "long"),
            frac=float(spec.get("frac", 0.10)), vol_win=int(spec.get("vol_win", 60)), mem_df=mem_df)
    except Exception:  # noqa: BLE001
        return None


def generate(specs: list, mem_df) -> list:
    """LLM hypotheses → FactorCandidate list (invalid ones dropped)."""
    out = []
    for spec in specs or []:
        c = materialize_hypothesis(spec, mem_df)
        if c is not None:
            out.append(c)
    return out


def validate(specs: list, holdout_start: str = _HOLDOUT_START) -> dict:
    """READ-ONLY in-sample PRE-SCREEN of LLM factor hypotheses through the FROZEN judge.

    Builds each candidate, scores it with `harness.score` on the in-sample slice (closes < holdout_start)
    with DSR deflated by the batch size (n_eff = #candidates — honest multiple-testing penalty), and
    returns the judge's per-candidate verdict. NEVER touches the holdout, the trial store, or promotion.
    A candidate that clears the per-candidate gauntlet here is a *worthy hypothesis to enroll* — the
    actual paper promotion is the deliberate `single_name_iterate.iterate(dry_run=False)` run (which adds
    the batch PBO/BH-FDR + the one-shot holdout). Degrade-safe: 'unavailable' if the panel can't load.
    """
    out = {"status": "unavailable", "n_specs": len(specs or []), "n_valid": 0, "candidates": [],
           "note": "READ-ONLY in-sample pre-screen via the frozen judge; real promotion is the "
                   "deliberate single_name_iterate.iterate run (holdout + batch PBO/BH-FDR)."}
    try:
        from loop import harness
        from loop.single_name_panel import load_panel
        panel = load_panel()
        closes, mem, bill = panel["closes"], panel["mem"], panel["bill"]
        cands = generate(specs, mem)
        out["n_valid"] = len(cands)
        if not cands:
            out["status"] = "no_valid_candidates"
            return out
        insample = closes[closes.index < holdout_start]
        s6040 = harness.sixty_forty_sharpe(insample, bill)
        n_eff = max(1, len(cands))                       # deflate DSR by the batch size (honest)
        res = []
        for c in cands:
            try:
                m = harness.score(c, insample, bill, n_eff=n_eff, sixty40_sharpe=s6040)
            except Exception:  # noqa: BLE001 — one bad candidate can't sink the screen
                m = None
            if not m:
                res.append({"name": c.name, "spec_hash": c.spec_hash, "screen": "unbacktestable"})
                continue
            survives = (str(m.get("dsr_verdict") or "").startswith("SURVIVES")
                        and bool(m.get("beats_spy")) and bool(m.get("beats_6040"))
                        and bool(m.get("crisis_pass")) and bool(m.get("fold_robust")))
            res.append({
                "name": c.name, "spec_hash": c.spec_hash, "kind": c.spec["kind"],
                "sharpe": round(float(m.get("sharpe") or 0), 3), "dsr": m.get("dsr"),
                "dsr_verdict": m.get("dsr_verdict"), "beats_spy": bool(m.get("beats_spy")),
                "beats_6040": bool(m.get("beats_6040")), "crisis_pass": bool(m.get("crisis_pass")),
                "fold_robust": bool(m.get("fold_robust")), "p_value": m.get("p_value"),
                # in-sample pre-screen verdict ONLY — NOT a promotion (holdout/PBO/BH-FDR pending)
                "prescreen": "worthy" if survives else "rejected"})
        res.sort(key=lambda r: (r.get("prescreen") != "worthy", -(r.get("dsr") or 0)))
        out["candidates"] = res
        out["n_worthy"] = sum(1 for r in res if r.get("prescreen") == "worthy")
        out["status"] = "scored"
        return out
    except Exception as exc:  # noqa: BLE001
        out["error"] = str(exc)[:160]
        return out


# A small SEED library of economically-grounded hypotheses — lets the pre-screen run end-to-end with no
# LLM (deterministic). The LLM proposer (cheap tier per config/agents.yml — never Opus for this) is the
# v2 hook: it would return dicts in this exact shape, optionally conditioned on the engine's recent IC
# evidence, and feed validate(); confirmed-worthy hypotheses are then run through the deliberate iterate.
SEED_HYPOTHESES = [
    {"name": "mom_12_1", "kind": "momentum", "lookback": 252, "skip": 21, "side": "long", "frac": 0.10},
    {"name": "mom_6_1", "kind": "momentum", "lookback": 126, "skip": 21, "side": "long", "frac": 0.10},
    {"name": "rev_1m", "kind": "reversal", "lookback": 21, "side": "long", "frac": 0.10},
    {"name": "lowvol_60", "kind": "lowvol", "lookback": 0, "vol_win": 60, "side": "long", "frac": 0.20},
    {"name": "near_52w_high", "kind": "high52w", "lookback": 252, "side": "long", "frac": 0.10},
]
