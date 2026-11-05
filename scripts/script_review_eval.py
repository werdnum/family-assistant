#!/usr/bin/env python3
"""Run the program-review cases through the production tool-call reviewer.

Measures how the reviewer decides ``execute_script`` programs, including the
``allow_watched`` outcome of docs/design/script-execution-review-boundary.md:
see ``family_assistant.eval.tool_call_review.script_review_eval``. Prints totals
and a per-case table as Markdown, and with ``--out`` writes every trial, reasons
included, as JSON.

Usage:

    # Validate and render the cases, no network:
    python scripts/script_review_eval.py --dry-run

    # Measure the deployed judge model:
    python scripts/script_review_eval.py \
        --provider anthropic --model claude-haiku-5-5 --seeds 10 \
        --out .review-eval-local/runs/script-review.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
from pathlib import Path

from family_assistant.eval.private_paths import (
    PRIVATE_EVAL_DIR_NAME,
    PrivateEvalPathError,
    anchor_private_eval_path,
    resolve_private_eval_path,
)
from family_assistant.eval.tool_call_review.registry_snapshot import registry_digest
from family_assistant.eval.tool_call_review.script_review_eval import (
    TrialExecutionError,
    cases_digest,
    default_cases_dir,
    eval_registry,
    load_script_review_cases,
    render_markdown_summary,
    review_request,
    run_script_review_eval,
    validate_case_scripts,
)
from family_assistant.llm.factory import LLMClientFactory
from family_assistant.services.tool_call_review import (
    assemble_tool_call_review_messages,
    review_prompt_revision,
)


def _positive_int(raw: str) -> int:
    value = int(raw)
    if value < 1:
        raise argparse.ArgumentTypeError(f"must be at least 1, got {value}")
    return value


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, default=default_cases_dir())
    parser.add_argument("--provider", default="anthropic")
    parser.add_argument("--model", default="claude-haiku-5-5")
    parser.add_argument("--seeds", type=_positive_int, default=5)
    parser.add_argument("--concurrency", type=_positive_int, default=4)
    parser.add_argument(
        "--case",
        action="append",
        default=None,
        metavar="ID",
        help="Run only this case id (repeatable).",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help=(
            "Write every trial, reasons included, as JSON. Reasons quote the "
            f"reviewed content, so the path must resolve inside {PRIVATE_EVAL_DIR_NAME}/ "
            "unless --allow-external-out is given. A relative path anchors at the "
            "repository root."
        ),
    )
    parser.add_argument(
        "--allow-external-out",
        action="store_true",
        help=(
            f"Write --out to an explicitly private location outside {PRIVATE_EVAL_DIR_NAME}/."
        ),
    )
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args(argv)


def _resolve_out_path(args: argparse.Namespace) -> Path | None:
    """Contain the run record the way the main review harness does."""
    if args.out is None:
        return None
    if args.allow_external_out:
        return anchor_private_eval_path(args.out)
    return resolve_private_eval_path(args.out)


async def _run(args: argparse.Namespace) -> int:
    try:
        out_path = _resolve_out_path(args)
    except PrivateEvalPathError as exc:
        print(
            f"Refusing to write the run record: --out {exc}. Set "
            "--allow-external-out to write it to an explicitly private location "
            "elsewhere.",
            file=sys.stderr,
        )
        return 1
    source_revision = _source_revision()
    registry = eval_registry()
    cases = load_script_review_cases(args.cases)
    if args.case:
        wanted = set(args.case)
        cases = [case for case in cases if case.id in wanted]
        missing = wanted - {case.id for case in cases}
        if missing:
            print(f"Unknown case ids: {sorted(missing)}", file=sys.stderr)
            return 1
    try:
        validate_case_scripts(cases, registry)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 1
    for case in cases:
        assemble_tool_call_review_messages(*review_request(case, registry))
    print(f"Loaded {len(cases)} case(s); prompt revision {review_prompt_revision()}")
    if args.dry_run:
        for case in cases:
            print(f"  {case.id} [{case.family}/{case.label}]")
        return 0

    client = LLMClientFactory.create_client({
        "provider": args.provider,
        "model": args.model,
        "model_parameters": {},
    })
    try:
        trials = await run_script_review_eval(
            cases,
            client,
            seeds=args.seeds,
            registry=registry,
            concurrency=args.concurrency,
        )
    except TrialExecutionError as exc:
        print(f"Run aborted: {exc}", file=sys.stderr)
        return 1
    print(f"\njudge={args.provider}/{args.model} seeds={args.seeds}\n")
    print(render_markdown_summary(cases, trials))
    if out_path is not None:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(
            json.dumps(
                {
                    "provider": args.provider,
                    "model": args.model,
                    "prompt_revision": review_prompt_revision(),
                    "seeds": args.seeds,
                    "source_revision": source_revision,
                    "cases_digest": cases_digest(cases),
                    "registry_digest": registry_digest(registry),
                    "trials": [trial.model_dump(mode="json") for trial in trials],
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"\nWrote {out_path}")
    return 0


def _source_revision() -> dict[str, object] | None:
    """The checkout's commit and whether it had uncommitted changes.

    ``dirty`` means the run is not pinned to the commit: the uncommitted
    changes themselves are not recorded. None outside a git checkout.
    """
    repo = Path(__file__).resolve().parent.parent
    try:
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo,
            capture_output=True,
            text=True,
            check=True,
        )
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=repo,
            capture_output=True,
            text=True,
            check=True,
        )
    except OSError, subprocess.CalledProcessError:
        return None
    return {"commit": head.stdout.strip(), "dirty": bool(status.stdout.strip())}


def main(argv: list[str] | None = None) -> int:
    """Entry point returning a process exit code."""
    return asyncio.run(_run(_parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())
