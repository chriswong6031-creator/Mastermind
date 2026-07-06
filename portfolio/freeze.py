"""freeze_to_prior — shared helper for failure-path exposure guardrails (Charter P2).

Charter P2: a failure path may NEVER yield more exposure or authority than the success path.

Every US book's finalize path wraps ``firm_exposure.clamp_book`` in a try/except.  The original
fallback ("filter to held names") correctly drops new adds but INCORRECTLY keeps held names at the
BUILT target weight, which can be HIGHER than what a successful clamp would have returned (the
clamp is subtract-only; the built weight may already be at a cap-exceeding level that the clamp
would have reduced).

The correct freeze semantics, given that all four downstream consumers (execute_or_queue /
rebalance / settle) treat an ABSENT name as "liquidate to zero":

  * names ONLY in prior  → RETAIN at prior weight (absence = liquidation = new risk reduction,
                            violating "freeze = do-not-trade");
  * names in BOTH        → weight = min(target, prior) — never increase vs. the prior book;
  * names ONLY in target → DROP (new adds: introducing a new name on the failure path is the
                            canonical Charter P2 violation — no new risk when context is unknown).

Invariants (all three hold simultaneously):
  1. per-name: frozen[name] <= prior[name] for all names in result
  2. total gross: sum(frozen.values()) <= sum(prior.values())
  3. no new names: set(frozen.keys()) <= set(prior.keys())

Downstream-semantics note (documented here because it affects the helper contract):

  ``paper_account.rebalance`` and ``bot.settle.execute_or_queue`` treat an ABSENT ticker as
  "liquidate to zero" — the rebalance loop explicitly calls ``del state["positions"][ticker]``
  for any held name not in the target dict.  Therefore, to implement FREEZE (do-not-trade) for
  prior-only names we must INCLUDE them in the returned dict at their prior weight; omitting them
  would silently trigger a liquidation, adding new risk-reduction that the caller did not intend.

  The ``positions`` dict returned by this helper may be used directly as the ``target`` argument
  to ``execute_or_queue`` or ``rebalance`` — any held name at prior weight will be a no-op trade
  after the no-trade-band filter (the actual trade size = 0 when target == current).

Usage::

    from portfolio.freeze import freeze_to_prior

    # Build prior from paper_account._load_account (weight not directly available — use
    # firm_exposure._load_book to get published weights) or from position_log.open_positions.
    prior_weights: dict[str, float] = ...

    frozen = freeze_to_prior(target_weights, prior_weights)
    # frozen satisfies all three invariants above.
"""
from __future__ import annotations


def freeze_to_prior(
    targets: dict[str, float],
    prior: dict[str, float],
) -> dict[str, float]:
    """Return a frozen target dict that satisfies Charter P2 invariants.

    Parameters
    ----------
    targets : ``{ticker: weight}`` — the built target weights BEFORE the failed clamp.
              Keys are uppercased internally; case of returned keys matches the prior dict.
    prior   : ``{ticker: weight}`` — the book's last-published / currently-held weights.
              Names absent here are treated as weight=0 (not held → new add → drop).

    Returns
    -------
    ``{ticker: weight}`` satisfying:
      - frozen[name] <= prior[name] for every returned name
      - sum(frozen.values()) <= sum(prior.values())  [gross never increases]
      - set(frozen.keys()) <= set(prior.keys())       [no new names]

    Never raises.  Returns an empty dict when both inputs are empty or non-dict.
    """
    try:
        if not isinstance(targets, dict) or not isinstance(prior, dict):
            return {}

        # Normalise prior keys to uppercase for lookup; track original casing for output
        prior_upper: dict[str, float] = {}
        prior_key_map: dict[str, str] = {}  # UPPER -> original-case key
        for k, v in prior.items():
            ku = str(k or "").upper().strip()
            if not ku:
                continue
            try:
                wv = float(v)
            except (TypeError, ValueError):
                wv = 0.0
            if wv < 0:
                wv = 0.0
            prior_upper[ku] = wv
            prior_key_map[ku] = k  # keep original casing

        result: dict[str, float] = {}

        # ── names only in prior → retain at prior weight (absent = liquidate, so omitting = new risk) ──
        targets_upper: set[str] = set()
        for k in targets:
            ku = str(k or "").upper().strip()
            if ku:
                targets_upper.add(ku)

        for ku, pw in prior_upper.items():
            if ku not in targets_upper:
                # prior-only: retain as-is — no trade, no reduction
                result[prior_key_map[ku]] = pw

        # ── names in both → min(target, prior); new adds (targets-only) → drop ──
        for k, v in targets.items():
            ku = str(k or "").upper().strip()
            if not ku:
                continue
            if ku not in prior_upper:
                continue  # new add — DROP (Charter P2: no new names on failure path)
            try:
                tw = float(v)
            except (TypeError, ValueError):
                tw = 0.0
            if tw < 0:
                tw = 0.0
            pw = prior_upper[ku]
            result[prior_key_map[ku]] = min(tw, pw)

        return result
    except Exception:  # noqa: BLE001 — a freeze helper must never raise
        # ultra-conservative fallback: return prior unchanged (no new adds, no increases)
        try:
            return dict(prior)
        except Exception:  # noqa: BLE001
            return {}
