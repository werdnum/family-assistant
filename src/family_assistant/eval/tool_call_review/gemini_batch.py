"""Private native Gemini Batch API evaluation.

This module deliberately has its own manifest and lifecycle.  Gemini's batch
input/output files and job state names are not OpenRouter batch artifacts, and
mixing the two would make a resumed run ambiguous.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import json
import math
import os
from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast

import httpx
from google import genai
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
from family_assistant.llm.messages import (
    LLMMessage,
    SystemMessage,
    UserMessage,
)
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
    "DEFAULT_BATCH_SIZE",
    "DEFAULT_MAX_TOKENS",
    "GEMINI_BATCH_SCHEMA_VERSION",
    "GeminiBatchChunk",
    "GeminiBatchClient",
    "GeminiBatchError",
    "GeminiBatchManifest",
    "harvest_gemini_batch",
    "prepare_gemini_batch",
    "submit_gemini_batch",
    "update_gemini_batch_status",
    "validate_gemini_prepare_inputs",
]

GEMINI_BATCH_SCHEMA_VERSION = "review_eval_gemini_batch_v1"
DEFAULT_BATCH_SIZE = 500
DEFAULT_MAX_TOKENS = 512
DEFAULT_GEMINI_API_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
_TERMINAL = frozenset({"succeeded", "failed", "cancelled", "expired"})


class GeminiBatchError(RuntimeError):
    """A Gemini batch artifact or remote response cannot be reconciled safely."""


class GeminiBatchChunk(BaseModel):
    model_config = ConfigDict(extra="forbid")

    index: int = Field(ge=0)
    request_ids: list[str]
    status: str = "pending"
    batch_name: str | None = None
    input_file_name: str | None = None
    result_file: str | None = None
    result_sha256: str | None = None
    usage: dict[str, object] | None = None
    error_code: str | None = None


class GeminiBatchManifest(BaseModel):
    """Private resumable state for a native Gemini request set."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str = GEMINI_BATCH_SCHEMA_VERSION
    run_id: str
    provider: str = "google_gemini_native"
    model: str
    dataset_hash: str
    registry_hash: str | None = None
    seeds: int = Field(ge=1)
    batch_size: int = Field(ge=1)
    max_tokens: int = Field(ge=1)
    request_count: int = Field(ge=0)
    request_file: str
    metadata_file: str
    request_sha256: str
    metadata_sha256: str
    skipped_cases: list[SkippedCase] = Field(default_factory=list)
    chunks: list[GeminiBatchChunk] = Field(default_factory=list)
    approved_spend_usd: float | None = Field(default=None, gt=0)
    spend_approved: bool = False
    observed_usage: dict[str, int] | None = None
    created_at: str
    report_file: str | None = None


class _PreparedMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    case_id: str
    seed_index: int = Field(ge=0)
    case: dict[str, object]
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


def _write_lines(path: Path, values: Sequence[BaseModel | dict[str, object]]) -> None:
    path = resolve_private_eval_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        for value in values:
            payload = (
                value.model_dump(mode="json") if isinstance(value, BaseModel) else value
            )
            handle.write(json.dumps(payload, sort_keys=True, ensure_ascii=False) + "\n")
    temporary.replace(path)


def _write_bytes(path: Path, value: bytes) -> None:
    path = resolve_private_eval_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_bytes(value)
    temporary.replace(path)


def _read_manifest(run_dir: str | Path) -> tuple[Path, GeminiBatchManifest]:
    directory = resolve_private_eval_path(run_dir)
    try:
        manifest = GeminiBatchManifest.model_validate_json(
            (directory / "manifest.json").read_text(encoding="utf-8")
        )
    except (OSError, ValidationError, json.JSONDecodeError) as exc:
        raise GeminiBatchError(
            "Cannot load a valid private Gemini batch manifest."
        ) from exc
    if (
        manifest.schema_version != GEMINI_BATCH_SCHEMA_VERSION
        or manifest.run_id != directory.name
    ):
        raise GeminiBatchError("Unsupported or mismatched Gemini batch manifest.")
    if manifest.provider != "google_gemini_native":
        raise GeminiBatchError("Manifest is not a native Gemini batch run.")
    return directory, manifest


def _load_artifacts(
    directory: Path, manifest: GeminiBatchManifest
) -> tuple[list[dict[str, object]], list[_PreparedMetadata]]:
    request_path = resolve_private_eval_path(directory / manifest.request_file)
    metadata_path = resolve_private_eval_path(directory / manifest.metadata_file)
    if request_path.parent != directory or metadata_path.parent != directory:
        raise GeminiBatchError(
            "Gemini request artifacts must be directly inside the run directory."
        )
    try:
        request_lines = [
            json.loads(line)
            for line in request_path.read_text(encoding="utf-8").splitlines()
            if line
        ]
        metadata = [
            _PreparedMetadata.model_validate_json(line)
            for line in metadata_path.read_text(encoding="utf-8").splitlines()
            if line
        ]
        request_digest = hashlib.sha256(request_path.read_bytes()).hexdigest()
        metadata_digest = hashlib.sha256(metadata_path.read_bytes()).hexdigest()
    except (OSError, ValueError, ValidationError, json.JSONDecodeError) as exc:
        raise GeminiBatchError(
            "Gemini request artifacts are malformed or unavailable."
        ) from exc
    if (
        request_digest != manifest.request_sha256
        or metadata_digest != manifest.metadata_sha256
    ):
        raise GeminiBatchError(
            "Gemini request artifact digest does not match the manifest."
        )
    if (
        len(request_lines) != manifest.request_count
        or len(metadata) != manifest.request_count
    ):
        raise GeminiBatchError("Gemini request count does not match the manifest.")
    keys = [
        line.get("key") if isinstance(line, dict) else None for line in request_lines
    ]
    if not all(isinstance(key, str) for key in keys):
        raise GeminiBatchError("Gemini request JSONL has a missing or invalid key.")
    string_keys = cast("list[str]", keys)
    metadata_keys = [entry.key for entry in metadata]
    expected = [key for chunk in manifest.chunks for key in chunk.request_ids]
    if (
        string_keys != sorted(string_keys)
        or string_keys != metadata_keys
        or string_keys != expected
        or len(set(string_keys)) != len(string_keys)
    ):
        raise GeminiBatchError(
            "Gemini request keys do not exactly match manifest chunks."
        )
    for line in request_lines:
        if (
            not isinstance(line, dict)
            or set(line) != {"key", "request"}
            or not isinstance(line.get("request"), dict)
        ):
            raise GeminiBatchError(
                "Gemini request JSONL has an invalid key/request envelope."
            )
    return cast("list[dict[str, object]]", request_lines), metadata


def _custom_key(case_id: str, seed_index: int) -> str:
    digest = hashlib.sha256(f"{case_id}\0{seed_index}".encode()).hexdigest()[:16]
    return f"review-{digest}"


def _schema() -> dict[str, object]:
    batch_module = importlib.import_module(
        "family_assistant.eval.tool_call_review.batch"
    )
    response_format = batch_module._response_format()
    return cast("dict[str, object]", response_format["json_schema"]["schema"])


def _text_parts(message: LLMMessage) -> list[dict[str, str]]:
    content: object = message.content
    if isinstance(content, str):
        return [{"text": content}]
    if isinstance(message, UserMessage):
        parts: list[dict[str, str]] = []
        if not isinstance(content, list):
            raise GeminiBatchError("Native Gemini batch received invalid user content.")
        for part in content:
            if getattr(part, "type", None) != "text":
                raise GeminiBatchError(
                    "Native Gemini batch does not support non-text review content."
                )
            text = getattr(part, "text", None)
            if not isinstance(text, str):
                raise GeminiBatchError(
                    "Native Gemini batch received an invalid text content part."
                )
            parts.append({"text": text})
        return parts
    raise GeminiBatchError(
        "Native Gemini batch received an unsupported message content."
    )


def _request_for_case(
    case: EvalCase,
    model: str,
    max_tokens: int,
    registry: Mapping[str, ToolDescriptor] | None,
) -> tuple[dict[str, object], str, bool]:
    review_input, constraints = case.to_review_input(descriptor_registry=registry)
    messages = (
        assemble_browser_action_review_messages(review_input, constraints)
        if isinstance(review_input, BrowserActionReviewInput)
        else assemble_tool_call_review_messages(review_input, constraints)
    )
    system = [message for message in messages if isinstance(message, SystemMessage)]
    user = [message for message in messages if isinstance(message, UserMessage)]
    if len(system) != 1 or len(user) != 1 or len(system) + len(user) != len(messages):
        raise GeminiBatchError(
            "Native Gemini batch requires one system and one user review message."
        )
    request = {
        "contents": [{"role": "user", "parts": _text_parts(user[0])}],
        "systemInstruction": {"parts": _text_parts(system[0])},
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseJsonSchema": _schema(),
            "maxOutputTokens": max_tokens,
            "seed": 0,
            "thinkingConfig": {"thinkingLevel": "low"},
        },
    }
    return (
        request,
        attack_input_key(review_input, constraints),
        ToolCallReviewVerdict.ALLOW in constraints.available_verdicts,
    )


def _validate_parameters(seeds: int, batch_size: int, max_tokens: int) -> None:
    if seeds < 1 or batch_size < 1 or max_tokens < 1:
        raise GeminiBatchError("seeds, batch_size, and max_tokens must be positive.")


def _collect(
    datasets: Sequence[str | Path],
    *,
    model: str,
    seeds: int,
    max_tokens: int,
    registry: Mapping[str, ToolDescriptor] | None,
) -> tuple[
    list[EvalCase], list[dict[str, object]], list[_PreparedMetadata], list[SkippedCase]
]:
    cases = load_cases(datasets, descriptor_registry=registry)
    requests: list[dict[str, object]] = []
    metadata: list[_PreparedMetadata] = []
    skipped: list[SkippedCase] = []
    for case in cases:
        skip = case_skip(case, descriptor_registry=registry)
        if skip is not None:
            skipped.append(
                SkippedCase(case_id=case.id, kind=skip.kind, reason=skip.reason)
            )
            continue
        request, input_key, allow = _request_for_case(case, model, max_tokens, registry)
        for seed_index in range(seeds):
            key = _custom_key(case.id, seed_index)
            request_copy = json.loads(json.dumps(request))
            generation = cast("dict[str, object]", request_copy["generationConfig"])
            generation["seed"] = seed_index
            requests.append({"key": key, "request": request_copy})
            metadata.append(
                _PreparedMetadata(
                    key=key,
                    case_id=case.id,
                    seed_index=seed_index,
                    case=case.model_dump(mode="json"),
                    attack_input_key=input_key,
                    allow_in_space=allow,
                )
            )
    order = sorted(range(len(metadata)), key=lambda index: metadata[index].key)
    return (
        cases,
        [requests[index] for index in order],
        [metadata[index] for index in order],
        skipped,
    )


def prepare_gemini_batch(
    datasets: Sequence[str | Path],
    run_dir: str | Path,
    *,
    model: str = "gemini-3.7-flash",
    seeds: int = 1,
    batch_size: int = DEFAULT_BATCH_SIZE,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    descriptor_registry: Mapping[str, ToolDescriptor] | None = None,
) -> GeminiBatchManifest:
    _validate_parameters(seeds, batch_size, max_tokens)
    directory = resolve_private_eval_path(run_dir)
    if directory.exists():
        raise GeminiBatchError(
            f"Refusing to reuse an existing Gemini batch directory: {directory}"
        )
    cases, requests, metadata, skipped = _collect(
        datasets,
        model=model,
        seeds=seeds,
        max_tokens=max_tokens,
        registry=descriptor_registry,
    )
    if not requests:
        raise GeminiBatchError(
            "No runnable cases were found; no batch artifacts were created."
        )
    chunks = [
        GeminiBatchChunk(
            index=index,
            request_ids=[
                cast("str", request["key"])
                for request in requests[start : start + batch_size]
            ],
        )
        for index, start in enumerate(range(0, len(requests), batch_size))
    ]
    directory.mkdir(parents=True)
    request_path = directory / "requests.jsonl"
    metadata_path = directory / "metadata.jsonl"
    _write_lines(request_path, requests)
    _write_lines(metadata_path, metadata)
    manifest = GeminiBatchManifest(
        run_id=directory.name,
        model=model,
        dataset_hash=content_hash(cases),
        registry_hash=registry_digest(descriptor_registry),
        seeds=seeds,
        batch_size=batch_size,
        max_tokens=max_tokens,
        request_count=len(requests),
        request_file=request_path.name,
        metadata_file=metadata_path.name,
        request_sha256=hashlib.sha256(request_path.read_bytes()).hexdigest(),
        metadata_sha256=hashlib.sha256(metadata_path.read_bytes()).hexdigest(),
        skipped_cases=skipped,
        chunks=chunks,
        created_at=datetime.now(UTC).isoformat(),
    )
    _write_json(directory / "manifest.json", manifest.model_dump(mode="json"))
    return manifest


def validate_gemini_prepare_inputs(
    datasets: Sequence[str | Path],
    *,
    model: str = "gemini-3.7-flash",
    seeds: int = 1,
    batch_size: int = DEFAULT_BATCH_SIZE,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    descriptor_registry: Mapping[str, ToolDescriptor] | None = None,
) -> tuple[int, int]:
    _validate_parameters(seeds, batch_size, max_tokens)
    cases, requests, _, _ = _collect(
        datasets,
        model=model,
        seeds=seeds,
        max_tokens=max_tokens,
        registry=descriptor_registry,
    )
    if not requests:
        raise GeminiBatchError(
            "No runnable cases were found; no batch artifacts were created."
        )
    return len(cases), len(requests) // seeds


def _field(value: object, name: str) -> object:
    if isinstance(value, dict):
        return value.get(name) or value.get(_camel(name))
    return getattr(value, name, None)


def _raw_status(raw: dict[str, object]) -> str:
    metadata = raw.get("metadata")
    if not isinstance(metadata, dict) or not isinstance(metadata.get("state"), str):
        raise GeminiBatchError("Gemini status response has no metadata state.")
    return _job_state({"state": metadata["state"]})


def _raw_name(raw: dict[str, object]) -> str:
    return _batch_name(raw, "Gemini status response has no job name.")


def _batch_name(value: object, message: str) -> str:
    name = _name(value, message)
    if not name.startswith("batches/") or name.count("/") != 1:
        raise GeminiBatchError("Gemini batch job name has an invalid shape.")
    return name


def _raw_output(
    raw: dict[str, object], chunk: GeminiBatchChunk
) -> tuple[str, bytes | None]:
    response = raw.get("response")
    if not isinstance(response, dict):
        raise GeminiBatchError("Succeeded Gemini response has no output.")
    responses_file = response.get("responsesFile")
    inlined = response.get("inlinedResponses")
    if (responses_file is None) == (inlined is None):
        raise GeminiBatchError("Gemini response must contain exactly one output arm.")
    if (
        isinstance(responses_file, str)
        and responses_file.startswith("files/")
        and responses_file.count("/") == 1
    ):
        return f"remote:{responses_file}", None
    if responses_file is not None:
        raise GeminiBatchError("Gemini response file name has an invalid shape.")
    if not isinstance(inlined, dict) or not isinstance(
        inlined.get("inlinedResponses"), list
    ):
        raise GeminiBatchError("Gemini inline output has an invalid shape.")
    lines: list[bytes] = []
    for value in inlined["inlinedResponses"]:
        if not isinstance(value, dict):
            raise GeminiBatchError("Gemini inline output has an invalid response.")
        metadata = value.get("metadata")
        key = metadata.get("key") if isinstance(metadata, dict) else None
        if not isinstance(key, str) or not key:
            raise GeminiBatchError("Gemini inline output is missing a request key.")
        item: dict[str, object] = {"key": key}
        for arm in ("response", "error"):
            if arm in value:
                item[arm] = value[arm]
        if len(item) == 1:
            raise GeminiBatchError("Gemini inline output has no response or error arm.")
        lines.append(_json_bytes(item) + b"\n")
    return f"inline:{chunk.index}", b"".join(lines)


def _camel(name: str) -> str:
    head, *tail = name.split("_")
    return head + "".join(part.capitalize() for part in tail)


def _job_state(job: object) -> str:
    state = _field(job, "state")
    state_name = getattr(state, "name", None)
    if isinstance(state_name, str):
        state = state_name
    if not isinstance(state, str):
        raise GeminiBatchError("Gemini Batch API returned a missing job state.")
    mapping = {
        "JOB_STATE_PENDING": "pending",
        "JOB_STATE_RUNNING": "running",
        "JOB_STATE_SUCCEEDED": "succeeded",
        "JOB_STATE_FAILED": "failed",
        "JOB_STATE_CANCELLED": "cancelled",
        "JOB_STATE_EXPIRED": "expired",
        "BATCH_STATE_PENDING": "pending",
        "BATCH_STATE_RUNNING": "running",
        "BATCH_STATE_SUCCEEDED": "succeeded",
        "BATCH_STATE_FAILED": "failed",
        "BATCH_STATE_CANCELLED": "cancelled",
        "BATCH_STATE_EXPIRED": "expired",
    }
    if state not in mapping:
        raise GeminiBatchError("Gemini Batch API returned an unknown job state.")
    return mapping[state]


class GeminiBatchClient:
    """Thin injectable wrapper around google-genai's async API."""

    def __init__(
        self,
        api_key: str | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._api_key = api_key or os.environ.get("GEMINI_API_KEY")
        self._client = genai.Client(api_key=self._api_key)
        self.aio = self._client.aio
        self._http = http_client or httpx.AsyncClient(timeout=60.0)
        self._owns_http = http_client is None

    async def get_batch_status(self, name: str) -> dict[str, object]:
        """Fetch the raw REST envelope; the SDK currently drops batch output fields."""
        response = await self._http.get(
            f"{DEFAULT_GEMINI_API_BASE_URL}/{name}",
            headers={"x-goog-api-key": self._api_key or ""},
        )
        if not 200 <= response.status_code < 300:
            raise GeminiBatchError("Gemini status request failed.")
        try:
            payload = response.json()
        except ValueError as exc:
            raise GeminiBatchError("Gemini status response was not JSON.") from exc
        if not isinstance(payload, dict):
            raise GeminiBatchError("Gemini status response was not an object.")
        return cast("dict[str, object]", payload)

    async def close(self) -> None:
        close = getattr(self.aio, "aclose", None)
        if close is not None:
            await close()
        if self._owns_http:
            await self._http.aclose()


def _name(value: object, message: str) -> str:
    name = _field(value, "name")
    if not isinstance(name, str) or not name:
        raise GeminiBatchError(message)
    return name


async def submit_gemini_batch(
    run_dir: str | Path,
    *,
    approved_spend_usd: float,
    approve_spend: bool,
    api_key: str | None = None,
    client: GeminiBatchClient | None = None,
) -> GeminiBatchManifest:
    if (
        not math.isfinite(approved_spend_usd)
        or approved_spend_usd <= 0
        or not approve_spend
    ):
        raise GeminiBatchError(
            "submit requires a positive --approved-spend-usd and --approve-spend."
        )
    directory, manifest = _read_manifest(run_dir)
    requests, _ = _load_artifacts(directory, manifest)
    if any(chunk.status == "submission_unknown" for chunk in manifest.chunks):
        raise GeminiBatchError(
            "Refusing submit: the run contains an ambiguous prior submission."
        )
    if not api_key and client is None and not os.environ.get("GEMINI_API_KEY"):
        raise GeminiBatchError("GEMINI_API_KEY is required for submit.")
    active = client or GeminiBatchClient(api_key)
    manifest.approved_spend_usd = approved_spend_usd
    manifest.spend_approved = True
    _write_json(directory / "manifest.json", manifest.model_dump(mode="json"))
    try:
        for chunk in manifest.chunks:
            if chunk.status != "pending":
                continue
            await _submit_chunk(active, directory, manifest, chunk, requests)
            _write_json(directory / "manifest.json", manifest.model_dump(mode="json"))
        return manifest
    finally:
        if client is None:
            await active.close()


async def _submit_chunk(
    active: GeminiBatchClient,
    directory: Path,
    manifest: GeminiBatchManifest,
    chunk: GeminiBatchChunk,
    requests: list[dict[str, object]],
) -> None:
    """Upload and create one chunk, recording every durable boundary."""
    request_path = _write_chunk_file(directory, manifest, chunk, requests)
    try:
        input_name = await _upload_chunk(active, request_path, manifest, chunk)
        chunk.input_file_name = input_name
        _write_json(directory / "manifest.json", manifest.model_dump(mode="json"))
        job = await _create_chunk(active, manifest, chunk, input_name)
        _apply_job(chunk, job)
    except asyncio.CancelledError:
        _mark_unknown(directory, manifest, chunk)
        raise
    except Exception as exc:
        _mark_unknown(directory, manifest, chunk)
        raise GeminiBatchError(
            "Gemini submission outcome is unknown; inspect the provider before retrying."
        ) from exc


def _mark_unknown(
    directory: Path, manifest: GeminiBatchManifest, chunk: GeminiBatchChunk
) -> None:
    chunk.status = "submission_unknown"
    chunk.error_code = "submission_unknown"
    _write_json(directory / "manifest.json", manifest.model_dump(mode="json"))


def _write_chunk_file(
    directory: Path,
    manifest: GeminiBatchManifest,
    chunk: GeminiBatchChunk,
    requests: list[dict[str, object]],
) -> Path:
    selected = [
        request for request in requests if request.get("key") in chunk.request_ids
    ]
    if len(selected) != len(chunk.request_ids):
        raise GeminiBatchError("Gemini chunk requests do not match its manifest IDs.")
    path = directory / f"upload-{chunk.index}.jsonl"
    _write_lines(path, selected)
    return path


async def _upload_chunk(
    active: GeminiBatchClient,
    request_path: Path,
    manifest: GeminiBatchManifest,
    chunk: GeminiBatchChunk,
) -> str:
    uploaded = await active.aio.files.upload(
        file=str(request_path),
        config={
            "mime_type": "application/jsonl",
            "display_name": f"{manifest.run_id}-{chunk.index}",
        },
    )
    return _name(uploaded, "Gemini upload response has no file name.")


async def _create_chunk(
    active: GeminiBatchClient,
    manifest: GeminiBatchManifest,
    chunk: GeminiBatchChunk,
    input_name: str,
) -> object:
    return await active.aio.batches.create(
        model=manifest.model,
        src=input_name,
        config={"display_name": f"{manifest.run_id}-{chunk.index}"},
    )


def _apply_job(chunk: GeminiBatchChunk, job: object) -> None:
    chunk.batch_name = _batch_name(
        job, "Gemini batch creation response has no job name."
    )
    chunk.status = "pending" if _field(job, "state") is None else _job_state(job)
    chunk.error_code = None


async def update_gemini_batch_status(
    run_dir: str | Path,
    *,
    api_key: str | None = None,
    client: GeminiBatchClient | None = None,
) -> GeminiBatchManifest:
    directory, manifest = _read_manifest(run_dir)
    if any(
        chunk.status == "pending" and chunk.batch_name is None
        for chunk in manifest.chunks
    ):
        raise GeminiBatchError("Cannot poll an unsubmitted Gemini chunk.")
    if not api_key and client is None and not os.environ.get("GEMINI_API_KEY"):
        raise GeminiBatchError("GEMINI_API_KEY is required for status.")
    active = client or GeminiBatchClient(api_key)
    try:
        for chunk in manifest.chunks:
            if chunk.batch_name is None or (
                chunk.status in _TERMINAL
                and not (chunk.status == "succeeded" and chunk.result_file is None)
            ):
                continue
            raw = await active.get_batch_status(chunk.batch_name)
            if _raw_name(raw) != chunk.batch_name:
                raise GeminiBatchError(
                    "Gemini status response name does not match the requested job."
                )
            chunk.status = _raw_status(raw)
            if chunk.status == "succeeded":
                result_file, inline_bytes = _raw_output(raw, chunk)
                if inline_bytes is not None:
                    result_path = directory / f"result-{chunk.index}.jsonl"
                    _write_bytes(result_path, inline_bytes)
                    chunk.result_file = result_path.name
                    chunk.result_sha256 = hashlib.sha256(inline_bytes).hexdigest()
                else:
                    chunk.result_file = result_file
                    chunk.result_sha256 = None
            _write_json(directory / "manifest.json", manifest.model_dump(mode="json"))
        return manifest
    finally:
        if client is None:
            await active.close()


def _response_payload(result: object) -> tuple[ToolCallReviewResponse, dict[str, int]]:
    response = _field(result, "response")
    if response is None or _field(result, "error") is not None:
        raise GeminiBatchError("Gemini batch item error is not a verdict.")
    candidates = _field(response, "candidates")
    if not isinstance(candidates, list) or len(candidates) != 1:
        raise GeminiBatchError("Gemini result must contain exactly one candidate.")
    candidate = candidates[0]
    finish_reason = _field(candidate, "finish_reason")
    finish_name = getattr(finish_reason, "name", None)
    if isinstance(finish_name, str):
        finish_reason = finish_name
    if finish_reason != "STOP":
        raise GeminiBatchError("Gemini result did not finish with STOP.")
    content = _field(candidate, "content")
    parts = _field(content, "parts")
    if not isinstance(parts, list):
        raise GeminiBatchError("Gemini result has no content parts.")
    text = "".join(
        str(_field(part, "text"))
        for part in parts
        if isinstance(_field(part, "text"), str)
    ).strip()
    if not text:
        raise GeminiBatchError("Gemini result has empty text.")
    try:
        payload = json.loads(text)
        parsed = ToolCallReviewResponse.model_validate(payload)
    except (json.JSONDecodeError, ValidationError) as exc:
        raise GeminiBatchError(
            "Gemini result failed structured-output validation."
        ) from exc
    usage = _field(response, "usage_metadata")
    usage_values: dict[str, int] = {}
    if usage is not None:
        for source, target in (
            ("prompt_token_count", "prompt_tokens"),
            ("candidates_token_count", "candidate_tokens"),
            ("thoughts_token_count", "thinking_tokens"),
            ("total_token_count", "total_tokens"),
        ):
            value = _field(usage, source)
            if isinstance(value, int) and value >= 0:
                usage_values[target] = value
    return parsed, usage_values


async def harvest_gemini_batch(
    run_dir: str | Path,
    *,
    api_key: str | None = None,
    client: GeminiBatchClient | None = None,
) -> EvalReport:
    directory, manifest = _read_manifest(run_dir)
    requests, metadata = _load_artifacts(directory, manifest)
    if any(chunk.status != "succeeded" for chunk in manifest.chunks):
        raise GeminiBatchError("Cannot harvest until every Gemini chunk succeeded.")
    if not api_key and client is None and not os.environ.get("GEMINI_API_KEY"):
        raise GeminiBatchError("GEMINI_API_KEY is required for harvest.")
    active = client or GeminiBatchClient(api_key)
    results: dict[str, object] = {}
    usage_totals: dict[str, int] = {}
    try:
        for chunk in manifest.chunks:
            if chunk.result_file is None:
                raise GeminiBatchError("Succeeded Gemini chunk has no result artifact.")
            if chunk.result_file.startswith("remote:"):
                raw = await active.aio.files.download(
                    file=chunk.result_file.removeprefix("remote:")
                )
                if not isinstance(raw, bytes):
                    raise GeminiBatchError("Gemini result download was not bytes.")
                result_path = directory / f"result-{chunk.index}.jsonl"
                _write_bytes(result_path, raw)
                chunk.result_file = result_path.name
                chunk.result_sha256 = hashlib.sha256(raw).hexdigest()
                _write_json(
                    directory / "manifest.json", manifest.model_dump(mode="json")
                )
            else:
                result_path = resolve_private_eval_path(directory / chunk.result_file)
                if result_path.parent != directory:
                    raise GeminiBatchError(
                        "Gemini result artifacts must be directly inside the run directory."
                    )
                try:
                    raw = result_path.read_bytes()
                except OSError as exc:
                    raise GeminiBatchError(
                        "Gemini result artifact is unavailable."
                    ) from exc
                if chunk.result_sha256 is None:
                    raise GeminiBatchError("Gemini result artifact has no digest.")
                if hashlib.sha256(raw).hexdigest() != chunk.result_sha256:
                    raise GeminiBatchError(
                        "Gemini result artifact digest does not match the manifest."
                    )
            try:
                output_lines = raw.decode("utf-8").splitlines()
            except UnicodeDecodeError as exc:
                raise GeminiBatchError(
                    "Gemini result artifact is not UTF-8 JSONL."
                ) from exc
            for line in output_lines:
                try:
                    item = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise GeminiBatchError(
                        "Gemini result artifact is malformed."
                    ) from exc
                if not isinstance(item, dict) or not isinstance(item.get("key"), str):
                    raise GeminiBatchError("Gemini result is missing a key.")
                key = item["key"]
                if key in results:
                    raise GeminiBatchError("Gemini results contain a duplicate key.")
                results[key] = item
        expected = {entry.key for entry in metadata}
        if set(results) != expected:
            raise GeminiBatchError("Gemini results do not exactly match prepared keys.")
        trials: list[TrialRecord] = []
        for entry in metadata:
            response, usage = _response_payload(results[entry.key])
            for key, value in usage.items():
                usage_totals[key] = usage_totals.get(key, 0) + value
            case = EvalCase.model_validate(entry.case)
            constraints = case.constraints.to_constraints()
            if response.verdict not in constraints.available_verdicts:
                raise GeminiBatchError(
                    "Gemini verdict is outside the case's available verdict space."
                )
            trials.append(
                TrialRecord(
                    case_id=entry.case_id,
                    boundary=case.boundary,
                    label=case.label,
                    source=case.source,
                    source_group=case.source_group,
                    matched_group=case.matched_group,
                    visibility=case.visibility,
                    control_kind=case.control_kind,
                    attack_class=case.attack_class,
                    expected_verdict=case.expected_verdict,
                    attack_input_key=entry.attack_input_key,
                    seed_index=entry.seed_index,
                    verdict=response.verdict,
                    latency_ms=None,
                    reason=response.reason,
                    allow_in_space=entry.allow_in_space,
                )
            )
        manifest.observed_usage = usage_totals or None
        report = EvalReport(
            trials=sorted(trials, key=lambda trial: (trial.case_id, trial.seed_index)),
            skipped_cases=manifest.skipped_cases,
            seeds=manifest.seeds,
            provider=manifest.provider,
            model=manifest.model,
            model_parameters={
                "batch_mode": True,
                "max_tokens": manifest.max_tokens,
                "thinking_level": "low",
                "response_format": "tool_call_review_response",
                "usage": usage_totals or None,
            },
            dataset_hash=manifest.dataset_hash,
            registry_hash=manifest.registry_hash,
            latency_source="unavailable for asynchronous batch",
        )
        manifest.report_file = "report.json"
        _write_json(directory / manifest.report_file, report.model_dump(mode="json"))
        _write_json(directory / "manifest.json", manifest.model_dump(mode="json"))
        return report
    finally:
        if client is None:
            await active.close()
