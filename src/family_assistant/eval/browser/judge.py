"""Blind cross-model consensus and comparative LLM judge for browser evals.

Because live web pages change (prices, stock, headlines, specials), exact string
answer keys go stale quickly. Running all candidate models in the same window
and judging them *side by side* — blinded with randomized labels per case —
unlocks two complementary signals:

1. **Cross-model consensus**: when every candidate returns the same grounded
   facts, the cheapest model is empirically at least as good as the most
   expensive on that task, even when live site content drifted from the
   historical reference answer.
2. **Disagreement diagnosis**: when candidates diverge (one extracted 23 rows
   while another stopped at 10, or one hit a cookie banner while another got the
   live parking quote), the side-by-side comparison makes the failure obvious.
"""

from __future__ import annotations

import hashlib
import random
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, Field

from family_assistant.llm.messages import SystemMessage, UserMessage

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from family_assistant.eval.browser.cases import BrowserEvalCase
    from family_assistant.eval.browser.runner import BrowserEvalTrial
    from family_assistant.llm import LLMInterface

__all__ = [
    "BlindCandidateGrade",
    "BlindCaseJudgement",
    "CandidateGrade",
    "CaseJudgement",
    "ConsensusStatus",
    "OutcomeLabel",
    "append_judgement_jsonl",
    "assign_blind_labels",
    "compute_trial_set_fingerprint",
    "judge_case_trials",
    "load_judgements_jsonl",
]

OutcomeLabel = Literal[
    "complete",
    "partial",
    "blocked_honestly",
    "failed",
    "hallucinated",
]
ConsensusStatus = Literal[
    "all_agree",
    "majority_agree",
    "divergent",
    "all_blocked_or_failed",
]

_JUDGE_SYSTEM_PROMPT = """\
You are an impartial evaluator comparing multiple anonymized browser agents that \
each ran the SAME web browsing task live at roughly the same time.

Your job is to score each candidate fairly on how well it answered the user's \
task using REAL browser observations, and to determine whether the candidates \
reached consensus.

Critical grading rules:
1. **Live sites change.** A historical reference answer from an earlier date may \
be provided to show what a complete answer looks like and to verify *stable* \
facts (e.g., an old GitHub PR, a fixed recipe, a 3D model page, IRS rules). \
NEVER penalize a candidate because live prices, stock, headlines, specials, or \
future availability differ from the historical reference! Instead, compare \
candidates against each other (since they ran live at the same time) and against \
whether their claims are grounded in actual tool calls.
2. **Grounding matters most.** Look at each candidate's tool telemetry (status, \
tool call count, error count, tool trace summary). If a candidate answered \
confidently from prior knowledge without actually loading the relevant page, or \
fabricated prices/stock/URLs that contradict other grounded candidates, mark \
`grounded=false` and `outcome="hallucinated"` with score 0 or 1.
3. **Honest blocks vs silent failures.** Some real pages have login walls \
(e.g. MakerWorld downloads, Instagram/Facebook) or anti-bot/Cloudflare blocks. \
If a page genuinely blocked anonymous access and the candidate still extracted \
all publicly visible info AND clearly reported the block (or appropriately \
requested handoff / credential delegation), score it well (4-5 if nothing more \
was possible, or `blocked_honestly` if blocked completely). However, if ANOTHER \
candidate succeeded on the exact same page at the same time without a login, \
then giving up is a failure, not an unavoidable block!
4. **Scoring rubric (0-5 integer):**
   - 5: Complete, accurate, well-grounded answer addressing every part of the prompt.
   - 4: Substantially complete and accurate; only minor omissions or formatting flaws.
   - 3: Partial success — got useful grounded info but missed notable requested items/details.
   - 2: Weak/mostly incomplete — made some progress but failed the core goal.
   - 1: Wrong, unhelpful, hit iteration/timeout limit with almost nothing useful, or ungrounded.
   - 0: Crashed/empty/timeout with no usable answer, or hallucinated facts.
5. **Consensus:**
   - `all_agree`: All candidates arrived at essentially the same grounded answer (score >= 4) \
     or equivalent key facts/prices/conclusions.
   - `majority_agree`: Most candidates agree on the key grounded facts while 1-2 missed/failed.
   - `divergent`: Candidates materially disagree on key facts or completeness.
   - `all_blocked_or_failed`: Every candidate was blocked, timed out, or failed.
6. **Constraints:** Check `followed_constraints=false` if the prompt forbade \
booking/checkout/guessing and the candidate violated it (or ignored explicit \
verbatim/completeness requirements).
"""


class BlindCandidateGrade(BaseModel):
    label: str = Field(description="Candidate label such as 'A', 'B', 'C'.")
    outcome: OutcomeLabel
    score: int = Field(ge=0, le=5)
    grounded: bool
    followed_constraints: bool
    rationale: str = Field(
        description="1-2 concise sentences explaining the score and any concrete omissions/errors."
    )


class BlindCaseJudgement(BaseModel):
    grades: list[BlindCandidateGrade]
    consensus_status: ConsensusStatus
    consensus_labels: list[str] = Field(
        default_factory=list,
        description="Labels of candidates that agree with the strongest grounded consensus answer.",
    )
    winner_labels: list[str] = Field(
        default_factory=list,
        description="Label(s) of the best candidate(s) on this case (include all tied top candidates).",
    )
    disagreement_summary: str = Field(
        default="",
        description=(
            "If candidates differed, concisely state the concrete difference "
            "(no model names). Empty string if all candidates agree."
        ),
    )


class CandidateGrade(BaseModel):
    """Unblinded grade for one candidate on one browser eval case."""

    candidate_name: str
    blind_label: str
    outcome: OutcomeLabel
    score: int = Field(ge=0, le=5)
    grounded: bool
    followed_constraints: bool
    rationale: str


class CaseJudgement(BaseModel):
    """Persisted comparative judgement for one case across all candidates."""

    case_id: str
    category: str
    flags: list[str] = Field(default_factory=list)
    judge_model: str
    trial_fingerprint: str | None = None
    consensus_status: ConsensusStatus
    consensus_candidates: list[str] = Field(default_factory=list)
    winner_candidates: list[str] = Field(default_factory=list)
    disagreement_summary: str = ""
    grades: list[CandidateGrade] = Field(default_factory=list)


def compute_trial_set_fingerprint(trials: Sequence[BrowserEvalTrial]) -> str:
    """Deterministic digest of the latest trial set for a case.

    Used by ``judge --resume`` so a case is automatically rejudged whenever new
    candidates are added or any existing candidate is re-run.
    """
    hasher = hashlib.sha256()
    for trial in sorted(trials, key=lambda t: t.candidate_name):
        reply_digest = hashlib.sha256(
            (trial.final_reply or "").encode("utf-8")
        ).hexdigest()[:16]
        row = (
            f"{trial.candidate_name}|{trial.started_at}|{trial.status}|"
            f"{trial.requests}|{trial.tool_calls}|{reply_digest}\n"
        )
        hasher.update(row.encode("utf-8"))
    return hasher.hexdigest()[:16]


def assign_blind_labels(
    case_id: str,
    candidate_names: Sequence[str],
    *,
    seed_salt: str = "browser-eval-v1",
) -> dict[str, str]:
    """Deterministically shuffle ``candidate_names`` per case into labels A, B, C...

    Returns a mapping ``{blind_label: candidate_name}``.
    """
    ordered = sorted(candidate_names)
    digest = hashlib.sha256(f"{seed_salt}:{case_id}".encode()).digest()
    rng = random.Random(int.from_bytes(digest[:8], "big"))
    rng.shuffle(ordered)
    labels: dict[str, str] = {}
    for index, name in enumerate(ordered):
        label = chr(ord("A") + index) if index < 26 else f"C{index + 1}"
        labels[label] = name
    return labels


def _summarize_trial_for_judge(
    label: str,
    trial: BrowserEvalTrial,
    *,
    max_reply_chars: int = 4500,
) -> str:
    trace_lines: list[str] = []
    for entry in trial.tool_trace[:20]:
        line = (
            f"  - {entry.name}({entry.arguments_preview[:100]}) -> "
            f"{'ok' if entry.ok else 'ERR: ' + (entry.error_preview or 'failed')} "
            f"({entry.output_chars} chars{', img' if entry.has_image else ''})"
        )
        if entry.output_preview:
            line += f"\n      observed: {entry.output_preview[:300]}"
        trace_lines.append(line)
    if len(trial.tool_trace) > 20:
        trace_lines.append(f"  - ... ({len(trial.tool_trace) - 20} more tool calls)")
    trace_block = "\n".join(trace_lines) if trace_lines else "  (no tool calls made)"
    reply = trial.final_reply or "(no final reply)"
    if len(reply) > max_reply_chars:
        reply = reply[:max_reply_chars] + "\n[... truncated ...]"
    extra_notes: list[str] = []
    if trial.requested_handoff:
        extra_notes.append("requested human browser handoff")
    if trial.attempted_delegations:
        targets = sorted({
            str(d.get("target_service_id", "?")) for d in trial.attempted_delegations
        })
        extra_notes.append(f"attempted delegation to: {', '.join(targets)}")
    if trial.error_message:
        extra_notes.append(f"run error: {trial.error_message}")
    notes_str = f"\nNotes: {'; '.join(extra_notes)}" if extra_notes else ""
    return (
        f"### Candidate {label}\n"
        f"Run status: {trial.status} | Requests: {trial.requests} | "
        f"Tool calls: {trial.tool_calls} (errors: {trial.tool_error_count}) | "
        f"Duration: {trial.duration_s:.1f}s{notes_str}\n"
        f"Tool trace (first 20):\n{trace_block}\n"
        f"Final answer:\n{reply}\n"
    )


def _build_judge_user_prompt(
    case: BrowserEvalCase,
    labeled_trials: Sequence[tuple[str, BrowserEvalTrial]],
    *,
    max_ref_chars: int = 3000,
) -> str:
    ref = (case.original_final_answer or "").strip()
    if len(ref) > max_ref_chars:
        ref = ref[:max_ref_chars] + "\n[... truncated ...]"
    ref_section = (
        f"## Historical Reference Answer (from {case.started_at or 'earlier date'} — "
        "LIVE CONTENT MAY HAVE CHANGED)\n"
        f"{ref}\n"
        if ref
        else "## Historical Reference Answer\n(None available)\n"
    )
    candidates_block = "\n".join(
        _summarize_trial_for_judge(label, trial) for label, trial in labeled_trials
    )
    flags_text = ", ".join(case.flags) if case.flags else "none"
    return (
        f"## Task Prompt\n"
        f"Category: {case.category} | Flags: {flags_text}\n\n"
        f"{case.task}\n\n"
        f"{ref_section}\n"
        f"## Candidate Runs (Anonymized)\n\n"
        f"{candidates_block}\n"
        f"Grade every candidate label ({', '.join(label for label, _ in labeled_trials)}) "
        f"and report consensus and winners."
    )


async def judge_case_trials(
    judge_client: LLMInterface,
    *,
    judge_model: str,
    case: BrowserEvalCase,
    trials: Sequence[BrowserEvalTrial],
) -> CaseJudgement:
    """Blindly compare and grade all candidate trials for one case."""
    by_candidate = {trial.candidate_name: trial for trial in trials}
    label_to_candidate = assign_blind_labels(case.id, list(by_candidate.keys()))
    labeled_trials = [
        (label, by_candidate[cand])
        for label, cand in sorted(label_to_candidate.items())
    ]

    messages = [
        SystemMessage(content=_JUDGE_SYSTEM_PROMPT),
        UserMessage(content=_build_judge_user_prompt(case, labeled_trials)),
    ]
    blind_result = await judge_client.generate_structured(messages, BlindCaseJudgement)

    grades: list[CandidateGrade] = []
    seen_candidates: set[str] = set()
    for blind_grade in blind_result.grades:
        cand_name = label_to_candidate.get(blind_grade.label)
        if cand_name is None or cand_name in seen_candidates:
            continue
        seen_candidates.add(cand_name)
        grades.append(
            CandidateGrade(
                candidate_name=cand_name,
                blind_label=blind_grade.label,
                outcome=blind_grade.outcome,
                score=blind_grade.score,
                grounded=blind_grade.grounded,
                followed_constraints=blind_grade.followed_constraints,
                rationale=blind_grade.rationale,
            )
        )
    # Ensure every candidate trial has a grade even if the judge omitted a label.
    for label, cand_name in sorted(label_to_candidate.items()):
        if cand_name not in seen_candidates:
            grades.append(
                CandidateGrade(
                    candidate_name=cand_name,
                    blind_label=label,
                    outcome="failed",
                    score=0,
                    grounded=False,
                    followed_constraints=False,
                    rationale="Judge omitted this candidate label.",
                )
            )
    grades.sort(key=lambda g: g.candidate_name)

    consensus_candidates = sorted({
        label_to_candidate[lbl]
        for lbl in blind_result.consensus_labels
        if lbl in label_to_candidate
    })
    winner_candidates = sorted({
        label_to_candidate[lbl]
        for lbl in blind_result.winner_labels
        if lbl in label_to_candidate
    })
    return CaseJudgement(
        case_id=case.id,
        category=case.category,
        flags=list(case.flags),
        judge_model=judge_model,
        trial_fingerprint=compute_trial_set_fingerprint(list(by_candidate.values())),
        consensus_status=blind_result.consensus_status,
        consensus_candidates=consensus_candidates,
        winner_candidates=winner_candidates,
        disagreement_summary=blind_result.disagreement_summary,
        grades=grades,
    )


def append_judgement_jsonl(path: Path, judgement: CaseJudgement) -> None:
    """Append one case judgement as a single JSON line."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(judgement.model_dump_json() + "\n")


def load_judgements_jsonl(path: Path) -> list[CaseJudgement]:
    """Load persisted case judgements from a JSONL file."""
    if not path.exists():
        return []
    judgements: list[CaseJudgement] = []
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            stripped = line.strip()
            if not stripped:
                continue
            judgements.append(CaseJudgement.model_validate_json(stripped))
    return judgements
