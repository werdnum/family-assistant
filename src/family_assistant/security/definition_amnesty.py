"""Operator amnesty for executable definitions that predate provenance stamps.

A definition written before definition records existed carries none, and
resolution reads that state exactly as it reads a hash mismatch or a denied
verdict: unresolved, fail-closed. For a deployment whose whole automation
estate predates the feature that is not a conservative default but a permanent
one -- every firing renders a stub and seeds its turn at ``unknown_external``,
forever, because nothing about a legacy definition changes with time.

This module is the bulk migration path for that estate: enumerate the
definitions that hold no record and predate a cutoff the operator states, and
-- on the operator's word -- record an amnesty for each. The amnesty is a
record like any other, written through the same stamping chokepoint, so it
binds to a hash of the content amnestied and is void the moment that content
changes. It is also revocable, which is what keeps the whole operation a
config-shaped decision rather than a one-way rewrite of stored provenance.

The stamp it writes is honest ``unknown_external``: the authoring turn really
is unknown, and nothing here fabricates a trusted one. What makes an amnestied
definition fire as trusted intent is the *disposition* beside that stamp,
resolved through the same cure path a judge-allowed creation takes.

See ``docs/design/legacy-definition-amnesty.md``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from family_assistant.security.definition_records import DefinitionArtifactKind

if TYPE_CHECKING:
    from collections.abc import Collection, Sequence
    from datetime import datetime

    from family_assistant.storage.database import Database

AMNESTIABLE_ARTIFACT_KINDS: tuple[DefinitionArtifactKind, ...] = (
    DefinitionArtifactKind.SCHEDULE_AUTOMATION,
    DefinitionArtifactKind.EVENT_LISTENER,
    DefinitionArtifactKind.SCRIPT,
)
"""The durable definition classes an amnesty covers.

One-shot callbacks are deliberately absent: their records ride an enqueued task
payload, they expire on firing, and the population clears itself within the
deployment's scheduling horizon, so there is nothing durable to migrate.
"""


@dataclass(frozen=True, slots=True)
class LegacyDefinition:
    """One definition an operator may amnesty, as the operator sees it."""

    kind: DefinitionArtifactKind
    artifact_id: str
    """The identity its own store addresses it by: a row id, or a script name."""

    name: str
    created_at: datetime | None

    def describe(self) -> str:
        """One line naming this definition for an operator's review."""
        created = (
            self.created_at.isoformat() if self.created_at is not None else "unknown"
        )
        return f"{self.kind.value} {self.artifact_id} '{self.name}' (created {created})"


async def list_legacy_definitions(
    db: Database,
    *,
    created_before: datetime,
    kinds: Collection[DefinitionArtifactKind] = AMNESTIABLE_ARTIFACT_KINDS,
) -> list[LegacyDefinition]:
    """Enumerate definitions holding no record that predate ``created_before``.

    The listing an operator reviews before granting anything. It is the same
    predicate the write applies, but the write re-checks it under its own lock,
    so a definition written between the listing and the grant is skipped rather
    than amnestied on the strength of a stale read.
    """
    legacy: list[LegacyDefinition] = []
    if DefinitionArtifactKind.SCHEDULE_AUTOMATION in kinds:
        for automation in await db.schedule_automations.list_unstamped_definitions(
            created_before=created_before
        ):
            legacy.append(
                LegacyDefinition(
                    kind=DefinitionArtifactKind.SCHEDULE_AUTOMATION,
                    artifact_id=str(automation["id"]),
                    name=automation["name"],
                    created_at=automation["created_at"],
                )
            )
    if DefinitionArtifactKind.EVENT_LISTENER in kinds:
        for listener in await db.events.list_unstamped_listener_definitions(
            created_before=created_before
        ):
            legacy.append(
                LegacyDefinition(
                    kind=DefinitionArtifactKind.EVENT_LISTENER,
                    artifact_id=str(listener["id"]),
                    name=listener["name"],
                    created_at=listener["created_at"],
                )
            )
    if DefinitionArtifactKind.SCRIPT in kinds:
        for script in await db.scripts.list_unstamped_definitions(
            created_before=created_before
        ):
            legacy.append(
                LegacyDefinition(
                    kind=DefinitionArtifactKind.SCRIPT,
                    artifact_id=script.name,
                    name=script.name,
                    created_at=script.created_at,
                )
            )
    return legacy


async def amnesty_definition(
    db: Database,
    definition: LegacyDefinition,
    *,
    created_before: datetime,
) -> bool:
    """Record an operator's amnesty for one definition.

    Returns whether it was recorded. ``False`` means the definition stopped
    being eligible between the listing and the write -- it was deleted, or
    written through a real gate -- which is the outcome that leaves the newer
    record standing.
    """
    match definition.kind:
        case DefinitionArtifactKind.SCHEDULE_AUTOMATION:
            return await db.schedule_automations.amnesty_legacy_definition(
                int(definition.artifact_id), created_before=created_before
            )
        case DefinitionArtifactKind.EVENT_LISTENER:
            return await db.events.amnesty_legacy_listener_definition(
                int(definition.artifact_id), created_before=created_before
            )
        case DefinitionArtifactKind.SCRIPT:
            return await db.scripts.amnesty_legacy_definition(
                definition.artifact_id, created_before=created_before
            )
        case DefinitionArtifactKind.TASK_PAYLOAD:
            return False


async def amnesty_legacy_definitions(
    db: Database,
    definitions: Sequence[LegacyDefinition],
    *,
    created_before: datetime,
) -> list[LegacyDefinition]:
    """Amnesty each definition in turn, returning those actually recorded."""
    granted: list[LegacyDefinition] = []
    for definition in definitions:
        if await amnesty_definition(db, definition, created_before=created_before):
            granted.append(definition)
    return granted


async def revoke_definition_amnesty(
    db: Database,
    definition: LegacyDefinition,
) -> bool:
    """Clear one definition's amnesty, restoring its fail-closed legacy state."""
    match definition.kind:
        case DefinitionArtifactKind.SCHEDULE_AUTOMATION:
            return await db.schedule_automations.revoke_legacy_amnesty(
                int(definition.artifact_id)
            )
        case DefinitionArtifactKind.EVENT_LISTENER:
            return await db.events.revoke_legacy_listener_amnesty(
                int(definition.artifact_id)
            )
        case DefinitionArtifactKind.SCRIPT:
            return await db.scripts.revoke_legacy_amnesty(definition.artifact_id)
        case DefinitionArtifactKind.TASK_PAYLOAD:
            return False


async def list_amnestied_definitions(
    db: Database,
    *,
    kinds: Collection[DefinitionArtifactKind] = AMNESTIABLE_ARTIFACT_KINDS,
) -> list[LegacyDefinition]:
    """Enumerate the definitions currently holding an operator amnesty.

    What a revocation acts on, and what an operator reviews when hardening the
    executable-persistence gate: an amnesty records a decision no gate made, so
    tightening the gate comes with a look at the estate already let through.
    """
    amnestied: list[LegacyDefinition] = []
    if DefinitionArtifactKind.SCHEDULE_AUTOMATION in kinds:
        for automation in await db.schedule_automations.list_amnestied_definitions():
            amnestied.append(
                LegacyDefinition(
                    kind=DefinitionArtifactKind.SCHEDULE_AUTOMATION,
                    artifact_id=str(automation["id"]),
                    name=automation["name"],
                    created_at=automation["created_at"],
                )
            )
    if DefinitionArtifactKind.EVENT_LISTENER in kinds:
        for listener in await db.events.list_amnestied_listener_definitions():
            amnestied.append(
                LegacyDefinition(
                    kind=DefinitionArtifactKind.EVENT_LISTENER,
                    artifact_id=str(listener["id"]),
                    name=listener["name"],
                    created_at=listener["created_at"],
                )
            )
    if DefinitionArtifactKind.SCRIPT in kinds:
        for script in await db.scripts.list_amnestied_definitions():
            amnestied.append(
                LegacyDefinition(
                    kind=DefinitionArtifactKind.SCRIPT,
                    artifact_id=script.name,
                    name=script.name,
                    created_at=script.created_at,
                )
            )
    return amnestied
