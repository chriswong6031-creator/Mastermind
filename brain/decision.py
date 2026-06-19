"""The falsifiable decision object (brain_decision.v1) + the ENGINE-derived falsifier.

Single source of truth for the decision schema. The model may author the thesis/lean,
but the FALSIFIER PREDICATE is derived here by the engine (mirrors ai_desk._derive_check) —
the model never writes its own escape hatch. This is also the doctrine's
"thesis-invalidation stop" (DOCTRINE.md §exit).
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from datetime import date, timedelta


def _business_days_out(start: date, n: int) -> date:
    d, added = start, 0
    while added < n:
        d += timedelta(days=1)
        if d.weekday() < 5:
            added += 1
    return d


def derive_check(subject_ticker: str, lean: str, horizon_d: int, *, vs: str = "SPY",
                 threshold: float = 0.05) -> dict:
    """Engine-derived falsifier predicate. A bullish lean is FALSE if the name
    underperforms the benchmark by >= threshold over the horizon (and vice-versa)."""
    bullish = lean in ("add", "overweight", "accumulate", "constructive")
    op, thr = ("<", -abs(threshold)) if bullish else (">", abs(threshold))
    return {
        "kind": "rel_return", "subject_ticker": subject_ticker, "vs": vs,
        "op": op, "threshold": thr, "horizon_d": horizon_d,
    }


@dataclass
class DecisionDoc:
    id: str
    subject: str
    lean: str
    conviction: str
    prob_correct: float          # [0.50, 0.85] after de-confidencing
    horizon_d: int
    thesis: str
    state_asof: str
    entry_levels: dict = field(default_factory=dict)
    evidence: list = field(default_factory=list)
    dissent: str = ""
    # doctrine fields
    sleeve: str = "conviction"
    stage: int | None = None
    order_layer: int | None = None
    scorecard_dims: dict = field(default_factory=dict)  # {dim: confirmed|absent|unverified}
    bottleneck: dict = field(default_factory=dict)
    time_stop_by: str | None = None
    schema: str = "brain_decision.v1"
    is_context_only: bool = True
    falsifier: dict = field(default_factory=dict)
    check_by: str = ""

    def finalize(self) -> "DecisionDoc":
        d0 = date.fromisoformat(self.state_asof)
        self.prob_correct = max(0.50, min(0.85, round(self.prob_correct, 2)))
        self.horizon_d = max(5, min(60, self.horizon_d))
        if not self.falsifier:
            sub = self.entry_levels.get("ticker", self.subject)
            self.falsifier = {"check": derive_check(sub, self.lean, self.horizon_d),
                              "text": f"FALSE if {sub} underperforms SPY by >=5% over {self.horizon_d} business days"}
        if not self.check_by:
            self.check_by = _business_days_out(d0, self.horizon_d).isoformat()
        if not self.time_stop_by:
            self.time_stop_by = _business_days_out(d0, 63).isoformat()
        return self

    def to_json(self) -> dict:
        return dataclasses.asdict(self)
