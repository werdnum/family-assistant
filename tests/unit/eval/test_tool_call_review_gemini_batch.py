"""Tests for the private native Gemini batch evaluator."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest
from google.genai import errors as genai_errors

from family_assistant.eval import private_paths
from family_assistant.eval.tool_call_review import gemini_batch as batch_module
from family_assistant.eval.tool_call_review.gemini_batch import (
    GeminiBatchClient,
    GeminiBatchError,
    harvest_gemini_batch,
    prepare_gemini_batch,
    submit_gemini_batch,
    update_gemini_batch_status,
)

pytestmark = pytest.mark.no_db

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def private_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    (root / ".review-eval-local").mkdir()
    monkeypatch.setattr(private_paths, "PROJECT_ROOT", root)
    return root / ".review-eval-local"


class _FakeFiles:
    def __init__(self) -> None:
        self.downloaded: list[str] = []
        self.output = b""
        self.upload_error: Exception | None = None
        self.upload_response: object = SimpleNamespace(name="files/input-0")

    async def upload(self, *, file: str, config: dict[str, object]) -> object:
        assert file.endswith("upload-0.jsonl")
        assert config["mime_type"] == "application/jsonl"
        if self.upload_error is not None:
            raise self.upload_error
        return self.upload_response

    async def download(self, *, file: str) -> bytes:
        self.downloaded.append(file)
        return self.output


class _FakeBatches:
    def __init__(self) -> None:
        self.create_calls: list[dict[str, object]] = []
        self.get_calls: list[str] = []
        self.state = "JOB_STATE_RUNNING"
        self.create_error: Exception | None = None

    async def create(self, **kwargs: object) -> object:
        self.create_calls.append(kwargs)
        if self.create_error is not None:
            raise self.create_error
        return SimpleNamespace(name="batches/run-0", state="JOB_STATE_RUNNING")

    async def get(self, *, name: str) -> object:
        self.get_calls.append(name)
        return SimpleNamespace(
            name=name,
            state=self.state,
            dest=SimpleNamespace(file_name="files/output-0"),
        )


class _FakeClient(GeminiBatchClient):
    def __init__(self) -> None:
        self.aio = SimpleNamespace(files=_FakeFiles(), batches=_FakeBatches())

    async def close(self) -> None:
        pass

    async def get_batch_status(self, name: str) -> dict[str, object]:
        return {
            "name": name,
            "metadata": {"state": self.aio.batches.state},
            "response": {"responsesFile": "files/output-0"}
            if self.aio.batches.state == "JOB_STATE_SUCCEEDED"
            else {},
        }


def _response_line(key: str) -> bytes:
    return (
        json.dumps({
            "key": key,
            "response": {
                "candidates": [
                    {
                        "finishReason": "STOP",
                        "content": {
                            "parts": [
                                {
                                    "text": json.dumps({
                                        "verdict": "deny",
                                        "reason": "reviewed",
                                        "safer_alternative": None,
                                    })
                                }
                            ]
                        },
                    }
                ],
                "usageMetadata": {
                    "promptTokenCount": 4,
                    "candidatesTokenCount": 5,
                    "thoughtsTokenCount": 2,
                    "totalTokenCount": 11,
                },
            },
        }).encode()
        + b"\n"
    )


def test_prepare_writes_native_wire_contract(private_root: Path) -> None:
    manifest = prepare_gemini_batch(
        ["src/family_assistant/eval/tool_call_review/datasets/manual"],
        private_root / "runs" / "native",
        seeds=1,
        batch_size=500,
    )
    assert manifest.provider == "google_gemini_native"
    assert manifest.schema_version == "review_eval_gemini_batch_v1"
    line = json.loads(
        (private_root / "runs/native/requests.jsonl").read_text().splitlines()[0]
    )
    assert set(line) == {"key", "request"}
    request = line["request"]
    assert set(request) == {"contents", "systemInstruction", "generationConfig"}
    config = request["generationConfig"]
    assert config["responseMimeType"] == "application/json"
    assert config["maxOutputTokens"] == 512
    assert config["thinkingConfig"] == {"thinkingLevel": "low"}
    assert isinstance(config["responseJsonSchema"], dict)
    assert (
        json.loads(
            (private_root / "runs/native/metadata.jsonl").read_text().splitlines()[0]
        )["key"]
        == line["key"]
    )


@pytest.mark.parametrize(
    "state",
    [
        "BATCH_STATE_PENDING",
        "BATCH_STATE_RUNNING",
        "BATCH_STATE_SUCCEEDED",
        "BATCH_STATE_FAILED",
        "BATCH_STATE_CANCELLED",
        "BATCH_STATE_EXPIRED",
        "JOB_STATE_RUNNING",
    ],
)
def test_documented_batch_and_sdk_state_vocabularies_are_accepted(
    state: str,
) -> None:
    assert batch_module._job_state({"state": state}) in {
        "pending",
        "running",
        "succeeded",
        "failed",
        "cancelled",
        "expired",
    }


@pytest.mark.asyncio
async def test_fake_submit_poll_and_harvest_records_usage(private_root: Path) -> None:
    run_dir = private_root / "runs" / "native"
    manifest = prepare_gemini_batch(
        ["src/family_assistant/eval/tool_call_review/datasets/manual"],
        run_dir,
    )
    client = _FakeClient()
    client.aio.files.output = b"".join(
        _response_line(key) for key in manifest.chunks[0].request_ids
    )
    submitted = await submit_gemini_batch(
        run_dir,
        approved_spend_usd=1.0,
        approve_spend=True,
        client=client,
    )
    assert submitted.chunks[0].status == "running"
    assert client.aio.batches.create_calls[0]["src"] == "files/input-0"
    client.aio.batches.state = "JOB_STATE_SUCCEEDED"
    completed = await update_gemini_batch_status(run_dir, client=client)
    assert completed.chunks[0].status == "succeeded"
    report = await harvest_gemini_batch(run_dir, client=client)
    assert report.provider == "google_gemini_native"
    assert report.trials[0].latency_ms is None
    assert report.model_parameters is not None
    assert report.model_parameters["thinking_level"] == "low"
    count = manifest.request_count
    assert json.loads((run_dir / "manifest.json").read_text())["observed_usage"] == {
        "prompt_tokens": 4 * count,
        "candidate_tokens": 5 * count,
        "thinking_tokens": 2 * count,
        "total_tokens": 11 * count,
    }


@pytest.mark.asyncio
async def test_upload_failure_is_pending_and_retryable(private_root: Path) -> None:
    run_dir = private_root / "runs" / "native"
    prepare_gemini_batch(
        ["src/family_assistant/eval/tool_call_review/datasets/manual"], run_dir
    )
    client = _FakeClient()
    client.aio.files.upload_error = RuntimeError("temporary upload failure")

    with pytest.raises(GeminiBatchError, match="upload failed"):
        await submit_gemini_batch(
            run_dir, approved_spend_usd=1.0, approve_spend=True, client=client
        )

    payload = json.loads((run_dir / "manifest.json").read_text())
    assert payload["chunks"][0]["status"] == "pending"
    assert payload["chunks"][0]["error_code"] == "upload_failed"
    assert payload["chunks"][0]["input_file_name"] is None
    assert client.aio.batches.create_calls == []

    client.aio.files.upload_error = None
    submitted = await submit_gemini_batch(
        run_dir, approved_spend_usd=1.0, approve_spend=True, client=client
    )
    assert submitted.chunks[0].status == "running"
    assert submitted.chunks[0].error_code is None
    assert len(client.aio.batches.create_calls) == 1


@pytest.mark.asyncio
async def test_malformed_upload_response_is_pending_and_retryable(
    private_root: Path,
) -> None:
    run_dir = private_root / "runs" / "native"
    prepare_gemini_batch(
        ["src/family_assistant/eval/tool_call_review/datasets/manual"], run_dir
    )
    client = _FakeClient()
    client.aio.files.upload_response = SimpleNamespace()

    with pytest.raises(GeminiBatchError, match="upload response was invalid"):
        await submit_gemini_batch(
            run_dir, approved_spend_usd=1.0, approve_spend=True, client=client
        )

    payload = json.loads((run_dir / "manifest.json").read_text())
    assert payload["chunks"][0]["status"] == "pending"
    assert payload["chunks"][0]["error_code"] == "invalid_upload_response"
    assert payload["chunks"][0]["input_file_name"] is None
    assert client.aio.batches.create_calls == []


@pytest.mark.asyncio
async def test_batch_creation_failure_is_submission_unknown(private_root: Path) -> None:
    run_dir = private_root / "runs" / "native"
    prepare_gemini_batch(
        ["src/family_assistant/eval/tool_call_review/datasets/manual"], run_dir
    )
    client = _FakeClient()
    client.aio.batches.create_error = RuntimeError("ambiguous create failure")

    with pytest.raises(GeminiBatchError, match="outcome is unknown"):
        await submit_gemini_batch(
            run_dir, approved_spend_usd=1.0, approve_spend=True, client=client
        )

    payload = json.loads((run_dir / "manifest.json").read_text())
    assert payload["chunks"][0]["status"] == "submission_unknown"
    assert payload["chunks"][0]["error_code"] == "submission_unknown"
    assert payload["chunks"][0]["input_file_name"] == "files/input-0"
    assert len(client.aio.batches.create_calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("code", [400, 401, 429])
async def test_definitive_creation_rejection_is_pending_and_retryable(
    private_root: Path, code: int
) -> None:
    run_dir = private_root / "runs" / "native"
    prepare_gemini_batch(
        ["src/family_assistant/eval/tool_call_review/datasets/manual"], run_dir
    )
    client = _FakeClient()
    client.aio.batches.create_error = genai_errors.ClientError(code, {})

    with pytest.raises(GeminiBatchError, match="creation was rejected"):
        await submit_gemini_batch(
            run_dir, approved_spend_usd=1.0, approve_spend=True, client=client
        )

    payload = json.loads((run_dir / "manifest.json").read_text())
    assert payload["chunks"][0]["status"] == "pending"
    assert payload["chunks"][0]["error_code"] == "creation_rejected"
    assert payload["chunks"][0]["input_file_name"] == "files/input-0"

    client.aio.batches.create_error = None
    submitted = await submit_gemini_batch(
        run_dir, approved_spend_usd=1.0, approve_spend=True, client=client
    )
    assert submitted.chunks[0].status == "running"
    assert submitted.chunks[0].error_code is None
    assert len(client.aio.batches.create_calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("code", [408, 409])
async def test_ambiguous_client_creation_error_is_submission_unknown(
    private_root: Path, code: int
) -> None:
    run_dir = private_root / "runs" / "native"
    prepare_gemini_batch(
        ["src/family_assistant/eval/tool_call_review/datasets/manual"], run_dir
    )
    client = _FakeClient()
    client.aio.batches.create_error = genai_errors.ClientError(code, {})

    with pytest.raises(GeminiBatchError, match="outcome is unknown"):
        await submit_gemini_batch(
            run_dir, approved_spend_usd=1.0, approve_spend=True, client=client
        )

    payload = json.loads((run_dir / "manifest.json").read_text())
    assert payload["chunks"][0]["status"] == "submission_unknown"
    assert payload["chunks"][0]["error_code"] == "submission_unknown"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "creation_error",
    [genai_errors.ServerError(500, {}), TimeoutError("request timed out")],
    ids=["server", "timeout"],
)
async def test_server_and_timeout_creation_errors_are_submission_unknown(
    private_root: Path, creation_error: Exception
) -> None:
    run_dir = private_root / "runs" / "native"
    prepare_gemini_batch(
        ["src/family_assistant/eval/tool_call_review/datasets/manual"], run_dir
    )
    client = _FakeClient()
    client.aio.batches.create_error = creation_error

    with pytest.raises(GeminiBatchError, match="outcome is unknown"):
        await submit_gemini_batch(
            run_dir, approved_spend_usd=1.0, approve_spend=True, client=client
        )

    payload = json.loads((run_dir / "manifest.json").read_text())
    assert payload["chunks"][0]["status"] == "submission_unknown"
    assert payload["chunks"][0]["error_code"] == "submission_unknown"


@pytest.mark.asyncio
async def test_local_result_digest_rejects_tampering(private_root: Path) -> None:
    run_dir = private_root / "runs" / "native"
    manifest = prepare_gemini_batch(
        ["src/family_assistant/eval/tool_call_review/datasets/manual"], run_dir
    )
    client = _FakeClient()
    client.aio.files.output = b"".join(
        _response_line(key) for key in manifest.chunks[0].request_ids
    )
    await submit_gemini_batch(
        run_dir, approved_spend_usd=1.0, approve_spend=True, client=client
    )
    client.aio.batches.state = "JOB_STATE_SUCCEEDED"
    await update_gemini_batch_status(run_dir, client=client)
    await harvest_gemini_batch(run_dir, client=client)
    result_path = run_dir / "result-0.jsonl"
    result_path.write_bytes(result_path.read_bytes() + b"tampered")
    with pytest.raises(GeminiBatchError, match="digest"):
        await harvest_gemini_batch(run_dir, client=client)


@pytest.mark.asyncio
async def test_unknown_gemini_state_fails_closed(private_root: Path) -> None:
    run_dir = private_root / "runs" / "native"
    prepare_gemini_batch(
        ["src/family_assistant/eval/tool_call_review/datasets/manual"], run_dir
    )
    client = _FakeClient()
    await submit_gemini_batch(
        run_dir,
        approved_spend_usd=1.0,
        approve_spend=True,
        client=client,
    )
    client.aio.batches.state = "JOB_STATE_PARTIALLY_SUCCEEDED"
    with pytest.raises(GeminiBatchError, match="unknown job state"):
        await update_gemini_batch_status(run_dir, client=client)


@pytest.mark.asyncio
async def test_result_reconciliation_rejects_duplicate_keys(private_root: Path) -> None:
    run_dir = private_root / "runs" / "native"
    manifest = prepare_gemini_batch(
        ["src/family_assistant/eval/tool_call_review/datasets/manual"], run_dir
    )
    client = _FakeClient()
    await submit_gemini_batch(
        run_dir, approved_spend_usd=1.0, approve_spend=True, client=client
    )
    client.aio.batches.state = "JOB_STATE_SUCCEEDED"
    await update_gemini_batch_status(run_dir, client=client)
    key = manifest.chunks[0].request_ids[0]
    client.aio.files.output = _response_line(key) + _response_line(key)
    with pytest.raises(GeminiBatchError, match="duplicate key"):
        await harvest_gemini_batch(run_dir, client=client)


@pytest.mark.asyncio
async def test_result_error_arm_is_not_a_verdict(private_root: Path) -> None:
    run_dir = private_root / "runs" / "native"
    manifest = prepare_gemini_batch(
        ["src/family_assistant/eval/tool_call_review/datasets/manual"], run_dir
    )
    client = _FakeClient()
    await submit_gemini_batch(
        run_dir, approved_spend_usd=1.0, approve_spend=True, client=client
    )
    client.aio.batches.state = "JOB_STATE_SUCCEEDED"
    await update_gemini_batch_status(run_dir, client=client)
    client.aio.files.output = b"".join(
        json.dumps({"key": key, "error": {"code": "failed"}}).encode() + b"\n"
        for key in manifest.chunks[0].request_ids
    )
    with pytest.raises(GeminiBatchError, match="not a verdict"):
        await harvest_gemini_batch(run_dir, client=client)


def test_tampered_native_requests_fail_before_network(private_root: Path) -> None:
    run_dir = private_root / "runs" / "native"
    prepare_gemini_batch(
        ["src/family_assistant/eval/tool_call_review/datasets/manual"], run_dir
    )
    request_path = run_dir / "requests.jsonl"
    request_path.write_text(request_path.read_text() + "\n", encoding="utf-8")
    with pytest.raises(GeminiBatchError, match="digest"):
        asyncio.run(
            submit_gemini_batch(
                run_dir,
                approved_spend_usd=1.0,
                approve_spend=True,
                client=_FakeClient(),
            )
        )


def test_native_submit_requires_explicit_positive_approval(private_root: Path) -> None:
    run_dir = private_root / "runs" / "native"
    prepare_gemini_batch(
        ["src/family_assistant/eval/tool_call_review/datasets/manual"], run_dir
    )
    with pytest.raises(GeminiBatchError, match="positive"):
        asyncio.run(
            submit_gemini_batch(
                run_dir, approved_spend_usd=0, approve_spend=True, client=_FakeClient()
            )
        )


def test_native_submit_refuses_ambiguous_run_before_network(private_root: Path) -> None:
    run_dir = private_root / "runs" / "native"
    prepare_gemini_batch(
        ["src/family_assistant/eval/tool_call_review/datasets/manual"], run_dir
    )
    manifest_path = run_dir / "manifest.json"
    payload = json.loads(manifest_path.read_text())
    payload["chunks"][0]["status"] = "submission_unknown"
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    client = _FakeClient()
    with pytest.raises(GeminiBatchError, match="ambiguous"):
        asyncio.run(
            submit_gemini_batch(
                run_dir, approved_spend_usd=1.0, approve_spend=True, client=client
            )
        )
    assert client.aio.batches.create_calls == []
