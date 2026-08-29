"""Load and validate evaluation cases from committed and local datasets."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import TYPE_CHECKING

import jsonschema
import yaml

from family_assistant.eval.tool_call_review.schema import (
    ConversationPayload,
    EvalCase,
    resolve_tool_descriptor,
)

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

__all__ = [
    "CaseSchemaValidationError",
    "DuplicateCaseIdError",
    "content_hash",
    "load_cases",
    "validate_against_tool_schema",
]

_CASE_SUFFIXES = (".jsonl", ".yaml", ".yml", ".json")


class CaseSchemaValidationError(Exception):
    """A case's arguments do not satisfy the resolved tool's parameter schema."""


class DuplicateCaseIdError(Exception):
    """Two loaded cases share the same id."""


def validate_against_tool_schema(case: EvalCase) -> None:
    """Validate a conversation case's arguments against the live tool schema.

    Name resolution alone would let a tool that kept its name but changed its
    schema replay stale, now-impossible calls that still count as clean trials,
    so a missing tool (via :func:`resolve_tool_descriptor`) or schema-invalid
    arguments must fail loudly here rather than passing silently.
    """
    payload = case.payload
    if not isinstance(payload, ConversationPayload):
        return
    descriptor = resolve_tool_descriptor(payload.tool_name)
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


def load_cases(paths: str | Path | Iterable[str | Path]) -> list[EvalCase]:
    """Load, validate, and de-duplicate cases from files or directories.

    Accepts ``.jsonl`` (one case per line), ``.yaml``/``.yml``, and ``.json``
    (a single case object or a list of them), and directories containing any of
    those. Cases are returned in deterministic order sorted by id; a duplicate
    id raises rather than silently overwriting.
    """
    files = _collect_files(paths)
    by_id: dict[str, EvalCase] = {}
    for file_path in files:
        for case in _parse_file(file_path):
            validate_against_tool_schema(case)
            if case.id in by_id:
                raise DuplicateCaseIdError(
                    f"Duplicate case id {case.id!r} (seen again in {file_path})."
                )
            by_id[case.id] = case
    return [by_id[case_id] for case_id in sorted(by_id)]


def content_hash(cases: Sequence[EvalCase]) -> str:
    """Return a stable content hash of a set of cases for run comparison."""
    serialized = [
        case.model_dump(mode="json") for case in sorted(cases, key=lambda case: case.id)
    ]
    encoded = json.dumps(serialized, sort_keys=True, ensure_ascii=False)
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
    return [EvalCase.model_validate(record) for record in records if record is not None]
