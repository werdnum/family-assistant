"""Machine- and human-readable reporting for eval runs.

Per-source and per-attack-class breakouts are mandatory: a judge that scores
well only on one corpus's house style must be visible rather than averaged away.
"""

from __future__ import annotations

import statistics
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field

from family_assistant.eval.tool_call_review.scoring import (
    GateEvaluation,
    TrialClassification,
    TrialRecord,
    evaluate_gate,
    seed_flips,
)

if TYPE_CHECKING:
    from collections.abc import Callable

__all__ = ["EvalReport", "LatencyStats", "SliceMetrics", "build_slice_metrics"]


class LatencyStats(BaseModel):
    """Latency distribution across a set of trials, in milliseconds."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    count: int
    min_ms: float
    median_ms: float
    p95_ms: float
    max_ms: float

    @classmethod
    def from_latencies(cls, latencies: list[float]) -> LatencyStats:
        """Summarize a list of latencies; zeros when empty."""
        if not latencies:
            return cls(count=0, min_ms=0.0, median_ms=0.0, p95_ms=0.0, max_ms=0.0)
        ordered = sorted(latencies)
        index = _quantile_index(len(ordered), 0.95)
        return cls(
            count=len(ordered),
            min_ms=ordered[0],
            median_ms=statistics.median(ordered),
            p95_ms=ordered[index],
            max_ms=ordered[-1],
        )


def _quantile_index(length: int, quantile: float) -> int:
    """Return a clamped index into a sorted list of ``length`` for a quantile."""
    if length == 0:
        return 0
    raw = round(quantile * (length - 1))
    return min(length - 1, max(0, raw))


class SliceMetrics(BaseModel):
    """Aggregated metrics for one slice of trials."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    dimension: str
    total_trials: int
    attack_trials: int
    benign_trials: int
    clean_trials: int
    inconclusive_trials: int
    attack_allow_rate: float
    attack_confirm_rate: float
    benign_deny_or_confirm_rate: float
    seed_flip_case_ids: list[str] = Field(default_factory=list)
    latency: LatencyStats


def _rate(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 0.0


def build_slice_metrics(
    name: str,
    dimension: str,
    trials: list[TrialRecord],
) -> SliceMetrics:
    """Aggregate one slice of trials into its reported metrics.

    Attack and friction rates are computed over genuine model verdicts only:
    fallback-resolved trials are inconclusive, not signal, and lumping them in
    would understate the judge's allow rate and overstate its friction.
    """
    attack = [trial for trial in trials if trial.label == "attack"]
    benign = [trial for trial in trials if trial.label == "benign"]
    attack_model = [trial for trial in attack if trial.is_model_verdict]
    benign_model = [trial for trial in benign if trial.is_model_verdict]
    attack_allow = sum(
        1
        for trial in attack_model
        if trial.classification is TrialClassification.SECURITY_FAILURE
    )
    attack_confirm = sum(
        1
        for trial in attack_model
        if trial.classification is TrialClassification.WEAK_PASS
    )
    benign_friction = sum(1 for trial in benign_model if trial.is_friction)
    return SliceMetrics(
        name=name,
        dimension=dimension,
        total_trials=len(trials),
        attack_trials=len(attack),
        benign_trials=len(benign),
        clean_trials=sum(1 for trial in trials if trial.is_clean_trial),
        inconclusive_trials=sum(1 for trial in trials if not trial.is_model_verdict),
        attack_allow_rate=_rate(attack_allow, len(attack_model)),
        attack_confirm_rate=_rate(attack_confirm, len(attack_model)),
        benign_deny_or_confirm_rate=_rate(benign_friction, len(benign_model)),
        seed_flip_case_ids=sorted(seed_flips(trials)),
        latency=LatencyStats.from_latencies([trial.latency_ms for trial in trials]),
    )


class EvalReport(BaseModel):
    """Full record of one eval run: every trial plus run metadata."""

    model_config = ConfigDict(extra="forbid")

    trials: list[TrialRecord] = Field(default_factory=list)
    skipped_case_ids: list[str] = Field(default_factory=list)
    seeds: int
    provider: str | None = None
    model: str | None = None
    dataset_hash: str | None = None
    generated_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())

    def slices(
        self, dimension: str, key: Callable[[TrialRecord], str | None]
    ) -> list[SliceMetrics]:
        """Group trials by a key and build metrics for each non-empty group."""
        grouped: dict[str, list[TrialRecord]] = {}
        for trial in self.trials:
            value = key(trial)
            if value is None:
                continue
            grouped.setdefault(value, []).append(trial)
        return [
            build_slice_metrics(name, dimension, group)
            for name, group in sorted(grouped.items())
        ]

    def overall_metrics(self) -> SliceMetrics:
        """Metrics across every trial in the run."""
        return build_slice_metrics("all", "overall", self.trials)

    def source_slices(self) -> list[SliceMetrics]:
        """Per-source-dataset metrics (mandatory breakout)."""
        return self.slices("source", lambda trial: trial.source)

    def attack_class_slices(self) -> list[SliceMetrics]:
        """Per-attack-class metrics (mandatory breakout)."""
        return self.slices("attack_class", lambda trial: trial.attack_class)

    def failing_and_weak_reasons(self) -> list[dict[str, str]]:
        """Retain the judge's reasons for security failures and weak passes.

        Reading *why* the judge allowed the attack it allowed is the eval's most
        useful output for prompt iteration, so these reasons are preserved.
        """
        kept = []
        for trial in self.trials:
            if trial.classification in {
                TrialClassification.SECURITY_FAILURE,
                TrialClassification.WEAK_PASS,
                TrialClassification.EXPECTATION_MISSED,
            }:
                kept.append({
                    "case_id": trial.case_id,
                    "classification": trial.classification.value,
                    "verdict": trial.verdict.value,
                    "reason": trial.reason,
                })
        return kept

    def gate(self, ceiling: float) -> GateEvaluation:
        """Evaluate the whole-run attack slice against a false-allow ceiling."""
        return evaluate_gate(self.trials, ceiling)

    def to_json_dict(self) -> dict[str, object]:
        """Serialize the full run to a JSON-safe dict."""
        return self.model_dump(mode="json")

    def to_text_summary(self, *, gate_ceiling: float | None = None) -> str:
        """Render a human-readable per-slice summary of the run."""
        lines: list[str] = []
        lines.append("Tool-call review eval summary")
        lines.append(
            f"  provider={self.provider or '-'} model={self.model or '-'} "
            f"seeds={self.seeds}"
        )
        lines.append(
            f"  cases_run={len({trial.case_id for trial in self.trials})} "
            f"trials={len(self.trials)} skipped={len(self.skipped_case_ids)}"
        )
        if self.dataset_hash:
            lines.append(f"  dataset_hash={self.dataset_hash}")
        lines.append("")
        lines.append("Overall:")
        lines.extend(_format_slice(self.overall_metrics()))
        lines.append("")
        lines.append("By source dataset:")
        for metrics in self.source_slices():
            lines.extend(_format_slice(metrics))
        lines.append("")
        lines.append("By attack class:")
        attack_class_slices = self.attack_class_slices()
        if not attack_class_slices:
            lines.append("  (no attack_class-labeled cases)")
        for metrics in attack_class_slices:
            lines.extend(_format_slice(metrics))

        reasons = self.failing_and_weak_reasons()
        if reasons:
            lines.append("")
            lines.append("Failing and weak-pass reasons:")
            for entry in reasons:
                lines.append(
                    f"  [{entry['classification']}] {entry['case_id']} "
                    f"-> {entry['verdict']}: {entry['reason']}"
                )

        if gate_ceiling is not None:
            lines.append("")
            gate = self.gate(gate_ceiling)
            lines.append(
                f"Gate (ceiling={gate_ceiling:.2%}): {gate.status.value.upper()} "
                f"-- {gate.reason}"
            )
            lines.append(
                f"  clean_trials={gate.clean_trials}/{gate.required_clean_trials} "
                f"inconclusive={gate.inconclusive_trials} "
                f"observed_allows={gate.observed_allows}"
            )
        return "\n".join(lines)


def _format_slice(metrics: SliceMetrics) -> list[str]:
    return [
        f"  {metrics.name} (trials={metrics.total_trials}, "
        f"attack={metrics.attack_trials}, benign={metrics.benign_trials})",
        f"    attack allow={metrics.attack_allow_rate:.2%} "
        f"confirm={metrics.attack_confirm_rate:.2%}; "
        f"benign friction={metrics.benign_deny_or_confirm_rate:.2%}",
        f"    clean={metrics.clean_trials} inconclusive={metrics.inconclusive_trials} "
        f"seed_flips={len(metrics.seed_flip_case_ids)}; "
        f"latency p50={metrics.latency.median_ms:.0f}ms "
        f"p95={metrics.latency.p95_ms:.0f}ms",
    ]
