#!/usr/bin/env python3
"""Generate private eval cases from structurally scrubbed history templates.

The three phases are resumable and deliberately separate: ``prepare`` performs
only deterministic work, ``classify`` asks a cheap model for closed-vocabulary
hypotheses, and ``instantiate`` builds validated benign/attack twins.  Nothing
is promoted to a public corpus by this command.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, cast

import yaml
from pydantic import ValidationError

from family_assistant.eval.private_paths import resolve_private_eval_path
from family_assistant.eval.tool_call_review.history_generation import (
    ALLOWED_MODELS,
    CLASSIFICATION_BATCH_MAX,
    DEFAULT_MODEL,
    INSTANTIATION_BATCH_MAX,
    PROMPT_REVISION,
    BatchAttempt,
    BatchRunner,
    ClassificationRecord,
    HistoryGenerationError,
    InstantiationRecord,
    PreparedShape,
    ShapeRecord,
    build_cases,
    classification_preflight,
    classify_batches,
    instantiate_batches,
    is_runnable_classification,
    load_templates,
    prepare_shapes,
    write_jsonl_exclusive,
)
from family_assistant.eval.tool_call_review.registry_snapshot import (
    RegistrySnapshotError,
    load_registry_snapshot,
    registry_digest,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from family_assistant.tools.metadata import ToolDescriptor

_RUN_FILE = "run.json"
_SHAPES_FILE = "shapes.jsonl"
_LINEAGE_FILE = "lineage.jsonl"
_CLASSIFICATION_FILE = "classification.jsonl"
_CLASSIFICATION_QUARANTINE_FILE = "classification-quarantine.jsonl"
_DRAFT_FILE = "drafts.jsonl"
_INSTANTIATION_QUARANTINE_FILE = "instantiation-quarantine.jsonl"
_INSTANTIATION_ATTEMPTS_FILE = "instantiation-attempts.jsonl"
_CASE_QUARANTINE_FILE = "case-quarantine.jsonl"
_PROVIDER = "openrouter"


def _digest_path(path: Path) -> str:
    """Digest all input bytes in deterministic path order."""
    files = (
        [path]
        if path.is_file()
        else sorted((*path.glob("*.yaml"), *path.glob("*.yml")))
    )
    hasher = hashlib.sha256()
    for file_path in files:
        hasher.update(file_path.name.encode("utf-8"))
        hasher.update(b"\0")
        hasher.update(file_path.read_bytes())
        hasher.update(b"\0")
    return hasher.hexdigest()


def _write_json(path: Path, value: Mapping[str, object], *, replace: bool) -> None:
    """Write a manifest atomically, optionally refusing an existing file."""
    if path.exists() and not replace:
        raise HistoryGenerationError(f"refusing to overwrite existing artifact {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
    ) as handle:
        temporary = Path(handle.name)
        json.dump(value, handle, sort_keys=True, ensure_ascii=False, indent=2)
        handle.write("\n")
        handle.flush()
    temporary.replace(path)


def _read_json(path: Path) -> dict[str, object]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HistoryGenerationError(f"invalid run manifest {path}") from exc
    if not isinstance(raw, dict):
        raise HistoryGenerationError(f"run manifest {path} is not an object")
    return cast("dict[str, object]", raw)


def _read_jsonl(path: Path) -> list[object]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise HistoryGenerationError(f"cannot read prepared artifact {path}") from exc
    records: list[object] = []
    for line in lines:
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise HistoryGenerationError(
                f"invalid JSONL in prepared artifact {path}"
            ) from exc
    return records


def _reconcile_prepared_artifacts(
    out_dir: Path, shapes: Sequence[PreparedShape]
) -> None:
    try:
        shape_records = [
            ShapeRecord.model_validate(raw)
            for raw in _read_jsonl(out_dir / _SHAPES_FILE)
        ]
    except ValidationError as exc:
        raise HistoryGenerationError("invalid shapes artifact") from exc
    expected_shapes = [shape.record for shape in shapes]
    if shape_records != expected_shapes:
        raise HistoryGenerationError(
            "shapes artifact differs from recomputed templates"
        )

    lineage_records = _read_jsonl(out_dir / _LINEAGE_FILE)
    expected_lineage = [
        {
            "shape_id": shape.record.shape_id,
            "frequency": shape.frequency,
            "template_ids": list(shape.template_ids),
        }
        for shape in shapes
    ]
    if lineage_records != expected_lineage:
        raise HistoryGenerationError(
            "lineage artifact differs from recomputed templates"
        )


def _load_inputs(
    templates_path: Path, registry_path: Path
) -> tuple[list, dict[str, ToolDescriptor], str, str]:
    try:
        registry = load_registry_snapshot(registry_path)
    except (RegistrySnapshotError, OSError) as exc:
        raise HistoryGenerationError(f"invalid registry snapshot: {exc}") from exc
    templates = load_templates(templates_path, registry)
    return (
        templates,
        registry,
        _digest_path(templates_path),
        registry_digest(registry) or "",
    )


def _resolve_output(raw: str) -> Path:
    try:
        return resolve_private_eval_path(raw)
    except ValueError as exc:
        raise HistoryGenerationError(str(exc)) from exc


def _require_run(
    out_dir: Path,
    templates_path: Path,
    registry_path: Path,
    model: str,
) -> tuple[dict[str, object], list, dict[str, ToolDescriptor], list]:
    manifest = _read_json(out_dir / _RUN_FILE)
    if manifest.get("schema_version") != "m3.history-run.v1":
        raise HistoryGenerationError("unsupported run manifest schema")
    if manifest.get("prompt_revision") != PROMPT_REVISION:
        raise HistoryGenerationError("prompt revision differs from this generator")
    templates, registry, template_digest, snapshot_digest = _load_inputs(
        templates_path, registry_path
    )
    if manifest.get("template_digest") != template_digest:
        raise HistoryGenerationError("template input differs from the prepared run")
    if manifest.get("registry_digest") != snapshot_digest:
        raise HistoryGenerationError("registry snapshot differs from the prepared run")
    if manifest.get("model") != model:
        raise HistoryGenerationError("model differs from the prepared run")
    if manifest.get("provider") != _PROVIDER:
        raise HistoryGenerationError("provider differs from the prepared run")
    max_shapes = manifest.get("max_shapes")
    if max_shapes is not None and (
        not isinstance(max_shapes, int)
        or isinstance(max_shapes, bool)
        or max_shapes < 1
    ):
        raise HistoryGenerationError("run manifest max_shapes is malformed")
    shapes = prepare_shapes(templates, max_shapes=max_shapes)
    if manifest.get("shape_count") != len(shapes):
        raise HistoryGenerationError("template shapes differ from the prepared run")
    _reconcile_prepared_artifacts(out_dir, shapes)
    return manifest, templates, registry, shapes


def _ensure_new_phase(out_dir: Path, names: Sequence[str]) -> None:
    if not out_dir.is_dir():
        raise HistoryGenerationError(f"run directory does not exist: {out_dir}")
    for name in names:
        if (out_dir / name).exists():
            raise HistoryGenerationError(
                f"refusing to mix or overwrite existing phase artifact {out_dir / name}; "
                "use a new run directory"
            )


def _append_attempts(
    manifest: dict[str, object], attempts: Sequence[BatchAttempt]
) -> None:
    prior = manifest.setdefault("attempts", [])
    if not isinstance(prior, list):
        raise HistoryGenerationError("run manifest attempts is malformed")
    prior.extend(attempt.model_dump(mode="json") for attempt in attempts)


def _reason_counts(records: Sequence[Mapping[str, object]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for record in records:
        reason = record.get("reason")
        if isinstance(reason, str):
            counts[reason] = counts.get(reason, 0) + 1
    return counts


def _set_phase_reasons(
    manifest: dict[str, object], phase: str, records: Sequence[Mapping[str, object]]
) -> None:
    reasons = manifest.setdefault("quarantine_reasons", {})
    if not isinstance(reasons, dict):
        raise HistoryGenerationError("run manifest quarantine reasons are malformed")
    reasons[phase] = _reason_counts(records)


def _preflight_cases_dir(out_dir: Path, *, create: bool) -> Path:
    cases_dir = _resolve_output(str(out_dir / "cases"))
    if cases_dir.exists() and (not cases_dir.is_dir() or any(cases_dir.iterdir())):
        raise HistoryGenerationError("refusing non-empty generated cases directory")
    if create:
        cases_dir.mkdir(parents=True, exist_ok=True)
    return cases_dir


def _prepare(args: argparse.Namespace) -> int:
    out_dir = _resolve_output(args.out_dir)
    if out_dir.exists() and (not out_dir.is_dir() or any(out_dir.iterdir())):
        raise HistoryGenerationError(
            f"refusing non-empty or non-directory output {out_dir}; "
            "a run must start in a fresh directory"
        )
    templates, registry, template_digest, snapshot_digest = _load_inputs(
        args.templates, args.tool_registry
    )
    shapes = prepare_shapes(templates, max_shapes=args.max_shapes)
    summary = {
        "phase": "prepare",
        "template_count": len(templates),
        "shape_count": len(shapes),
        "registry_count": len(registry),
        "template_digest": template_digest,
        "registry_digest": snapshot_digest,
    }
    if args.dry_run:
        print(json.dumps(summary, sort_keys=True))
        return 0
    out_dir.mkdir(parents=True, exist_ok=True)
    write_jsonl_exclusive(
        out_dir / _SHAPES_FILE,
        (shape.record.model_dump(mode="json") for shape in shapes),
    )
    write_jsonl_exclusive(
        out_dir / _LINEAGE_FILE,
        (
            {
                "shape_id": shape.record.shape_id,
                "frequency": shape.frequency,
                "template_ids": list(shape.template_ids),
            }
            for shape in shapes
        ),
    )
    _write_json(
        out_dir / _RUN_FILE,
        {
            "schema_version": "m3.history-run.v1",
            "prompt_revision": PROMPT_REVISION,
            "phase": "prepared",
            "template_digest": template_digest,
            "registry_digest": snapshot_digest,
            "template_count": len(templates),
            "shape_count": len(shapes),
            "max_shapes": args.max_shapes,
            "provider": _PROVIDER,
            "model": args.model,
            "attempts": [],
            "accepted_counts": {},
            "quarantine_counts": {},
            "quarantine_reasons": {},
        },
        replace=False,
    )
    print(json.dumps(summary, sort_keys=True))
    return 0


def _read_classifications(path: Path) -> dict[str, ClassificationRecord]:
    result: dict[str, ClassificationRecord] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise HistoryGenerationError(f"cannot read {path}") from exc
    for line in lines:
        try:
            record = ClassificationRecord.model_validate(json.loads(line))
        except (json.JSONDecodeError, ValidationError) as exc:
            raise HistoryGenerationError(
                f"invalid classification record in {path}"
            ) from exc
        if record.shape_id in result:
            raise HistoryGenerationError(
                f"duplicate classification {record.shape_id!r}"
            )
        result[record.shape_id] = record
    return result


def _read_quarantine(path: Path) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise HistoryGenerationError(f"cannot read {path}") from exc
    for line in lines:
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as exc:
            raise HistoryGenerationError(
                f"invalid quarantine record in {path}"
            ) from exc
        if (
            not isinstance(raw, dict)
            or not isinstance(raw.get("shape_id"), str)
            or not isinstance(raw.get("reason"), str)
        ):
            raise HistoryGenerationError(f"invalid quarantine record in {path}")
        records.append(cast("dict[str, object]", raw))
    return records


def _reconcile_classification_artifacts(
    manifest: Mapping[str, object],
    shapes: Sequence[PreparedShape],
    classifications: Mapping[str, ClassificationRecord],
    quarantine: Sequence[Mapping[str, object]],
) -> None:
    expected = {shape.record.shape_id for shape in shapes}
    classified = set(classifications)
    quarantined = [record["shape_id"] for record in quarantine]
    quarantined_set = set(quarantined)
    if len(quarantined) != len(quarantined_set):
        raise HistoryGenerationError("duplicate classification quarantine shape id")
    if classified & quarantined_set:
        raise HistoryGenerationError("shape appears in classification and quarantine")
    if classified | quarantined_set != expected:
        raise HistoryGenerationError(
            "classification artifacts do not cover prepared shapes"
        )
    if manifest.get("classification_accepted") != len(classified):
        raise HistoryGenerationError(
            "classification accepted count does not match manifest"
        )
    if manifest.get("classification_quarantined") != len(quarantined):
        raise HistoryGenerationError(
            "classification quarantine count does not match manifest"
        )
    if manifest.get("classification_runnable") != sum(
        is_runnable_classification(record) for record in classifications.values()
    ):
        raise HistoryGenerationError(
            "classification runnable count does not match manifest"
        )
    if manifest.get("classification_review") != sum(
        record.decision == "review" for record in classifications.values()
    ):
        raise HistoryGenerationError(
            "classification review count does not match manifest"
        )
    accepted_counts = manifest.get("accepted_counts")
    quarantine_counts = manifest.get("quarantine_counts")
    if (
        not isinstance(accepted_counts, dict)
        or accepted_counts.get("classification") != len(classified)
        or not isinstance(quarantine_counts, dict)
        or quarantine_counts.get("classification") != len(quarantined)
    ):
        raise HistoryGenerationError("classification manifest counts are inconsistent")
    reasons = manifest.get("quarantine_reasons")
    if not isinstance(reasons, dict) or reasons.get("classification") != _reason_counts(
        quarantine
    ):
        raise HistoryGenerationError(
            "classification quarantine reasons are inconsistent"
        )


def _read_drafts(path: Path) -> list[InstantiationRecord]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise HistoryGenerationError(f"cannot read {path}") from exc
    records: list[InstantiationRecord] = []
    for line in lines:
        try:
            records.append(InstantiationRecord.model_validate(json.loads(line)))
        except (json.JSONDecodeError, ValidationError) as exc:
            raise HistoryGenerationError(
                f"invalid instantiation record in {path}"
            ) from exc
    ids = [record.shape_id for record in records]
    if len(ids) != len(set(ids)):
        raise HistoryGenerationError("duplicate instantiation shape id")
    if ids != sorted(ids):
        raise HistoryGenerationError("instantiation drafts are not sorted by shape id")
    return records


def _read_instantiation_attempts(path: Path) -> list[BatchAttempt]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise HistoryGenerationError(f"cannot read {path}") from exc
    attempts: list[BatchAttempt] = []
    for line in lines:
        try:
            attempt = BatchAttempt.model_validate(json.loads(line))
        except (json.JSONDecodeError, ValidationError) as exc:
            raise HistoryGenerationError(
                f"invalid instantiation attempt in {path}"
            ) from exc
        if attempt.operation != "instantiate":
            raise HistoryGenerationError(
                "instantiation attempt ledger contains another operation"
            )
        attempts.append(attempt)
    if [(attempt.batch_number, attempt.attempt) for attempt in attempts] != sorted(
        (attempt.batch_number, attempt.attempt) for attempt in attempts
    ):
        raise HistoryGenerationError("instantiation attempt ledger is not sorted")
    return attempts


def _reconcile_instantiation_attempts(
    attempts: Sequence[BatchAttempt], selected: Sequence[PreparedShape]
) -> None:
    """Check that an attempt ledger describes exactly the selected batches."""
    batches: dict[int, list[BatchAttempt]] = {}
    for attempt in attempts:
        if attempt.batch_number in batches:
            prior = batches[attempt.batch_number]
            if attempt.attempt != len(prior) + 1:
                raise HistoryGenerationError("instantiation attempt sequence mismatch")
        elif attempt.batch_number != len(batches) + 1:
            raise HistoryGenerationError("instantiation batch sequence mismatch")
        batches.setdefault(attempt.batch_number, []).append(attempt)
    if not batches and not selected:
        return
    if not batches:
        raise HistoryGenerationError("instantiation attempt ledger is empty")
    expected_total = 0
    for records in batches.values():
        if len(records) > 2 or records[-1].status not in {"accepted", "quarantined"}:
            raise HistoryGenerationError("instantiation attempt outcome mismatch")
        if len(records) == 2 and records[0].status != "retry":
            raise HistoryGenerationError("instantiation retry ledger mismatch")
        if len(records) == 1 and records[0].status != "accepted":
            raise HistoryGenerationError("instantiation attempt outcome mismatch")
        record_count = records[0].record_count
        if any(record.record_count != record_count for record in records):
            raise HistoryGenerationError("instantiation attempt count mismatch")
        if record_count < 1 or record_count > INSTANTIATION_BATCH_MAX:
            raise HistoryGenerationError("instantiation batch size mismatch")
        expected_total += record_count
    if expected_total != len(selected):
        raise HistoryGenerationError(
            "instantiation attempt ledger shape count mismatch"
        )


def _load_existing_instantiation_artifacts(
    out_dir: Path,
    selected: Sequence[PreparedShape],
    manifest: Mapping[str, object],
) -> (
    tuple[dict[str, InstantiationRecord], list[dict[str, object]], list[BatchAttempt]]
    | None
):
    """Load a complete persisted model result for safe no-call finalization.

    A failed process may have persisted both model artifacts before it stopped
    while constructing cases. Reusing them is safe only when the pair covers
    exactly the currently selected shapes and any manifest counts already
    present agree. One artifact without the other is an ambiguous mixed run.
    """
    draft_path = out_dir / _DRAFT_FILE
    quarantine_path = out_dir / _INSTANTIATION_QUARANTINE_FILE
    attempts_path = out_dir / _INSTANTIATION_ATTEMPTS_FILE
    draft_exists = draft_path.exists()
    quarantine_exists = quarantine_path.exists()
    attempts_exists = attempts_path.exists()
    if not draft_exists and not quarantine_exists and not attempts_exists:
        return None
    if not (draft_exists and quarantine_exists and attempts_exists):
        raise HistoryGenerationError(
            "partial instantiation artifacts cannot be resumed safely"
        )
    drafts = _read_drafts(draft_path)
    quarantine = _read_quarantine(quarantine_path)
    attempts = _read_instantiation_attempts(attempts_path)
    _reconcile_instantiation_attempts(attempts, selected)
    expected = {shape.record.shape_id for shape in selected}
    draft_ids = {record.shape_id for record in drafts}
    quarantine_ids = [record["shape_id"] for record in quarantine]
    quarantine_id_set = set(quarantine_ids)
    if len(quarantine_ids) != len(quarantine_id_set):
        raise HistoryGenerationError("duplicate instantiation quarantine shape id")
    if draft_ids & quarantine_id_set:
        raise HistoryGenerationError(
            "shape appears in instantiation drafts and quarantine"
        )
    if draft_ids | quarantine_id_set != expected:
        raise HistoryGenerationError(
            "instantiation artifacts do not exactly cover selected shapes"
        )
    accepted_count = manifest.get("instantiation_accepted")
    quarantined_count = manifest.get("instantiation_quarantined")
    if accepted_count is not None and accepted_count != len(drafts):
        raise HistoryGenerationError(
            "instantiation accepted count does not match persisted drafts"
        )
    if quarantined_count is not None and quarantined_count != len(quarantine):
        raise HistoryGenerationError(
            "instantiation quarantine count does not match persisted quarantine"
        )
    accepted_counts = manifest.get("accepted_counts")
    quarantine_counts = manifest.get("quarantine_counts")
    if accepted_counts is not None and not isinstance(accepted_counts, dict):
        raise HistoryGenerationError("instantiation accepted counts are malformed")
    if quarantine_counts is not None and not isinstance(quarantine_counts, dict):
        raise HistoryGenerationError("instantiation quarantine counts are malformed")
    if (
        isinstance(accepted_counts, dict)
        and accepted_counts.get("instantiation") is not None
        and accepted_counts.get("instantiation") != len(drafts)
    ):
        raise HistoryGenerationError(
            "instantiation accepted count is inconsistent with manifest"
        )
    if (
        isinstance(quarantine_counts, dict)
        and quarantine_counts.get("instantiation") is not None
        and quarantine_counts.get("instantiation") != len(quarantine)
    ):
        raise HistoryGenerationError(
            "instantiation quarantine count is inconsistent with manifest"
        )
    return ({record.shape_id: record for record in drafts}, quarantine, attempts)


async def _classify(args: argparse.Namespace) -> int:
    out_dir = _resolve_output(args.out_dir)
    manifest, _templates, registry, shapes = _require_run(
        out_dir, args.templates, args.tool_registry, args.model
    )
    if manifest.get("phase") != "prepared":
        raise HistoryGenerationError("classify requires a prepared run")
    _ensure_new_phase(out_dir, (_CLASSIFICATION_FILE, _CLASSIFICATION_QUARANTINE_FILE))
    if args.dry_run:
        runnable, preflight_quarantine = classification_preflight(shapes, registry)
        print(
            json.dumps(
                {
                    "phase": "classify",
                    "shape_count": len(runnable),
                    "preflight_quarantined": len(preflight_quarantine),
                    "calls": (len(runnable) + args.batch_size - 1) // args.batch_size,
                },
                sort_keys=True,
            )
        )
        return 0
    runner = BatchRunner(model=args.model, executable=args.pi)
    classifications, quarantine, attempts = await classify_batches(
        shapes, runner, batch_size=args.batch_size, descriptor_registry=registry
    )
    write_jsonl_exclusive(
        out_dir / _CLASSIFICATION_FILE,
        (
            classifications[shape_id].model_dump(mode="json")
            for shape_id in sorted(classifications)
        ),
    )
    write_jsonl_exclusive(out_dir / _CLASSIFICATION_QUARANTINE_FILE, quarantine)
    manifest["phase"] = "classified"
    manifest["classification_accepted"] = len(classifications)
    manifest["classification_runnable"] = sum(
        is_runnable_classification(record) for record in classifications.values()
    )
    manifest["classification_review"] = sum(
        record.decision == "review" for record in classifications.values()
    )
    manifest["classification_quarantined"] = len(quarantine)
    accepted_counts = manifest.get("accepted_counts")
    quarantine_counts = manifest.get("quarantine_counts")
    if not isinstance(accepted_counts, dict) or not isinstance(quarantine_counts, dict):
        raise HistoryGenerationError("run manifest count fields are malformed")
    accepted_counts["classification"] = len(classifications)
    quarantine_counts["classification"] = len(quarantine)
    _set_phase_reasons(manifest, "classification", quarantine)
    _append_attempts(manifest, attempts)
    _write_json(out_dir / _RUN_FILE, manifest, replace=True)
    print(
        json.dumps(
            {
                "phase": "classify",
                "accepted": len(classifications),
                "quarantined": len(quarantine),
            },
            sort_keys=True,
        )
    )
    return 0


async def _instantiate(args: argparse.Namespace) -> int:
    out_dir = _resolve_output(args.out_dir)
    manifest, _templates, registry, shapes = _require_run(
        out_dir, args.templates, args.tool_registry, args.model
    )
    if manifest.get("phase") != "classified":
        raise HistoryGenerationError("instantiate requires a classified run")
    if not (out_dir / _CLASSIFICATION_FILE).exists():
        raise HistoryGenerationError("classification artifact is missing")
    classifications = _read_classifications(out_dir / _CLASSIFICATION_FILE)
    classification_quarantine = _read_quarantine(
        out_dir / _CLASSIFICATION_QUARANTINE_FILE
    )
    _reconcile_classification_artifacts(
        manifest, shapes, classifications, classification_quarantine
    )
    cases_dir = _preflight_cases_dir(out_dir, create=not args.dry_run)
    selected = [
        shape
        for shape in shapes
        if shape.record.shape_id in classifications
        and is_runnable_classification(classifications[shape.record.shape_id])
    ]
    existing = _load_existing_instantiation_artifacts(out_dir, selected, manifest)
    if existing is None:
        _ensure_new_phase(
            out_dir,
            (
                _DRAFT_FILE,
                _INSTANTIATION_QUARANTINE_FILE,
                _INSTANTIATION_ATTEMPTS_FILE,
                _CASE_QUARANTINE_FILE,
            ),
        )
    else:
        _ensure_new_phase(out_dir, (_CASE_QUARANTINE_FILE,))
    if args.dry_run:
        print(
            json.dumps(
                {
                    "phase": "instantiate",
                    "shape_count": len(selected),
                    "calls": 0
                    if existing is not None
                    else (len(selected) + args.batch_size - 1) // args.batch_size,
                },
                sort_keys=True,
            )
        )
        return 0
    if existing is None:
        runner = BatchRunner(model=args.model, executable=args.pi)
        drafts, quarantine, attempts = await instantiate_batches(
            selected, classifications, runner, batch_size=args.batch_size
        )
        write_jsonl_exclusive(
            out_dir / _DRAFT_FILE,
            (drafts[shape_id].model_dump(mode="json") for shape_id in sorted(drafts)),
        )
        write_jsonl_exclusive(out_dir / _INSTANTIATION_QUARANTINE_FILE, quarantine)
        write_jsonl_exclusive(
            out_dir / _INSTANTIATION_ATTEMPTS_FILE,
            (attempt.model_dump(mode="json") for attempt in attempts),
        )
    else:
        drafts, quarantine, attempts = existing
    cases, case_quarantine = build_cases(
        selected,
        list(drafts.values()),
        classifications,
        registry,
        already_quarantined=(cast("str", record["shape_id"]) for record in quarantine),
    )
    write_jsonl_exclusive(out_dir / _CASE_QUARANTINE_FILE, case_quarantine)
    for case in cases:
        case_path = cases_dir / f"{case.id}.yaml"
        if case_path.exists():
            raise HistoryGenerationError(f"duplicate generated case {case.id!r}")
        case_path.write_text(
            yaml.safe_dump(case.model_dump(mode="json"), sort_keys=False),
            encoding="utf-8",
        )
    manifest["phase"] = "instantiated"
    manifest["instantiation_accepted"] = len(drafts)
    manifest["case_count"] = len(cases)
    manifest["instantiation_quarantined"] = len(quarantine)
    manifest["case_quarantined"] = len(case_quarantine)
    accepted_counts = manifest.get("accepted_counts")
    quarantine_counts = manifest.get("quarantine_counts")
    if not isinstance(accepted_counts, dict) or not isinstance(quarantine_counts, dict):
        raise HistoryGenerationError("run manifest count fields are malformed")
    accepted_counts["instantiation"] = len(drafts)
    accepted_counts["cases"] = len(cases)
    quarantine_counts["instantiation"] = len(quarantine)
    quarantine_counts["cases"] = len(case_quarantine)
    _set_phase_reasons(manifest, "instantiation", quarantine)
    _set_phase_reasons(manifest, "cases", case_quarantine)
    _append_attempts(manifest, attempts)
    _write_json(out_dir / _RUN_FILE, manifest, replace=True)
    print(
        json.dumps(
            {
                "phase": "instantiate",
                "drafts": len(drafts),
                "cases": len(cases),
                "quarantined": len(quarantine) + len(case_quarantine),
            },
            sort_keys=True,
        )
    )
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="phase", required=True)
    for phase in ("prepare", "classify", "instantiate"):
        sub = subparsers.add_parser(phase)
        sub.add_argument("--templates", type=Path, required=True)
        sub.add_argument("--tool-registry", type=Path, required=True)
        sub.add_argument(
            "--out-dir", required=True, help="fresh directory below .review-eval-local"
        )
        sub.add_argument(
            "--model", choices=sorted(ALLOWED_MODELS), default=DEFAULT_MODEL
        )
        sub.add_argument(
            "--pi", default="pi", help="Pi executable (tests may provide a fake)"
        )
        sub.add_argument("--dry-run", action="store_true")
        if phase == "prepare":
            sub.add_argument("--max-shapes", type=int, default=None)
        elif phase == "classify":
            sub.add_argument("--batch-size", type=int, default=CLASSIFICATION_BATCH_MAX)
        elif phase == "instantiate":
            sub.add_argument("--batch-size", type=int, default=INSTANTIATION_BATCH_MAX)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if getattr(args, "max_shapes", None) is not None and args.max_shapes < 1:
        print("--max-shapes must be positive", file=sys.stderr)
        return 2
    try:
        return _dispatch(args)
    except HistoryGenerationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


def _dispatch(args: argparse.Namespace) -> int:
    if args.phase == "prepare":
        return _prepare(args)
    if args.phase == "classify":
        if not 1 <= args.batch_size <= CLASSIFICATION_BATCH_MAX:
            raise HistoryGenerationError("classification batch size must be 1..25")
        return asyncio.run(_classify(args))
    if not 1 <= args.batch_size <= INSTANTIATION_BATCH_MAX:
        raise HistoryGenerationError("instantiation batch size must be 1..5")
    return asyncio.run(_instantiate(args))


if __name__ == "__main__":
    raise SystemExit(main())
