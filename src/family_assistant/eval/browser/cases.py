"""Case schemas, raw-export loading, and filtering for browser task evals."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from family_assistant.eval.private_paths import resolve_private_eval_path

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence
    from pathlib import Path

__all__ = [
    "DEFAULT_EXCLUDED_FLAGS",
    "BrowserEvalCase",
    "BrowserEvalDataset",
    "filter_cases",
    "load_browser_cases",
]

# Flags that mark cases unsuitable for unattended live browser evals by default:
# - privacy-sensitive personal event pages
# - expired tracking numbers
# - bot-detection / Cloudflare Turnstile probes (must not automate anti-bot gates)
# - non-standalone follow-ups that depend on an earlier turn's reply
# - date-relative prompts anchored to a specific past date
DEFAULT_EXCLUDED_FLAGS: frozenset[str] = frozenset({
    "private_event_page",
    "ephemeral_tracking_number",
    "cloudflare_turnstile",
    "own_test_site",
    "date_relative",
})


class BrowserEvalCase(BaseModel):
    """One browser task extracted from historical ``browser_profile`` turns."""

    id: str
    category: str
    task: str
    flags: list[str] = Field(default_factory=list)
    started_at: str | None = None
    duration_s: float | None = None
    tool_calls: int | None = None
    tool_sequence: list[str] = Field(default_factory=list)
    providers: list[str] | None = None
    original_final_answer: str | None = None
    turn_id: str | None = None
    conversation_id: str | None = None


class BrowserEvalDataset(BaseModel):
    """Metadata plus cases loaded from a private browser eval file."""

    description: str = ""
    extracted_at: str | None = None
    source: str | None = None
    cases: list[BrowserEvalCase] = Field(default_factory=list)


# ast-grep-ignore: no-dict-any - parses external query-export JSON shapes
def _extract_eval_set_payload(raw: object) -> dict[str, Any]:
    """Unwrap either raw SQL query export (``.rows[0].eval_set``) or direct dataset JSON."""
    if isinstance(raw, list):
        return {"cases": raw}
    if not isinstance(raw, dict):
        raise ValueError(
            f"Unsupported browser eval JSON top-level type: {type(raw).__name__}"
        )
    if "cases" in raw and isinstance(raw["cases"], list):
        return raw
    rows = raw.get("rows")
    if isinstance(rows, list) and rows:
        first = rows[0]
        if isinstance(first, dict) and isinstance(first.get("eval_set"), dict):
            return first["eval_set"]
    raise ValueError(
        "Could not locate browser eval cases: expected top-level 'cases' list "
        "or '.rows[0].eval_set.cases'."
    )


def load_browser_cases(
    path: str | Path, *, require_private_path: bool = True
) -> BrowserEvalDataset:
    """Load browser eval cases from a JSON file.

    Household-derived eval sets must live under ``.review-eval-local/``; callers
    pass ``require_private_path=False`` only for synthetic unit-test fixtures.
    """
    resolved = resolve_private_eval_path(path) if require_private_path else path
    with open(resolved, encoding="utf-8") as handle:
        raw = json.load(handle)
    payload = _extract_eval_set_payload(raw)
    return BrowserEvalDataset.model_validate(payload)


def filter_cases(
    cases: Sequence[BrowserEvalCase],
    *,
    include_ids: Iterable[str] | None = None,
    exclude_ids: Iterable[str] | None = None,
    include_categories: Iterable[str] | None = None,
    exclude_categories: Iterable[str] | None = None,
    exclude_flags: Iterable[str] | None = DEFAULT_EXCLUDED_FLAGS,
    exclude_followup_flags: bool = True,
    max_cases: int | None = None,
) -> list[BrowserEvalCase]:
    """Filter browser eval cases by IDs, categories, and flags while preserving order."""
    include_id_set = set(include_ids) if include_ids is not None else None
    exclude_id_set = set(exclude_ids or ())
    include_cat_set = (
        set(include_categories) if include_categories is not None else None
    )
    exclude_cat_set = set(exclude_categories or ())
    exclude_flag_set = set(exclude_flags or ())

    selected: list[BrowserEvalCase] = []
    for case in cases:
        if include_id_set is not None and case.id not in include_id_set:
            continue
        if case.id in exclude_id_set:
            continue
        if include_cat_set is not None and case.category not in include_cat_set:
            continue
        if case.category in exclude_cat_set:
            continue
        if exclude_flag_set and (set(case.flags) & exclude_flag_set):
            continue
        if exclude_followup_flags and any(
            flag.startswith("followup_") for flag in case.flags
        ):
            continue
        selected.append(case)
        if max_cases is not None and len(selected) >= max_cases:
            break
    return selected
