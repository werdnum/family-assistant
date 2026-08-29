#!/usr/bin/env python3
"""Adapt a locally-fetched public injection corpus into eval cases.

Runs one public-corpus adapter over a corpus you have fetched locally and writes
schema-valid :class:`EvalCase` records (plus a lineage sidecar) into a target
directory the harness can load. The corpora themselves are never committed — the
adapter and its pin are the committed artifacts — so this script is how a
maintainer materializes cases from a corpus fetched at its pinned revision.

Any corpus a gate consumes must be pinned: pass ``--verify-pin`` to fail unless
the fetched corpus matches the checksum recorded in ``adapters/PINS.toml``.
Unpinned fetch-on-demand is acceptable only for dev slices.

Usage:

    # Dev slice from a locally-fetched corpus (unpinned):
    python scripts/build_public_corpus_cases.py \\
        --corpus deepset_prompt_injections \\
        --input ~/corpora/deepset-prompt-injections/train.csv \\
        --out-dir .review-eval-local/public/deepset

    # Gate slice: verify the pin first, and stamp the revision into lineage:
    python scripts/build_public_corpus_cases.py \\
        --corpus injecagent \\
        --input ~/corpora/InjecAgent/data \\
        --out-dir .review-eval-local/public/injecagent \\
        --verify-pin --upstream-revision <commit-sha> --dedup

    # Smoke-check the mapping with the committed synthetic sample:
    python scripts/build_public_corpus_cases.py \\
        --corpus injecagent --use-sample \\
        --out-dir .review-eval-local/public/injecagent-sample
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from family_assistant.eval.tool_call_review.adapters import (
    ADAPTERS,
    corpus_checksum,
    lineage_aware_dedup,
    verify_pin,
)
from family_assistant.eval.tool_call_review.loader import load_cases

if TYPE_CHECKING:
    from family_assistant.eval.tool_call_review.adapters.base import AdaptedCase


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--corpus",
        required=True,
        choices=sorted(ADAPTERS),
        help="Which adapter to run.",
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--input",
        type=Path,
        metavar="PATH",
        help="Locally-fetched corpus file or directory.",
    )
    source.add_argument(
        "--use-sample",
        action="store_true",
        help="Use the committed synthetic sample instead of a fetched corpus.",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        required=True,
        help="Directory to write the case file and lineage sidecar into.",
    )
    parser.add_argument(
        "--verify-pin",
        action="store_true",
        help="Fail unless the fetched corpus matches its recorded pin (gate use).",
    )
    parser.add_argument(
        "--pins-path",
        type=Path,
        default=None,
        help="Override the PINS.toml location (default: the adapters' PINS.toml).",
    )
    parser.add_argument(
        "--upstream-revision",
        default=None,
        help="Revision to stamp into each case's lineage record.",
    )
    parser.add_argument(
        "--dedup",
        action="store_true",
        help="Apply lineage-aware dedup before writing.",
    )
    return parser.parse_args(argv)


def _build(args: argparse.Namespace) -> int:
    adapter_cls = ADAPTERS[args.corpus]

    if args.use_sample:
        adapter = adapter_cls.from_sample()
        print(f"Adapting the committed sample for {args.corpus}.")
    else:
        if not args.input.exists():
            print(f"Input corpus not found: {args.input}", file=sys.stderr)
            return 1
        if args.verify_pin:
            pin = verify_pin(args.corpus, args.input, pins_path=args.pins_path)
            print(f"Pin verified: {args.corpus} @ {pin.revision} ({pin.checksum}).")
        else:
            print(
                f"Corpus checksum (unverified): {corpus_checksum(args.input)}",
                file=sys.stderr,
            )
        adapter = adapter_cls.from_path(
            args.input, upstream_revision=args.upstream_revision
        )

    adapted: list[AdaptedCase] = list(adapter.iter_adapted())
    if args.dedup:
        before = len(adapted)
        adapted = lineage_aware_dedup(adapted)
        print(f"Lineage-aware dedup: {before} -> {len(adapted)} case(s).")
    if not adapted:
        print("Adapter produced no cases.", file=sys.stderr)
        return 1

    args.out_dir.mkdir(parents=True, exist_ok=True)
    cases_path = args.out_dir / f"{args.corpus}.jsonl"
    # The lineage sidecar is not a case and would abort validation if the loader
    # parsed it, so it goes in a `lineage/` subdirectory the loader excludes
    # rather than beside the case JSONL.
    lineage_dir = args.out_dir / "lineage"
    lineage_dir.mkdir(parents=True, exist_ok=True)
    lineage_path = lineage_dir / f"{args.corpus}.lineage.jsonl"
    with cases_path.open("w", encoding="utf-8") as handle:
        for item in adapted:
            handle.write(
                json.dumps(item.case.model_dump(mode="json"), ensure_ascii=False)
            )
            handle.write("\n")
    with lineage_path.open("w", encoding="utf-8") as handle:
        for item in adapted:
            record = {"id": item.case.id, **item.lineage.to_source_metadata()}
            handle.write(json.dumps(record, ensure_ascii=False))
            handle.write("\n")

    # Load the written cases back through the harness loader so a mapping that
    # produced a schema-invalid or duplicate-id case fails here, not at eval time.
    loaded = load_cases(cases_path)
    print(f"Wrote {len(loaded)} case(s) to {cases_path}")
    print(f"Wrote lineage sidecar to {lineage_path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    """Entry point returning a process exit code."""
    args = _parse_args(argv)
    return _build(args)


if __name__ == "__main__":
    sys.exit(main())
