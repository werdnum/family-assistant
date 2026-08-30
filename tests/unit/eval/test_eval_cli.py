"""End-to-end tests for the two modes of the review-eval CLI.

The script is loaded by path: ``scripts/`` is not an importable package, and the
entry point is what the maintainer actually runs, so exercising ``main()``
covers the argument surface and the exit codes rather than the library alone.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest
import yaml

import family_assistant.eval.tool_call_review as tool_call_review_eval
from family_assistant.eval import private_paths
from family_assistant.eval.tool_call_review.registry_snapshot import (
    descriptors_to_snapshot,
)
from family_assistant.services.tool_call_review import (
    ToolCallReviewResponse,
    ToolCallReviewVerdict,
)
from family_assistant.tools import LOCAL_TOOL_DESCRIPTORS

# pylint cannot resolve the tests namespace package, so it reports a
# false no-name-in-module here only.
from tests.mocks.mock_llm import (  # pylint: disable=no-name-in-module
    RuleBasedMockLLMClient,
)

if TYPE_CHECKING:
    from types import ModuleType

    from family_assistant.llm import LLMInterface

pytestmark = pytest.mark.no_db


def _repo_root() -> Path:
    return Path(tool_call_review_eval.__file__).parents[3].parent


_SCRIPT_PATH = _repo_root() / "scripts" / "tool_call_review_eval.py"


def _load_cli() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "tool_call_review_eval_cli", _SCRIPT_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def cli() -> ModuleType:
    return _load_cli()


def _dataset_dir(name: str) -> str:
    return str(Path(tool_call_review_eval.__file__).parent / "datasets" / name)


def _write_config(tmp_path: Path, block: object) -> Path:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump({"tool_call_review": block}), encoding="utf-8"
    )
    return config_path


def _install_fake_judge(
    monkeypatch: pytest.MonkeyPatch, verdict: ToolCallReviewVerdict
) -> None:
    """Point the judge factory at a mock that returns one fixed verdict."""
    mock = RuleBasedMockLLMClient(
        rules=[],
        structured_rules=[
            (
                lambda _args: True,
                ToolCallReviewResponse(verdict=verdict, reason="scripted verdict"),
            )
        ],
    )

    def _create_client(_config: dict[str, object]) -> LLMInterface:
        return cast("LLMInterface", mock)

    monkeypatch.setattr(
        "family_assistant.eval.tool_call_review.runner.LLMClientFactory.create_client",
        _create_client,
    )


def test_dry_run_validates_the_manual_dataset_without_a_judge(
    cli: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["--dataset", _dataset_dir("manual"), "--dry-run"]) == 0
    output = capsys.readouterr().out
    assert "dataset_hash=" in output
    assert "manual-dgsm-attack-delegate [conversation/attack] source=manual" in output


def test_report_mode_runs_end_to_end_against_a_fake_judge(
    cli: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _install_fake_judge(monkeypatch, ToolCallReviewVerdict.DENY)
    exit_code = cli.main([
        "--dataset",
        _dataset_dir("examples"),
        "--seeds",
        "2",
        "--provider",
        "mock",
        "--model",
        "mock-judge",
    ])
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "By source dataset:" in output
    assert "By attack class:" in output
    assert "deduplicated clean attack cases" in output
    assert "No observed attack allows." in output


def test_report_mode_prints_the_judge_reason_for_every_weak_pass(
    cli: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Reasons are the iteration loop's whole point, so they are never withheld:
    # confirming an attack is a weak pass, and its reason is printed with it.
    _install_fake_judge(monkeypatch, ToolCallReviewVerdict.CONFIRM)
    cli.main(["--dataset", _dataset_dir("manual"), "--seeds", "1"])
    output = capsys.readouterr().out
    assert "Failing, weak-pass and expectation-miss reasons:" in output
    assert "[weak_pass] manual-dgsm-attack-delegate -> confirm: scripted verdict" in (
        output
    )


def test_observed_attack_allow_exits_nonzero(
    cli: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _install_fake_judge(monkeypatch, ToolCallReviewVerdict.ALLOW)
    exit_code = cli.main(["--dataset", _dataset_dir("manual"), "--seeds", "1"])
    assert exit_code == 1
    assert "attack trial(s) were allowed" in capsys.readouterr().out


def test_stamp_mode_writes_one_record(
    cli: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _install_fake_judge(monkeypatch, ToolCallReviewVerdict.DENY)
    out_path = tmp_path / "runs" / "stamp.json"
    exit_code = cli.main([
        "--dataset",
        _dataset_dir("manual"),
        "--mode",
        "stamp",
        "--seeds",
        "1",
        "--provider",
        "mock",
        "--model",
        "mock-judge",
        "--out",
        str(out_path),
    ])
    assert exit_code == 0

    record = json.loads(out_path.read_text(encoding="utf-8"))
    assert record["judge"] == {
        "provider": "mock",
        "model": "mock-judge",
        # Free-form operator config is digested, not printed; None survives as
        # None so "not supplied" stays distinct from "supplied and empty".
        "model_parameters_digest": None,
        "retry_config": None,
        "timeout_seconds": 30.0,
        # An explicit --provider/--model run replays each case's stored
        # guidance, so there is no deployment guidance to digest.
        "deployment_guidance_digest": None,
    }
    assert record["dataset_hash"]
    assert record["date"]
    assert record["observed_allows"] == 0
    assert record["clean_attack_cases"] > 0
    assert record["supported_bound"] == pytest.approx(
        3.0 / record["clean_attack_cases"]
    )
    assert record["by_attack_class"]
    assert "overall" in record["slice_bounds"]


def test_report_mode_out_outside_the_private_tree_is_refused(
    cli: ModuleType, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    # A report record holds the judge's reason for every trial, which quotes the
    # reviewed content, so a mistyped --out must not be able to drop household
    # -derived text into a tracked location.
    exit_code = cli.main([
        "--dataset",
        _dataset_dir("manual"),
        "--seeds",
        "1",
        "--out",
        str(tmp_path / "report.json"),
    ])
    assert exit_code == 1
    assert ".review-eval-local" in capsys.readouterr().err
    assert not (tmp_path / "report.json").exists()


def test_report_mode_out_inside_the_private_tree_is_written(
    cli: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(private_paths, "PROJECT_ROOT", tmp_path)
    _install_fake_judge(monkeypatch, ToolCallReviewVerdict.DENY)
    out_path = tmp_path / ".review-eval-local" / "runs" / "report.json"
    exit_code = cli.main([
        "--dataset",
        _dataset_dir("manual"),
        "--seeds",
        "1",
        "--out",
        str(out_path),
    ])
    assert exit_code == 0
    record = json.loads(out_path.read_text(encoding="utf-8"))
    assert record["trials"]
    assert record["trials"][0]["reason"] == "scripted verdict"


def test_report_mode_out_override_allows_an_external_private_location(
    cli: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The deliberate escape hatch, for a mounted private store the containment
    # rule cannot know about.
    _install_fake_judge(monkeypatch, ToolCallReviewVerdict.DENY)
    out_path = tmp_path / "mounted" / "report.json"
    exit_code = cli.main([
        "--dataset",
        _dataset_dir("manual"),
        "--seeds",
        "1",
        "--out",
        str(out_path),
        "--allow-external-out",
    ])
    assert exit_code == 0
    assert json.loads(out_path.read_text(encoding="utf-8"))["trials"]


def test_stamp_mode_out_is_writable_anywhere(
    cli: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The stamp is a committed artifact by design and carries slice numbers, not
    # per-trial reasons, so the private-tree rule does not apply to it.
    _install_fake_judge(monkeypatch, ToolCallReviewVerdict.DENY)
    out_path = tmp_path / "stamp.json"
    exit_code = cli.main([
        "--dataset",
        _dataset_dir("manual"),
        "--mode",
        "stamp",
        "--seeds",
        "1",
        "--out",
        str(out_path),
    ])
    assert exit_code == 0
    record = json.loads(out_path.read_text(encoding="utf-8"))
    assert record["scored_trials"]
    assert record["overall"]
    # No judge reason reaches the stamp: the only "reason" fields it carries are
    # the harness's own statistical notes on each slice's bound.
    assert "scripted verdict" not in json.dumps(record)


def test_stamp_mode_without_out_is_refused(
    cli: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = cli.main([
        "--dataset",
        _dataset_dir("manual"),
        "--mode",
        "stamp",
    ])
    assert exit_code == 1
    assert "--out" in capsys.readouterr().err


def test_config_file_resolves_the_judge_through_the_layered_loader(
    cli: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # defaults.yaml ships llm_parameters an operator file does not repeat, so
    # reading that one file would measure a differently-parameterised fallback
    # judge than the deployment actually runs.
    monkeypatch.chdir(_repo_root())
    config_path = _write_config(
        tmp_path,
        {
            "model": "gemini-3.7-flash",
            "retry_config": {
                "fallback": {"provider": "openai", "model": "gpt-5.6-terra"}
            },
        },
    )

    args = cli._parse_args([
        "--dataset",
        _dataset_dir("manual"),
        "--config-file",
        str(config_path),
    ])
    judge = cli._resolve_judge_config(args)

    assert judge.model == "gemini-3.7-flash"
    assert judge.retry_config is not None
    assert judge.retry_config["fallback"]["model"] == "gpt-5.6-terra"
    assert judge.model_parameters is not None
    assert "gpt-5.6-terra" in judge.model_parameters


def test_config_file_with_the_reviewer_disabled_is_refused(
    cli: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    # A deployment with the reviewer off constructs no judge, so a run stamped
    # with its provider and model would describe a judge nothing there runs.
    monkeypatch.chdir(_repo_root())
    config_path = _write_config(tmp_path, {"enabled": False})

    exit_code = cli.main([
        "--dataset",
        _dataset_dir("manual"),
        "--config-file",
        str(config_path),
        "--dry-run",
    ])

    assert exit_code == 1
    error = capsys.readouterr().err
    assert "tool_call_review.enabled: false" in error
    assert "--provider/--model" in error


def test_config_file_with_no_reviewer_block_is_refused(
    cli: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    # Substituting a fresh default reviewer would stamp a configuration this
    # deployment never assembled, which is a different claim from a disabled one.
    monkeypatch.chdir(_repo_root())
    config_path = _write_config(tmp_path, None)

    exit_code = cli.main([
        "--dataset",
        _dataset_dir("manual"),
        "--config-file",
        str(config_path),
        "--dry-run",
    ])

    assert exit_code == 1
    error = capsys.readouterr().err
    assert "no tool_call_review configuration" in error


def test_config_file_that_does_not_exist_is_refused(
    cli: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    # load_config treats both of its files as optional, so a typo would resolve
    # to the shipped defaults — which enable a reviewer — and the run would pay
    # for every trial before stamping a deployment it never read.
    monkeypatch.chdir(_repo_root())
    missing = tmp_path / "typo.yaml"

    exit_code = cli.main([
        "--dataset",
        _dataset_dir("manual"),
        "--config-file",
        str(missing),
        "--dry-run",
    ])

    assert exit_code == 1
    error = capsys.readouterr().err
    assert "is not a file" in error


def test_explicit_provider_and_model_need_no_config_file(cli: ModuleType) -> None:
    # Measuring an arbitrary candidate judge is deliberate and stays available:
    # the deployment-replay restriction is about what --config-file claims.
    args = cli._parse_args([
        "--dataset",
        _dataset_dir("manual"),
        "--provider",
        "openai",
        "--model",
        "gpt-5.6-terra",
    ])

    judge = cli._resolve_judge_config(args)

    assert judge.provider == "openai"
    assert judge.model == "gpt-5.6-terra"
    assert judge.retry_config is None


@pytest.mark.parametrize("ceiling", ["0", "-0.1", "1.5", "nan"])
def test_out_of_range_ceiling_is_refused_before_any_trial(
    cli: ModuleType, ceiling: str
) -> None:
    # required_clean_cases would reject the value while rendering the report,
    # after every trial had been paid for. A typo must not cost a whole run.
    exit_code = cli.main([
        "--dataset",
        _dataset_dir("manual"),
        "--seeds",
        "1",
        "--ceiling",
        ceiling,
    ])

    assert exit_code == 1


def test_the_snapshot_governs_case_loading_not_only_execution(
    cli: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Load and run must see the same registry, or validation is skipped.

    `load_cases` validates each case's arguments against its resolved tool and
    reconstructs its reviewer input; a case whose tool the registry cannot
    resolve is loaded unvalidated so one missing tool does not take the dataset
    down. Give the runner a snapshot but not the loader and those two rules
    combine badly: every MCP case loads unvalidated and then executes under the
    snapshot, so schema-invalid arguments run as clean trials.
    """
    snapshot = tmp_path / "registry.json"
    snapshot.write_text(
        json.dumps(descriptors_to_snapshot(LOCAL_TOOL_DESCRIPTORS)), encoding="utf-8"
    )
    seen: dict[str, object] = {}

    def _capture(paths: object, *, descriptor_registry: object = None) -> list[object]:
        seen["registry"] = descriptor_registry
        return []

    monkeypatch.setattr(cli, "load_cases", _capture, raising=True)
    cli.main([
        "--dataset",
        _dataset_dir("manual"),
        "--tool-registry",
        str(snapshot),
        "--dry-run",
    ])

    assert seen["registry"] is not None
    assert "add_calendar_event" in cast("dict[str, object]", seen["registry"])
