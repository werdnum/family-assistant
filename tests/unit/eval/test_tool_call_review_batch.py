"""Tests for the private staged batch evaluator."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, cast

import httpx
import pytest

import scripts.tool_call_review_batch as batch_cli
from family_assistant.eval import private_paths
from family_assistant.eval.tool_call_review.batch import (
    BatchClient,
    BatchError,
    BatchRejectedError,
    harvest_batch,
    prepare_batch,
    submit_batch,
    update_batch_status,
)
from family_assistant.eval.tool_call_review.report import LatencyStats

pytestmark = pytest.mark.no_db

if TYPE_CHECKING:
    from pathlib import Path


class _FakeBatchClient(BatchClient):
    def __init__(self) -> None:
        self.submitted: list[dict[str, object]] = []

    async def close(self) -> None:
        pass

    async def request(
        self, method: str, path: str, payload: dict[str, object] | None = None
    ) -> dict[str, object]:
        if method == "POST":
            assert payload is not None
            self.submitted.append(payload)
            return {"id": f"batch-{len(self.submitted)}", "status": "validating"}
        assert method == "GET"
        request_ids = self.submitted[int(path.rsplit("-", 1)[1]) - 1]["requests"]
        assert isinstance(request_ids, list)
        return {
            "id": path.rsplit("/", 1)[1],
            "status": "completed",
            "usage": {"cost": 0.01},
            "results": [
                {
                    "custom_id": request["custom_id"],
                    "response": {
                        "choices": [
                            {
                                "finish_reason": "stop",
                                "message": {
                                    "content": json.dumps({
                                        "verdict": "deny",
                                        "reason": "fake result",
                                        "safer_alternative": None,
                                    })
                                },
                            }
                        ]
                    },
                }
                for request in request_ids
            ],
        }


class _CompletedOnSubmitClient(_FakeBatchClient):
    async def request(
        self, method: str, path: str, payload: dict[str, object] | None = None
    ) -> dict[str, object]:
        if method == "POST":
            assert payload is not None
            self.submitted.append(payload)
            return {"id": "batch-1", "status": "completed"}
        return await super().request(method, path, payload)


class _UnknownStatusOnSubmitClient(_FakeBatchClient):
    async def request(
        self, method: str, path: str, payload: dict[str, object] | None = None
    ) -> dict[str, object]:
        if method == "POST":
            assert payload is not None
            self.submitted.append(payload)
            return {"id": "batch-1"}
        return await super().request(method, path, payload)


@pytest.fixture
def private_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    (root / ".review-eval-local").mkdir()
    monkeypatch.setattr(private_paths, "PROJECT_ROOT", root)
    return root / ".review-eval-local"


def test_prepare_chunks_and_private_artifacts(private_root: Path) -> None:
    manifest = prepare_batch(
        ["src/family_assistant/eval/tool_call_review/datasets/manual"],
        private_root / "runs" / "pilot",
        seeds=1,
        batch_size=7,
    )

    assert manifest.request_count == 19
    assert manifest.provider == "openrouter"
    assert manifest.api_base_url == "https://openrouter.ai/api"
    assert [len(chunk.request_ids) for chunk in manifest.chunks] == [7, 7, 5]
    assert (private_root / "runs/pilot/manifest.json").is_file()
    assert (private_root / "runs/pilot/requests.jsonl").is_file()


def test_prepare_refuses_zero_runnable_cases_before_artifacts(
    private_root: Path,
) -> None:
    empty_dataset = private_root / "empty-dataset"
    empty_dataset.mkdir()
    run_dir = private_root / "runs" / "empty"

    with pytest.raises(BatchError, match="No runnable cases"):
        prepare_batch([empty_dataset], run_dir)

    assert not run_dir.exists()


@pytest.mark.parametrize(
    ("option", "value"),
    [("--seeds", "0"), ("--batch-size", "0"), ("--max-tokens", "0")],
)
def test_prepare_dry_run_reuses_positive_parameter_validation(
    private_root: Path, option: str, value: str
) -> None:
    run_dir = private_root / "runs" / "dry-run"

    assert (
        batch_cli.main([
            "prepare",
            "--dataset",
            "src/family_assistant/eval/tool_call_review/datasets/manual",
            "--run-dir",
            str(run_dir),
            option,
            value,
            "--dry-run",
        ])
        == 1
    )
    assert not run_dir.exists()


def test_prepare_dry_run_reuses_zero_runnable_validation(
    private_root: Path,
) -> None:
    empty_dataset = private_root / "empty-dataset"
    empty_dataset.mkdir()
    run_dir = private_root / "runs" / "dry-run"

    assert (
        batch_cli.main([
            "prepare",
            "--dataset",
            str(empty_dataset),
            "--run-dir",
            str(run_dir),
            "--dry-run",
        ])
        == 1
    )
    assert not run_dir.exists()


def test_prepare_dry_run_validates_without_writing_artifacts(
    private_root: Path,
) -> None:
    run_dir = private_root / "runs" / "dry-run"

    assert (
        batch_cli.main([
            "prepare",
            "--dataset",
            "src/family_assistant/eval/tool_call_review/datasets/manual",
            "--run-dir",
            str(run_dir),
            "--seeds",
            "2",
            "--batch-size",
            "7",
            "--max-tokens",
            "256",
            "--dry-run",
        ])
        == 0
    )
    assert not run_dir.exists()


@pytest.mark.asyncio
async def test_status_rejects_unsubmitted_pending_chunks_without_network_call(
    private_root: Path,
) -> None:
    run_dir = private_root / "runs" / "pilot"
    prepare_batch(
        ["src/family_assistant/eval/tool_call_review/datasets/manual"], run_dir
    )
    fake = _FakeBatchClient()

    with pytest.raises(BatchError, match="unsubmitted pending chunks"):
        await update_batch_status(run_dir, api_key="test-only", client=fake)

    assert fake.submitted == []


@pytest.mark.asyncio
async def test_submit_status_harvest_is_resumable(private_root: Path) -> None:
    run_dir = private_root / "runs" / "pilot"
    prepare_batch(
        ["src/family_assistant/eval/tool_call_review/datasets/manual"],
        run_dir,
        batch_size=50,
    )
    fake = _FakeBatchClient()
    manifest = await submit_batch(
        run_dir,
        approved_spend_usd=1.0,
        approve_spend=True,
        api_key="test-only",
        client=fake,
    )
    assert all(chunk.batch_id for chunk in manifest.chunks)
    assert list(fake.submitted[0]) == ["endpoint", "model", "requests"]
    first_request = cast("list[dict[str, object]]", fake.submitted[0]["requests"])[0]
    first_body = cast("dict[str, object]", first_request["body"])
    assert first_body["provider"] == {"require_parameters": True}
    assert first_body["max_tokens"] == 512
    response_format = cast("dict[str, object]", first_body["response_format"])
    schema = cast("dict[str, object]", response_format["json_schema"])
    assert schema["strict"] is True
    await update_batch_status(run_dir, api_key="test-only", client=fake)
    report = harvest_batch(run_dir)

    assert len(report.trials) == 19
    assert all(trial.latency_ms is None for trial in report.trials)
    assert report.latency_source == "batch_api_unavailable"
    assert report.model_parameters == {
        "batch_mode": True,
        "max_tokens": 512,
        "provider_require_parameters": True,
        "response_format": "tool_call_review_response",
    }
    assert report.to_stamp_record()["latency_source"] == "batch_api_unavailable"
    assert "latency unavailable" in report.to_text_summary()


@pytest.mark.asyncio
async def test_completed_submit_without_results_is_fetched_by_status(
    private_root: Path,
) -> None:
    run_dir = private_root / "runs" / "pilot"
    prepare_batch(
        ["src/family_assistant/eval/tool_call_review/datasets/manual"], run_dir
    )
    fake = _CompletedOnSubmitClient()

    submitted = await submit_batch(
        run_dir,
        approved_spend_usd=1.0,
        approve_spend=True,
        api_key="test-only",
        client=fake,
    )
    assert submitted.chunks[0].status == "completed"
    assert submitted.chunks[0].result_file is None

    updated = await update_batch_status(run_dir, api_key="test-only", client=fake)

    assert updated.chunks[0].result_file == "batch-result-0.json"
    report = harvest_batch(run_dir)
    assert len(report.trials) == 19


@pytest.mark.asyncio
async def test_unknown_submission_with_batch_id_can_be_reconciled_by_status(
    private_root: Path,
) -> None:
    run_dir = private_root / "runs" / "pilot"
    prepare_batch(
        ["src/family_assistant/eval/tool_call_review/datasets/manual"], run_dir
    )
    fake = _UnknownStatusOnSubmitClient()

    with pytest.raises(BatchError, match="missing or unknown batch status"):
        await submit_batch(
            run_dir,
            approved_spend_usd=1.0,
            approve_spend=True,
            api_key="test-only",
            client=fake,
        )

    persisted = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert persisted["chunks"][0]["status"] == "submission_unknown"
    assert persisted["chunks"][0]["batch_id"] == "batch-1"

    updated = await update_batch_status(run_dir, api_key="test-only", client=fake)

    assert updated.chunks[0].status == "completed"
    assert updated.chunks[0].result_file == "batch-result-0.json"


@pytest.mark.asyncio
async def test_harvest_rejects_valid_json_with_truncated_finish_reason(
    private_root: Path,
) -> None:
    run_dir = private_root / "runs" / "pilot"
    prepare_batch(
        ["src/family_assistant/eval/tool_call_review/datasets/manual"], run_dir
    )
    fake = _FakeBatchClient()
    await submit_batch(
        run_dir,
        approved_spend_usd=1.0,
        approve_spend=True,
        api_key="test-only",
        client=fake,
    )
    await update_batch_status(run_dir, api_key="test-only", client=fake)

    result_path = run_dir / "batch-result-0.json"
    result = json.loads(result_path.read_text(encoding="utf-8"))
    first_choice = result["results"][0]["response"]["choices"][0]
    first_choice["finish_reason"] = "length"
    result_path.write_text(json.dumps(result), encoding="utf-8")

    with pytest.raises(BatchError, match="finish successfully|truncated"):
        harvest_batch(run_dir)


def test_submit_requires_explicit_spend_approval(private_root: Path) -> None:
    run_dir = private_root / "runs" / "pilot"
    prepare_batch(
        ["src/family_assistant/eval/tool_call_review/datasets/manual"], run_dir
    )

    with pytest.raises(BatchError, match="approve-spend"):
        asyncio.run(
            submit_batch(
                run_dir,
                approved_spend_usd=1.0,
                approve_spend=False,
                api_key="test-only",
                client=_FakeBatchClient(),
            )
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "amount", [0.0, -1.0, float("nan"), float("inf"), -float("inf")]
)
async def test_submit_rejects_nonfinite_or_nonpositive_spend_before_mutation(
    private_root: Path, amount: float
) -> None:
    run_dir = private_root / "runs" / "pilot"
    prepare_batch(
        ["src/family_assistant/eval/tool_call_review/datasets/manual"], run_dir
    )
    fake = _FakeBatchClient()

    with pytest.raises(BatchError, match="positive --approved-spend-usd"):
        await submit_batch(
            run_dir,
            approved_spend_usd=amount,
            approve_spend=True,
            api_key="test-only",
            client=fake,
        )

    assert fake.submitted == []
    manifest_json = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest_json["spend_approved"] is False
    assert manifest_json["approved_spend_usd"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [400, 401, 429])
async def test_http_rejection_leaves_chunk_pending_and_retryable(
    private_root: Path, status_code: int
) -> None:
    run_dir = private_root / "runs" / "pilot"
    prepare_batch(
        ["src/family_assistant/eval/tool_call_review/datasets/manual"], run_dir
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(status_code, json={"error": "request rejected"})

    transport = httpx.MockTransport(handler)
    http_client = httpx.AsyncClient(transport=transport)
    batch_client = BatchClient(
        "https://openrouter.ai/api", "test-only", client=http_client
    )
    try:
        with pytest.raises(BatchRejectedError, match=f"HTTP {status_code}"):
            await submit_batch(
                run_dir,
                approved_spend_usd=1.0,
                approve_spend=True,
                api_key="test-only",
                client=batch_client,
            )
    finally:
        await http_client.aclose()

    manifest_json = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    chunk = manifest_json["chunks"][0]
    assert chunk["status"] == "pending"
    assert chunk["batch_id"] is None
    assert chunk["error_code"] == "submission_rejected"

    retried = await submit_batch(
        run_dir,
        approved_spend_usd=1.0,
        approve_spend=True,
        api_key="test-only",
        client=_FakeBatchClient(),
    )
    assert retried.chunks[0].status == "validating"
    assert retried.chunks[0].error_code is None


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [408, 409, 500, 503])
async def test_ambiguous_http_submission_is_permanent(
    private_root: Path, status_code: int
) -> None:
    run_dir = private_root / "runs" / "pilot"
    prepare_batch(
        ["src/family_assistant/eval/tool_call_review/datasets/manual"], run_dir
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(status_code, json={"error": "ambiguous response"})

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    batch_client = BatchClient(
        "https://openrouter.ai/api", "test-only", client=http_client
    )
    try:
        with pytest.raises(BatchError, match="outcome is unknown"):
            await submit_batch(
                run_dir,
                approved_spend_usd=1.0,
                approve_spend=True,
                api_key="test-only",
                client=batch_client,
            )
    finally:
        await http_client.aclose()

    manifest_json = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest_json["chunks"][0]["status"] == "submission_unknown"
    assert manifest_json["chunks"][0]["error_code"] == "submission_unknown"

    retry_client = _FakeBatchClient()
    retried = await submit_batch(
        run_dir,
        approved_spend_usd=1.0,
        approve_spend=True,
        api_key="test-only",
        client=retry_client,
    )
    assert retried.chunks[0].status == "submission_unknown"
    assert retry_client.submitted == []

    status_client = _FakeBatchClient()
    unchanged = await update_batch_status(
        run_dir, api_key="test-only", client=status_client
    )
    assert unchanged.chunks[0].status == "submission_unknown"
    assert status_client.submitted == []


@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf", "-inf"])
def test_cli_rejects_invalid_approved_spend(value: str) -> None:
    with pytest.raises(SystemExit):
        batch_cli._parser().parse_args([
            "submit",
            "--run-dir",
            "run",
            "--approved-spend-usd",
            value,
        ])


class _UnknownOutcomeClient(_FakeBatchClient):
    async def request(
        self, method: str, path: str, payload: dict[str, object] | None = None
    ) -> dict[str, object]:
        self.submitted.append(payload or {})
        raise httpx.ReadTimeout("test timeout")


class _MalformedStatusClient(_FakeBatchClient):
    def __init__(self, status: object) -> None:
        super().__init__()
        self.status = status

    async def request(
        self, method: str, path: str, payload: dict[str, object] | None = None
    ) -> dict[str, object]:
        if method == "POST":
            return {"id": "batch-1", "status": self.status}
        assert method == "GET"
        return {"id": path.rsplit("/", 1)[1], "status": self.status}


class _MismatchedBatchIdClient(_FakeBatchClient):
    def __init__(self, response_id: object) -> None:
        super().__init__()
        self.response_id = response_id

    async def request(
        self, method: str, path: str, payload: dict[str, object] | None = None
    ) -> dict[str, object]:
        result = await super().request(method, path, payload)
        if method == "GET":
            if self.response_id is None:
                result.pop("id", None)
            else:
                result["id"] = self.response_id
        return result


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status", [None, "submitted", "not-a-status", {"state": "done"}]
)
async def test_batch_status_must_be_documented_and_well_formed(
    private_root: Path, status: object
) -> None:
    run_dir = private_root / "runs" / "pilot"
    prepare_batch(
        ["src/family_assistant/eval/tool_call_review/datasets/manual"], run_dir
    )

    with pytest.raises(BatchError, match="missing or unknown batch status"):
        await submit_batch(
            run_dir,
            approved_spend_usd=1.0,
            approve_spend=True,
            api_key="test-only",
            client=_MalformedStatusClient(status),
        )

    manifest_json = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest_json["chunks"][0]["status"] == "submission_unknown"
    assert manifest_json["chunks"][0]["error_code"] == "invalid_status"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status", [None, "submitted", "not-a-status", {"state": "done"}]
)
async def test_status_poll_rejects_malformed_remote_lifecycle_status(
    private_root: Path, status: object
) -> None:
    run_dir = private_root / "runs" / "pilot"
    prepare_batch(
        ["src/family_assistant/eval/tool_call_review/datasets/manual"], run_dir
    )
    submit_client = _FakeBatchClient()
    await submit_batch(
        run_dir,
        approved_spend_usd=1.0,
        approve_spend=True,
        api_key="test-only",
        client=submit_client,
    )
    malformed_client = _MalformedStatusClient(status)
    malformed_client.submitted = submit_client.submitted

    with pytest.raises(BatchError, match="missing or unknown batch status"):
        await update_batch_status(run_dir, api_key="test-only", client=malformed_client)

    persisted = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert persisted["chunks"][0]["status"] == "validating"
    assert persisted["chunks"][0]["status"] != "in_progress"


@pytest.mark.asyncio
async def test_status_poll_accepts_cancelling_lifecycle_status(
    private_root: Path,
) -> None:
    run_dir = private_root / "runs" / "pilot"
    prepare_batch(
        ["src/family_assistant/eval/tool_call_review/datasets/manual"], run_dir
    )
    submit_client = _FakeBatchClient()
    await submit_batch(
        run_dir,
        approved_spend_usd=1.0,
        approve_spend=True,
        api_key="test-only",
        client=submit_client,
    )
    cancelling_client = _MalformedStatusClient("cancelling")
    cancelling_client.submitted = submit_client.submitted

    manifest = await update_batch_status(
        run_dir, api_key="test-only", client=cancelling_client
    )

    assert manifest.chunks[0].status == "cancelling"


@pytest.mark.asyncio
@pytest.mark.parametrize("response_id", [None, "batch-other"])
async def test_status_poll_rejects_a_mismatched_batch_id_without_manifest_mutation(
    private_root: Path, response_id: object
) -> None:
    run_dir = private_root / "runs" / "pilot"
    prepare_batch(
        ["src/family_assistant/eval/tool_call_review/datasets/manual"], run_dir
    )
    submit_client = _FakeBatchClient()
    await submit_batch(
        run_dir,
        approved_spend_usd=1.0,
        approve_spend=True,
        api_key="test-only",
        client=submit_client,
    )
    mismatched_client = _MismatchedBatchIdClient(response_id)
    mismatched_client.submitted = submit_client.submitted

    with pytest.raises(BatchError, match="id does not match"):
        await update_batch_status(
            run_dir, api_key="test-only", client=mismatched_client
        )

    persisted = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert persisted["chunks"][0]["status"] == "validating"
    assert persisted["chunks"][0]["usage"] is None
    assert persisted["chunks"][0]["result_file"] is None
    assert not (run_dir / "batch-result-0.json").exists()


@pytest.mark.parametrize(
    "option,value",
    [
        ("--interval-seconds", "0"),
        ("--interval-seconds", "nan"),
        ("--interval-seconds", "inf"),
        ("--max-wait-seconds", "-1"),
        ("--max-wait-seconds", "nan"),
        ("--max-wait-seconds", "inf"),
    ],
)
def test_poll_timing_arguments_must_be_finite_and_in_range(
    option: str, value: str
) -> None:
    with pytest.raises(SystemExit):
        batch_cli._parser().parse_args(["poll", "--run-dir", "run", option, value])


@pytest.mark.asyncio
async def test_ambiguous_submission_is_recorded_and_not_retried(
    private_root: Path,
) -> None:
    run_dir = private_root / "runs" / "pilot"
    prepare_batch(
        ["src/family_assistant/eval/tool_call_review/datasets/manual"], run_dir
    )
    fake = _UnknownOutcomeClient()

    with pytest.raises(BatchError, match="outcome is unknown"):
        await submit_batch(
            run_dir,
            approved_spend_usd=1.0,
            approve_spend=True,
            api_key="test-only",
            client=fake,
        )

    assert len(fake.submitted) == 1
    manifest_json = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest_json["chunks"][0]["status"] == "submission_unknown"


@pytest.mark.asyncio
async def test_harvest_rejects_missing_result(private_root: Path) -> None:
    run_dir = private_root / "runs" / "pilot"
    prepare_batch(
        ["src/family_assistant/eval/tool_call_review/datasets/manual"], run_dir
    )
    fake = _FakeBatchClient()
    await submit_batch(
        run_dir,
        approved_spend_usd=1.0,
        approve_spend=True,
        api_key="test-only",
        client=fake,
    )
    await update_batch_status(run_dir, api_key="test-only", client=fake)
    result_path = run_dir / "batch-result-0.json"
    payload = json.loads(result_path.read_text(encoding="utf-8"))
    payload["results"].pop()
    result_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(BatchError, match="exactly match"):
        harvest_batch(run_dir)


@pytest.mark.asyncio
async def test_request_artifact_tampering_fails_before_submit(
    private_root: Path,
) -> None:
    run_dir = private_root / "runs" / "pilot"
    prepare_batch(
        ["src/family_assistant/eval/tool_call_review/datasets/manual"], run_dir
    )
    request_path = run_dir / "requests.jsonl"
    request_path.write_text(
        request_path.read_text(encoding="utf-8") + "\n", encoding="utf-8"
    )

    with pytest.raises(BatchError, match="digest"):
        await submit_batch(
            run_dir,
            approved_spend_usd=1.0,
            approve_spend=True,
            api_key="test-only",
            client=_FakeBatchClient(),
        )


def test_legacy_latency_stats_infer_available_count() -> None:
    stats = LatencyStats.model_validate({
        "count": 3,
        "min_ms": 1,
        "median_ms": 2,
        "p95_ms": 3,
        "max_ms": 3,
    })
    assert stats.available_count == 3
    assert stats.unavailable_count == 0
