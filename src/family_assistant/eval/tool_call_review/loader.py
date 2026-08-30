"""Load and validate evaluation cases from committed and local datasets."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import TYPE_CHECKING

import jsonschema
import yaml

from family_assistant.eval.tool_call_review.adapters.pins import verify_pin
from family_assistant.eval.tool_call_review.schema import (
    ConversationPayload,
    EvalCase,
    resolve_tool_descriptor,
)

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

    from family_assistant.tools.metadata import ToolDescriptor

__all__ = [
    "CaseSchemaValidationError",
    "DuplicateCaseIdError",
    "UnpinnedPublicCaseError",
    "canonical_attack_input",
    "content_hash",
    "gate_generation_hash",
    "load_cases",
    "validate_against_tool_schema",
    "verify_public_source_pins",
]

_CASE_SUFFIXES = (".jsonl", ".yaml", ".yml", ".json")

_PUBLIC_SOURCE_PREFIX = "public:"

# Run reports and consumed-generation markers are harness *outputs*, and a
# report written under a scanned dataset directory would otherwise be parsed as
# a case on the next load. Excluding well-known directory names is
# deterministic; sniffing file contents to guess what is a case would instead
# make a malformed case disappear silently. ``lineage`` holds the build
# script's lineage sidecars, which are not cases and would abort validation.
_EXCLUDED_DIR_NAMES = frozenset({"consumed_generations", "runs", "lineage"})


class CaseSchemaValidationError(Exception):
    """A case's arguments do not satisfy the resolved tool's parameter schema."""


class DuplicateCaseIdError(Exception):
    """Two loaded cases share the same id."""


class UnpinnedPublicCaseError(Exception):
    """A ``public:*``-sourced case reached a gate without a verifiable pin."""


def validate_against_tool_schema(
    case: EvalCase,
    *,
    descriptor_registry: Mapping[str, ToolDescriptor] | None = None,
) -> None:
    """Validate a conversation case's arguments against the live tool schema.

    Name resolution alone would let a tool that kept its name but changed its
    schema replay stale, now-impossible calls that still count as clean trials,
    so a missing tool (via :func:`resolve_tool_descriptor`) or schema-invalid
    arguments must fail loudly here rather than passing silently. Pass the
    evaluated deployment's ``descriptor_registry`` when cases involve MCP or
    named-sink tools the local registry cannot supply.
    """
    payload = case.payload
    if not isinstance(payload, ConversationPayload):
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
    """
    files = _collect_files(paths)
    by_id: dict[str, EvalCase] = {}
    for file_path in files:
        for case in _parse_file(file_path):
            validate_against_tool_schema(case, descriptor_registry=descriptor_registry)
            if case.id in by_id:
                raise DuplicateCaseIdError(
                    f"Duplicate case id {case.id!r} (seen again in {file_path})."
                )
            case.stamp_origin_path(file_path)
            by_id[case.id] = case
    return [by_id[case_id] for case_id in sorted(by_id)]


def verify_public_source_pins(
    cases: Iterable[EvalCase],
    *,
    pins_path: Path | None = None,
) -> None:
    """Verify every ``public:*``-sourced case's origin file against a pin.

    ``--gate`` loads plain JSONL and would otherwise gate an unpinned or edited
    public-corpus dataset without noticing. This fails closed before a gate
    consumes its generation: each public-sourced case's stamped origin file is
    checksum-verified against the pins manifest (``adapters/PINS.toml`` by
    default, overridable via ``pins_path``). A public case with no stamped
    origin, an unpinned corpus (:class:`PinNotFoundError`), or a file that no
    longer matches its recorded checksum (:class:`PinMismatchError`) each abort
    rather than being gated silently.
    """
    checked: set[tuple[str, Path]] = set()
    for case in cases:
        if not case.source.startswith(_PUBLIC_SOURCE_PREFIX):
            continue
        corpus_id = case.source[len(_PUBLIC_SOURCE_PREFIX) :]
        origin = case.origin_path
        if origin is None:
            raise UnpinnedPublicCaseError(
                f"Public-sourced case {case.id!r} has no stamped origin file; a gate "
                "cannot verify its pin. Load public cases from disk before gating."
            )
        key = (corpus_id, origin)
        if key in checked:
            continue
        checked.add(key)
        verify_pin(corpus_id, origin, pins_path=pins_path)


def content_hash(cases: Sequence[EvalCase]) -> str:
    """Return a stable content hash of a set of cases for run comparison.

    This covers every field of every case, so it answers "did two runs see the
    same dataset?". It is deliberately *not* the gate identity: renaming a case
    or adding a benign one changes it, which would let already-consumed attack
    material be re-gated as a fresh generation. Gate consumption keys on
    :func:`gate_generation_hash`.
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


def gate_generation_hash(cases: Sequence[EvalCase]) -> str:
    """Return the digest identifying a gate generation's held-out material.

    A generation's identity is the attack material a gate run actually
    consulted: each attack case's payload and the verdict space it was judged
    under. Envelope metadata (ids, source, axis labels) and benign cases are
    excluded, so re-labelling or extending a dataset cannot mint a "new"
    generation over attacks that have already been consumed — only changing
    what an attack proposes, or the space it is judged in, does. A dataset with
    no attack cases digests to the empty generation, which costs nothing: such
    a run has no attack trials and cannot pass a gate anyway.
    """
    serialized = sorted(
        json.dumps(
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
        for case in cases
        if case.label == "attack"
    )
    encoded = json.dumps(serialized, ensure_ascii=False)
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
                if path.is_file()
                and path.suffix.lower() in _CASE_SUFFIXES
                and not _EXCLUDED_DIR_NAMES.intersection(
                    path.relative_to(candidate).parts[:-1]
                )
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
