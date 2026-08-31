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

The script does not deduplicate. Whether two rows are the same attack input is
settled at load time, over whatever corpus a run actually evaluates, by
:func:`~family_assistant.eval.tool_call_review.loader.attack_input_key`; an
answer given here could only ever cover one invocation's corpus.

Usage:

    # Adapt a locally-fetched corpus:
    python scripts/build_public_corpus_cases.py \\
        --corpus deepset_prompt_injections \\
        --input .review-eval-local/upstream/deepset \\
        --out-dir .review-eval-local/public/deepset

    # Record the revision the cases came from (base is the default):
    python scripts/build_public_corpus_cases.py \\
        --corpus injecagent \\
        --input .review-eval-local/upstream/InjecAgent/data \\
        --out-dir .review-eval-local/public/injecagent \\
        --upstream-revision <commit-sha>

    # Include the enhanced variant as a separate source slice:
    python scripts/build_public_corpus_cases.py \\
        --corpus injecagent \\
        --input .review-eval-local/upstream/InjecAgent/data \\
        --injecagent-variants both \\
        --out-dir .review-eval-local/public/injecagent-both

    # Materialize only held-out family groups for a ship-decision run:
    python scripts/build_public_corpus_cases.py \\
        --corpus injecagent \\
        --input .review-eval-local/upstream/InjecAgent/data \\
        --evaluation-split gate \\
        --out-dir .review-eval-local/public/injecagent-gate

    # Smoke-check the mapping with the committed synthetic sample:
    python scripts/build_public_corpus_cases.py \\
        --corpus injecagent --use-sample \\
        --out-dir .review-eval-local/public/injecagent-sample
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from family_assistant.eval.tool_call_review.adapters import ADAPTERS, InjecAgentAdapter
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
        "--injecagent-variants",
        choices=("base", "enhanced", "both"),
        default="base",
        help="InjecAgent variant(s) to select when --input is a directory.",
    )
    parser.add_argument(
        "--evaluation-split",
        choices=("all", "dev", "gate"),
        default="all",
        help="Write all cases or only the deterministic family-level dev/gate split.",
    )
    return parser.parse_args(argv)


def _build(args: argparse.Namespace) -> int:
    adapter_cls = ADAPTERS[args.corpus]
    if args.out_dir.exists() or args.out_dir.is_symlink():
        print(
            f"Output target already exists; refusing to overwrite {args.out_dir}",
            file=sys.stderr,
        )
        return 1

    if args.use_sample:
        if args.upstream_revision is not None:
            print(
                "--upstream-revision cannot be combined with --use-sample: the "
                "sample is committed here, so its revision is 'sample' and the "
                "supplied value would only disagree with what each lineage "
                "record already carries.",
                file=sys.stderr,
            )
            return 1
        if args.injecagent_variants != "base":
            print(
                "--injecagent-variants requires --input and cannot be used with "
                "--use-sample.",
                file=sys.stderr,
            )
            return 1
        adapter = adapter_cls.from_sample()
        print(f"Adapting the committed sample for {args.corpus}.")
    else:
        if not args.input.exists():
            print(f"Input corpus not found: {args.input}", file=sys.stderr)
            return 1
        if args.corpus == "injecagent":
            adapter = InjecAgentAdapter.from_path(
                args.input,
                upstream_revision=args.upstream_revision,
                variants=args.injecagent_variants,
            )
        else:
            if args.injecagent_variants != "base":
                print(
                    "--injecagent-variants is only valid with --corpus injecagent.",
                    file=sys.stderr,
                )
                return 1
            adapter = adapter_cls.from_path(
                args.input, upstream_revision=args.upstream_revision
            )

    adapted: list[AdaptedCase] = list(adapter.iter_adapted())
    if args.evaluation_split != "all":
        adapted = [
            item
            for item in adapted
            if item.lineage.evaluation_split == args.evaluation_split
        ]
    if not adapted:
        print("Adapter produced no cases.", file=sys.stderr)
        return 1

    parent = args.out_dir.parent
    parent.mkdir(parents=True, exist_ok=True)
    staging_dir = Path(
        tempfile.mkdtemp(prefix=f".{args.out_dir.name}.staging.", dir=parent)
    )
    try:
        cases_path = staging_dir / f"{args.corpus}.jsonl"
        with cases_path.open("w", encoding="utf-8") as handle:
            for item in adapted:
                handle.write(
                    json.dumps(
                        item.case.model_dump(mode="json"),
                        ensure_ascii=False,
                        sort_keys=True,
                    )
                )
                handle.write("\n")
        # Markdown, and beside the cases rather than under a subdirectory: the
        # loader skips it on suffix alone, so the provenance record needs no
        # excluded-directory rule to stay out of the case set.
        provenance_path = staging_dir / f"{args.corpus}.provenance.md"
        # The adapter's own revision, not the CLI value: ``from_sample`` records
        # "sample", so passing the flag through would print "unrecorded" in the
        # sidecar while every lineage record beside it said "sample".
        provenance_path.write_text(
            _render_provenance(adapter_cls, adapter.upstream_revision, adapted),
            encoding="utf-8",
        )

        # Load the staged cases back through the harness loader so a mapping
        # that produced a schema-invalid or duplicate-id case fails before the
        # target is published.
        loaded = load_cases(cases_path)
        if args.out_dir.exists() or args.out_dir.is_symlink():
            raise FileExistsError(
                f"Output target appeared while materializing: {args.out_dir}"
            )
        staging_dir.rename(args.out_dir)
    finally:
        if staging_dir.exists():
            shutil.rmtree(staging_dir)

    cases_path = args.out_dir / f"{args.corpus}.jsonl"
    provenance_path = args.out_dir / f"{args.corpus}.provenance.md"
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
    maintainer fetched, the license, and each case's source/paired ids and
    group. It is a record for a reader, not an integrity check: committed
    datasets are pinned by git, and nothing re-verifies this at run time.
    """
    lines = [
        f"# Provenance: {adapter_cls.corpus_id}",
        "",
        f"- **Upstream**: {adapter_cls.upstream}",
        f"- **Revision**: {upstream_revision or 'unrecorded'}",
        f"- **License**: {adapter_cls.license}",
        f"- **Adapter version**: {adapter_cls.adapter_version}",
        "- **Evaluation split**: SHA-256 family bucket 0 is `gate`; buckets 1-4 are `dev`.",
        f"- **Cases**: {len(adapted)}",
        "",
        "| Case id | Upstream id | Paired upstream id | Group | Source split | Evaluation split | Adapter version |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    lines.extend(
        f"| {item.case.id} | {item.lineage.upstream_id} | "
        f"{item.lineage.paired_upstream_id or '-'} | {item.lineage.group} | "
        f"{item.lineage.source_split or '-'} | {item.lineage.evaluation_split} | "
        f"{item.lineage.adapter_version} |"
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
