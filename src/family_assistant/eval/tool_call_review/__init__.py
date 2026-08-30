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
    CaseSkip,
    DuplicateCaseIdError,
    SkipKind,
    attack_input_key,
    case_skip,
    content_hash,
    load_cases,
    validate_against_tool_schema,
    validate_review_input_constructible,
)
from family_assistant.eval.tool_call_review.report import (
    EvalReport,
    LatencyStats,
    SkippedCase,
    SliceMetrics,
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
    GateEvaluation,
    GateStatus,
    SeedFlip,
    TrialClassification,
    TrialEvidence,
    TrialRecord,
    classify_trial,
    clean_attack_case_count,
    evaluate_gate,
    required_clean_cases,
    seed_flip_case_ids,
    seed_flips,
)

__all__ = [
    "DEFAULT_FALSE_ALLOW_CEILING",
    "BrowserPayload",
    "CaseConstraints",
    "CaseInputConstructionError",
    "CaseParseError",
    "CaseSchemaValidationError",
    "CaseSkip",
    "ConversationPayload",
    "DerivationPayload",
    "DuplicateCaseIdError",
    "EvalCase",
    "EvalReport",
    "GateEvaluation",
    "GateStatus",
    "LatencyStats",
    "SeedFlip",
    "SkipKind",
    "SkippedCase",
    "SliceMetrics",
    "ToolResolutionError",
    "TrialClassification",
    "TrialEvidence",
    "TrialRecord",
    "TriggerSpec",
    "attack_input_key",
    "build_reviewer",
    "build_slice_metrics",
    "case_skip",
    "classify_trial",
    "clean_attack_case_count",
    "content_hash",
    "evaluate_gate",
    "load_cases",
    "required_clean_cases",
    "resolve_tool_descriptor",
    "run_eval",
    "seed_flip_case_ids",
    "seed_flips",
    "validate_against_tool_schema",
    "validate_review_input_constructible",
]
