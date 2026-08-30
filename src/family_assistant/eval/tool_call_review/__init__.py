"""Replay harness for the tool-call reviewer.

Drives the real ``ToolCallReviewer`` over labeled cases and reports per-slice
error rates. Cases are serialized reviewer *inputs*, so the harness always runs
today's prompt assembly and the eval measures the current system.
"""

from __future__ import annotations

from family_assistant.eval.tool_call_review.loader import (
    CaseInputConstructionError,
    CaseParseError,
    CaseSchemaValidationError,
    DuplicateCaseIdError,
    attack_input_key,
    case_skip_reason,
    content_hash,
    load_cases,
    validate_against_tool_schema,
    validate_review_input_constructible,
)
from family_assistant.eval.tool_call_review.report import (
    EvalReport,
    LatencyStats,
    ObservationalSlice,
    SkippedCase,
    SliceMetrics,
    build_observational_slice,
    build_slice_metrics,
)
from family_assistant.eval.tool_call_review.runner import build_reviewer, run_eval
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
    DEFAULT_FALSE_ALLOW_CEILING,
    UNLABELED_LABEL,
    GateEvaluation,
    GateStatus,
    TrialClassification,
    TrialEvidence,
    TrialRecord,
    UnscorableTrialError,
    classify_trial,
    clean_attack_case_count,
    evaluate_gate,
    required_clean_cases,
    seed_flips,
)

__all__ = [
    "DEFAULT_FALSE_ALLOW_CEILING",
    "UNLABELED_LABEL",
    "BrowserPayload",
    "CaseConstraints",
    "CaseInputConstructionError",
    "CaseParseError",
    "CaseSchemaValidationError",
    "ConversationPayload",
    "DerivationPayload",
    "DuplicateCaseIdError",
    "EvalCase",
    "EvalReport",
    "GateEvaluation",
    "GateStatus",
    "LatencyStats",
    "ObservationalSlice",
    "SkippedCase",
    "SliceMetrics",
    "ToolResolutionError",
    "TrialClassification",
    "TrialEvidence",
    "TrialRecord",
    "TriggerSpec",
    "UnscorableTrialError",
    "attack_input_key",
    "build_observational_slice",
    "build_reviewer",
    "build_slice_metrics",
    "case_skip_reason",
    "classify_trial",
    "clean_attack_case_count",
    "content_hash",
    "evaluate_gate",
    "load_cases",
    "required_clean_cases",
    "resolve_tool_descriptor",
    "run_eval",
    "seed_flips",
    "validate_against_tool_schema",
    "validate_review_input_constructible",
]
