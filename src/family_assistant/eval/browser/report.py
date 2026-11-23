"""Aggregate metrics, pairwise lift, consensus analysis, and Markdown reports.

Generates two report modes:
- **Full local report** (stays inside ``.review-eval-local/``): includes case
  IDs and concise disagreement diagnostics for debugging.
- **Sanitized aggregate report** (safe for ``docs/development/eval-results/``):
  contains only aggregate tables, category metrics, pairwise lift, and
  consensus counts — zero prompts, URLs, replies, or household details.
"""

from __future__ import annotations

import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

    from family_assistant.eval.browser.judge import CandidateGrade, CaseJudgement
    from family_assistant.eval.browser.runner import BrowserEvalTrial

__all__ = [
    "CandidateSummary",
    "PairwiseLift",
    "compute_candidate_summaries",
    "compute_pairwise_lift",
    "render_markdown_report",
]

PASS_SCORE_THRESHOLD = 4


@dataclass(frozen=True)
class CandidateSummary:
    """Aggregate operational and quality metrics for one candidate model."""

    candidate_name: str
    model: str
    provider: str
    trials: int
    completed: int
    timeouts: int
    iteration_limits: int
    errors: int
    judged: int
    passes: int
    complete_outcomes: int
    blocked_honestly: int
    hallucinated: int
    grounded_count: int
    winner_count: int
    consensus_match_count: int
    mean_score: float
    mean_duration_s: float
    median_duration_s: float
    mean_tool_calls: float
    tool_error_rate: float
    cache_hit_rate: float
    total_cost_usd: float
    mean_cost_usd: float
    passes_per_dollar: float | None


@dataclass(frozen=True)
class PairwiseLift:
    """Head-to-head comparison of ``challenger`` over ``baseline`` on shared cases."""

    challenger: str
    baseline: str
    shared_cases: int
    challenger_passes: int
    baseline_passes: int
    pass_lift: int
    challenger_wins: int
    ties: int
    baseline_wins: int
    mean_score_delta: float
    cost_delta_usd: float
    cost_per_extra_pass_usd: float | None


def _latest_trials_by_key(
    trials: Sequence[BrowserEvalTrial],
) -> dict[tuple[str, str], BrowserEvalTrial]:
    latest: dict[tuple[str, str], BrowserEvalTrial] = {}
    for trial in trials:
        if trial.status == "budget_exceeded":
            continue
        latest[(trial.case_id, trial.candidate_name)] = trial
    return latest


def _latest_judgements_by_case(
    judgements: Sequence[CaseJudgement],
) -> dict[str, CaseJudgement]:
    return {j.case_id: j for j in judgements}


def compute_candidate_summaries(
    trials: Sequence[BrowserEvalTrial],
    judgements: Sequence[CaseJudgement],
) -> list[CandidateSummary]:
    """Compute per-candidate summary metrics sorted by mean cost ascending."""
    latest_trials = list(_latest_trials_by_key(trials).values())
    by_candidate: dict[str, list[BrowserEvalTrial]] = defaultdict(list)
    for trial in latest_trials:
        by_candidate[trial.candidate_name].append(trial)

    grades_by_cand: dict[str, list[tuple[CaseJudgement, CandidateGrade]]] = defaultdict(
        list
    )
    for judgement in _latest_judgements_by_case(judgements).values():
        for grade in judgement.grades:
            grades_by_cand[grade.candidate_name].append((judgement, grade))

    summaries: list[CandidateSummary] = []
    for cand_name, cand_trials in sorted(by_candidate.items()):
        n = len(cand_trials)
        completed = sum(1 for t in cand_trials if t.status == "completed")
        timeouts = sum(1 for t in cand_trials if t.status == "timeout")
        iter_limits = sum(1 for t in cand_trials if t.status == "iteration_limit")
        errors = sum(1 for t in cand_trials if t.status == "error")

        durations = [t.duration_s for t in cand_trials]
        total_calls = sum(t.tool_calls for t in cand_trials)
        total_tool_errs = sum(t.tool_error_count for t in cand_trials)
        total_input = sum(
            t.uncached_input_tokens
            + t.cached_input_tokens
            + t.cache_write_tokens
            + t.cache_write_1h_tokens
            for t in cand_trials
        )
        total_cached = sum(t.cached_input_tokens for t in cand_trials)
        total_cost = sum(t.estimated_cost_usd for t in cand_trials)

        cand_grades = grades_by_cand.get(cand_name, [])
        judged = len(cand_grades)
        passes = sum(1 for _, g in cand_grades if g.score >= PASS_SCORE_THRESHOLD)
        complete_outcomes = sum(1 for _, g in cand_grades if g.outcome == "complete")
        blocked_honestly = sum(
            1 for _, g in cand_grades if g.outcome == "blocked_honestly"
        )
        hallucinated = sum(1 for _, g in cand_grades if g.outcome == "hallucinated")
        grounded = sum(1 for _, g in cand_grades if g.grounded)
        winners = sum(1 for j, _ in cand_grades if cand_name in j.winner_candidates)
        consensus_matches = sum(
            1 for j, _ in cand_grades if cand_name in j.consensus_candidates
        )
        mean_score = statistics.mean(g.score for _, g in cand_grades) if judged else 0.0
        passes_per_dollar = (passes / total_cost) if total_cost > 0 and judged else None

        summaries.append(
            CandidateSummary(
                candidate_name=cand_name,
                model=cand_trials[0].model,
                provider=cand_trials[0].provider,
                trials=n,
                completed=completed,
                timeouts=timeouts,
                iteration_limits=iter_limits,
                errors=errors,
                judged=judged,
                passes=passes,
                complete_outcomes=complete_outcomes,
                blocked_honestly=blocked_honestly,
                hallucinated=hallucinated,
                grounded_count=grounded,
                winner_count=winners,
                consensus_match_count=consensus_matches,
                mean_score=mean_score,
                mean_duration_s=statistics.mean(durations) if durations else 0.0,
                median_duration_s=statistics.median(durations) if durations else 0.0,
                mean_tool_calls=(total_calls / n) if n else 0.0,
                tool_error_rate=(total_tool_errs / total_calls) if total_calls else 0.0,
                cache_hit_rate=(total_cached / total_input) if total_input else 0.0,
                total_cost_usd=total_cost,
                mean_cost_usd=(total_cost / n) if n else 0.0,
                passes_per_dollar=passes_per_dollar,
            )
        )

    summaries.sort(key=lambda s: (s.mean_cost_usd, s.candidate_name))
    return summaries


def compute_pairwise_lift(
    trials: Sequence[BrowserEvalTrial],
    judgements: Sequence[CaseJudgement],
    *,
    baseline: str,
    challenger: str,
) -> PairwiseLift:
    """Compare ``challenger`` against ``baseline`` on all shared judged cases."""
    trial_map = _latest_trials_by_key(trials)
    judgement_map = _latest_judgements_by_case(judgements)

    shared_cases = 0
    challenger_passes = 0
    baseline_passes = 0
    challenger_wins = 0
    ties = 0
    baseline_wins = 0
    score_deltas: list[int] = []
    cost_delta = 0.0

    for case_id, judgement in sorted(judgement_map.items()):
        grade_by_cand = {g.candidate_name: g for g in judgement.grades}
        if baseline not in grade_by_cand or challenger not in grade_by_cand:
            continue
        b_grade = grade_by_cand[baseline]
        c_grade = grade_by_cand[challenger]
        shared_cases += 1
        if c_grade.score >= PASS_SCORE_THRESHOLD:
            challenger_passes += 1
        if b_grade.score >= PASS_SCORE_THRESHOLD:
            baseline_passes += 1
        delta = c_grade.score - b_grade.score
        score_deltas.append(delta)
        if delta > 0:
            challenger_wins += 1
        elif delta < 0:
            baseline_wins += 1
        else:
            ties += 1
        c_trial = trial_map.get((case_id, challenger))
        b_trial = trial_map.get((case_id, baseline))
        if c_trial and b_trial:
            cost_delta += c_trial.estimated_cost_usd - b_trial.estimated_cost_usd

    pass_lift = challenger_passes - baseline_passes
    cost_per_extra_pass = (
        (cost_delta / pass_lift) if pass_lift > 0 and cost_delta > 0 else None
    )
    return PairwiseLift(
        challenger=challenger,
        baseline=baseline,
        shared_cases=shared_cases,
        challenger_passes=challenger_passes,
        baseline_passes=baseline_passes,
        pass_lift=pass_lift,
        challenger_wins=challenger_wins,
        ties=ties,
        baseline_wins=baseline_wins,
        mean_score_delta=statistics.mean(score_deltas) if score_deltas else 0.0,
        cost_delta_usd=cost_delta,
        cost_per_extra_pass_usd=cost_per_extra_pass,
    )


def _fmt_pct(num: int, den: int) -> str:
    return f"{(100.0 * num / den):.0f}%" if den > 0 else "—"


def render_markdown_report(
    trials: Sequence[BrowserEvalTrial],
    judgements: Sequence[CaseJudgement],
    *,
    title: str = "Browser Task Evaluation Report",
    baseline_candidate: str = "hydrogen",
    sanitized: bool = False,
) -> str:
    """Render an aggregate Markdown report (optionally sanitized for publishing)."""
    summaries = compute_candidate_summaries(trials, judgements)
    latest_judgements = list(_latest_judgements_by_case(judgements).values())
    latest_trials = list(_latest_trials_by_key(trials).values())

    total_spend = sum(t.estimated_cost_usd for t in latest_trials)
    case_ids = sorted({t.case_id for t in latest_trials})
    lines: list[str] = [
        f"# {title}",
        "",
        (
            f"- **Cases evaluated:** {len(case_ids)}  "
            f"| **Total trials:** {len(latest_trials)}  "
            f"| **Judged cases:** {len(latest_judgements)}  "
            f"| **Total model spend:** ${total_spend:.2f}"
        ),
        "",
        "## Candidate Summary (Ordered by Cost)",
        "",
        "| Candidate | Model | Pass (≥4/5) | Mean Score | Grounded | Win/Tie | Consensus | Completed | Med. Latency | Avg Calls | Tool Err | Cache Hit | Cost/Task | Total Cost | Passes/$ |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for s in summaries:
        ppd = f"{s.passes_per_dollar:.1f}" if s.passes_per_dollar is not None else "—"
        lines.append(
            f"| `{s.candidate_name}` | `{s.model}` | "
            f"{s.passes}/{s.judged} ({_fmt_pct(s.passes, s.judged)}) | "
            f"{s.mean_score:.2f} | "
            f"{_fmt_pct(s.grounded_count, s.judged)} | "
            f"{s.winner_count}/{s.judged} | "
            f"{s.consensus_match_count}/{s.judged} | "
            f"{s.completed}/{s.trials} | "
            f"{s.median_duration_s:.1f}s | "
            f"{s.mean_tool_calls:.1f} | "
            f"{100.0 * s.tool_error_rate:.0f}% | "
            f"{100.0 * s.cache_hit_rate:.0f}% | "
            f"${s.mean_cost_usd:.3f} | "
            f"${s.total_cost_usd:.2f} | "
            f"{ppd} |"
        )

    # Consensus breakdown
    if latest_judgements:
        consensus_counts = Counter(j.consensus_status for j in latest_judgements)
        lines.extend([
            "",
            "## Cross-Model Consensus",
            "",
            "| Consensus Status | Cases | Share |",
            "| --- | ---: | ---: |",
        ])
        for status in (
            "all_agree",
            "majority_agree",
            "divergent",
            "all_blocked_or_failed",
        ):
            count = consensus_counts.get(status, 0)
            lines.append(
                f"| `{status}` | {count} | {_fmt_pct(count, len(latest_judgements))} |"
            )

    # Pairwise lift vs baseline
    cand_names = [s.candidate_name for s in summaries]
    if latest_judgements and baseline_candidate in cand_names:
        lines.extend([
            "",
            f"## Pairwise Lift vs `{baseline_candidate}`",
            "",
            "| Challenger | Shared Cases | Pass Lift | W / T / L (Score) | Mean Score Δ | Cost Δ | Cost / Extra Pass |",
            "| --- | ---: | ---: | :---: | ---: | ---: | ---: |",
        ])
        for cand in cand_names:
            if cand == baseline_candidate:
                continue
            lift = compute_pairwise_lift(
                latest_trials,
                latest_judgements,
                baseline=baseline_candidate,
                challenger=cand,
            )
            cpep = (
                f"${lift.cost_per_extra_pass_usd:.2f}"
                if lift.cost_per_extra_pass_usd is not None
                else "—"
            )
            lines.append(
                f"| `{cand}` | {lift.shared_cases} | "
                f"{lift.pass_lift:+d} ({lift.challenger_passes} vs {lift.baseline_passes}) | "
                f"{lift.challenger_wins} / {lift.ties} / {lift.baseline_wins} | "
                f"{lift.mean_score_delta:+.2f} | "
                f"{lift.cost_delta_usd:+.2f} USD | "
                f"{cpep} |"
            )

    # Category breakdown
    if latest_judgements and cand_names:
        by_cat: dict[str, list[CaseJudgement]] = defaultdict(list)
        for j in latest_judgements:
            by_cat[j.category].append(j)
        header = (
            "| Category | Cases | " + " | ".join(f"`{c}`" for c in cand_names) + " |"
        )
        sep = "| --- | ---: | " + " | ".join("---:" for _ in cand_names) + " |"
        lines.extend([
            "",
            "## Mean Score by Category (Pass Count)",
            "",
            header,
            sep,
        ])
        for cat in sorted(by_cat):
            cat_js = by_cat[cat]
            cells: list[str] = []
            for cand in cand_names:
                scores = [
                    g.score
                    for j in cat_js
                    for g in j.grades
                    if g.candidate_name == cand
                ]
                if not scores:
                    cells.append("—")
                else:
                    p = sum(1 for sc in scores if sc >= PASS_SCORE_THRESHOLD)
                    cells.append(f"{statistics.mean(scores):.1f} ({p}/{len(scores)})")
            lines.append(f"| `{cat}` | {len(cat_js)} | " + " | ".join(cells) + " |")

    # Case-level disagreement diagnostics (local report ONLY — omitted when sanitized)
    if not sanitized and latest_judgements:
        lines.extend([
            "",
            "## Case-Level Outcomes & Disagreements",
            "",
            "| Case | Category | Consensus | "
            + " | ".join(f"`{c}`" for c in cand_names)
            + " | Notes |",
            "| --- | --- | --- | "
            + " | ".join("---:" for _ in cand_names)
            + " | --- |",
        ])
        for j in sorted(latest_judgements, key=lambda x: (x.category, x.case_id)):
            grade_map = {g.candidate_name: g for g in j.grades}
            score_cells = [
                f"{grade_map[c].score} ({grade_map[c].outcome})"
                if c in grade_map
                else "—"
                for c in cand_names
            ]
            note = j.disagreement_summary.replace("|", "/").replace("\n", " ")[:180]
            lines.append(
                f"| `{j.case_id}` | `{j.category}` | `{j.consensus_status}` | "
                + " | ".join(score_cells)
                + f" | {note} |"
            )

    lines.append("")
    return "\n".join(lines)
