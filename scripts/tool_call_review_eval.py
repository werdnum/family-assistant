#!/usr/bin/env python3
"""Replay the tool-call reviewer over labeled cases and report per-slice scores.

The harness drives the real ``ToolCallReviewer`` — real prompt assembly, real
provider, real model — over stored cases and reports asymmetric, expected-aware
error rates per slice. Cases are serialized reviewer inputs, so this always
measures the current system.

Two modes, and nothing else:

``report`` (default)
    Run the datasets and print everything: per-slice attack allow/confirm rates,
    benign friction, expectation misses, seed flips, fallback counts, latency,
    and the judge's reason for every failing and weak-pass trial. This is the
    iteration loop — run it, read the reasons, change the prompt, run it again.
    ``--out`` writes the whole run, reasons included, so it resolves inside the
    private ``.review-eval-local/`` tree unless ``--allow-external-out`` names
    a deliberately private location elsewhere.

``stamp``
    The same run, plus one JSON record of what was measured and under what
    configuration (judge, dataset digest, date, per-slice numbers, supported
    bound), written to ``--out``. A record, not a permission: nothing consults
    it, and no run is refused because of it.

Either mode exits nonzero when the judge allowed an attack.

A ``--dataset`` directory holds cases and nothing else. The private
``.review-eval-local/`` tree is not one directory but several, one per artifact
kind with its own consumer — ``public/<corpus>/`` holds cases and is what this
command scans, while ``templates/`` (history-derived task templates) and
``runs/`` (this harness's own output) are read by other steps and scanning them
aborts the load.

Usage:

    # Load and validate cases only, no network:
    python scripts/tool_call_review_eval.py \
        --dataset src/family_assistant/eval/tool_call_review/datasets/manual \
        --dry-run

    # Iterate against a judge:
    python scripts/tool_call_review_eval.py \
        --dataset src/family_assistant/eval/tool_call_review/datasets/manual \
        --dataset .review-eval-local/public/deepset \
        --provider google --model gemini-3.7-flash --seeds 5

    # Record what the deployed judge scored today:
    python scripts/tool_call_review_eval.py \
        --dataset src/family_assistant/eval/tool_call_review/datasets/manual \
        --config-file config.yaml --mode stamp \
        --out .review-eval-local/runs/stamp.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from family_assistant.config_loader import DEFAULT_DEFAULTS_FILE, load_config
from family_assistant.eval.private_paths import (
    PRIVATE_EVAL_DIR_NAME,
    PrivateEvalPathError,
    anchor_private_eval_path,
    resolve_private_eval_path,
)
from family_assistant.eval.tool_call_review import (
    DEFAULT_FALSE_ALLOW_CEILING,
    EvalReport,
    build_reviewer,
    case_skip_reason,
    content_hash,
    load_cases,
    run_eval,
)

if TYPE_CHECKING:
    from collections.abc import Mapping


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dataset",
        action="append",
        required=True,
        metavar="PATH",
        help=(
            "Case file, or a directory holding case files and nothing else "
            "(repeatable). In the private tree that is a specific artifact "
            f"directory — {PRIVATE_EVAL_DIR_NAME}/public/<corpus> — not the tree "
            "root, which also holds templates and run records."
        ),
    )
    parser.add_argument(
        "--mode",
        choices=("report", "stamp"),
        default="report",
        help=(
            "report (default): print every number and reason. stamp: the same "
            "run plus one JSON record of what was measured, written to --out."
        ),
    )
    parser.add_argument(
        "--provider",
        default="google",
        help="Judge provider (default: google). Ignored when --config-file is given.",
    )
    parser.add_argument(
        "--model",
        default="gemini-3.7-flash",
        help=(
            "Judge model (default: gemini-3.7-flash). Ignored when --config-file "
            "is given."
        ),
    )
    parser.add_argument(
        "--config-file",
        type=Path,
        default=None,
        metavar="YAML",
        help=(
            "A deployment's operator config YAML. It is resolved through the same "
            "layered loader the runtime uses (defaults.yaml, then this file, then "
            "environment overrides), so the judge measured here is the judge "
            "production runs — including a primary/fallback retry client and the "
            "shipped llm_parameters a single --provider/--model cannot express. "
            "Run from the repository root so defaults.yaml resolves. Overrides "
            "--provider/--model/--timeout-seconds, and supplies --llm-params "
            "unless that flag is given. A deployment whose reviewer is disabled "
            "or unconfigured is refused: it runs no judge for a measurement to "
            "describe. Use --provider/--model to measure an arbitrary judge."
        ),
    )
    parser.add_argument(
        "--llm-params",
        default=None,
        metavar="JSON",
        help=(
            "JSON object of model parameters for the judge client, mirroring the "
            "deployment's llm_parameters pattern map (e.g. "
            '\'{"gemini-3.7-flash": {"temperature": 0}}\'). Recorded in the '
            "result so a stamp states the configuration it measured."
        ),
    )
    parser.add_argument(
        "--seeds",
        type=int,
        default=5,
        help="Runs per case (default: 5).",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=30.0,
        help="Per-review timeout for the judge (default: 30).",
    )
    parser.add_argument(
        "--ceiling",
        type=float,
        default=DEFAULT_FALSE_ALLOW_CEILING,
        help=(
            "False-allow ceiling the per-slice bounds are reported against "
            f"(default: {DEFAULT_FALSE_ALLOW_CEILING}). Reported for a human to "
            "read; only an observed allow fails a run."
        ),
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help=(
            "Where to write JSON: the full run record in report mode, the stamp "
            "record in stamp mode (required there). A report-mode record holds "
            "the judge's reason for every trial, which quotes the reviewed "
            f"content, so it must land inside the {PRIVATE_EVAL_DIR_NAME}/ tree "
            "unless --allow-external-out says otherwise. A relative path anchors "
            "at the repository root, not the working directory."
        ),
    )
    parser.add_argument(
        "--allow-external-out",
        action="store_true",
        help=(
            "Write the report-mode run record to an explicitly private location "
            f"outside {PRIVATE_EVAL_DIR_NAME}/ (a mounted private volume). The "
            "stamp record carries slice numbers rather than reviewed content and "
            "is writable anywhere without this."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Load and validate cases only; do not call the judge.",
    )
    return parser.parse_args(argv)


def _parse_llm_params(raw: str | None) -> dict[str, object] | None:
    if raw is None:
        return None
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise ValueError("--llm-params must be a JSON object of model parameters.")
    return parsed


class _DeploymentJudgeError(RuntimeError):
    """The deployment named by ``--config-file`` runs no reviewer to replay."""


@dataclass(frozen=True, slots=True)
class _JudgeConfig:
    """The effective judge configuration a run measures under."""

    provider: str | None
    model: str
    timeout_seconds: float
    model_parameters: Mapping[str, object] | None
    retry_config: dict[str, object] | None
    # None means "leave each case's stored guidance alone", which is what an
    # explicit --provider/--model run wants. A deployment replay always carries
    # a value, empty string included: production injects review_config.guidance
    # into every review input, so replaying stored guidance would measure a
    # prompt the deployment does not send.
    deployment_guidance: str | None


def _resolve_judge_config(args: argparse.Namespace) -> _JudgeConfig:
    """Resolve the judge from CLI flags, or a deployment config when supplied.

    ``--config-file`` goes through :func:`load_config`, not a bare YAML read:
    production deep-merges ``defaults.yaml``, the operator file, and environment
    overrides, so reading one file would measure a judge assembled differently
    from the deployed one — most visibly by dropping the shipped
    ``llm_parameters`` a configured fallback leg relies on.

    That flag claims the run measures *this deployment's* judge, so a deployment
    that builds no reviewer — the block disabled, or absent altogether once the
    operator file sets it to null or no defaults file supplies one — is refused
    rather than replayed against a substituted default. Measuring a judge no
    deployment runs is what ``--provider``/``--model`` are for.

    The path's existence is checked here rather than left to :func:`load_config`,
    which treats *both* of its files as optional and silently drops one that is
    absent. A typo or a deleted file would otherwise resolve to the shipped
    defaults, which enable a reviewer — so the run would pay for every trial and
    issue a stamp naming a deployment it never read.
    """
    model_parameters: Mapping[str, object] | None = _parse_llm_params(args.llm_params)
    if args.config_file is None:
        return _JudgeConfig(
            provider=args.provider,
            model=args.model,
            timeout_seconds=args.timeout_seconds,
            model_parameters=model_parameters,
            retry_config=None,
            deployment_guidance=None,
        )
    if not Path(args.config_file).is_file():
        raise _DeploymentJudgeError(
            f"{args.config_file} is not a file, so no deployment configuration "
            "was read."
        )
    app_config = load_config(
        defaults_file_path=DEFAULT_DEFAULTS_FILE,
        config_file_path=str(args.config_file),
    )
    review_cfg = app_config.tool_call_review
    if review_cfg is None:
        raise _DeploymentJudgeError(
            f"{args.config_file} resolves to no tool_call_review configuration "
            "at all, so this deployment builds no reviewer."
        )
    if not review_cfg.enabled:
        raise _DeploymentJudgeError(
            f"{args.config_file} sets tool_call_review.enabled: false, so this "
            "deployment builds no reviewer."
        )
    retry_config = (
        review_cfg.retry_config.model_dump(exclude_none=True)
        if review_cfg.retry_config is not None
        else None
    )
    if model_parameters is None and app_config.llm_parameters:
        model_parameters = app_config.llm_parameters
    return _JudgeConfig(
        provider=review_cfg.provider,
        model=review_cfg.model,
        timeout_seconds=review_cfg.timeout_seconds,
        model_parameters=model_parameters,
        retry_config=retry_config,
        deployment_guidance=review_cfg.guidance,
    )


def _resolve_out_path(args: argparse.Namespace) -> Path | None:
    """Resolve ``--out``, containing a report-mode record in the private tree.

    A report record holds every trial's reason, which quotes the reviewed
    messages, arguments and identifiers of whatever the case carried, so it is
    household-derived material and resolves through the same containment rule
    the history export uses. The stamp record keeps its own policy: it is a
    committed artifact by design and states slice numbers, not reasons.
    """
    if args.out is None or args.mode == "stamp":
        return args.out
    if args.allow_external_out:
        return anchor_private_eval_path(args.out)
    return resolve_private_eval_path(args.out)


async def _run(args: argparse.Namespace) -> int:
    if args.mode == "stamp" and args.out is None:
        print("--mode stamp requires --out to write the stamp record.", file=sys.stderr)
        return 1
    # Checked before the judge is built: required_clean_cases would otherwise
    # reject the value while rendering the report, after every trial has been
    # paid for.
    if not 0.0 < args.ceiling <= 1.0:
        print(
            f"--ceiling must be a rate in (0, 1]; got {args.ceiling}.",
            file=sys.stderr,
        )
        return 1
    try:
        out_path = _resolve_out_path(args)
    except PrivateEvalPathError as exc:
        print(
            f"Refusing to write the full run record: --out {exc}. Set "
            "--allow-external-out to write it to an explicitly private location "
            "elsewhere.",
            file=sys.stderr,
        )
        return 1

    try:
        judge = _resolve_judge_config(args)
    except _DeploymentJudgeError as exc:
        print(
            f"Refusing to issue a measurement for this deployment: {exc} A run "
            "under --config-file states the judge that deployment serves; use "
            "--provider/--model to measure an arbitrary judge instead.",
            file=sys.stderr,
        )
        return 1
    cases = load_cases(args.dataset)
    if not cases:
        print("No cases found in the supplied datasets.", file=sys.stderr)
        return 1
    dataset_digest = content_hash(cases)
    print(f"Loaded {len(cases)} case(s); dataset_hash={dataset_digest}")

    if args.dry_run:
        for case in cases:
            skip_reason = case_skip_reason(case)
            suffix = f" SKIPPED: {skip_reason}" if skip_reason is not None else ""
            print(
                f"  {case.id} [{case.boundary}/{case.label}] "
                f"source={case.source}{suffix}"
            )
        return 0

    reviewer = build_reviewer(
        judge.provider,
        judge.model,
        timeout_seconds=judge.timeout_seconds,
        model_parameters=judge.model_parameters,
        retry_config=judge.retry_config,
    )
    try:
        report = await run_eval(
            cases,
            reviewer,
            seeds=args.seeds,
            provider=judge.provider,
            model=judge.model,
            model_parameters=judge.model_parameters,
            retry_config=judge.retry_config,
            timeout_seconds=judge.timeout_seconds,
            deployment_guidance=judge.deployment_guidance,
            dataset_hash=dataset_digest,
        )
    finally:
        await reviewer.close()

    print()
    print(report.to_text_summary(ceiling=args.ceiling))
    _write_output(report, args, out_path)
    return 1 if report.observed_allows() else 0


def _write_output(
    report: EvalReport, args: argparse.Namespace, out_path: Path | None
) -> None:
    if out_path is None:
        return
    payload = (
        report.to_stamp_record(ceiling=args.ceiling)
        if args.mode == "stamp"
        else report.to_json_dict()
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    label = "stamp record" if args.mode == "stamp" else "run record"
    print(f"\nWrote {label} to {out_path}")


def main(argv: list[str] | None = None) -> int:
    """Entry point returning a process exit code."""
    args = _parse_args(argv)
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
