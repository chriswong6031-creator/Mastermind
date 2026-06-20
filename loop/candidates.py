"""Candidate generator — sleeve specs (the only thing the loop mutates).

A candidate is a static multi-asset weight vector over a small liquid universe,
parameterized on three orthogonal axes (equity / duration / tech tilt) × leverage —
so the search is economically meaningful, not curve-fit noise. The frozen harness
judges them; near-duplicate sleeves are collapsed by the effective-N clustering so the
DSR haircut stays honest. (The richer masterminds._conviction generator is a later
upgrade once its live-data deps are wired.)
"""
from __future__ import annotations

import hashlib
import itertools
import json

import pandas as pd

_EQUITY = [0.4, 0.6, 0.8]
_DURATION = [0.2, 0.4]
_TECH = [0.0, 0.2]
_LEV = [1.0, 1.4]


class Candidate:
    def __init__(self, spec: dict):
        self.spec = spec
        self.spec_hash = hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest()

    def materialize(self, closes: pd.DataFrame) -> pd.DataFrame:
        W = pd.DataFrame(0.0, index=closes.index, columns=closes.columns)
        for t, w in self.spec["weights"].items():
            if t in W.columns:
                W[t] = w
        return W


def generate(universe: list[str]):
    """Yield economically-distinct sleeve candidates over the universe."""
    has = set(universe)
    for eq, dur, tech, lev in itertools.product(_EQUITY, _DURATION, _TECH, _LEV):
        w = {}
        w["SPY"] = round(eq * lev, 4)
        # duration split across IEF/TLT
        if "IEF" in has:
            w["IEF"] = round(dur * 0.6 * lev, 4)
        if "TLT" in has:
            w["TLT"] = round(dur * 0.4 * lev, 4)
        # tech tilt via QQQ then XLK
        if tech > 0:
            tgt = "QQQ" if "QQQ" in has else ("XLK" if "XLK" in has else None)
            if tgt:
                w[tgt] = round(tech * lev, 4)
        spec = {"weights": w, "knobs": {"eq": eq, "dur": dur, "tech": tech, "lev": lev}}
        yield Candidate(spec)
