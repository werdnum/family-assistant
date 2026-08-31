"""Private, resumable OpenRouter-compatible batch evaluation."""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import json
import os
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from family_assistant.eval.private_paths import resolve_private_eval_path
from family_assistant.eval.tool_call_review.loader import (
    attack_input_key,
    case_skip,
    content_hash,
    load_cases,
)
from family_assistant.eval.tool_call_review.registry_snapshot import registry_digest
from family_assistant.eval.tool_call_review.report import EvalReport, SkippedCase
from family_assistant.eval.tool_call_review.schema import EvalCase
from family_assistant.eval.tool_call_review.scoring import TrialRecord
from family_assistant.llm.providers.openai_client import OpenAIClient
from family_assistant.services.tool_call_review import (
    BrowserActionReviewInput,
    ToolCallReviewResponse,
    ToolCallReviewVerdict,
    assemble_browser_action_review_messages,
    assemble_tool_call_review_messages,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from pathlib import Path

    from family_assistant.tools.metadata import ToolDescriptor

__all__ = [
    "BATCH_SCHEMA_VERSION",
    "BatchError",
    "BatchManifest",
    "harvest_batch",
    "prepare_batch",
    "submit_batch",
    "update_batch_status",
]

BATCH_SCHEMA_VERSION = "review_eval_batch_v1"
DEFAULT_BATCH_SIZE = 500
DEFAULT_MAX_TOKENS = 512
DEFAULT_API_BASE_URL = "https://openrouter.ai/api"
_TERMINAL = frozenset({"completed", "failed", "expired", "cancelled"})


class BatchError(RuntimeError):
    """A batch artifact or remote response cannot be reconciled safely."""


class BatchChunk(BaseModel):
    model_config = ConfigDict(extra="forbid")

    index: int = Field(ge=0)
    request_ids: list[str]
    status: str = "pending"
    batch_id: str | None = None
    result_file: str | None = None
    usage: dict[str, object] | None = None
    error_code: str | None = None


class BatchManifest(BaseModel):
    """Private, resumable state for one complete request set."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str = BATCH_SCHEMA_VERSION
    run_id: str
    provider: str
    model: str
    api_base_url: str
    dataset_hash: str
    registry_hash: str | None = None
    seeds: int = Field(ge=1)
    batch_size: int = Field(ge=1)
    max_tokens: int = Field(default=DEFAULT_MAX_TOKENS, ge=1)
    request_count: int = Field(ge=0)
    request_file: str
    skipped_cases: list[SkippedCase] = Field(default_factory=list)
    chunks: list[BatchChunk] = Field(default_factory=list)
    approved_spend_usd: float | None = Field(default=None, gt=0)
    spend_approved: bool = False
    request_sha256: str
    observed_cost_usd: float | None = Field(default=None, ge=0)
    created_at: str
    report_file: str | None = None


class _PreparedRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    custom_id: str
    case_id: str
    seed_index: int = Field(ge=0)
    case: dict[str, object]
    body: dict[str, object]
    attack_input_key: str
    allow_in_space: bool


def _json_bytes(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    ).encode()


def _write_json(path: Path, value: object) -> None:
    path = resolve_private_eval_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_bytes(_json_bytes(value))
    temporary.replace(path)


def _write_jsonl(path: Path, values: Sequence[BaseModel]) -> None:
    path = resolve_private_eval_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        "".join(
            json.dumps(value.model_dump(mode="json"), sort_keys=True) + "\n"
            for value in values
        ),
        encoding="utf-8",
    )
    temporary.replace(path)


def _read_manifest(run_dir: str | Path) -> tuple[Path, BatchManifest]:
    directory = resolve_private_eval_path(run_dir)
    path = directory / "manifest.json"
    try:
        manifest = BatchManifest.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValidationError, json.JSONDecodeError) as exc:
        raise BatchError("Cannot load a valid private batch manifest.") from exc
    if manifest.schema_version != BATCH_SCHEMA_VERSION:
        raise BatchError("Unsupported batch manifest schema version.")
    if manifest.run_id != directory.name:
        raise BatchError("Batch manifest run_id does not match its private directory.")
    if (
        manifest.provider != "openrouter"
        or manifest.api_base_url != DEFAULT_API_BASE_URL
    ):
        raise BatchError("Batch manifest must use the fixed OpenRouter endpoint.")
    return directory, manifest


def _load_requests(directory: Path, manifest: BatchManifest) -> list[_PreparedRequest]:
    path = resolve_private_eval_path(directory / manifest.request_file)
    if path.parent != directory:
        raise BatchError(
            "The request artifact must be directly inside the run directory."
        )
    try:
        requests = [
            _PreparedRequest.model_validate_json(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line
        ]
    except (OSError, ValidationError, json.JSONDecodeError) as exc:
        raise BatchError(
            "The private request artifact is malformed or unavailable."
        ) from exc
    try:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise BatchError(
            "The private request artifact is malformed or unavailable."
        ) from exc
    if digest != manifest.request_sha256:
        raise BatchError(
            "The private request artifact digest does not match the manifest."
        )
    if len(requests) != manifest.request_count:
        raise BatchError("Request count does not match the prepared manifest.")
    ids = [request.custom_id for request in requests]
    expected = [
        request_id for chunk in manifest.chunks for request_id in chunk.request_ids
    ]
    if ids != sorted(ids) or ids != expected or len(set(ids)) != len(ids):
        raise BatchError("Prepared request ids do not exactly match manifest chunks.")
    return requests


def _response_format() -> dict[str, object]:
    strict_json_schema = importlib.import_module(
        "openai.lib._pydantic"
    ).to_strict_json_schema
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "tool_call_review_response",
            "strict": True,
            "schema": strict_json_schema(ToolCallReviewResponse),
        },
    }


def _custom_id(case_id: str, seed_index: int) -> str:
    digest = hashlib.sha256(f"{case_id}\0{seed_index}".encode()).hexdigest()[:16]
    return f"review-{digest}"


def _build_request_body(
    case: EvalCase,
    *,
    model: str,
    max_tokens: int,
    descriptor_registry: Mapping[str, ToolDescriptor] | None,
) -> tuple[dict[str, object], str, bool]:
    review_input, constraints = case.to_review_input(
        descriptor_registry=descriptor_registry
    )
    if isinstance(review_input, BrowserActionReviewInput):
        messages = assemble_browser_action_review_messages(review_input, constraints)
    else:
        messages = assemble_tool_call_review_messages(review_input, constraints)
    body = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [
            OpenAIClient._to_chat_completions_message(message) for message in messages
        ],
        "response_format": _response_format(),
        "provider": {"require_parameters": True},
    }
    return (
        body,
        attack_input_key(review_input, constraints),
        ToolCallReviewVerdict.ALLOW in constraints.available_verdicts,
    )


def prepare_batch(
    datasets: Sequence[str | Path],
    run_dir: str | Path,
    *,
    model: str = "google/gemini-3.7-flash",
    seeds: int = 1,
    batch_size: int = DEFAULT_BATCH_SIZE,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    descriptor_registry: Mapping[str, ToolDescriptor] | None = None,
) -> BatchManifest:
    """Prepare deterministic private requests without contacting a provider."""
    if seeds < 1 or batch_size < 1 or max_tokens < 1:
        raise BatchError("seeds, batch_size, and max_tokens must be positive.")
    directory = resolve_private_eval_path(run_dir)
    if directory.exists():
        raise BatchError(f"Refusing to reuse an existing batch directory: {directory}")
    cases = load_cases(datasets, descriptor_registry=descriptor_registry)
    requests: list[_PreparedRequest] = []
    skipped: list[SkippedCase] = []
    for case in cases:
        skip = case_skip(case, descriptor_registry=descriptor_registry)
        if skip is not None:
            skipped.append(
                SkippedCase(case_id=case.id, kind=skip.kind, reason=skip.reason)
            )
            continue
        body, input_key, allow_in_space = _build_request_body(
            case,
            model=model,
            max_tokens=max_tokens,
            descriptor_registry=descriptor_registry,
        )
        for seed_index in range(seeds):
            requests.append(
                _PreparedRequest(
                    custom_id=_custom_id(case.id, seed_index),
                    case_id=case.id,
                    seed_index=seed_index,
                    case=case.model_dump(mode="json"),
                    body=body,
                    attack_input_key=input_key,
                    allow_in_space=allow_in_space,
                )
            )
    requests.sort(key=lambda request: request.custom_id)
    if not requests:
        raise BatchError(
            "No runnable cases were found; no batch artifacts were created."
        )
    chunks = [
        BatchChunk(
            index=index,
            request_ids=[
                request.custom_id for request in requests[start : start + batch_size]
            ],
        )
        for index, start in enumerate(range(0, len(requests), batch_size))
    ]
    directory.mkdir(parents=True)
    request_path = directory / "requests.jsonl"
    _write_jsonl(request_path, requests)
    request_sha256 = hashlib.sha256(request_path.read_bytes()).hexdigest()
    manifest = BatchManifest(
        run_id=directory.name,
        provider="openrouter",
        model=model,
        api_base_url=DEFAULT_API_BASE_URL,
        dataset_hash=content_hash(cases),
        registry_hash=registry_digest(descriptor_registry),
        seeds=seeds,
        batch_size=batch_size,
        max_tokens=max_tokens,
        request_count=len(requests),
        request_file="requests.jsonl",
        request_sha256=request_sha256,
        skipped_cases=skipped,
        chunks=chunks,
        created_at=datetime.now(UTC).isoformat(),
    )
    _write_json(directory / "manifest.json", manifest.model_dump(mode="json"))
    return manifest


class BatchClient:
    """Minimal HTTP client for the provider's beta batch endpoint."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._client = client or httpx.AsyncClient(timeout=60.0)
        self._owns_client = client is None

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def request(
        self, method: str, path: str, payload: dict[str, object] | None = None
    ) -> dict[str, object]:
        response = await self._client.request(
            method,
            f"{self._base_url}{path}",
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
        )
        if not 200 <= response.status_code < 300:
            raise BatchError(f"Batch API returned HTTP {response.status_code}.")
        try:
            value = response.json()
        except ValueError as exc:
            raise BatchError("Batch API returned a non-JSON response.") from exc
        if not isinstance(value, dict):
            raise BatchError("Batch API returned a non-object response.")
        return value


async def submit_batch(
    run_dir: str | Path,
    *,
    approved_spend_usd: float,
    approve_spend: bool,
    api_key: str | None = None,
    client: BatchClient | None = None,
) -> BatchManifest:
    """Submit pending chunks after explicit operator spend approval."""
    if approved_spend_usd <= 0 or not approve_spend:
        raise BatchError(
            "submit requires a positive --approved-spend-usd and --approve-spend."
        )
    directory, manifest = _read_manifest(run_dir)
    requests = _load_requests(directory, manifest)
    request_by_id = {request.custom_id: request for request in requests}
    key = api_key or os.environ.get("OPENROUTER_API_KEY")
    if not key:
        raise BatchError("OPENROUTER_API_KEY is required for submit.")
    active = client or BatchClient(manifest.api_base_url, key)
    try:
        manifest.approved_spend_usd = approved_spend_usd
        manifest.spend_approved = True
        _write_json(directory / "manifest.json", manifest.model_dump(mode="json"))
        for chunk in manifest.chunks:
            if chunk.status != "pending":
                continue
            payload = {
                "endpoint": "/v1/chat/completions",
                "model": manifest.model,
                "requests": [
                    {"custom_id": request_id, "body": request_by_id[request_id].body}
                    for request_id in chunk.request_ids
                ],
            }
            try:
                result = await active.request("POST", "/beta/batches", payload)
            except asyncio.CancelledError:
                chunk.status = "submission_unknown"
                chunk.error_code = "submission_unknown"
                _write_json(
                    directory / "manifest.json", manifest.model_dump(mode="json")
                )
                raise
            except Exception as exc:
                chunk.status = "submission_unknown"
                chunk.error_code = "submission_unknown"
                _write_json(
                    directory / "manifest.json", manifest.model_dump(mode="json")
                )
                raise BatchError(
                    "Batch submission outcome is unknown; inspect the provider before retrying."
                ) from exc
            batch_id = result.get("id")
            if not isinstance(batch_id, str) or not batch_id:
                chunk.status = "submission_unknown"
                chunk.error_code = "missing_batch_id"
                _write_json(
                    directory / "manifest.json", manifest.model_dump(mode="json")
                )
                raise BatchError("Batch API response did not contain a batch id.")
            chunk.batch_id = batch_id
            chunk.status = str(result.get("status", "submitted"))
            if chunk.status not in {"validating", "in_progress", "submitted"}:
                chunk.status = "submitted"
            _write_json(directory / "manifest.json", manifest.model_dump(mode="json"))
        return manifest
    finally:
        if client is None:
            await active.close()


async def update_batch_status(
    run_dir: str | Path,
    *,
    api_key: str | None = None,
    client: BatchClient | None = None,
) -> BatchManifest:
    """Poll each submitted chunk once and retain completed result envelopes privately."""
    directory, manifest = _read_manifest(run_dir)
    key = api_key or os.environ.get("OPENROUTER_API_KEY")
    if not key:
        raise BatchError("OPENROUTER_API_KEY is required for status.")
    active = client or BatchClient(manifest.api_base_url, key)
    try:
        for index, chunk in enumerate(manifest.chunks):
            if chunk.batch_id is None or chunk.status in _TERMINAL | {
                "submission_unknown"
            }:
                continue
            result = await active.request("GET", f"/beta/batches/{chunk.batch_id}")
            chunk.status = str(result.get("status", "unknown"))
            usage = result.get("usage")
            chunk.usage = usage if isinstance(usage, dict) else None
            costs = [
                chunk.usage.get("cost")
                for chunk in manifest.chunks
                if chunk.usage is not None
            ]
            numeric_costs = [cost for cost in costs if isinstance(cost, int | float)]
            manifest.observed_cost_usd = sum(numeric_costs) if numeric_costs else None
            if chunk.status == "completed":
                chunk.result_file = f"batch-result-{index}.json"
                _write_json(directory / chunk.result_file, result)
            elif chunk.status in _TERMINAL:
                chunk.error_code = "remote_batch_failed"
            else:
                chunk.status = "in_progress"
            _write_json(directory / "manifest.json", manifest.model_dump(mode="json"))
        return manifest
    finally:
        if client is None:
            await active.close()


def _parse_result(result: dict[str, object]) -> ToolCallReviewResponse:
    response = result.get("response")
    if not isinstance(response, dict):
        raise BatchError("A completed batch result has no response object.")
    choices = response.get("choices")
    if (
        not isinstance(choices, list)
        or len(choices) != 1
        or not isinstance(choices[0], dict)
    ):
        raise BatchError("A completed batch result has an invalid choice list.")
    message = choices[0].get("message")
    if not isinstance(message, dict):
        raise BatchError("A completed batch result has no assistant message.")
    content = message.get("content")
    if isinstance(content, dict):
        payload = content
    elif isinstance(content, str):
        try:
            payload = json.loads(content)
        except json.JSONDecodeError as exc:
            raise BatchError(
                "A completed batch result has malformed structured content."
            ) from exc
    else:
        raise BatchError("A completed batch result has no structured content.")
    try:
        return ToolCallReviewResponse.model_validate(payload)
    except ValidationError as exc:
        raise BatchError(
            "A completed batch result failed structured-output validation."
        ) from exc


def harvest_batch(run_dir: str | Path) -> EvalReport:
    """Reconcile all completed results and write a normal private EvalReport."""
    directory, manifest = _read_manifest(run_dir)
    requests = _load_requests(directory, manifest)
    if any(chunk.status != "completed" for chunk in manifest.chunks):
        raise BatchError("Cannot harvest until every batch chunk is completed.")
    results_by_id: dict[str, dict[str, object]] = {}
    for chunk in manifest.chunks:
        if chunk.result_file is None:
            raise BatchError("Completed batch chunk has no result artifact.")
        result_path = resolve_private_eval_path(directory / chunk.result_file)
        if result_path.parent != directory:
            raise BatchError(
                "Batch result artifacts must be directly inside the run directory."
            )
        try:
            envelope = json.loads(result_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise BatchError(
                "A completed batch result artifact is unavailable."
            ) from exc
        values = envelope.get("results") if isinstance(envelope, dict) else None
        if not isinstance(values, list):
            raise BatchError("Completed batch response has no results list.")
        for value in values:
            if not isinstance(value, dict) or not isinstance(
                value.get("custom_id"), str
            ):
                raise BatchError("Batch result is missing a custom_id.")
            custom_id = value["custom_id"]
            if custom_id in results_by_id:
                raise BatchError("Batch results contain a duplicate custom_id.")
            results_by_id[custom_id] = value
    expected = {request.custom_id for request in requests}
    if set(results_by_id) != expected:
        raise BatchError("Batch results do not exactly match prepared request ids.")
    trials: list[TrialRecord] = []
    for request in requests:
        result = results_by_id[request.custom_id]
        if result.get("error") is not None or result.get("response") is None:
            raise BatchError(
                "Batch item error is not a verdict and cannot be harvested."
            )
        response = _parse_result(result)
        case = EvalCase.model_validate(request.case)
        constraints = case.constraints.to_constraints()
        if response.verdict not in constraints.available_verdicts:
            raise BatchError(
                "Batch verdict is outside the case's available verdict space."
            )
        trials.append(
            TrialRecord(
                case_id=request.case_id,
                boundary=case.boundary,
                label=case.label,
                source=case.source,
                source_group=case.source_group,
                matched_group=case.matched_group,
                visibility=case.visibility,
                control_kind=case.control_kind,
                attack_class=case.attack_class,
                expected_verdict=case.expected_verdict,
                attack_input_key=request.attack_input_key,
                seed_index=request.seed_index,
                verdict=response.verdict,
                latency_ms=None,
                reason=response.reason,
                allow_in_space=request.allow_in_space,
            )
        )
    report = EvalReport(
        trials=sorted(trials, key=lambda trial: (trial.case_id, trial.seed_index)),
        skipped_cases=manifest.skipped_cases,
        seeds=manifest.seeds,
        provider=manifest.provider,
        model=manifest.model,
        model_parameters={
            "batch_mode": True,
            "max_tokens": manifest.max_tokens,
            "provider_require_parameters": True,
            "response_format": "tool_call_review_response",
        },
        dataset_hash=manifest.dataset_hash,
        registry_hash=manifest.registry_hash,
        latency_source="batch_api_unavailable",
    )
    _write_json(directory / "report.json", report.to_json_dict())
    manifest.report_file = "report.json"
    _write_json(directory / "manifest.json", manifest.model_dump(mode="json"))
    return report
