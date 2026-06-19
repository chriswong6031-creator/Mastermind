"""Phase 1 acceptance — the one-name paper thesis runs end-to-end on real data."""
import bot  # noqa: F401

from bot import phase1


def test_phase1_end_to_end():
    out = phase1.run()
    d = out["decision"]

    # a valid falsifiable decision was produced
    assert d["schema"] == "brain_decision.v1"
    assert d["lean"] == "add" and d["subject"] == "NVDA"
    assert 0.50 <= d["prob_correct"] <= 0.85
    assert d["falsifier"]["check"]["kind"] == "rel_return"   # ENGINE-derived, not model-authored
    assert d["check_by"] and d["time_stop_by"]               # both stops set

    # the confluence gate is honest: RS+regime confirmed, the new-leaf dims unverified
    g = out["gate"]
    assert g["size"] in ("none", "initial", "full")
    assert "rs" in g["confirmed"] and "catalyst" in g["unverified"]
    # doctrine discipline: no full size without the catalyst gate
    assert not (g["size"] == "full" and not g["catalyst_present"])

    # bottleneck baton-pass produced an order layer
    assert out["bottleneck"]["leading_order"] in (1, 2, 3)

    # paper-only / building track record until a thesis resolves
    assert out["track_record"]["status"] in ("building", "scoring")
