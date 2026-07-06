"""control_plane — job governance substrate for Mastermind (MW1).

Modules
-------
run_events   Append-only event log at data/governance/run_events.jsonl.
guardrail    R3 severity taxonomy + GuardrailResult reporting dataclass.
locks        Advisory file locks (fcntl.flock, non-blocking) per book/op.
run_ledger   Job-level run records (extends brain/runlog scope — see docstring).
flags        Enumerate every MASTERMIND_* env var currently set.

Zero integration into existing call sites this wave (substrate-only).
No third-party dependencies; stdlib only.
"""
