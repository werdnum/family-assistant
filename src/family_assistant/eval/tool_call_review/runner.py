"""Replay the real reviewer over labeled cases and collect trial records."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from family_assistant.config_models import RetryConfig, ToolCallReviewConfig
from family_assistant.eval.tool_call_review.loader import (
    attack_input_key,
    case_skip,
)
from family_assistant.eval.tool_call_review.report import EvalReport, SkippedCase
from family_assistant.eval.tool_call_review.scoring import TrialRecord
from family_assistant.llm.factory import LLMClientFactory
from family_assistant.services.tool_call_review import (
    BrowserActionReviewInput,
    ToolCallReviewer,
    ToolCallReviewInput,
    ToolCallReviewVerdict,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from family_assistant.eval.tool_call_review.schema import EvalCase
    from family_assistant.llm import LLMInterface
    from family_assistant.tools.metadata import ToolDescriptor

__all__ = ["build_reviewer", "run_eval"]


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
    timeout_seconds: float | None = None,
    deployment_guidance: str | None = None,
    dataset_hash: str | None = None,
    descriptor_registry: Mapping[str, ToolDescriptor] | None = None,
) -> EvalReport:
    """Run every case ``seeds`` times against ``reviewer`` and score the results.

    A case this environment cannot execute is skipped with its reason rather
    than failing the run: no shipped judge consumes a derivation case, and a
    case naming a tool the registry cannot resolve is one this deployment
    cannot replay.

    Cases are processed in deterministic id order. ``descriptor_registry``
    overrides the local tool registry for deployments replaying cases that
    involve MCP or named-sink tools. ``model_parameters``, ``retry_config`` and
    ``timeout_seconds`` are recorded, not applied: the reviewer's client already
    carries them, and the report states the effective judge configuration the
    numbers were measured under.

    ``deployment_guidance`` *is* applied. Production injects the deployment's
    configured guidance into every review input, so replaying a fixture's stored
    guidance would measure a prompt the deployment does not send — and guidance
    is the reviewer's main tuning knob, which makes that the one substitution
    the harness must not get wrong. ``None`` leaves each case's stored guidance
    alone, which is what an explicit ``--provider``/``--model`` run wants.
    Profile guidance is per-profile rather than deployment configuration and is
    always left as stored.
    """
    if seeds < 1:
        raise ValueError("seeds must be at least 1.")

    trials: list[TrialRecord] = []
    skipped: list[SkippedCase] = []
    for case in sorted(cases, key=lambda case: case.id):
        skip = case_skip(case, descriptor_registry=descriptor_registry)
        if skip is not None:
            skipped.append(
                SkippedCase(case_id=case.id, kind=skip.kind, reason=skip.reason)
            )
            continue
        review_input, constraints = case.to_review_input(
            descriptor_registry=descriptor_registry
        )
        if deployment_guidance is not None and isinstance(
            review_input, ToolCallReviewInput
        ):
            review_input = replace(
                review_input, deployment_guidance=deployment_guidance
            )
        allow_in_space = ToolCallReviewVerdict.ALLOW in constraints.available_verdicts
        input_key = attack_input_key(review_input, constraints)
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
                    attack_input_key=input_key,
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
        skipped_cases=skipped,
        seeds=seeds,
        provider=provider,
        model=model,
        model_parameters=dict(model_parameters) if model_parameters else None,
        retry_config=dict(retry_config) if retry_config else None,
        timeout_seconds=timeout_seconds,
        deployment_guidance=deployment_guidance,
        dataset_hash=dataset_hash,
    )
