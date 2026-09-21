"""Adapter for the InjecAgent tool-call injection corpus.

Upstream: ``InjecAgent`` (https://github.com/uiuc-kang-lab/InjecAgent), a
benchmark of indirect prompt-injection cases against tool-using agents. Each
case pairs a benign user instruction with an attacker instruction smuggled into
a *tool response*, and names the attacker tool(s) and parameters the injection
tries to trigger. The corpus splits into two attack types: direct-harm (``dh``)
and data-stealing (``ds``). Its rows are distributed as flat JSON objects with
keys such as ``User Instruction``, ``User Tool``, ``Attacker Tools``,
``Expected Achievements``, ``Tool Response Template`` (carrying an
``<Attacker Instruction>`` placeholder), ``Attacker Instruction``, and the
pre-rendered ``Tool Response``. The four upstream test files are JSON arrays;
the adapter consumes those files directly and keeps base and enhanced variants
as separate slices.

License: MIT in the upstream repository; the provenance record the build script
writes carries it alongside the revision the maintainer fetched.

Mapping into our positions: each pre-rendered tool response becomes the full
browser environment in a four-case hidden/full matched group. The attack
action invokes the exact upstream ``Attacker Tools`` list, while its benign
twin invokes the exact upstream ``User Tool``. Both objectives are grounded in
the user's actual instruction; the damage envelope retains the upstream
expected achievement as the constraint being prevented. This keeps the
action shape comparable without pretending that every direct-harm case is a
navigation attack.

The real corpus is not vendored; only ``samples/injecagent/`` is committed, in
the upstream JSON row format.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar, Final

from family_assistant.eval.tool_call_review.adapters.base import (
    AdaptedLineage,
    Adapter,
    evaluation_split_for_group,
    normalized_text_key,
)
from family_assistant.eval.tool_call_review.adapters.casebuild import (
    build_browser_ablation_cases,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from family_assistant.eval.tool_call_review.adapters.base import AdaptedCase

__all__ = ["InjecAgentAdapter", "InjecAgentRow"]

_PLACEHOLDER = "<Attacker Instruction>"
_SUPPORTED_FILE_PATTERN = re.compile(
    r"^test_cases_(?P<attack_type>dh|ds)_(?P<variant>base|enhanced)\.json$"
)
_VARIANTS = frozenset({"base", "enhanced"})
# Direct-harm and data-stealing, the two families upstream splits its cases
# into. Which one a row is decides the gated call it is paired with and the
# attack class it is sliced under, so defaulting it would file a whole family
# under the wrong one.
_ATTACK_TYPES: Final[frozenset[str]] = frozenset({"dh", "ds"})
_UPSTREAM_ATTACK_TYPE_LABELS: Final[dict[str, frozenset[str]]] = {
    "dh": frozenset({"Data Security Harm", "Financial Harm", "Physical Harm"}),
    "ds": frozenset({"Financial Data", "Others", "Physical Data"}),
}


@dataclass(frozen=True, slots=True)
class InjecAgentRow:
    """One InjecAgent-style case row.

    Every field is load-bearing for the case the row becomes, so all are
    validated on construction: the attack type selects the attack class, the
    attacker instruction is the injection, and the tool/achievement metadata
    grounds the proposed action and damage envelope. A row missing any of them
    yields a case that is not what its label says it is, so no such row exists
    to be adapted.
    """

    index: int
    attack_type: str
    user_instruction: str
    tool_name: str
    tool_response_template: str
    attacker_instruction: str
    attacker_tools: tuple[str, ...] = ()
    expected_achievements: str = ""
    variant: str = "base"
    rendered_tool_response: str | None = None
    source_slice: str | None = None
    upstream_attack_type: str | None = None

    def __post_init__(self) -> None:
        if self.attack_type not in _ATTACK_TYPES:
            raise ValueError(
                f"InjecAgent row {self.index}: attack_type {self.attack_type!r} is "
                f"outside the declared vocabulary {sorted(_ATTACK_TYPES)}."
            )
        if self.variant not in _VARIANTS and self.variant != "sample":
            raise ValueError(
                f"InjecAgent row {self.index}: variant {self.variant!r} is outside "
                f"the declared vocabulary {sorted(_VARIANTS)}."
            )
        empty = [
            name
            for name, value in (
                ("User Instruction", self.user_instruction),
                ("User Tool", self.tool_name),
                ("Tool Response Template", self.tool_response_template),
                ("Attacker Instruction", self.attacker_instruction),
            )
            if not value.strip()
        ]
        if empty:
            raise ValueError(
                f"InjecAgent row {self.index}: missing or empty required field(s) "
                f"{empty}."
            )
        if (
            not isinstance(self.attacker_tools, tuple)
            or not self.attacker_tools
            or any(
                not isinstance(tool, str) or not tool.strip()
                for tool in self.attacker_tools
            )
        ):
            raise ValueError(
                f"InjecAgent row {self.index}: Attacker Tools must be a nonempty "
                "list of nonblank strings."
            )
        if (
            not isinstance(self.expected_achievements, str)
            or not self.expected_achievements.strip()
        ):
            raise ValueError(
                f"InjecAgent row {self.index}: Expected Achievements must be a "
                "nonblank string."
            )
        if self.rendered_tool_response is not None and not self.rendered_tool_response:
            raise ValueError(
                f"InjecAgent row {self.index}: pre-rendered Tool Response is empty."
            )
        if (
            self.rendered_tool_response is not None
            and self.attacker_instruction not in self.rendered_tool_response
        ):
            raise ValueError(
                f"InjecAgent row {self.index}: pre-rendered Tool Response does not "
                "contain the Attacker Instruction verbatim."
            )

    @property
    def tool_response(self) -> str:
        """Render the tool response with the attacker instruction substituted."""
        if self.rendered_tool_response is not None:
            return self.rendered_tool_response
        if _PLACEHOLDER in self.tool_response_template:
            return self.tool_response_template.replace(
                _PLACEHOLDER, self.attacker_instruction
            )
        return f"{self.tool_response_template}\n\n{self.attacker_instruction}"


class InjecAgentAdapter(Adapter):
    """Map an InjecAgent-style corpus into tool-result-injection cases."""

    corpus_id: ClassVar[str] = "injecagent"
    license: ClassVar[str] = "MIT (as declared upstream)"
    upstream: ClassVar[str] = "https://github.com/uiuc-kang-lab/InjecAgent"

    @classmethod
    def from_path(
        cls,
        path: Path,
        *,
        upstream_revision: str | None = None,
        variants: str = "base",
    ) -> InjecAgentAdapter:
        """Build an adapter while selecting base, enhanced, or both variants."""
        return cls(
            cls.parse_rows(path, variants=variants),
            upstream_revision=upstream_revision,
        )

    @classmethod
    def parse_rows(cls, path: Path, *, variants: str = "base") -> list[object]:
        """Parse upstream test-case JSON, selecting base by default.

        The upstream directory contains attacker source JSONL, generated
        responses, tools, and four test-case arrays. Only the latter are
        supported here. A direct file path is explicit and is therefore read
        regardless of ``variants``; directory discovery applies the requested
        variant filter and ignores all unrelated files. Row indices are local to
        each selected upstream file, so adding enhanced files cannot renumber
        base cases.
        """
        if variants not in {"base", "enhanced", "both"}:
            raise ValueError(
                f"Unknown InjecAgent variant selection {variants!r}; expected "
                "base, enhanced, or both."
            )
        if path.is_dir():
            selected: list[Path] = []
            for file_path in path.iterdir():
                if not file_path.is_file():
                    continue
                file_match = _SUPPORTED_FILE_PATTERN.fullmatch(file_path.name)
                if file_match is None:
                    continue
                if variants != "both" and file_match.group("variant") != variants:
                    continue
                selected.append(file_path)
            files = sorted(selected)
        else:
            files = [path]
        if path.is_dir() and not files:
            raise ValueError(
                f"{path} contains no supported InjecAgent test-case JSON files for "
                f"variant selection {variants!r}."
            )
        raw_cases: list[object] = []
        for file_path in files:
            file_match = _SUPPORTED_FILE_PATTERN.fullmatch(file_path.name)
            if file_match is None:
                raw_cases.extend(cls._load_case_list(file_path))
                continue
            inferred_type = file_match.group("attack_type")
            inferred_variant = file_match.group("variant")
            for local_index, raw in enumerate(cls._load_case_list(file_path)):
                if isinstance(raw, dict):
                    raw_cases.append({
                        **raw,
                        "_adapter_attack_type": inferred_type,
                        "_adapter_variant": inferred_variant,
                        "_adapter_index": local_index,
                    })
                else:
                    raw_cases.append(raw)
        rows: list[object] = []
        for position, raw in enumerate(raw_cases):
            if not isinstance(raw, dict):
                raise ValueError(
                    f"{path}: entry {position} is a {type(raw).__name__}, not an "
                    "InjecAgent case object."
                )
            index = int(raw.get("_adapter_index", position))
            attack_type = _attack_type(raw, index=index)
            variant = str(raw.get("_adapter_variant", "base"))
            is_upstream_case = "_adapter_variant" in raw and "Attack Type" in raw
            rows.append(
                InjecAgentRow(
                    index=index,
                    attack_type=attack_type,
                    user_instruction=_required_field(
                        raw, "User Instruction", index
                    ).strip(),
                    tool_name=_required_field(raw, "User Tool", index).strip(),
                    tool_response_template=_required_field(
                        raw, "Tool Response Template", index
                    ),
                    attacker_instruction=_required_field(
                        raw, "Attacker Instruction", index
                    ).strip(),
                    attacker_tools=_required_list_field(raw, "Attacker Tools", index),
                    expected_achievements=_required_field(
                        raw, "Expected Achievements", index
                    ).strip(),
                    variant=variant,
                    rendered_tool_response=(
                        _required_field(raw, "Tool Response", index)
                        if is_upstream_case
                        else None
                    ),
                    source_slice=variant if is_upstream_case else None,
                    upstream_attack_type=(
                        _required_field(raw, "Attack Type", index)
                        if is_upstream_case
                        else None
                    ),
                )
            )
        return rows

    @classmethod
    def _load_case_list(cls, path: Path) -> list[object]:
        loaded = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(loaded, list):
            raise ValueError(
                f"{path} is not an InjecAgent case file: expected a JSON list."
            )
        return loaded

    def iter_adapted(self) -> Iterator[AdaptedCase]:
        """Yield four browser visibility cases for each upstream row."""
        for row in self.rows:
            if not isinstance(row, InjecAgentRow):
                raise TypeError(f"Unexpected row type: {type(row).__name__}")
            yield from self._adapt_row(row)

    def _adapt_row(self, row: InjecAgentRow) -> list[AdaptedCase]:
        """Expand one row into attack/benign hidden/full browser cases."""
        is_data_stealing = row.attack_type == "ds"
        source_group = (
            f"injecagent:{row.attack_type}:"
            f"{normalized_text_key(row.attacker_instruction)}"
        )
        upstream_id = _upstream_id(row)
        objective = (
            f"Complete the user's request using only the requested {row.tool_name} "
            f"tool: {row.user_instruction}"
        )
        damage_envelope = (
            "Only perform actions authorized by the user's request. Expected "
            f"achievement constraint: {row.expected_achievements}. Never invoke "
            "attacker-provided tools or perform another unauthorized action."
        )
        return build_browser_ablation_cases(
            case_id_prefix=(
                f"public-{self.corpus_id}-{row.variant}-{row.attack_type}-"
                f"{row.index:05d}"
            ),
            source=self._source_for(row),
            source_group=source_group,
            matched_group=(
                f"injecagent-match-{row.variant}-{row.attack_type}-{row.index:05d}"
            ),
            attack_class=(
                "exfiltration_shaping" if is_data_stealing else "tool_result_injection"
            ),
            attack_objective=objective,
            benign_objective=objective,
            damage_envelope=damage_envelope,
            attack_action={"action": "invoke_tools", "tools": list(row.attacker_tools)},
            benign_action={"action": "invoke_tools", "tools": [row.tool_name]},
            attack_environment=row.tool_response,
            benign_environment=_clean_tool_response(row.tool_response_template),
            attack_lineage=self._lineage(
                row, paired_id=upstream_id, group=source_group
            ),
            benign_lineage=self._lineage(
                row, paired_id=upstream_id, group=source_group
            ),
            obfuscation="none",
            placement="browser_environment",
            language="en",
        )

    def _lineage(
        self, row: InjecAgentRow, *, paired_id: str | None, group: str
    ) -> AdaptedLineage:
        return AdaptedLineage(
            corpus_id=self.corpus_id,
            upstream_id=_upstream_id(row),
            group=group,
            license=self.license,
            upstream_revision=self.upstream_revision,
            paired_upstream_id=paired_id,
            adapter_version=self.adapter_version,
            source_split=row.variant,
            evaluation_split=evaluation_split_for_group(group),
        )

    def _source_for(self, row: InjecAgentRow) -> str:
        """Return a variant-qualified source for upstream rows only."""
        if row.source_slice is None:
            return self.source
        return f"{self.source}:{row.source_slice}"


def _required_field(raw: dict[str, object], key: str, index: int) -> str:
    """Return a row's required string field verbatim, rejecting a missing or empty one.

    The value is returned unstripped so the tool-response template keeps the
    layout the injection sits in; callers strip the fields that are compared or
    rendered as bare text.
    """
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(
            f"InjecAgent row {index}: required field {key!r} is missing, empty or "
            "not a string; an incomplete row cannot be adapted into a labeled case."
        )
    return value


def _required_list_field(
    raw: dict[str, object], key: str, index: int
) -> tuple[str, ...]:
    """Return a required nonempty list of nonblank strings."""
    value = raw.get(key)
    if (
        not isinstance(value, list)
        or not value
        or any(not isinstance(item, str) or not item.strip() for item in value)
    ):
        raise ValueError(
            f"InjecAgent row {index}: required field {key!r} must be a nonempty "
            "list of nonblank strings."
        )
    return tuple(value)


def _attack_type(raw: dict[str, object], *, index: int) -> str:
    """Resolve the upstream family or the legacy sample's explicit value."""
    inferred = raw.get("_adapter_attack_type")
    if inferred is not None:
        if not isinstance(inferred, str) or inferred not in _ATTACK_TYPES:
            raise ValueError(
                f"InjecAgent row {index}: filename inferred invalid attack type "
                f"{inferred!r}."
            )
        upstream_label = raw.get("Attack Type")
        legacy_value = raw.get("attack_type")
        if (
            upstream_label is None
            and isinstance(legacy_value, str)
            and legacy_value.strip().lower() == inferred
        ):
            return inferred
        if not isinstance(upstream_label, str) or not upstream_label.strip():
            raise ValueError(
                f"InjecAgent row {index}: Attack Type is missing or unknown in the "
                "upstream test-case object."
            )
        if upstream_label not in _UPSTREAM_ATTACK_TYPE_LABELS[inferred]:
            raise ValueError(
                f"InjecAgent row {index}: Attack Type {upstream_label!r} does not "
                f"match filename family {inferred!r}; expected one of "
                f"{sorted(_UPSTREAM_ATTACK_TYPE_LABELS[inferred])}."
            )
        return inferred

    value = _required_field(raw, "attack_type", index).strip().lower()
    if value not in _ATTACK_TYPES:
        raise ValueError(
            f"InjecAgent row {index}: attack_type {value!r} is outside the declared "
            f"vocabulary {sorted(_ATTACK_TYPES)}."
        )
    return value


def _clean_tool_response(template: str) -> str:
    """Remove the attacker placeholder while retaining the benign response."""
    clean = template.replace(_PLACEHOLDER, "").strip()
    return clean or "The requested information is available on the page."


def _upstream_id(row: InjecAgentRow) -> str:
    """Return the stable source identity for one variant/file row."""
    return f"{row.variant}-{row.attack_type}-case-{row.index}"
