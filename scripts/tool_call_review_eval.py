#!/usr/bin/env python3
"""Replay the tool-call reviewer over labeled cases and report per-slice scores.

The harness drives the real ``ToolCallReviewer`` — real prompt assembly, real
provider, real model — over stored cases and reports asymmetric, expected-aware
error rates per slice. Cases are serialized reviewer inputs, so this always
measures the current system.

Usage:

    # Load and validate only, no network:
    python scripts/tool_call_review_eval.py \
        --dataset src/family_assistant/eval/tool_call_review/datasets/examples \
        --dry-run

    # Score against a judge and enforce a false-allow ceiling:
    python scripts/tool_call_review_eval.py \
        --dataset src/family_assistant/eval/tool_call_review/datasets/examples \
        --dataset .review-eval-local \
        --provider google --model gemini-3.7-flash \
        --seeds 5 --gate --ceiling 0.01 --out .review-eval-local/run.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from family_assistant.eval.tool_call_review import (
    DEFAULT_GENERATION_LEDGER_DIR,
    EvalReport,
    build_reviewer,
    consume_gate_generation,
    content_hash,
    load_cases,
    run_eval,
)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dataset",
        action="append",
        required=True,
        metavar="PATH",
        help="Dataset file or directory (repeatable).",
    )
    parser.add_argument(
        "--provider",
        default="google",
        help="Judge provider (default: google).",
    )
    parser.add_argument(
        "--model",
        default="gemini-3.7-flash",
        help="Judge model (default: gemini-3.7-flash).",
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
        "--gate",
        action="store_true",
        help="Enforce the false-allow ceiling; exit nonzero unless the gate passes.",
    )
    parser.add_argument(
        "--ceiling",
        type=float,
        default=0.01,
        help="Accepted false-allow ceiling for the gate (default: 0.01).",
    )
    parser.add_argument(
        "--ledger-dir",
        type=Path,
        default=DEFAULT_GENERATION_LEDGER_DIR,
        help=(
            "Marker store for consumed gate generations "
            f"(default: {DEFAULT_GENERATION_LEDGER_DIR})."
        ),
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Path to write the machine-readable JSON result.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Load and validate cases only; do not call the judge.",
    )
    return parser.parse_args(argv)


async def _run(args: argparse.Namespace) -> int:
    cases = load_cases(args.dataset)
    if not cases:
        print("No cases found in the supplied datasets.", file=sys.stderr)
        return 1
    dataset_digest = content_hash(cases)
    print(f"Loaded {len(cases)} case(s); dataset_hash={dataset_digest}")

    if args.dry_run:
        for case in cases:
            print(f"  {case.id} [{case.boundary}/{case.label}] source={case.source}")
        return 0

    reviewer = build_reviewer(
        args.provider,
        args.model,
        timeout_seconds=args.timeout_seconds,
    )
    try:
        report = await run_eval(
            cases,
            reviewer,
            seeds=args.seeds,
            provider=args.provider,
            model=args.model,
            dataset_hash=dataset_digest,
        )
    finally:
        await reviewer.close()

    if not args.gate:
        _emit(report, args, include_reasons=True)
        return 0

    # Consume the generation BEFORE exposing any result: printing the summary
    # or writing --out first would let a failed export leave no marker after
    # the results were already consulted, reopening the single-use gate.
    _gates, combined = report.combined_gate(args.ceiling)
    decision = consume_gate_generation(
        dataset_digest, combined, ledger_dir=args.ledger_dir
    )
    _emit(report, args, include_reasons=False)
    print()
    if decision.already_consumed:
        print("NON-SHIPPABLE (dev-only): this gate generation was already consumed.")
    print(f"Gate status: {combined.status.value} -- {combined.reason}")
    print(f"Shippable stamp: {'yes' if decision.shippable else 'no'}")
    print(f"Generation marker: {decision.marker_path}")
    return 0 if decision.shippable else 1


def _emit(
    report: EvalReport, args: argparse.Namespace, *, include_reasons: bool
) -> None:
    print()
    print(
        report.to_text_summary(
            gate_ceiling=args.ceiling if args.gate else None,
            include_reasons=include_reasons,
        )
    )
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(
            json.dumps(report.to_json_dict(), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        print(f"\nWrote results to {args.out}")


def main(argv: list[str] | None = None) -> int:
    """Entry point returning a process exit code."""
    args = _parse_args(argv)
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
