#!/usr/bin/env python3
"""Prepare, submit, poll, and harvest private OpenRouter batch eval runs.

Preparation is local and deterministic. ``submit`` is the only command that
can spend money and requires both a positive approved USD amount and ``--approve-spend``.
The default model is an OpenRouter Gemini route; any OpenRouter model accepting
the Chat Completions structured-output contract may be selected explicitly.
"""

from __future__ import annotations

import argparse
import asyncio
import math
import sys
import time
from pathlib import Path

from family_assistant.eval.tool_call_review import (
    BatchError,
    BatchManifest,
    harvest_batch,
    prepare_batch,
    submit_batch,
    update_batch_status,
)
from family_assistant.eval.tool_call_review.loader import case_skip, load_cases
from family_assistant.eval.tool_call_review.registry_snapshot import (
    RegistrySnapshotError,
    load_registry_snapshot,
)


def _finite_positive(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed) or parsed <= 0:
        raise argparse.ArgumentTypeError("must be finite and greater than zero")
    return parsed


def _finite_nonnegative(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed) or parsed < 0:
        raise argparse.ArgumentTypeError("must be finite and nonnegative")
    return parsed


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    prepare = commands.add_parser(
        "prepare", help="Build private requests without network access."
    )
    prepare.add_argument("--dataset", action="append", required=True)
    prepare.add_argument("--run-dir", required=True, type=Path)
    prepare.add_argument("--tool-registry", type=Path)
    prepare.add_argument("--model", default="google/gemini-3.7-flash")
    prepare.add_argument("--seeds", type=int, default=1)
    prepare.add_argument("--batch-size", type=int, default=500)
    prepare.add_argument("--max-tokens", type=int, default=512)
    prepare.add_argument("--dry-run", action="store_true")

    submit = commands.add_parser(
        "submit", help="Submit pending private chunks after explicit approval."
    )
    submit.add_argument("--run-dir", required=True, type=Path)
    submit.add_argument(
        "--approved-spend-usd", required=True, type=float, metavar="USD"
    )
    submit.add_argument("--approve-spend", action="store_true")

    for name, help_text in (
        ("status", "Poll each submitted chunk once."),
        ("poll", "Poll until all chunks are terminal."),
    ):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("--run-dir", required=True, type=Path)
        command.add_argument("--interval-seconds", type=_finite_positive, default=30.0)
        command.add_argument(
            "--max-wait-seconds", type=_finite_nonnegative, default=86400.0
        )

    harvest = commands.add_parser(
        "harvest", help="Reconcile completed results into EvalReport."
    )
    harvest.add_argument("--run-dir", required=True, type=Path)
    return parser


def _registry(path: Path | None) -> dict | None:
    if path is None:
        return None
    try:
        return load_registry_snapshot(path)
    except RegistrySnapshotError as exc:
        raise BatchError(f"Cannot load --tool-registry: {exc}") from exc


def _prepare_dry_run(args: argparse.Namespace) -> int:
    registry = _registry(args.tool_registry)
    cases = load_cases(args.dataset, descriptor_registry=registry)
    runnable = sum(
        case_skip(case, descriptor_registry=registry) is None for case in cases
    )
    print(
        f"Validated {len(cases)} case(s); {runnable} runnable case(s); no network call."
    )
    return 0


def _ensure_terminal_success(manifest: BatchManifest) -> None:
    if any(
        chunk.status in {"failed", "expired", "cancelled", "submission_unknown"}
        for chunk in manifest.chunks
    ):
        raise BatchError(
            "One or more batch chunks reached a non-completed terminal state."
        )


async def _main(args: argparse.Namespace) -> int:
    if args.command == "prepare":
        if args.dry_run:
            return _prepare_dry_run(args)
        manifest = prepare_batch(
            args.dataset,
            args.run_dir,
            model=args.model,
            seeds=args.seeds,
            batch_size=args.batch_size,
            max_tokens=args.max_tokens,
            descriptor_registry=_registry(args.tool_registry),
        )
        print(f"Prepared {manifest.request_count} request(s) in {args.run_dir}.")
        return 0
    if args.command == "submit":
        manifest = await submit_batch(
            args.run_dir,
            approved_spend_usd=args.approved_spend_usd,
            approve_spend=args.approve_spend,
        )
        print(f"Submitted {len(manifest.chunks)} batch chunk(s).")
        return 0
    if args.command == "status":
        manifest = await update_batch_status(args.run_dir)
        _ensure_terminal_success(manifest)
        print(" ".join(f"{chunk.index}:{chunk.status}" for chunk in manifest.chunks))
        return 0
    if args.command == "poll":
        if not math.isfinite(args.interval_seconds) or args.interval_seconds <= 0:
            raise BatchError("--interval-seconds must be finite and greater than zero.")
        if not math.isfinite(args.max_wait_seconds) or args.max_wait_seconds < 0:
            raise BatchError("--max-wait-seconds must be finite and nonnegative.")
        deadline = time.monotonic() + args.max_wait_seconds
        while True:
            manifest = await update_batch_status(args.run_dir)
            _ensure_terminal_success(manifest)
            if all(
                chunk.status
                in {"completed", "failed", "expired", "cancelled", "submission_unknown"}
                for chunk in manifest.chunks
            ):
                print(
                    " ".join(
                        f"{chunk.index}:{chunk.status}" for chunk in manifest.chunks
                    )
                )
                return 0
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise BatchError(
                    "Polling deadline elapsed before all chunks became terminal."
                )
            await asyncio.sleep(min(args.interval_seconds, remaining))
    report = harvest_batch(args.run_dir)
    print(report.to_text_summary())
    return 1 if report.observed_allows() else 0


def main(argv: list[str] | None = None) -> int:
    try:
        return asyncio.run(_main(_parser().parse_args(argv)))
    except BatchError as exc:
        print(f"Batch run refused: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
