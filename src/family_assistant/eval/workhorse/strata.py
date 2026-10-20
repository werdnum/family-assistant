"""Classify turns into strata and draw a stratified candidate sample.

The classifiers here are coarse on purpose. They exist to spread a sample of
a few dozen cases across the workload and to weight results back to it, so a
turn that lands in a neighbouring bucket costs a little representativeness,
not correctness; curation is where each candidate is actually read.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections import Counter, defaultdict
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:
    from collections.abc import Sequence

    from family_assistant.eval.workhorse.turn_records import TurnRecord

__all__ = [
    "DEFAULT_TEMPLATED_PREFIXES",
    "SampledTurn",
    "Stratum",
    "allocate_picks",
    "intent_bucket",
    "sample_candidates",
    "size_bucket",
    "stratum_frequencies",
    "stratum_of",
    "template_key",
    "tool_family",
]

# Automation prompts a scheduled job writes, not a person. They dominate the
# raw turn count and would swamp every per-turn statistic if left in the
# human population.
DEFAULT_TEMPLATED_PREFIXES: tuple[str, ...] = (
    "Generate an e-ink display",
    "System Callback Trigger",
    "Create ONE self-contained",
)

# First match wins, so narrower families precede the broad patterns after them.
_FAMILY_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "meta",
        re.compile(
            r"^(activate_tools|get_user_documentation_content|"
            r"report_technical_problem|finish_thread)$"
        ),
    ),
    (
        "delegation",
        re.compile(r"^(delegate_to_service|get_delegation_status|spawn_worker)"),
    ),
    ("calendar", re.compile(r"calendar")),
    (
        "scheduling",
        re.compile(
            r"(callback|reminder|^schedule_|automation(?!s?_config)|_event_listener)"
        ),
    ),
    ("notes", re.compile(r"(^|_)notes?($|_)|^propose_memory_edits$")),
    (
        "documents",
        re.compile(r"^(search_documents|get_full_document_content|ingest_)"),
    ),
    ("google", re.compile(r"^(gmail_|drive_|google_(?!shopping))")),
    ("home", re.compile(r"^ha_|home_assistant|camera")),
    ("tasks", re.compile(r"^(tuit_|find_tasks|get_task|create_task|update_task)")),
    (
        "travel_weather",
        re.compile(r"(trip|transport|departure|weather|maps_|directions)"),
    ),
    (
        "web",
        re.compile(
            r"(web_search|search_web|shopping|amazon_|scrape|fetch_url|browse|"
            r"brave_|web_fetch)"
        ),
    ),
    (
        "code_data",
        re.compile(
            r"^(execute_|jq_query|query_|list_tables|get_table_schema|"
            r"get_script|list_scripts|run_script)"
        ),
    ),
    (
        "media",
        re.compile(r"(image|attachment|attach_to_response|transcribe|audio|video)"),
    ),
    ("history", re.compile(r"message_history|conversation")),
    ("messaging", re.compile(r"(send_|notify|message)")),
)

_INTENT_RULES: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (name, re.compile(pattern, re.IGNORECASE | re.DOTALL))
    for name, pattern in (
        ("reminder", r"\b(remind|reminder)\b"),
        (
            "calendar write",
            r"(?=.*\b(calendar|appointment|event|booked|booking)\b)"
            r"(?=.*\b(add|put|create|book|schedule|move|change|cancel|delete|reschedule)\b)",
        ),
        (
            "agenda lookup",
            r"\b(what|when|anything|something|do (i|we) have)\b.{0,40}"
            r"\b(on|today|tomorrow|this week|next week|weekend|calendar)\b",
        ),
        ("shopping list", r"\b(shopping|groceries|grocery)\b"),
        ("note/memory", r"\b(note|notes|write down|jot|record this|remember)\b"),
        (
            "smart home",
            r"\b(turn (on|off)|lights?|vacuum|heater|aircon|air con|thermostat|"
            r"garage|blinds?|floodlight|inkplate|e-ink|display)\b",
        ),
        ("weather", r"\b(weather|rain|forecast|temperature)\b"),
        (
            "email/drive/docs",
            r"\b(email|gmail|drive|document|pdf|file|spreadsheet)\b",
        ),
        (
            "web lookup",
            r"\b(search|look up|find out|google|price|cost|buy|order|amazon|"
            r"cheapest|compare)\b",
        ),
        ("media", r"\b(photo|picture|image|screenshot|video|draw|generate)\b"),
        ("tasks", r"\b(task|todo|to-do|chore|project|tuit)\b"),
        ("automation", r"\b(automation|every (day|week|morning)|daily|weekly)\b"),
        (
            "self-diagnosis",
            r"\b(engineer|debug|error|why (did|didn|is|isn)|not working|broken|bug)\b",
        ),
        ("other question", r"\?"),
    )
)

_SIZE_BUCKETS: tuple[tuple[int, str], ...] = (
    (0, "0"),
    (3, "1-3"),
    (7, "4-7"),
    (15, "8-15"),
)


def tool_family(name: str) -> str:
    """Map a tool name to the coarse family the coverage table is written in."""
    for family, pattern in _FAMILY_RULES:
        if pattern.search(name):
            return family
    return "other"


def intent_bucket(prompt: str) -> str:
    """Guess what a prompt asks for from keywords; ``other`` when nothing fits."""
    for name, pattern in _INTENT_RULES:
        if pattern.search(prompt):
            return name
    return "other"


def template_key(prompt: str, prefixes: Sequence[str]) -> str | None:
    """Return the templated-automation prefix a prompt starts with, if any."""
    stripped = prompt.lstrip()
    return next((prefix for prefix in prefixes if stripped.startswith(prefix)), None)


def size_bucket(tool_call_count: int) -> str:
    for ceiling, label in _SIZE_BUCKETS:
        if tool_call_count <= ceiling:
            return label
    return "16+"


class Stratum(BaseModel):
    """The cell of the workload a turn is sampled from and weighted back to.

    Turn size is not part of the cell: with a few dozen cases, a cell per
    size bucket as well would outnumber the picks. Picks are spread across
    sizes inside each cell instead.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    population: str
    profile: str
    family: str

    @property
    def key(self) -> str:
        return f"{self.population}/{self.profile}/{self.family}"


def stratum_of(record: TurnRecord) -> Stratum:
    """Templated turns are one stratum per template: their shape is fixed."""
    if record.template is not None:
        return Stratum(
            population="templated", profile=record.profile, family=record.template
        )
    return Stratum(
        population="human", profile=record.profile, family=record.primary_family
    )


class SampledTurn(BaseModel):
    """One candidate and the share of the workload it stands for."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    turn_id: str
    stratum: str
    weight: float
    negative_signal: bool


def _rank(seed: str, turn_id: str) -> str:
    return hashlib.sha256(f"{seed}:{turn_id}".encode()).hexdigest()


def allocate_picks(sizes: dict[str, int], target: int) -> dict[str, int]:
    """Split ``target`` picks across strata in proportion to sqrt(size).

    Square-root allocation is the usual compromise between following the
    workload (proportional) and covering it (equal per cell): a stratum four
    times larger gets twice the picks. Every stratum gets one pick while the
    target allows, largest first; the rest go one at a time to the stratum
    with the highest ``sqrt(size) / (picks + 1)``, which converges on the
    square-root shares without rounding drift, and none gets more than it
    holds.
    """
    order = sorted(sizes, key=lambda key: (-sizes[key], key))
    allocation = dict.fromkeys(order, 0)
    for key in order[: max(target, 0)]:
        allocation[key] = 1
    for _ in range(max(target - len(order), 0)):
        open_keys = [key for key in order if allocation[key] < sizes[key]]
        if not open_keys:
            break
        key = max(
            open_keys,
            key=lambda k: (math.sqrt(sizes[k]) / (allocation[k] + 1), -order.index(k)),
        )
        allocation[key] += 1
    return allocation


def _spread_by_size(turns: Sequence[TurnRecord], seed: str) -> list[TurnRecord]:
    """Order a stratum's turns round-robin across tool-call size buckets.

    The commonest size leads each round, so a stratum given a single pick
    takes its typical turn, and further picks reach the long tail.
    """
    by_size: dict[str, list[TurnRecord]] = defaultdict(list)
    for turn in sorted(turns, key=lambda t: _rank(seed, t.turn_id)):
        by_size[size_bucket(len(turn.tool_calls))].append(turn)
    queues = sorted(by_size.values(), key=lambda queue: -len(queue))
    ordered: list[TurnRecord] = []
    for round_index in range(max(len(queue) for queue in queues)):
        ordered.extend(
            queue[round_index] for queue in queues if round_index < len(queue)
        )
    return ordered


def sample_candidates(
    records: Sequence[TurnRecord],
    *,
    target: int,
    templated_per_template: int,
    negative_share: float,
    seed: str,
) -> list[SampledTurn]:
    """Draw a deterministic stratified sample of candidate turns.

    Human turns share ``target`` by square-root allocation; each templated
    automation contributes a fixed handful, since its turns differ only in
    the data they render. Within a stratum, up to ``negative_share`` of the
    picks go to turns with a negative signal, because the weak labels are
    rare and a uniform draw would show almost none of them. The order inside
    each group is a hash of ``seed`` and the turn id, so the same snapshot and
    seed give the same sample.
    """
    by_stratum: dict[str, list[TurnRecord]] = defaultdict(list)
    for record in records:
        by_stratum[stratum_of(record).key].append(record)

    human_sizes = {
        key: len(turns) for key, turns in by_stratum.items() if key.startswith("human/")
    }
    allocation = allocate_picks(human_sizes, target)
    for key, turns in by_stratum.items():
        if key.startswith("templated/"):
            allocation[key] = min(templated_per_template, len(turns))

    sampled: list[SampledTurn] = []
    for key in sorted(allocation):
        picks = allocation[key]
        if picks == 0:
            continue
        turns = by_stratum[key]
        ordered = _spread_by_size(turns, seed)
        negatives = [turn for turn in ordered if turn.negative_signal]
        negative_picks = min(len(negatives), math.floor(picks * negative_share))
        chosen = negatives[:negative_picks]
        chosen_ids = {turn.turn_id for turn in chosen}
        chosen += [turn for turn in ordered if turn.turn_id not in chosen_ids][
            : picks - negative_picks
        ]
        weight = len(turns) / len(chosen)
        sampled.extend(
            SampledTurn(
                turn_id=turn.turn_id,
                stratum=key,
                weight=weight,
                negative_signal=turn.negative_signal,
            )
            for turn in chosen
        )
    return sampled


def stratum_frequencies(records: Sequence[TurnRecord]) -> dict[str, int]:
    """How many turns of the extracted workload fall in each stratum."""
    counts = Counter(stratum_of(record).key for record in records)
    return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))
