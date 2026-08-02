"""WebChatInterface emits an account-global activity ping for out-of-band sends.

Scheduled/reminder callbacks and tool-initiated messages reach the web UI via
``WebChatInterface.send_message`` (not the ``/turns`` turn lifecycle), so without
this the conversation list would stay stale for those replies on a client sitting
on another thread.
"""

import asyncio

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.config_models import AppConfig
from family_assistant.llm.messages import UserMessage
from family_assistant.security.taint import (
    SourceTrustTier,
    TaintSource,
    TaintSourceType,
    TurnTaintState,
)
from family_assistant.services.user_identity import UserIdentityResolver
from family_assistant.storage import init_db
from family_assistant.storage.database import Database
from family_assistant.utils.clock import SystemClock
from family_assistant.web.conversation_stream_hub import ConversationStreamHub
from family_assistant.web.web_chat_interface import WebChatInterface


@pytest.mark.asyncio
async def test_send_message_pings_activity_for_conversation_owner(
    db_engine: AsyncEngine,
) -> None:
    conversation_id = "web_conv_scheduled"
    owner_id = "user-1"

    # Seed a user message so the conversation has a resolvable owner (the
    # assistant send itself carries no user_id).
    ctx = Database(engine=db_engine)
    await init_db(db_engine)
    await ctx.init_vector_db()
    await ctx.message_history.add_message(
        UserMessage(content="remind me later"),
        interface_type="web",
        conversation_id=conversation_id,
        timestamp=SystemClock().now(),
        user_id=owner_id,
    )

    hub = ConversationStreamHub()
    interface = WebChatInterface(db_engine, notifier=None, stream_hub=hub)
    handle = hub.subscribe_activity(owner_id)
    other = hub.subscribe_activity("someone-else")

    saved = await interface.send_message(conversation_id, "Your reminder: tea time")
    assert saved is not None

    activity = await asyncio.wait_for(handle.queue.get(), timeout=1.0)
    assert activity.conversation_id == conversation_id
    assert activity.reason == "message"
    # Not delivered to a different user's activity subscriber.
    assert other.queue.empty()


@pytest.mark.asyncio
async def test_send_message_canonicalizes_alias_owner_for_activity(
    db_engine: AsyncEngine,
) -> None:
    """A conversation stored under an alias owner id (e.g. a Telegram numeric id)
    still pings the canonical web subscriber, because owner ids are canonicalized
    before scoping the activity ping."""
    conversation_id = "web_conv_alias_owner"
    config = AppConfig.model_validate({
        "users": [
            {
                "id": "andrew@example.com",
                "oidc": {"emails": ["andrew@example.com"]},
                "telegram": {"user_ids": [123456789]},
            }
        ]
    })
    resolver = UserIdentityResolver(config)

    ctx = Database(engine=db_engine)
    await init_db(db_engine)
    await ctx.init_vector_db()
    # Owner stored under the raw Telegram numeric id.
    await ctx.message_history.add_message(
        UserMessage(content="set a reminder"),
        interface_type="web",
        conversation_id=conversation_id,
        timestamp=SystemClock().now(),
        user_id="123456789",
    )

    hub = ConversationStreamHub()
    interface = WebChatInterface(
        db_engine, notifier=None, stream_hub=hub, identity_resolver=resolver
    )
    # The web session subscribes under its canonical id, not the Telegram alias.
    handle = hub.subscribe_activity("andrew@example.com")

    saved = await interface.send_message(conversation_id, "Your reminder: tea time")
    assert saved is not None

    activity = await asyncio.wait_for(handle.queue.get(), timeout=1.0)
    assert activity.conversation_id == conversation_id


@pytest.mark.asyncio
async def test_send_message_persists_runtime_taint_metadata(
    db_engine: AsyncEngine,
) -> None:
    """Web sends record taint state: caller-provided when given, explicit empty
    (trusted baseline) otherwise — never a metadata-less row, which would be
    escalated to unknown_external at read time."""
    conversation_id = "web_conv_taint"
    ctx = Database(engine=db_engine)
    await init_db(db_engine)
    await ctx.init_vector_db()

    interface = WebChatInterface(db_engine, notifier=None, stream_hub=None)

    default_saved = await interface.send_message(conversation_id, "plain notification")
    assert default_saved is not None

    tainted_state = TurnTaintState.empty().add_source(
        TaintSource(
            source_type=TaintSourceType.EMAIL,
            source_id="email-1",
            tier=SourceTrustTier.UNKNOWN_EXTERNAL,
            labels=frozenset(),
            reason="test email source",
        )
    )
    tainted_saved = await interface.send_message(
        conversation_id,
        "derived from a tainted turn",
        taint_metadata=tainted_state.to_metadata(),
    )
    assert tainted_saved is not None

    ctx = Database(engine=db_engine)
    default_row = await ctx.message_history.get_row_by_internal_id(int(default_saved))
    tainted_row = await ctx.message_history.get_row_by_internal_id(int(tainted_saved))

    assert default_row is not None
    assert default_row["taint_metadata_version"] == "runtime_v1"
    assert default_row["taint_metadata_json"] is not None
    assert default_row["taint_metadata_json"].get("max_tier") == "trusted_user"

    assert tainted_row is not None
    assert tainted_row["taint_metadata_version"] == "runtime_v1"
    assert tainted_row["taint_metadata_json"] is not None
    assert tainted_row["taint_metadata_json"].get("max_tier") == "unknown_external"
