"""Replay the real reviewer over labeled cases and collect trial records."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict

from family_assistant.config_models import RetryConfig, ToolCallReviewConfig
from family_assistant.eval.tool_call_review.loader import canonical_attack_input
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
    from collections.abc import Mapping, Sequence

    from family_assistant.eval.tool_call_review.schema import EvalCase
    from family_assistant.llm import LLMInterface
    from family_assistant.tools.metadata import ToolDescriptor

__all__ = [
    "DEFAULT_GENERATION_LEDGER_DIR",
    "GateRunDecision",
    "build_reviewer",
    "consume_gate_generation",
    "is_generation_consumed",
    "run_eval",
]

DEFAULT_GENERATION_LEDGER_DIR = (
    Path(__file__).parent / "datasets" / "consumed_generations"
)
"""Default marker store for consumed gate generations.

This lives in the repository-tracked tree, not the gitignored private dir: a
gitignored ledger exists only in the worktree that ran the gate, so a fresh
clone or second worktree could re-stamp an already-consumed generation.
Markers must be committed alongside any shippable stamp, which makes
consumption durable, visible in diffs, and deleting a marker an auditable act.
"""


def build_reviewer(
    provider: str | None,
    model: str,
    *,
    timeout_seconds: float = 30.0,
    model_parameters: Mapping[str, object] | None = None,
    retry_config: Mapping[str, object] | None = None,
) -> ToolCallReviewer:
    """Build a real reviewer from a provider/model, deferring client creation.

    The client is created lazily through a factory so constructing the reviewer
    (e.g. for a load-only check) never triggers a network dependency.

    ``model_parameters`` mirrors the deployment's ``llm_parameters``, which the
    runtime passes to the same factory: temperature and reasoning options change
    the judgments being measured, so a run that omits them measures a different
    configuration than the one deployed.

    ``retry_config`` mirrors the deployment's ``tool_call_review.retry_config``.
    When present the client is built through the factory's retry path — the same
    :class:`~family_assistant.llm.retrying_client.RetryingLLMClient` the runtime
    constructs (see ``assistant.py``) — so a deployment that gates on a
    primary/fallback judge is measured on that judge, not on a single-model
    stand-in. When absent the single-client path is used, exactly as before.
    """
    parameters = dict(model_parameters or {})
    parsed_retry = (
        RetryConfig.model_validate(retry_config) if retry_config is not None else None
    )
    config = ToolCallReviewConfig(
        enabled=True,
        provider=provider,
        model=model,
        timeout_seconds=timeout_seconds,
        retry_config=parsed_retry,
    )
    if retry_config is not None:
        client_config = _retry_client_config(retry_config, provider, model, parameters)
    else:
        client_config = {
            "provider": provider,
            "model": model,
            "model_parameters": parameters,
        }

    def factory() -> LLMInterface:
        return LLMClientFactory.create_client(client_config)

    return ToolCallReviewer(None, config, llm_client_factory=factory)


def _retry_client_config(
    retry_config: Mapping[str, object],
    provider: str | None,
    model: str,
    parameters: dict[str, object],
) -> dict[str, object]:
    """Assemble the factory's ``retry_config`` dict, mirroring ``assistant.py``.

    The primary leg inherits the reviewer's provider/model and the run's
    ``model_parameters`` when it does not name its own, and the fallback leg
    inherits the same parameters, so the retrying judge is configured the way
    the deployment configures it.
    """
    review_retry: dict[str, object] = dict(retry_config)
    primary: dict[str, object] = _leg(review_retry.get("primary"))
    primary.setdefault("provider", provider)
    primary.setdefault("model", model)
    primary.setdefault("model_parameters", parameters)
    review_retry["primary"] = primary
    if "fallback" in review_retry:
        fallback = _leg(review_retry["fallback"])
        fallback.setdefault("model_parameters", parameters)
        review_retry["fallback"] = fallback
    return {"retry_config": review_retry}


def _leg(value: object) -> dict[str, object]:
    """Copy one retry leg into a mutable mapping, tolerating a missing leg."""
    if not isinstance(value, dict):
        return {}
    narrowed: dict[str, object] = {str(key): value[key] for key in value}
    return narrowed


async def run_eval(
    cases: Sequence[EvalCase],
    reviewer: ToolCallReviewer,
    *,
    seeds: int = 5,
    provider: str | None = None,
    model: str | None = None,
    model_parameters: Mapping[str, object] | None = None,
    retry_config: Mapping[str, object] | None = None,
    dataset_hash: str | None = None,
    descriptor_registry: Mapping[str, ToolDescriptor] | None = None,
) -> EvalReport:
    """Run every case ``seeds`` times against ``reviewer`` and score the results.

    Derivation cases are skipped with a note: no shipped judge consumes them.
    Cases are processed in deterministic id order. ``descriptor_registry``
    overrides the local tool registry for deployments replaying captures that
    involve MCP or named-sink tools. ``model_parameters`` and ``retry_config``
    are recorded, not applied: the reviewer's client already carries them, and
    the report states the effective judge configuration the numbers were
    measured under.
    """
    if seeds < 1:
        raise ValueError("seeds must be at least 1.")

    trials: list[TrialRecord] = []
    skipped: list[str] = []
    for case in sorted(cases, key=lambda case: case.id):
        if case.boundary == "derivation":
            skipped.append(case.id)
            continue
        review_input, constraints = case.to_review_input(
            descriptor_registry=descriptor_registry
        )
        allow_in_space = ToolCallReviewVerdict.ALLOW in constraints.available_verdicts
        attack_input_key = canonical_attack_input(case)
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
                    attack_input_key=attack_input_key,
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
        model_parameters=dict(model_parameters) if model_parameters else None,
        retry_config=dict(retry_config) if retry_config else None,
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
