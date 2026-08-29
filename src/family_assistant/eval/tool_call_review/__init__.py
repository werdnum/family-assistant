"""Replay harness for the tool-call reviewer.

Drives the real ``ToolCallReviewer`` over labeled cases and reports per-slice
error rates. Cases are serialized reviewer *inputs*, so the harness always runs
today's prompt assembly and the eval measures the current system.
"""

from __future__ import annotations

from family_assistant.eval.tool_call_review.loader import (
    CaseSchemaValidationError,
    DuplicateCaseIdError,
    content_hash,
    load_cases,
    validate_against_tool_schema,
)
from family_assistant.eval.tool_call_review.report import (
    EvalReport,
    LatencyStats,
    SliceMetrics,
    build_slice_metrics,
)
from family_assistant.eval.tool_call_review.runner import (
    DEFAULT_GENERATION_LEDGER_DIR,
    GateRunDecision,
    build_reviewer,
    consume_gate_generation,
    is_generation_consumed,
    run_eval,
)
from family_assistant.eval.tool_call_review.schema import (
    BrowserPayload,
    CaseConstraints,
    ConversationPayload,
    DerivationPayload,
    EvalCase,
    ToolResolutionError,
    TriggerSpec,
    resolve_tool_descriptor,
)
from family_assistant.eval.tool_call_review.scoring import (
    GateEvaluation,
    GateStatus,
    TrialClassification,
    TrialRecord,
    classify_trial,
    evaluate_gate,
    required_clean_trials,
    seed_flips,
)

__all__ = [
    "DEFAULT_GENERATION_LEDGER_DIR",
    "BrowserPayload",
    "CaseConstraints",
    "CaseSchemaValidationError",
    "ConversationPayload",
    "DerivationPayload",
    "DuplicateCaseIdError",
    "EvalCase",
    "EvalReport",
    "GateEvaluation",
    "GateRunDecision",
    "GateStatus",
    "LatencyStats",
    "SliceMetrics",
    "ToolResolutionError",
    "TrialClassification",
    "TrialRecord",
    "TriggerSpec",
    "build_reviewer",
    "build_slice_metrics",
    "classify_trial",
    "consume_gate_generation",
    "content_hash",
    "evaluate_gate",
    "is_generation_consumed",
    "load_cases",
    "required_clean_trials",
    "resolve_tool_descriptor",
    "run_eval",
    "seed_flips",
    "validate_against_tool_schema",
]
