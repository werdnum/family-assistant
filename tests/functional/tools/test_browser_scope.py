"""Which delegated turns share a browser tab, and which get their own.

Two browser runs the main assistant starts at once must not drive the same tab,
while the visual hop a browser profile delegates to must keep working in its
parent's tab.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

import pytest

from family_assistant.storage.database import Database
from family_assistant.storage.repositories.delegation_runs import DelegationRunCreate
from family_assistant.tools.browser_dom import browser_open_tool
from family_assistant.tools.browser_session import (
    browser_scope_key,
    delegated_browser_scope,
    get_browser_session,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

    from family_assistant.tools.types import ToolDefinition, ToolExecutionContext

pytestmark = pytest.mark.asyncio

ASSISTANT = "default_assistant"
BROWSER = "browser_profile"
VISUAL = "browser_visual_profile"
CREDENTIAL = "credential_browser_profile"
CREDENTIAL_VISUAL = "credential_browser_visual_profile"


def _definition(name: str) -> ToolDefinition:
    return cast(
        "ToolDefinition",
        {"type": "function", "function": {"name": name, "parameters": {}}},
    )


@dataclass
class _ToolsProvider:
    names: list[str]

    async def get_tool_definitions(self) -> list[ToolDefinition]:
        return [_definition(name) for name in self.names]


@dataclass
class _Service:
    """The part of a processing service browser scoping reads."""

    tools_provider: _ToolsProvider
    processing_services_registry: dict[str, _Service] = field(default_factory=dict)
    app_config: SimpleNamespace = field(
        default_factory=lambda: SimpleNamespace(
            browser_handoff_config=SimpleNamespace(
                autofill_capable_profiles=[CREDENTIAL, CREDENTIAL_VISUAL]
            )
        )
    )


def _registry() -> dict[str, _Service]:
    # browser_open's wrapper is what puts its name in the browser registry.
    assert browser_open_tool.browser_tool_name == "browser_open"  # pyright: ignore[reportFunctionMemberAccess]
    registry: dict[str, _Service] = {
        ASSISTANT: _Service(_ToolsProvider(["delegate_to_service", "get_note"])),
        BROWSER: _Service(_ToolsProvider(["browser_open", "delegate_to_service"])),
        VISUAL: _Service(_ToolsProvider(["computer_use_click"])),
        CREDENTIAL: _Service(
            _ToolsProvider(["browser_open", "browser_autofill", "delegate_to_service"])
        ),
        CREDENTIAL_VISUAL: _Service(_ToolsProvider(["computer_use_click"])),
    }
    for service in registry.values():
        service.processing_services_registry = registry
    return registry


def _context(
    db: Database,
    registry: dict[str, _Service],
    conversation_id: str,
    *,
    profile_id: str,
    subconversation_id: str | None,
) -> ToolExecutionContext:
    return cast(
        "ToolExecutionContext",
        SimpleNamespace(
            conversation_id=conversation_id,
            subconversation_id=subconversation_id,
            db_context=db,
            processing_service=registry[profile_id],
            processing_profile_id=profile_id,
            timezone=None,
        ),
    )


async def _delegate(
    db: Database,
    conversation_id: str,
    *,
    source_profile_id: str,
    target_profile_id: str,
    source_subconversation_id: str | None,
) -> str:
    """Record a queued delegation run and return its subconversation id."""
    delegation_id = f"deleg-{uuid.uuid4().hex}"
    subconversation_id = str(uuid.uuid4())
    await db.delegation_runs.create_run(
        DelegationRunCreate(
            delegation_id=delegation_id,
            task_id=f"task-{delegation_id}",
            source_profile_id=source_profile_id,
            target_service_id=target_profile_id,
            interface_type="web",
            conversation_id=conversation_id,
            subconversation_id=subconversation_id,
            source_subconversation_id=source_subconversation_id,
            request_text="Look something up.",
            content_parts_json=[],
        )
    )
    return subconversation_id


async def test_parallel_browser_runs_from_the_assistant_get_separate_sessions(
    db_engine: AsyncEngine,
) -> None:
    db = Database(db_engine)
    registry = _registry()
    conversation_id = str(uuid.uuid4())
    first, second = [
        await _delegate(
            db,
            conversation_id,
            source_profile_id=ASSISTANT,
            target_profile_id=BROWSER,
            source_subconversation_id=None,
        )
        for _ in range(2)
    ]

    first_session = await get_browser_session(
        _context(
            db, registry, conversation_id, profile_id=BROWSER, subconversation_id=first
        )
    )
    second_session = await get_browser_session(
        _context(
            db, registry, conversation_id, profile_id=BROWSER, subconversation_id=second
        )
    )

    assert first_session is not second_session


async def test_visual_hop_shares_its_browser_parents_scope(
    db_engine: AsyncEngine,
) -> None:
    db = Database(db_engine)
    registry = _registry()
    conversation_id = str(uuid.uuid4())
    browser_run = await _delegate(
        db,
        conversation_id,
        source_profile_id=ASSISTANT,
        target_profile_id=BROWSER,
        source_subconversation_id=None,
    )
    visual_run = await _delegate(
        db,
        conversation_id,
        source_profile_id=BROWSER,
        target_profile_id=VISUAL,
        source_subconversation_id=browser_run,
    )

    visual_scope = await browser_scope_key(
        _context(
            db,
            registry,
            conversation_id,
            profile_id=VISUAL,
            subconversation_id=visual_run,
        )
    )

    assert visual_scope == await browser_scope_key(
        _context(
            db,
            registry,
            conversation_id,
            profile_id=BROWSER,
            subconversation_id=browser_run,
        )
    )


async def test_hop_from_a_top_level_browser_turn_uses_the_conversation_scope(
    db_engine: AsyncEngine,
) -> None:
    db = Database(db_engine)
    registry = _registry()
    conversation_id = str(uuid.uuid4())
    visual_run = await _delegate(
        db,
        conversation_id,
        source_profile_id=BROWSER,
        target_profile_id=VISUAL,
        source_subconversation_id=None,
    )

    scope = await browser_scope_key(
        _context(
            db,
            registry,
            conversation_id,
            profile_id=VISUAL,
            subconversation_id=visual_run,
        )
    )

    assert scope == conversation_id


async def test_inline_hop_from_a_browser_turn_shares_its_scope(
    db_engine: AsyncEngine,
) -> None:
    db = Database(db_engine)
    registry = _registry()
    conversation_id = str(uuid.uuid4())
    browser_run = await _delegate(
        db,
        conversation_id,
        source_profile_id=ASSISTANT,
        target_profile_id=BROWSER,
        source_subconversation_id=None,
    )
    parent = _context(
        db,
        registry,
        conversation_id,
        profile_id=BROWSER,
        subconversation_id=browser_run,
    )
    inline_child = str(uuid.uuid4())

    async with delegated_browser_scope(parent, inline_child, VISUAL):
        child_scope = await browser_scope_key(
            _context(
                db,
                registry,
                conversation_id,
                profile_id=VISUAL,
                subconversation_id=inline_child,
            )
        )

    assert child_scope == await browser_scope_key(parent)


async def test_inline_delegation_from_a_non_browser_turn_gets_its_own_scope(
    db_engine: AsyncEngine,
) -> None:
    db = Database(db_engine)
    registry = _registry()
    conversation_id = str(uuid.uuid4())
    parent = _context(
        db, registry, conversation_id, profile_id=ASSISTANT, subconversation_id=None
    )
    inline_child = str(uuid.uuid4())

    async with delegated_browser_scope(parent, inline_child, BROWSER):
        child_scope = await browser_scope_key(
            _context(
                db,
                registry,
                conversation_id,
                profile_id=BROWSER,
                subconversation_id=inline_child,
            )
        )

    assert child_scope != conversation_id


async def test_parallel_credential_runs_from_a_browser_run_get_separate_scopes(
    db_engine: AsyncEngine,
) -> None:
    """A credential child never shares its parent's browser, so siblings stay apart."""
    db = Database(db_engine)
    registry = _registry()
    conversation_id = str(uuid.uuid4())
    browser_run = await _delegate(
        db,
        conversation_id,
        source_profile_id=ASSISTANT,
        target_profile_id=BROWSER,
        source_subconversation_id=None,
    )
    first, second = [
        await _delegate(
            db,
            conversation_id,
            source_profile_id=BROWSER,
            target_profile_id=CREDENTIAL,
            source_subconversation_id=browser_run,
        )
        for _ in range(2)
    ]

    first_scope = await browser_scope_key(
        _context(
            db,
            registry,
            conversation_id,
            profile_id=CREDENTIAL,
            subconversation_id=first,
        )
    )
    second_scope = await browser_scope_key(
        _context(
            db,
            registry,
            conversation_id,
            profile_id=CREDENTIAL,
            subconversation_id=second,
        )
    )

    assert first_scope != second_scope


async def test_inline_credential_visual_hop_shares_the_credential_scope(
    db_engine: AsyncEngine,
) -> None:
    db = Database(db_engine)
    registry = _registry()
    conversation_id = str(uuid.uuid4())
    credential_run = await _delegate(
        db,
        conversation_id,
        source_profile_id=BROWSER,
        target_profile_id=CREDENTIAL,
        source_subconversation_id=None,
    )
    parent = _context(
        db,
        registry,
        conversation_id,
        profile_id=CREDENTIAL,
        subconversation_id=credential_run,
    )
    inline_child = str(uuid.uuid4())

    async with delegated_browser_scope(parent, inline_child, CREDENTIAL_VISUAL):
        child_scope = await browser_scope_key(
            _context(
                db,
                registry,
                conversation_id,
                profile_id=CREDENTIAL_VISUAL,
                subconversation_id=inline_child,
            )
        )

    assert child_scope == await browser_scope_key(parent)


async def test_parallel_browser_runs_from_a_browser_run_get_separate_scopes(
    db_engine: AsyncEngine,
) -> None:
    """A browser run fanning out to its own profile gives each child its own tab."""
    db = Database(db_engine)
    registry = _registry()
    conversation_id = str(uuid.uuid4())
    browser_run = await _delegate(
        db,
        conversation_id,
        source_profile_id=ASSISTANT,
        target_profile_id=BROWSER,
        source_subconversation_id=None,
    )
    first, second = [
        await _delegate(
            db,
            conversation_id,
            source_profile_id=BROWSER,
            target_profile_id=BROWSER,
            source_subconversation_id=browser_run,
        )
        for _ in range(2)
    ]

    first_scope = await browser_scope_key(
        _context(
            db, registry, conversation_id, profile_id=BROWSER, subconversation_id=first
        )
    )
    second_scope = await browser_scope_key(
        _context(
            db, registry, conversation_id, profile_id=BROWSER, subconversation_id=second
        )
    )

    assert first_scope != second_scope
