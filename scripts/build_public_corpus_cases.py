#!/usr/bin/env python3
"""Adapt a locally-fetched public injection corpus into eval cases.

Runs one public-corpus adapter over a corpus you have fetched locally and writes
schema-valid :class:`EvalCase` records into a target directory the harness can
load, alongside a Markdown provenance record naming the upstream, the revision
you fetched, and the license. The corpora themselves are never committed — the
adapter is the committed artifact — so this script is how a maintainer
materializes cases from a corpus they hold locally.

Provenance is recorded, not verified: pass ``--upstream-revision`` so the record
says which revision the cases came from. Nothing re-checks it at run time.

Usage:

    # Adapt a locally-fetched corpus:
    python scripts/build_public_corpus_cases.py \\
        --corpus deepset_prompt_injections \\
        --input ~/corpora/deepset-prompt-injections/train.csv \\
        --out-dir .review-eval-local/public/deepset

    # Record the revision the cases came from, and deduplicate:
    python scripts/build_public_corpus_cases.py \\
        --corpus injecagent \\
        --input ~/corpora/InjecAgent/data \\
        --out-dir .review-eval-local/public/injecagent \\
        --upstream-revision <commit-sha> --dedup

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
    lineage_aware_dedup,
)
from family_assistant.eval.tool_call_review.loader import load_cases

if TYPE_CHECKING:
    from family_assistant.eval.tool_call_review.adapters.base import (
        AdaptedCase,
        Adapter,
    )


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
        "--upstream-revision",
        default=None,
        help="Revision to record in the provenance output and each lineage record.",
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
    with cases_path.open("w", encoding="utf-8") as handle:
        for item in adapted:
            handle.write(
                json.dumps(item.case.model_dump(mode="json"), ensure_ascii=False)
            )
            handle.write("\n")
    # Markdown, and beside the cases rather than under a subdirectory: the
    # loader skips it on suffix alone, so the provenance record needs no
    # excluded-directory rule to stay out of the case set.
    provenance_path = args.out_dir / f"{args.corpus}.provenance.md"
    provenance_path.write_text(
        _render_provenance(adapter_cls, args.upstream_revision, adapted),
        encoding="utf-8",
    )

    # Load the written cases back through the harness loader so a mapping that
    # produced a schema-invalid or duplicate-id case fails here, not at eval time.
    loaded = load_cases(cases_path)
    print(f"Wrote {len(loaded)} case(s) to {cases_path}")
    print(f"Wrote provenance record to {provenance_path}")
    return 0


def _render_provenance(
    adapter_cls: type[Adapter],
    upstream_revision: str | None,
    adapted: list[AdaptedCase],
) -> str:
    """Render the provenance record that travels with an adapted corpus.

    It documents where these cases came from — upstream, the revision the
    maintainer fetched, the license, and each case's upstream id and group. It
    is a record for a reader, not an integrity check: committed datasets are
    pinned by git, and nothing re-verifies this at run time.
    """
    lines = [
        f"# Provenance: {adapter_cls.corpus_id}",
        "",
        f"- **Upstream**: {adapter_cls.upstream}",
        f"- **Revision**: {upstream_revision or 'unrecorded'}",
        f"- **License**: {adapter_cls.license}",
        f"- **Cases**: {len(adapted)}",
        "",
        "| Case id | Upstream id | Group |",
        "| --- | --- | --- |",
    ]
    lines.extend(
        f"| {item.case.id} | {item.lineage.upstream_id} | {item.lineage.group} |"
        for item in adapted
    )
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """Entry point returning a process exit code."""
    args = _parse_args(argv)
    return _build(args)


if __name__ == "__main__":
    sys.exit(main())
