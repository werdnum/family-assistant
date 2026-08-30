"""Load and validate evaluation cases from committed and local datasets."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import TYPE_CHECKING

import jsonschema
import yaml
from pydantic import ValidationError

from family_assistant.eval.tool_call_review.schema import (
    ConversationPayload,
    EvalCase,
    ToolResolutionError,
    resolve_tool_descriptor,
)

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

    from family_assistant.tools.metadata import ToolDescriptor

__all__ = [
    "CaseInputConstructionError",
    "CaseParseError",
    "CaseSchemaValidationError",
    "DuplicateCaseIdError",
    "canonical_attack_input",
    "case_skip_reason",
    "content_hash",
    "load_cases",
    "validate_against_tool_schema",
    "validate_review_input_constructible",
]

_CASE_SUFFIXES = (".jsonl", ".yaml", ".yml", ".json")


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
        jsonschema.validate(instance=payload.arguments, schema=parameters)
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
        case.to_review_input(descriptor_registry=descriptor_registry)
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


def canonical_attack_input(case: EvalCase) -> str:
    """Return the canonical ``(payload, constraints)`` serialization of a case.

    This identifies one attack *input*: the payload the reviewer rules on and
    the verdict space it is judged under, with envelope metadata (id, source,
    axis labels) stripped. The clean-case count keys on this string so that N
    copies of one attack payload under different ids collapse to a single
    independent sample instead of each satisfying a ceiling on their own.
    """
    return json.dumps(
        {
            "payload": case.payload.model_dump(mode="json"),
            "constraints": {
                "available_verdicts": sorted(
                    verdict.value for verdict in case.constraints.available_verdicts
                ),
                "fallback_verdict": case.constraints.fallback_verdict.value,
            },
        },
        sort_keys=True,
        ensure_ascii=False,
    )


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
    if suffix == ".jsonl":
        records = [json.loads(line) for line in text.splitlines() if line.strip()]
    elif suffix in {".yaml", ".yml"}:
        loaded = yaml.safe_load(text)
        records = loaded if isinstance(loaded, list) else [loaded]
    elif suffix == ".json":
        loaded = json.loads(text)
        records = loaded if isinstance(loaded, list) else [loaded]
    else:
        raise ValueError(f"Unsupported dataset file extension: {file_path}")
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
