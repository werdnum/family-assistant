"""Load and validate evaluation cases from committed and local datasets."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

import jsonschema
import yaml
from jsonschema import Draft202012Validator, validators
from pydantic import ValidationError

from family_assistant.eval.tool_call_review.schema import (
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
    "DuplicateCaseIdError",
    "attack_input_key",
    "case_skip_reason",
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


def case_skip_reason(
    case: EvalCase,
    *,
    descriptor_registry: Mapping[str, ToolDescriptor] | None = None,
) -> str | None:
    """Return why this environment cannot execute ``case``, or ``None`` if it can.

    "Cannot run here" is not "malformed". A case naming a tool this deployment's
    registry does not contain — an MCP tool, a direct named-sink descriptor — is
    well-formed data the harness simply cannot replay, so it is named and
    skipped the way derivation cases are, and the rest of a mixed dataset still
    runs. Malformed data keeps failing loudly at load.
    """
    if case.boundary == "derivation":
        return "derivation cases have no shipped review contract"
    payload = case.payload
    if isinstance(payload, ConversationPayload):
        try:
            resolve_tool_descriptor(payload.tool_name, registry=descriptor_registry)
        except ToolResolutionError as exc:
            return str(exc)
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
    schema to check against and is left to :func:`case_skip_reason`. Pass the
    evaluated deployment's ``descriptor_registry`` when cases involve MCP or
    named-sink tools the local registry cannot supply.
    """
    payload = case.payload
    if not isinstance(payload, ConversationPayload):
        return
    if case_skip_reason(case, descriptor_registry=descriptor_registry) is not None:
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
    if case_skip_reason(case, descriptor_registry=descriptor_registry) is not None:
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
    environment cannot execute at all (see :func:`case_skip_reason`) is loaded
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
    return [by_id[case_id] for case_id in sorted(by_id)]


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
    encoded = json.dumps(serialized, sort_keys=True, ensure_ascii=False)
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


def _parse_file(file_path: Path) -> list[EvalCase]:
    text = file_path.read_text(encoding="utf-8")
    suffix = file_path.suffix.lower()
    try:
        records = _decode_records(text, suffix, file_path)
    except _DuplicateKeyError as exc:
        raise CaseParseError(f"{file_path} has a {exc}.") from exc
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
