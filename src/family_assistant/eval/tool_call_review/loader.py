"""Load and validate evaluation cases from committed and local datasets."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any

import jsonschema
import yaml
from jsonschema import Draft202012Validator, validators
from pydantic import ValidationError

from family_assistant.eval.tool_call_review.schema import (
    BrowserPayload,
    ConversationPayload,
    EvalCase,
    ToolResolutionError,
    resolve_tool_descriptor,
)
from family_assistant.llm.messages import message_to_json_dict
from family_assistant.services.tool_call_review import (
    BrowserActionReviewInput,
    assemble_browser_action_review_messages,
    assemble_tool_call_review_messages,
)

if TYPE_CHECKING:
    from collections.abc import Hashable, Iterable, Mapping, Sequence

    from family_assistant.services.tool_call_review import (
        ToolCallReviewConstraints,
        ToolCallReviewInput,
    )
    from family_assistant.tools.metadata import ToolDescriptor

__all__ = [
    "CaseInputConstructionError",
    "CaseParseError",
    "CaseSchemaValidationError",
    "CaseSkip",
    "DuplicateCaseIdError",
    "SkipKind",
    "attack_input_key",
    "case_skip",
    "content_hash",
    "load_cases",
    "validate_against_tool_schema",
    "validate_review_input_constructible",
]

_CASE_SUFFIXES = (".jsonl", ".yaml", ".yml", ".json")


class _DuplicateKeyError(Exception):
    """A case file repeats a mapping key."""


class _StrictSafeLoader(yaml.SafeLoader):
    """A YAML loader that refuses a repeated mapping key.

    Both YAML and JSON resolve a repeated key by keeping the last value, which
    at this boundary means a file can declare one case and be scored as
    another: a case carrying ``label: attack`` twice over, the second time as
    ``benign``, loads as benign and moves an attack into the evidence the bound
    is computed from. There is no reading under which a repeated key is what
    the author meant, so it fails here with the key named.
    """

    def construct_mapping(
        self, node: yaml.MappingNode, deep: bool = False
    ) -> dict[Hashable, Any]:
        seen: set[object] = set()
        for key_node, _ in node.value:
            key = self.construct_object(key_node, deep=deep)
            if key in seen:
                raise _DuplicateKeyError(f"duplicate key {key!r}")
            seen.add(key)
        return super().construct_mapping(node, deep=deep)


def _no_duplicate_json_keys(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    """``object_pairs_hook`` that rejects a repeated key. See _StrictSafeLoader."""
    seen: set[str] = set()
    for key, _ in pairs:
        if key in seen:
            raise _DuplicateKeyError(f"duplicate key {key!r}")
        seen.add(key)
    return dict(pairs)


# Tool descriptors use a project-specific ``type: attachment`` for parameters
# that carry an attachment id (see ``tools/attachment_utils.py``). Plain
# jsonschema rejects it while checking the *schema*, so a case naming any such
# tool would abort the whole dataset even with the argument absent. Teaching the
# validator that an attachment is its id keeps the check meaningful instead of
# skipping those tools.
_TOOL_ARGUMENT_VALIDATOR = validators.extend(
    Draft202012Validator,
    type_checker=Draft202012Validator.TYPE_CHECKER.redefine(
        "attachment", lambda _checker, instance: isinstance(instance, str)
    ),
)


class CaseParseError(Exception):
    """A file in a scanned dataset directory does not hold evaluation cases."""


class CaseSchemaValidationError(Exception):
    """A case's arguments do not satisfy the resolved tool's parameter schema."""


class DuplicateCaseIdError(Exception):
    """Two loaded cases share the same id."""


class CaseInputConstructionError(Exception):
    """A case cannot be rebuilt into the typed input the reviewer replays."""


class SkipKind(StrEnum):
    """Why a run could not execute a case, as a closed vocabulary.

    Decided where the skip is decided, so nothing downstream has to recover it
    by matching on the prose -- and so a committable record can say *what*
    happened without quoting text the case supplied.
    """

    DERIVATION = "derivation"
    """A derivation case: no shipped judge consumes this boundary."""
    UNRESOLVABLE_TOOL = "unresolvable_tool"
    """The case names a tool this deployment's registry cannot supply."""


@dataclass(frozen=True)
class CaseSkip:
    """A skip, as a category plus the prose that explains it."""

    kind: SkipKind
    reason: str


def case_skip(
    case: EvalCase,
    *,
    descriptor_registry: Mapping[str, ToolDescriptor] | None = None,
) -> CaseSkip | None:
    """Return why this environment cannot execute ``case``, or ``None`` if it can.

    "Cannot run here" is not "malformed". A case naming a tool this deployment's
    registry does not contain — an MCP tool, a direct named-sink descriptor — is
    well-formed data the harness simply cannot replay, so it is named and
    skipped the way derivation cases are, and the rest of a mixed dataset still
    runs. Malformed data keeps failing loudly at load.

    The reason quotes the case's own tool name, which a case is free to make up
    — that is why it was skipped — so the prose can carry whatever a private
    case's author wrote. Callers that publish choose ``kind``; callers that
    inform a maintainer at the terminal use ``reason``.
    """
    if case.boundary == "derivation":
        return CaseSkip(
            kind=SkipKind.DERIVATION,
            reason="derivation cases have no shipped review contract",
        )
    payload = case.payload
    if isinstance(payload, ConversationPayload):
        try:
            resolve_tool_descriptor(payload.tool_name, registry=descriptor_registry)
        except ToolResolutionError as exc:
            return CaseSkip(kind=SkipKind.UNRESOLVABLE_TOOL, reason=str(exc))
    return None


def validate_against_tool_schema(
    case: EvalCase,
    *,
    descriptor_registry: Mapping[str, ToolDescriptor] | None = None,
) -> None:
    """Validate a conversation case's arguments against the live tool schema.

    Name resolution alone would let a tool that kept its name but changed its
    schema replay stale, now-impossible calls that still count as clean trials,
    so schema-invalid arguments must fail loudly here rather than passing
    silently. A case whose tool this environment cannot resolve at all has no
    schema to check against and is left to :func:`case_skip`. Pass the
    evaluated deployment's ``descriptor_registry`` when cases involve MCP or
    named-sink tools the local registry cannot supply.
    """
    payload = case.payload
    if not isinstance(payload, ConversationPayload):
        return
    if case_skip(case, descriptor_registry=descriptor_registry) is not None:
        return
    descriptor = resolve_tool_descriptor(
        payload.tool_name, registry=descriptor_registry
    )
    function = descriptor.definition.get("function")
    if not isinstance(function, dict):
        raise CaseSchemaValidationError(
            f"Tool {payload.tool_name!r} has no function definition to validate "
            "arguments against."
        )
    parameters = function.get("parameters")
    if not isinstance(parameters, dict):
        raise CaseSchemaValidationError(
            f"Tool {payload.tool_name!r} declares no parameter schema."
        )
    try:
        _TOOL_ARGUMENT_VALIDATOR(parameters).validate(payload.arguments)
    except jsonschema.ValidationError as exc:
        raise CaseSchemaValidationError(
            f"Case {case.id!r} arguments violate the schema of tool "
            f"{payload.tool_name!r}: {exc.message}"
        ) from exc


def validate_review_input_constructible(
    case: EvalCase,
    *,
    descriptor_registry: Mapping[str, ToolDescriptor] | None = None,
) -> None:
    """Rebuild the case's executable reviewer input and discard it.

    Validating the arguments against the tool schema leaves the rest of the
    payload — message rows, taint state, sink class, policy contexts — checked
    only when the runner converts the case, which is after judge setup and
    possibly after live calls have been paid for. ``--dry-run`` advertises
    itself as the validation boundary, so every executable input is constructed
    here instead. A case the runner will skip — a derivation case, or one naming
    a tool this environment cannot resolve — is not reconstructed.
    """
    if case_skip(case, descriptor_registry=descriptor_registry) is not None:
        return
    try:
        review_input, constraints = case.to_review_input(
            descriptor_registry=descriptor_registry
        )
        # Assemble and hash the prompt too: constructing the dataclass leaves
        # prompt assembly untried, so a payload value that survives pydantic but
        # is not JSON-serializable (an unquoted YAML date, say) would pass
        # --dry-run and raise mid-run instead.
        attack_input_key(review_input, constraints)
    except Exception as exc:
        raise CaseInputConstructionError(
            f"Case {case.id!r} cannot be rebuilt into a reviewer input: {exc}"
        ) from exc


def load_cases(
    paths: str | Path | Iterable[str | Path],
    *,
    descriptor_registry: Mapping[str, ToolDescriptor] | None = None,
) -> list[EvalCase]:
    """Load, validate, and de-duplicate cases from files or directories.

    Accepts ``.jsonl`` (one case per line), ``.yaml``/``.yml``, and ``.json``
    (a single case object or a list of them), and directories containing any of
    those. Cases are returned in deterministic order sorted by id; a duplicate
    id raises rather than silently overwriting.

    A scanned directory holds cases and nothing else: every candidate file in it
    is parsed as one, and a file that is not a case aborts the load naming
    itself. The harness's other artifact kinds — run records, history-derived
    templates, provenance notes — are separate directories, never mixed into a
    case directory, because the alternative is guessing from a file's contents
    which is which and thereby letting a genuinely malformed case disappear.

    Every executable case is fully reconstructed here, not merely parsed, so an
    unusable dataset is rejected at load rather than mid-run. A case this
    environment cannot execute at all (see :func:`case_skip`) is loaded
    unvalidated and skipped by the runner with its reason, so one unresolvable
    tool does not take the whole dataset down with it.
    """
    files = _collect_files(paths)
    by_id: dict[str, EvalCase] = {}
    for file_path in files:
        for case in _parse_file(file_path):
            validate_against_tool_schema(case, descriptor_registry=descriptor_registry)
            validate_review_input_constructible(
                case, descriptor_registry=descriptor_registry
            )
            if case.id in by_id:
                raise DuplicateCaseIdError(
                    f"Duplicate case id {case.id!r} (seen again in {file_path})."
                )
            by_id[case.id] = case
    cases = [by_id[case_id] for case_id in sorted(by_id)]
    _validate_matched_groups(cases)
    return cases


def _validate_matched_groups(cases: Sequence[EvalCase]) -> None:
    """Reject incomplete or contradictory matched-ablation groups.

    The four visibility/control variants are one dataset-level contract, not
    an invariant a single case can establish. Validation therefore happens
    after all case files have been loaded. Legacy cases and natural-benign
    controls have no matched group and are unaffected.
    """
    groups: dict[str, list[EvalCase]] = {}
    for case in cases:
        if case.matched_group is not None:
            groups.setdefault(case.matched_group, []).append(case)

    expected = {
        ("attack", "hidden"),
        ("attack", "full"),
        ("benign_twin", "hidden"),
        ("benign_twin", "full"),
    }
    for group, members in groups.items():
        actual = {(case.control_kind, case.visibility) for case in members}
        if len(members) != len(expected) or actual != expected:
            raise CaseSchemaValidationError(
                f"Matched ablation group {group!r} must contain exactly one "
                "attack and one benign_twin at each hidden/full visibility "
                f"treatment; found {len(members)} case(s) with {sorted(actual)!r}."
            )
        source_groups = {case.source_group for case in members}
        if len(source_groups) != 1:
            raise CaseSchemaValidationError(
                f"Matched ablation group {group!r} must use one source_group; "
                f"found {sorted(source_groups, key=lambda value: value or '')!r}."
            )
        baseline = members[0]
        shared_identity = _matched_group_shared_identity(baseline)
        for member in members[1:]:
            if _matched_group_shared_identity(member) != shared_identity:
                raise CaseSchemaValidationError(
                    f"Matched ablation group {group!r} must keep source, "
                    "source_group, attack_class, constraints, and security "
                    "metadata identical across all four variants."
                )
        shared_browser_security = _matched_browser_security(baseline)
        for member in members[1:]:
            if _matched_browser_security(member) != shared_browser_security:
                raise CaseSchemaValidationError(
                    f"Matched ablation group {group!r} must keep browser "
                    "security fields damage_envelope, mitigation_guidance, "
                    "policy_contexts, and recent_actions identical across "
                    "attack and benign_twin variants."
                )
        by_control: dict[str, list[EvalCase]] = {}
        for member in members:
            if member.control_kind is not None:
                by_control.setdefault(member.control_kind, []).append(member)
        for control_kind, control_members in by_control.items():
            if _matched_control_pair_identity(
                control_members[0]
            ) != _matched_control_pair_identity(control_members[1]):
                raise CaseSchemaValidationError(
                    f"Matched ablation group {group!r} {control_kind} hidden/full "
                    "variants must keep expectation and case metadata identical."
                )
            if _matched_browser_payload(control_members[0]) != _matched_browser_payload(
                control_members[1]
            ):
                raise CaseSchemaValidationError(
                    f"Matched ablation group {group!r} {control_kind} hidden/full "
                    "variants must keep browser payload fields other than "
                    "environment identical."
                )
        action_key_sets = {
            frozenset(_browser_payload(case).proposed_action) for case in members
        }
        if len(action_key_sets) != 1:
            raise CaseSchemaValidationError(
                f"Matched ablation group {group!r} must keep the proposed-action "
                "key set comparable across attack and benign_twin variants."
            )


def _matched_group_shared_identity(case: EvalCase) -> tuple[object, ...]:
    """Return top-level identity/security fields shared by all group variants."""
    return (
        case.boundary,
        case.source,
        case.source_group,
        case.attack_class,
        case.constraints.model_dump(mode="json"),
    )


def _matched_control_pair_identity(case: EvalCase) -> tuple[object, ...]:
    """Return metadata that must match between hidden/full control pairs."""
    return (
        case.expected_verdict,
        case.obfuscation,
        case.placement,
        case.language,
    )


def _matched_browser_payload(case: EvalCase) -> dict[str, object]:
    """Return the browser payload with the controlled environment removed."""
    payload = _browser_payload(case).model_dump(mode="json")
    payload.pop("environment")
    return payload


def _matched_browser_security(case: EvalCase) -> tuple[object, ...]:
    """Return browser security context that must not vary by control kind."""
    payload = _browser_payload(case)
    return (
        payload.damage_envelope,
        payload.mitigation_guidance,
        payload.policy_contexts,
        payload.recent_actions,
    )


def _browser_payload(case: EvalCase) -> BrowserPayload:
    """Return a matched case's browser payload with a clear contract error."""
    if not isinstance(case.payload, BrowserPayload):
        raise CaseSchemaValidationError(
            f"Matched ablation case {case.id!r} must use a browser payload."
        )
    return case.payload


def content_hash(cases: Sequence[EvalCase]) -> str:
    """Return a stable content hash of a set of cases for run comparison.

    This covers every field of every case, so it answers "did two runs see the
    same dataset?" — which is what a stamp records and what a regression diff
    compares. Committed datasets are pinned by git; this digest is how a run
    states which dataset it measured.
    """
    serialized = [
        case.model_dump(mode="json") for case in sorted(cases, key=lambda case: case.id)
    ]
    encoded = json.dumps(
        serialized, sort_keys=True, ensure_ascii=False, allow_nan=False
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def attack_input_key(
    review_input: ToolCallReviewInput | BrowserActionReviewInput,
    constraints: ToolCallReviewConstraints,
) -> str:
    """Return the identity of one review input: a digest of what the judge saw.

    Identity is the assembled reviewer messages plus the verdict space they are
    ruled under, because that is the whole of what the judge is given: two cases
    the harness cannot tell apart here are two trials of one input, and the
    clean-case count behind the ``3/N`` bound keys on this so that N copies of
    one attack collapse to a single independent sample.

    Deriving it from the assembly the run actually executes is what keeps the
    answer from being restated anywhere else. Envelope metadata (id, source,
    axis labels), and payload fields the prompt never renders — a non-trusted
    tier's ``source_id``, a tool row's ``tool_call_id`` — cannot split one input
    into two, and the same attack reaching the harness through two corpora is
    one input whenever it lands in the same position. Conversely, anything that
    changes the prompt changes the identity, including a change to assembly
    itself: a run measures the judge as it is assembled today.

    Derivation cases have no reviewer input and therefore no key; they are never
    executed, so they are never trial evidence to deduplicate.
    """
    if isinstance(review_input, BrowserActionReviewInput):
        messages = assemble_browser_action_review_messages(review_input, constraints)
    else:
        messages = assemble_tool_call_review_messages(review_input, constraints)
    encoded = json.dumps(
        {
            "messages": [message_to_json_dict(message) for message in messages],
            "available_verdicts": sorted(
                verdict.value for verdict in constraints.available_verdicts
            ),
            "fallback_verdict": constraints.fallback_verdict.value,
        },
        sort_keys=True,
        ensure_ascii=False,
        allow_nan=False,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _collect_files(paths: str | Path | Iterable[str | Path]) -> list[Path]:
    if isinstance(paths, (str, Path)):
        candidates: list[Path] = [Path(paths)]
    else:
        candidates = [Path(path) for path in paths]
    files: list[Path] = []
    seen: set[Path] = set()
    for candidate in candidates:
        if not candidate.exists():
            raise FileNotFoundError(f"Dataset path does not exist: {candidate}")
        if candidate.is_dir():
            matched = sorted(
                path
                for path in candidate.rglob("*")
                if path.is_file() and path.suffix.lower() in _CASE_SUFFIXES
            )
        else:
            matched = [candidate]
        for path in matched:
            resolved = path.resolve()
            if resolved not in seen:
                seen.add(resolved)
                files.append(path)
    return files


def _reject_non_finite(record: object, file_path: Path, path: str = "") -> None:
    """Reject NaN and infinities anywhere in a parsed case.

    Checked on the parsed record rather than at the hashing boundary, because by
    then the value is no longer a float: the reviewer renders arguments into the
    prompt with its own serializer, which writes NaN as the bare text ``NaN``,
    so a case carrying ``.nan`` would assemble, hash and score like any other.
    Python's JSON and YAML readers both accept these; strict JSON does not, and
    neither does any reviewer input production can serialize -- a case
    containing one describes a call the system cannot make.
    """
    if isinstance(record, float) and not math.isfinite(record):
        raise CaseParseError(
            f"{file_path} has a non-finite number ({record}) at {
                path or 'the top level'
            }. No reviewer input production can serialize contains "
            "one, so a case carrying it describes a call the system cannot make."
        )
    if isinstance(record, dict):
        for key, value in record.items():
            _reject_non_finite(value, file_path, f"{path}.{key}" if path else str(key))
    elif isinstance(record, list):
        for index, value in enumerate(record):
            _reject_non_finite(value, file_path, f"{path}[{index}]")


def _parse_file(file_path: Path) -> list[EvalCase]:
    text = file_path.read_text(encoding="utf-8")
    suffix = file_path.suffix.lower()
    try:
        records = _decode_records(text, suffix, file_path)
    except _DuplicateKeyError as exc:
        raise CaseParseError(f"{file_path} has a {exc}.") from exc
    for record in records:
        _reject_non_finite(record, file_path)
    # A ``None`` record — an empty YAML document, or a null entry in a list — is
    # validated rather than filtered out. Dropping it would let a truncated case
    # vanish from a directory whose whole contract is that it holds cases and
    # aborts on anything else.
    try:
        return [EvalCase.model_validate(record) for record in records]
    except ValidationError as exc:
        raise CaseParseError(
            f"{file_path} is not an evaluation case file: {exc}"
        ) from exc


def _decode_records(text: str, suffix: str, file_path: Path) -> list[object]:
    if suffix == ".jsonl":
        return [
            json.loads(line, object_pairs_hook=_no_duplicate_json_keys)
            for line in text.splitlines()
            if line.strip()
        ]
    if suffix in {".yaml", ".yml"}:
        loaded = yaml.load(text, Loader=_StrictSafeLoader)
        return loaded if isinstance(loaded, list) else [loaded]
    if suffix == ".json":
        loaded = json.loads(text, object_pairs_hook=_no_duplicate_json_keys)
        return loaded if isinstance(loaded, list) else [loaded]
    raise ValueError(f"Unsupported dataset file extension: {file_path}")
