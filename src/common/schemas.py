"""A4 result object and trace event shapes (Appendix A, section A4/A5).

RESULT_FIELDS is the exact key order of the appendix's example result object.
"""

from __future__ import annotations

CONFIG_ID = "agent"
TRIALS = (1, 2, 3)

RESULT_FIELDS = (
    "run_id", "config_id", "challenge_id", "trial", "status", "verified",
    "elapsed_s", "input_tokens", "output_tokens", "cost_usd",
    "trace_file", "reset_id", "sample_type",
)

SUCCESS_STATUS = "solved"   # confirm against the kit's ctf_single_agent_summary.py
FAILURE_STATUSES = frozenset({"no_flag", "wrong_flag", "timeout", "budget_exhausted", "tool_error"})
INCIDENT_STATUSES = frozenset({"infrastructure_error", "scope_violation"})  # retained; challenge incomplete
STATUSES = frozenset({SUCCESS_STATUS}) | FAILURE_STATUSES | INCIDENT_STATUSES

SAMPLE_TYPES = frozenset({"live", "synthetic"})
NULLABLE_COUNTERS = ("input_tokens", "output_tokens", "cost_usd")

# Trace events (one JSON object per line in traces/<run_id>.jsonl).
TRACE_EVENTS = frozenset({
    "attempt_started",      # config snapshot, budget, reset_id, export inventory
    "target_started", "target_stopped", "isolation_check",
    "model_request",        # model-visible messages
    "model_response",       # content, tool proposals, usage, latency
    "tool_proposal",        # tool name + arguments as proposed
    "tool_result",          # output (capped), error, latency, exit status
    "truncation",           # original length, kept head/tail
    "submission",           # candidate flag text
    "verification",         # {"verified": bool}
    "budget_stop",          # which cap was hit
    "error",
    "attempt_ended",        # stopping reason + terminal status
})


class SchemaError(ValueError):
    """A result record does not match the A4 schema."""


def validate_result(rec: dict) -> dict:
    """Raise SchemaError unless rec is a well-formed A4 result object. Returns rec."""
    if tuple(rec) != RESULT_FIELDS:
        missing = [k for k in RESULT_FIELDS if k not in rec]
        extra = [k for k in rec if k not in RESULT_FIELDS]
        raise SchemaError(f"keys must be exactly {RESULT_FIELDS} in order; missing={missing} extra={extra}")
    if rec["config_id"] != CONFIG_ID:
        raise SchemaError(f"config_id must be {CONFIG_ID!r}")
    if rec["trial"] not in TRIALS or isinstance(rec["trial"], bool):
        raise SchemaError(f"trial must be one of {TRIALS}")
    if rec["status"] not in STATUSES:
        raise SchemaError(f"unknown status {rec['status']!r}")
    if not isinstance(rec["verified"], bool):
        raise SchemaError("verified must be a boolean")
    if rec["verified"] != (rec["status"] == SUCCESS_STATUS):
        raise SchemaError("verified must be true exactly when status is 'solved'")
    if rec["sample_type"] not in SAMPLE_TYPES:
        raise SchemaError(f"sample_type must be one of {sorted(SAMPLE_TYPES)}")
    if not isinstance(rec["elapsed_s"], (int, float)) or isinstance(rec["elapsed_s"], bool) or rec["elapsed_s"] < 0:
        raise SchemaError("elapsed_s must be a non-negative number")
    for k in ("input_tokens", "output_tokens"):
        v = rec[k]
        if v is not None and (not isinstance(v, int) or isinstance(v, bool) or v < 0):
            raise SchemaError(f"{k} must be a non-negative int or null")
    if rec["cost_usd"] is not None and (not isinstance(rec["cost_usd"], (int, float)) or rec["cost_usd"] < 0):
        raise SchemaError("cost_usd must be a non-negative number or null")
    for k in ("run_id", "challenge_id", "trace_file", "reset_id"):
        if not isinstance(rec[k], str) or not rec[k]:
            raise SchemaError(f"{k} must be a non-empty string")
    return rec


def make_result(*, run_id: str, challenge_id: str, trial: int, status: str, elapsed_s: float,
                input_tokens: int | None, output_tokens: int | None, cost_usd: float | None,
                trace_file: str, reset_id: str, sample_type: str) -> dict:
    """Build an A4 result object in the appendix's key order. verified is derived from status."""
    rec = {
        "run_id": run_id,
        "config_id": CONFIG_ID, "challenge_id": challenge_id,
        "trial": trial, "status": status, "verified": status == SUCCESS_STATUS,
        "elapsed_s": round(elapsed_s, 3), "input_tokens": input_tokens,
        "output_tokens": output_tokens, "cost_usd": cost_usd,
        "trace_file": trace_file,
        "reset_id": reset_id, "sample_type": sample_type,
    }
    return validate_result(rec)
