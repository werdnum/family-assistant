#!/usr/bin/env python3
"""Live evaluation CLI for Family Assistant's ``browser_profile`` agent.

Runs historical browser tasks against multiple candidate models (each with a
single-entry, zero-fallback client built from ``defaults.yaml``), grades them
with a blind side-by-side consensus LLM judge, and writes local & sanitized
Markdown comparison reports.

All household-derived inputs and full run artifacts are confined to the
gitignored ``.review-eval-local/`` tree via ``resolve_private_eval_path``.

Examples:

    # Inspect the 50-case attachment export
    python scripts/browser_eval.py inspect .review-eval-local/browser/eval_set_raw.json

    # Run a mini-eval across Hydrogen, Neon, GPT-6.2 Sol, and Sonnet 5.5
    python scripts/browser_eval.py run \\
        .review-eval-local/browser/eval_set_raw.json \\
        --candidates flash-3.8,hydrogen,neon,gpt-6.2-sol,sonnet-5.5 \\
        --out-dir .review-eval-local/browser/runs/mini-eval \\
        --max-spend-usd 45.0 --concurrency 3 --resume

    # Run blind comparative consensus judge & generate report
    python scripts/browser_eval.py judge \\
        .review-eval-local/browser/eval_set_raw.json \\
        --run-dir .review-eval-local/browser/runs/mini-eval \\
        --judge-tier claude_sonnet_5_5
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from collections import Counter
from pathlib import Path
from typing import TYPE_CHECKING

from family_assistant.config_loader import load_config
from family_assistant.eval.browser.candidates import (
    BUILTIN_CANDIDATES,
    BrowserEvalCandidate,
    build_candidate_client,
    resolve_candidates,
)
from family_assistant.eval.browser.cases import (
    DEFAULT_EXCLUDED_FLAGS,
    BrowserEvalCase,
    filter_cases,
    load_browser_cases,
)
from family_assistant.eval.browser.judge import (
    append_judgement_jsonl,
    compute_trial_set_fingerprint,
    judge_case_trials,
    load_judgements_jsonl,
)
from family_assistant.eval.browser.report import render_markdown_report
from family_assistant.eval.browser.runner import (
    BrowserEvalTrial,
    SpendBudget,
    append_trial_jsonl,
    completed_trial_keys,
    get_profile_system_prompt,
    load_trials_jsonl,
    run_browser_trial,
)
from family_assistant.eval.private_paths import resolve_private_eval_path
from family_assistant.llm.factory import LLMClientFactory
from family_assistant.llm.model_tiers import resolve_entry_client_config

if TYPE_CHECKING:
    from family_assistant.config_models import AppConfig
    from family_assistant.llm import LLMInterface

logger = logging.getLogger("browser_eval")


def _parse_csv(value: str | None) -> list[str] | None:
    if not value:
        return None
    items = [part.strip() for part in value.split(",") if part.strip()]
    return items or None


def _load_id_list(path: str | None) -> list[str] | None:
    if not path:
        return None
    resolved = resolve_private_eval_path(path)
    ids: list[str] = []
    for raw_line in resolved.read_text(encoding="utf-8").splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if line:
            ids.append(line)
    return ids


def _select_cases(args: argparse.Namespace) -> list[BrowserEvalCase]:
    dataset = load_browser_cases(args.cases)
    include_ids = _parse_csv(getattr(args, "include_ids", None))
    ids_from_file = _load_id_list(getattr(args, "ids_file", None))
    if ids_from_file is not None:
        include_ids = (include_ids or []) + ids_from_file
    exclude_ids = _parse_csv(getattr(args, "exclude_ids", None))
    include_cats = _parse_csv(getattr(args, "categories", None))
    exclude_flags = (
        set(_parse_csv(args.exclude_flags) or ())
        if getattr(args, "exclude_flags", None) is not None
        else set(DEFAULT_EXCLUDED_FLAGS)
    )
    selected = filter_cases(
        dataset.cases,
        include_ids=include_ids,
        exclude_ids=exclude_ids,
        include_categories=include_cats,
        exclude_flags=exclude_flags,
        exclude_followup_flags=not getattr(args, "include_followups", False),
        max_cases=getattr(args, "max_cases", None),
    )
    # Preserve explicit file ordering when --ids-file was provided.
    if ids_from_file is not None:
        order = {cid: idx for idx, cid in enumerate(ids_from_file)}
        selected.sort(key=lambda c: order.get(c.id, len(order)))
    return selected


def _cmd_inspect(args: argparse.Namespace) -> int:
    dataset = load_browser_cases(args.cases)
    selected = _select_cases(args)
    print(f"Dataset: {len(dataset.cases)} total cases, {len(selected)} selected")
    if dataset.description:
        print(f"Description: {dataset.description}")
    print("\nSelected categories:")
    for cat, count in Counter(c.category for c in selected).most_common():
        print(f"  {cat}: {count}")
    print("\nSelected cases:")
    for idx, case in enumerate(selected, start=1):
        flags_str = ",".join(case.flags) if case.flags else "-"
        print(
            f"  {idx:02d}. {case.id} [{case.category}] "
            f"flags=({flags_str}) orig_dur={case.duration_s or '?'}s"
        )
    return 0


async def _run_eval_async(args: argparse.Namespace) -> int:
    cases = _select_cases(args)
    if not cases:
        print("No cases selected.", file=sys.stderr)
        return 1

    candidate_names = _parse_csv(args.candidates) or [
        "hydrogen",
        "neon",
        "gpt-6.2-sol",
    ]
    candidates = resolve_candidates(candidate_names)
    out_dir = resolve_private_eval_path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    trials_path = out_dir / "trials.jsonl"

    existing_trials = load_trials_jsonl(trials_path) if args.resume else []
    done_keys = completed_trial_keys(existing_trials)
    prior_spend = sum(t.estimated_cost_usd for t in existing_trials)
    budget = SpendBudget(max_spend_usd=args.max_spend_usd, spent_usd=prior_spend)

    config: AppConfig = load_config(load_dotenv_file=True)
    dom_system_prompt = get_profile_system_prompt(
        config, "browser_profile", timezone_name=args.timezone
    )
    visual_system_prompt = get_profile_system_prompt(
        config, "browser_visual_profile", timezone_name=args.timezone
    )

    built_candidates: list[
        tuple[BrowserEvalCandidate, LLMInterface, str, str, dict[str, object]]
    ] = []
    for cand in candidates:
        client, provider, model, effective_params = build_candidate_client(config, cand)
        built_candidates.append((cand, client, provider, model, effective_params))

    print(
        f"Running {len(cases)} case(s) x {len(built_candidates)} candidate(s) "
        f"(already done: {len(done_keys)}, prior spend: ${prior_spend:.2f}, "
        f"cap: ${args.max_spend_usd:.2f})"
    )

    semaphore = asyncio.Semaphore(max(args.concurrency, 1))
    write_lock = asyncio.Lock()

    async def _run_one(
        case: BrowserEvalCase,
        cand: BrowserEvalCandidate,
        client: LLMInterface,
        provider: str,
        model: str,
        effective_params: dict[str, object],
    ) -> None:
        if (case.id, cand.name) in done_keys:
            return
        async with semaphore:
            if not await budget.check_available():
                return
            trial = await run_browser_trial(
                client,
                case,
                candidate_name=cand.name,
                mode=cand.mode,
                provider=provider,
                model=model,
                system_prompt=(
                    visual_system_prompt if cand.mode == "visual" else dom_system_prompt
                ),
                llm_parameters=effective_params,
                timezone_name=args.timezone,
                max_iterations=args.max_iterations,
                timeout_s=args.timeout_s,
                budget=budget,
            )
            async with write_lock:
                append_trial_jsonl(trials_path, trial)
                print(
                    f"[{case.id} | {case.category}] {cand.name:<15} "
                    f"status={trial.status:<15} calls={trial.tool_calls:2d} "
                    f"dur={trial.duration_s:5.1f}s cost=${trial.estimated_cost_usd:.3f} "
                    f"(total=${budget.spent_usd:.2f})",
                    flush=True,
                )

    # Run all candidates for one case together so live pages are sampled in the
    # same time window, minimizing live-price / headline / stock drift.
    for case in cases:
        if not await budget.check_available():
            print(
                f"Budget cap reached (${budget.spent_usd:.2f} / ${budget.max_spend_usd:.2f}); stopping.",
                flush=True,
            )
            break
        case_tasks = [
            _run_one(case, cand, client, provider, model, effective_params)
            for cand, client, provider, model, effective_params in built_candidates
        ]
        await asyncio.gather(*case_tasks)

    all_trials = load_trials_jsonl(trials_path)
    report_md = render_markdown_report(
        all_trials,
        load_judgements_jsonl(out_dir / "judgements.jsonl"),
        title="Browser Task Eval (Operational Summary)",
        baseline_candidate=args.baseline,
    )
    (out_dir / "report.md").write_text(report_md, encoding="utf-8")
    print(f"\nSaved {len(all_trials)} trial(s) to {trials_path}")
    print(f"Total estimated spend: ${budget.spent_usd:.2f}")
    return 0


async def _judge_eval_async(args: argparse.Namespace) -> int:
    dataset = load_browser_cases(args.cases)
    case_map = {case.id: case for case in dataset.cases}

    run_dir = resolve_private_eval_path(args.run_dir)
    trials_path = run_dir / "trials.jsonl"
    judgements_path = run_dir / "judgements.jsonl"

    trials = load_trials_jsonl(trials_path)
    if not trials:
        print(f"No trials found at {trials_path}", file=sys.stderr)
        return 1

    existing_judgements = load_judgements_jsonl(judgements_path) if args.resume else []
    latest_judgement_by_case = {j.case_id: j for j in existing_judgements}

    # Latest non-budget-skipped trial per (case_id, candidate_name)
    dedup: dict[tuple[str, str], int] = {}
    for idx, t in enumerate(trials):
        if t.status != "budget_exceeded":
            dedup[(t.case_id, t.candidate_name)] = idx
    by_case_trials: dict[str, list[BrowserEvalTrial]] = {}
    for (cid, _cand), idx in dedup.items():
        by_case_trials.setdefault(cid, []).append(trials[idx])

    config = load_config(load_dotenv_file=True)
    tier = config.model_tiers[args.judge_tier]
    entry = tier.chain[args.judge_entry]
    client_config = resolve_entry_client_config(entry, config.llm_parameters)
    judge_model = str(client_config["model"])
    judge_client = LLMClientFactory.create_client(client_config)

    to_judge: list[str] = []
    skipped_up_to_date = 0
    for cid in sorted(by_case_trials):
        if cid not in case_map:
            continue
        prior = latest_judgement_by_case.get(cid)
        current_fp = compute_trial_set_fingerprint(by_case_trials[cid])
        if prior is not None and prior.trial_fingerprint == current_fp:
            skipped_up_to_date += 1
            continue
        to_judge.append(cid)
    print(
        f"Judging {len(to_judge)} case(s) with {judge_model} "
        f"(up-to-date cached judgements: {skipped_up_to_date})"
    )

    semaphore = asyncio.Semaphore(max(args.concurrency, 1))
    write_lock = asyncio.Lock()

    async def _judge_one(cid: str) -> None:
        async with semaphore:
            judgement = await judge_case_trials(
                judge_client,
                judge_model=judge_model,
                case=case_map[cid],
                trials=by_case_trials[cid],
            )
            async with write_lock:
                append_judgement_jsonl(judgements_path, judgement)
                score_str = ", ".join(
                    f"{g.candidate_name}={g.score}" for g in judgement.grades
                )
                print(
                    f"[{cid} | {judgement.category}] {judgement.consensus_status:<21} "
                    f"{score_str}",
                    flush=True,
                )

    await asyncio.gather(*(_judge_one(cid) for cid in to_judge))

    all_judgements = load_judgements_jsonl(judgements_path)
    report_md = render_markdown_report(
        trials,
        all_judgements,
        title=args.title,
        baseline_candidate=args.baseline,
        sanitized=False,
    )
    report_path = run_dir / "report.md"
    report_path.write_text(report_md, encoding="utf-8")
    print(f"\nWrote local report to {report_path}")
    return 0


def _cmd_report(args: argparse.Namespace) -> int:
    run_dir = resolve_private_eval_path(args.run_dir)
    trials = load_trials_jsonl(run_dir / "trials.jsonl")
    judgements = load_judgements_jsonl(run_dir / "judgements.jsonl")
    local_md = render_markdown_report(
        trials,
        judgements,
        title=args.title,
        baseline_candidate=args.baseline,
        sanitized=False,
    )
    local_path = run_dir / "report.md"
    local_path.write_text(local_md, encoding="utf-8")
    print(local_md)
    if args.sanitized_out:
        sanitized_md = render_markdown_report(
            trials,
            judgements,
            title=args.title,
            baseline_candidate=args.baseline,
            sanitized=True,
        )
        out_path = Path(args.sanitized_out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(sanitized_md, encoding="utf-8")
        print(f"Wrote sanitized aggregate report to {out_path}")
    return 0


def _add_filter_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("cases", help="Case JSON path inside .review-eval-local/")
    parser.add_argument("--include-ids", help="Comma-separated case IDs to include")
    parser.add_argument(
        "--ids-file", help="Path inside .review-eval-local/ listing case IDs"
    )
    parser.add_argument("--exclude-ids", help="Comma-separated case IDs to exclude")
    parser.add_argument("--categories", help="Comma-separated categories to include")
    parser.add_argument(
        "--exclude-flags",
        help="Comma-separated flags to exclude (overrides default exclusions)",
    )
    parser.add_argument(
        "--include-followups",
        action="store_true",
        help="Include followup_* cases (excluded by default)",
    )
    parser.add_argument("--max-cases", type=int, help="Cap number of selected cases")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Live evaluation harness for Family Assistant browser_profile."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_inspect = sub.add_parser(
        "inspect", help="Inspect and filter a browser eval dataset"
    )
    _add_filter_args(p_inspect)

    p_run = sub.add_parser(
        "run", help="Run live browser trials across candidate models"
    )
    _add_filter_args(p_run)
    p_run.add_argument(
        "--candidates",
        default="hydrogen,neon,gpt-6.2-sol",
        help=f"Comma-separated candidates ({', '.join(sorted(BUILTIN_CANDIDATES))})",
    )
    p_run.add_argument(
        "--out-dir",
        required=True,
        help="Output run directory inside .review-eval-local/",
    )
    p_run.add_argument("--max-spend-usd", type=float, default=45.0)
    p_run.add_argument("--max-iterations", type=int, default=50)
    p_run.add_argument("--timeout-s", type=float, default=600.0)
    p_run.add_argument("--concurrency", type=int, default=3)
    p_run.add_argument("--timezone", default="Australia/Sydney")
    p_run.add_argument("--baseline", default="hydrogen")
    p_run.add_argument("--resume", action="store_true")

    p_judge = sub.add_parser(
        "judge", help="Blindly grade completed trials and measure cross-model consensus"
    )
    p_judge.add_argument("cases", help="Case JSON path inside .review-eval-local/")
    p_judge.add_argument(
        "--run-dir",
        required=True,
        help="Run directory inside .review-eval-local/ containing trials.jsonl",
    )
    p_judge.add_argument("--judge-tier", default="claude_sonnet_5_5")
    p_judge.add_argument("--judge-entry", type=int, default=0)
    p_judge.add_argument("--concurrency", type=int, default=4)
    p_judge.add_argument("--baseline", default="hydrogen")
    p_judge.add_argument("--title", default="Browser Task Evaluation Report")
    p_judge.add_argument("--resume", action="store_true")

    p_report = sub.add_parser("report", help="Render Markdown reports from a run dir")
    p_report.add_argument(
        "--run-dir",
        required=True,
        help="Run directory inside .review-eval-local/",
    )
    p_report.add_argument("--baseline", default="hydrogen")
    p_report.add_argument("--title", default="Browser Task Evaluation Report")
    p_report.add_argument(
        "--sanitized-out",
        help="Optional path for sanitized aggregate Markdown (safe for docs/)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s"
    )
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "inspect":
        return _cmd_inspect(args)
    if args.command == "run":
        return asyncio.run(_run_eval_async(args))
    if args.command == "judge":
        return asyncio.run(_judge_eval_async(args))
    if args.command == "report":
        return _cmd_report(args)
    return 1


if __name__ == "__main__":
    sys.exit(main())
