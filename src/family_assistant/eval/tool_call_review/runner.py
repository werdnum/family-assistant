"""Replay the real reviewer over labeled cases and collect trial records."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict

from family_assistant.config_models import ToolCallReviewConfig
from family_assistant.eval.tool_call_review.report import EvalReport
from family_assistant.eval.tool_call_review.scoring import (
    GateEvaluation,
    GateStatus,
    TrialRecord,
)
from family_assistant.llm.factory import LLMClientFactory
from family_assistant.services.tool_call_review import (
    BrowserActionReviewInput,
    ToolCallReviewer,
    ToolCallReviewVerdict,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from family_assistant.eval.tool_call_review.schema import EvalCase
    from family_assistant.llm import LLMInterface

__all__ = [
    "DEFAULT_GENERATION_LEDGER_DIR",
    "GateRunDecision",
    "build_reviewer",
    "consume_gate_generation",
    "is_generation_consumed",
    "run_eval",
]

DEFAULT_GENERATION_LEDGER_DIR = Path(".review-eval-local/consumed_generations")
"""Default marker store for consumed gate generations, under the private dir."""


def build_reviewer(
    provider: str | None,
    model: str,
    *,
    timeout_seconds: float = 30.0,
) -> ToolCallReviewer:
    """Build a real reviewer from a provider/model, deferring client creation.

    The client is created lazily through a factory so constructing the reviewer
    (e.g. for a load-only check) never triggers a network dependency.
    """
    config = ToolCallReviewConfig(
        enabled=True,
        provider=provider,
        model=model,
        timeout_seconds=timeout_seconds,
    )

    def factory() -> LLMInterface:
        return LLMClientFactory.create_client({
            "provider": provider,
            "model": model,
            "model_parameters": {},
        })

    return ToolCallReviewer(None, config, llm_client_factory=factory)


async def run_eval(
    cases: Sequence[EvalCase],
    reviewer: ToolCallReviewer,
    *,
    seeds: int = 5,
    provider: str | None = None,
    model: str | None = None,
    dataset_hash: str | None = None,
) -> EvalReport:
    """Run every case ``seeds`` times against ``reviewer`` and score the results.

    Derivation cases are skipped with a note: no shipped judge consumes them.
    Cases are processed in deterministic id order.
    """
    if seeds < 1:
        raise ValueError("seeds must be at least 1.")

    trials: list[TrialRecord] = []
    skipped: list[str] = []
    for case in sorted(cases, key=lambda case: case.id):
        if case.boundary == "derivation":
            skipped.append(case.id)
            continue
        review_input, constraints = case.to_review_input()
        allow_in_space = ToolCallReviewVerdict.ALLOW in constraints.available_verdicts
        for seed_index in range(seeds):
            if isinstance(review_input, BrowserActionReviewInput):
                result = await reviewer.review_browser_action(review_input, constraints)
            else:
                result = await reviewer.review_tool_call(review_input, constraints)
            trials.append(
                TrialRecord(
                    case_id=case.id,
                    boundary=case.boundary,
                    label=case.label,
                    source=case.source,
                    attack_class=case.attack_class,
                    expected_verdict=case.expected_verdict,
                    seed_index=seed_index,
                    verdict=result.verdict,
                    status=result.status,
                    used_fallback=result.used_fallback,
                    latency_ms=result.latency_ms,
                    reason=result.reason,
                    allow_in_space=allow_in_space,
                )
            )

    return EvalReport(
        trials=trials,
        skipped_case_ids=skipped,
        seeds=seeds,
        provider=provider,
        model=model,
        dataset_hash=dataset_hash,
    )


class GateRunDecision(BaseModel):
    """Whether a gate run may emit a shippable stamp for its generation.

    A gate generation is single-use: consulting it once (reading its verdicts to
    diagnose a pass or a failure) makes any re-gate of the same generation
    iterative overfitting with a security stamp on it. The harness enforces this
    rather than leaving it to discipline -- a second gate run over a generation
    already recorded in the ledger is refused a shippable stamp and downgraded to
    a dev-only run.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    shippable: bool
    already_consumed: bool
    gate_status: GateStatus
    reason: str
    marker_path: str


def _generation_ledger_path(generation_hash: str, ledger_dir: Path) -> Path:
    return ledger_dir / f"{generation_hash}.json"


def is_generation_consumed(
    generation_hash: str,
    *,
    ledger_dir: Path = DEFAULT_GENERATION_LEDGER_DIR,
) -> bool:
    """Whether a gate generation has already been consumed by a prior gate run."""
    return _generation_ledger_path(generation_hash, ledger_dir).exists()


def consume_gate_generation(
    generation_hash: str,
    gate: GateEvaluation,
    *,
    ledger_dir: Path = DEFAULT_GENERATION_LEDGER_DIR,
    now: datetime | None = None,
) -> GateRunDecision:
    """Record a gate run and decide whether it may emit a shippable stamp.

    The first gate run over a generation records a durable marker and is
    shippable only when the gate itself passed. A subsequent run over the same
    generation is refused a shippable stamp regardless of its verdicts and is
    reported as a dev-only run, leaving the original marker untouched.
    """
    marker_path = _generation_ledger_path(generation_hash, ledger_dir)
    if marker_path.exists():
        return GateRunDecision(
            shippable=False,
            already_consumed=True,
            gate_status=gate.status,
            reason=(
                "Gate generation was already consumed by a prior gate run; this run "
                "is dev-only and cannot emit a shippable stamp. Gate the next judge "
                "against reserved, never-consulted material."
            ),
            marker_path=str(marker_path),
        )

    timestamp = (now or datetime.now(UTC)).isoformat()
    marker_path.parent.mkdir(parents=True, exist_ok=True)
    marker_path.write_text(
        json.dumps(
            {
                "generation_hash": generation_hash,
                "consumed_at": timestamp,
                "gate_status": gate.status.value,
                "gate_reason": gate.reason,
                "ceiling": gate.ceiling,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    shippable = gate.status is GateStatus.PASS
    reason = "First consumption of this gate generation; " + (
        "shippable pass recorded." if shippable else f"gate {gate.status.value}."
    )
    return GateRunDecision(
        shippable=shippable,
        already_consumed=False,
        gate_status=gate.status,
        reason=reason,
        marker_path=str(marker_path),
    )
