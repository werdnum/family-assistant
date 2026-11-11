#!/usr/bin/env python3
"""Replay recorded assistant turns with and without a chain entry's custom instructions.

Each case is a JSON file under ``.review-eval-local/turn-replay/`` (household
conversations stay in the private tree): the system prompt, the history up to
the user message that started the turn, the tool definitions offered, and the
recorded results to answer tool calls with. See
``family_assistant.eval.turn_replay`` for the format.

The runner builds the shipped chain entry twice -- as shipped, and with its
``custom_instructions`` removed -- and runs ``--trials`` turns of every case on
each, interleaved. It prints, per case and variant, how often the turn passed
its expectations, the tool calls made, requests, tokens, estimated cost and
wall time, and writes every trial to ``--out`` when given.

Usage:

    python scripts/turn_replay_eval.py \
        .review-eval-local/turn-replay/crows-nest-earlier-t1.json \
        .review-eval-local/turn-replay/control-already-answered.json \
        --tier claude_sonnet_5_5 --trials 5 \
        --out .review-eval-local/turn-replay/runs/sonnet-completion.json

Needs the provider's API key in the environment (ANTHROPIC_API_KEY for the
Sonnet tiers). Every trial is a real, billed model call.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
from dataclasses import asdict

from family_assistant.config_loader import load_config
from family_assistant.eval.private_paths import resolve_private_eval_path
from family_assistant.eval.turn_replay import (
    ReplayCase,
    ReplayTrial,
    build_tier_entry_client,
    run_trial,
)

# USD per million tokens, Claude Sonnet 5.5 list prices (input, cache read,
# 5-minute cache write, output). An estimate for comparing variants, not a bill.
SONNET_5_5_PRICES = (2.00, 0.10, 2.50, 10.00)

VARIANTS = (("shipped", True), ("without_policy", False))


def _cost(trial: ReplayTrial) -> float:
    # The Anthropic client reports prompt_tokens as the uncached remainder;
    # cache reads and writes are separate, disjoint buckets.
    price_in, price_read, price_write, price_out = SONNET_5_5_PRICES
    return (
        trial.prompt_tokens * price_in
        + trial.cached_prompt_tokens * price_read
        + trial.cache_write_tokens * price_write
        + trial.completion_tokens * price_out
    ) / 1_000_000


def _summarise(trials: list[ReplayTrial]) -> None:
    keys = sorted({(t.case_id, t.variant) for t in trials})
    for case_id, variant in keys:
        group = [t for t in trials if t.case_id == case_id and t.variant == variant]
        passed = sum(t.passed for t in group)
        print(f"\n{case_id} [{variant}] model={group[0].model}")
        print(f"  passed {passed}/{len(group)}")
        print(
            "  tool calls/trial: "
            f"{statistics.mean(len(t.tool_calls) for t in group):.1f}  "
            f"requests/trial: {statistics.mean(t.requests for t in group):.1f}  "
            f"unrecorded calls: {sum(t.unrecorded_calls for t in group)}"
        )
        print(
            f"  cost/trial ~${statistics.mean(_cost(t) for t in group):.4f}  "
            f"wall time/trial {statistics.mean(t.duration_s for t in group):.1f}s"
        )
        for index, trial in enumerate(group):
            names = ", ".join(str(c["name"]) for c in trial.tool_calls) or "none"
            verdict = "pass" if trial.passed else "FAIL: " + "; ".join(trial.failures)
            print(f"  #{index + 1} tools: {names} -> {verdict}")


async def _run(args: argparse.Namespace, cases: list[ReplayCase]) -> list[ReplayTrial]:
    config = load_config(load_dotenv_file=True)
    model = config.model_tiers[args.tier].chain[args.entry].model or "?"
    clients = {
        name: build_tier_entry_client(
            config, args.tier, args.entry, with_custom_instructions=with_policy
        )
        for name, with_policy in VARIANTS
    }
    trials: list[ReplayTrial] = []
    for case in cases:
        for _ in range(args.trials):
            for name, _with_policy in VARIANTS:
                trial = await run_trial(clients[name], case, variant=name, model=model)
                trials.append(trial)
                print(f"{case.id} [{name}] {'pass' if trial.passed else 'fail'}")
    return trials


def _write(args: argparse.Namespace, trials: list[ReplayTrial]) -> None:
    if args.out:
        out = resolve_private_eval_path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps(
                [asdict(t) | {"estimated_cost_usd": _cost(t)} for t in trials],
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"\nWrote {len(trials)} trials to {out}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Replay recorded turns with and without custom instructions."
    )
    parser.add_argument("cases", nargs="+", help="Case JSON files")
    parser.add_argument("--tier", default="claude_sonnet_5_5")
    parser.add_argument(
        "--entry", type=int, default=0, help="Index of the chain entry to run"
    )
    parser.add_argument("--trials", type=int, default=3)
    parser.add_argument("--out", help="Trial records, inside .review-eval-local/")
    args = parser.parse_args()
    cases = [
        ReplayCase.model_validate_json(
            resolve_private_eval_path(path).read_text(encoding="utf-8")
        )
        for path in args.cases
    ]
    trials = asyncio.run(_run(args, cases))
    _summarise(trials)
    _write(args, trials)
    return 0


if __name__ == "__main__":
    sys.exit(main())
