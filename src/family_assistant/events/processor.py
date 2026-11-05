"""
Event processor that routes events to storage and listeners.
"""

import asyncio
import json
import logging
import time
from collections.abc import Callable, Collection, Mapping
from datetime import tzinfo
from typing import Any, TypedDict, cast

from sqlalchemy import select, text

from family_assistant.actions import (
    EVENT_HANDLER_PROFILE_ID,
    ActionType,
    execute_action,
)
from family_assistant.events.condition_evaluator import EventConditionEvaluator
from family_assistant.events.mcp_source import SERVER_KEY
from family_assistant.events.sources import EventSource
from family_assistant.events.storage import EventStorage
from family_assistant.scripting.errors import ScriptExecutionError
from family_assistant.storage.database import (
    Database,
    DatabaseTransaction,
)
from family_assistant.storage.events import (
    EventSourceType,
    check_and_update_rate_limit,
    event_listeners_table,
)
from family_assistant.storage.types import (
    EventConditionEvaluatorConfig,
    EventListenerDict,
    MatchConditions,
)

logger = logging.getLogger(__name__)


class SourceHealthInfoDict(TypedDict, total=False):
    """Health info for an individual event source.

    For sources with _connection_healthy: healthy, reconnect_attempts, last_event_time.
    For other sources: status.
    """

    healthy: bool | None
    reconnect_attempts: int
    last_event_time: float
    status: str


class ListenerCacheInfoDict(TypedDict):
    """Info about the listener cache state."""

    last_refresh: float
    listener_count: int
    by_source: dict[str, int]


class EventProcessorHealthStatus(TypedDict):
    """Return type for EventProcessor.get_health_status()."""

    processor_running: bool
    sources: dict[str, SourceHealthInfoDict]
    listener_cache: ListenerCacheInfoDict


class EventProcessor:
    """Routes events from sources to storage and listeners."""

    def __init__(
        self,
        sources: dict[str, EventSource],
        db_context: Database | None = None,
        sample_interval_hours: float = 1.0,
        config: EventConditionEvaluatorConfig | None = None,
        get_db_context_func: Callable[[], Database] | None = None,
        timezone: tzinfo | None = None,
        profile_wake_llm_flags: Mapping[str, bool] | None = None,
        trusted_event_server_ids: Collection[str] = (),
    ) -> None:
        """
        Initialize event processor.

        Args:
            sources: Dictionary of source_id -> EventSource instances
            db_context: Database handle (optional, will create if needed)
            sample_interval_hours: Hours between storing event samples
            config: Optional configuration for script execution
            get_db_context_func: Function to get a Database handle
            timezone: Timezone for condition script time API functions.
            profile_wake_llm_flags: Mapping of profile id -> allow_wake_llm, used
                to refuse wake_llm listeners whose *origin* profile may not wake
                the LLM (creation-path guards cannot cover pre-existing or
                admin-created listeners). None disables the check.
            trusted_event_server_ids: MCP servers configured with
                ``trusted_events``, whose wakes run under the listener's origin
                profile instead of event_handler.
        """
        self.sources = sources
        self.profile_wake_llm_flags = profile_wake_llm_flags
        self.trusted_event_server_ids = frozenset(trusted_event_server_ids)
        self.event_storage = EventStorage(
            sample_interval_hours, get_db_context_func=get_db_context_func
        )
        self._db_context = db_context  # Store for tests
        self.get_db_context_func = get_db_context_func
        self._listener_cache: dict[str, list[EventListenerDict]] = {}
        self._cache_refresh_interval = 60  # Refresh from DB every minute
        self._last_cache_refresh = 0
        self._running = False
        # Lock to prevent concurrent database operations
        self._process_lock = asyncio.Lock()
        # Initialize condition evaluator for script execution
        self.condition_evaluator = EventConditionEvaluator(config, timezone=timezone)

    async def start(self) -> None:
        """Start all event sources."""
        self._running = True
        logger.info(f"Starting EventProcessor with {len(self.sources)} sources")

        # Refresh listener cache
        await self._refresh_listener_cache()

        # Start all sources
        for source_id, source in self.sources.items():
            try:
                await source.start(self)
                logger.info(f"Started event source: {source_id}")
            except Exception as e:
                logger.exception(f"Failed to start event source {source_id}: {e}")

    async def stop(self) -> None:
        """Stop all event sources."""
        self._running = False
        logger.info("Stopping EventProcessor")

        for source_id, source in self.sources.items():
            try:
                await source.stop()
                logger.info(f"Stopped event source: {source_id}")
            except Exception as e:
                logger.exception(f"Failed to stop event source {source_id}: {e}")

    @property
    def is_running(self) -> bool:
        """Whether events handed to :meth:`process_event` are processed."""
        return self._running

    async def process_event(
        self,
        source_id: str,
        # ast-grep-ignore: no-dict-any - event_data is arbitrary JSON from external sources with no fixed schema
        event_data: dict[str, Any],
        *,
        listener_ids: Collection[int] | None = None,
    ) -> None:
        """Process an event from a source.

        ``listener_ids`` confines the event to those listeners, for a source
        whose events were fetched on behalf of particular listeners -- with
        their creator's credential -- and so must not reach anyone else's.
        """
        if not self._running:
            return

        # Use lock to prevent concurrent cache refreshes
        async with self._process_lock:
            # A source that names its listeners read them from the database
            # itself, so the cache must be at least as new as that read: a
            # listener created or edited since the last refresh would otherwise
            # be missed or matched against its old conditions.
            if (
                listener_ids is not None
                or time.time() - self._last_cache_refresh > self._cache_refresh_interval
            ):
                await self._refresh_listener_cache()

            # Get all active listeners for this source
            listeners = self._listener_cache.get(source_id, [])
            if listener_ids is not None:
                wanted = set(listener_ids)
                listeners = [item for item in listeners if item["id"] in wanted]

        # Get database handle (for tests or via factory function)
        if self._db_context:
            db_ctx = self._db_context
        elif self.get_db_context_func:
            db_ctx = self.get_db_context_func()
        else:
            raise RuntimeError(
                "EventProcessor requires get_db_context_func to be provided"
            )

        # Match conditions may run a condition script, so they are evaluated
        # before the transaction opens rather than inside it.
        matched = [
            listener
            for listener in listeners
            if await self._check_match_conditions(
                event_data,
                listener["match_conditions"],
                listener.get("condition_script"),
            )
        ]

        async def _process_matched(txn: DatabaseTransaction) -> list[int]:
            """Every matched listener as one unit.

            Within a listener the rate-limit update, the action enqueue and the
            one-time disable must not come apart, or a failed disable re-fires a
            one-time action and a failed enqueue burns quota.

            Across listeners it matters just as much. A failure here reaches the
            caller -- it has to, or a webhook source acknowledges an event whose
            action was dropped and the sender never retries -- and the retry
            replays the whole event. A listener that had committed on its own
            would then fire a second time.

            ``triggered`` is built inside the closure because ``atomic`` replays
            this body after a rollback.
            """
            triggered: list[int] = []
            for listener in matched:
                allowed, reason = await check_and_update_rate_limit(
                    txn, listener["id"], listener["conversation_id"]
                )
                if not allowed:
                    logger.warning(f"Listener {listener['id']} rate limited: {reason}")
                    continue

                await self._execute_action_in_context(txn, listener, event_data)

                # Handle one-time listeners
                if listener.get("one_time"):
                    await self._disable_listener_in_context(txn, listener["id"])
                triggered.append(listener["id"])

            return triggered

        triggered_listener_ids = await db_ctx.atomic(_process_matched)

        # A one-time listener that just fired is disabled in the database, but
        # the cache would offer it to the next event until the next refresh.
        spent = {
            listener["id"]
            for listener in matched
            if listener.get("one_time") and listener["id"] in triggered_listener_ids
        }
        if spent:
            async with self._process_lock:
                self._listener_cache[source_id] = [
                    item
                    for item in self._listener_cache.get(source_id, [])
                    if item["id"] not in spent
                ]

        # Deliberately after the commit, on the handle, and unable to fail the
        # call. This row is a debug and sampling artifact: nothing derives from
        # it, so losing one costs nothing, while letting it affect the listeners
        # costs plenty. Inside the transaction a failed INSERT would poison it
        # on PostgreSQL and roll back real listener work; raising here would
        # make the caller retry an event whose actions are already committed,
        # firing them twice.
        try:
            await self.event_storage.store_event_in_context(
                db_ctx, source_id, event_data, triggered_listener_ids
            )
        except Exception:
            logger.exception(
                "Failed to record event from %s for debugging; its listener "
                "actions are already committed and are unaffected.",
                source_id,
            )

    async def _check_match_conditions(
        self,
        # ast-grep-ignore: no-dict-any - event_data is arbitrary JSON from external sources with no fixed schema
        event_data: dict[str, Any],
        match_conditions: MatchConditions | None,
        condition_script: str | None,
    ) -> bool:
        """Check if event matches the listener's conditions.

        Both match_conditions and condition_script are evaluated with AND semantics:
        dict conditions are checked first (cheap), then script if present.
        """
        if match_conditions:
            for key, expected_value in match_conditions.items():
                actual_value = self._get_nested_value(event_data, key)
                if actual_value != expected_value:
                    return False

        if condition_script:
            try:
                return await self.condition_evaluator.evaluate_condition(
                    condition_script, event_data
                )
            except ScriptExecutionError as e:
                logger.error(f"Script condition error: {e}")
                return False
            except Exception as e:
                logger.error(f"Unexpected error evaluating script condition: {e}")
                return False

        return True

    def _get_nested_value(
        self,
        # ast-grep-ignore: no-dict-any - navigates arbitrary nested JSON from external event sources
        data: dict[str, Any],
        key_path: str,
        # ast-grep-ignore: no-dict-any - return type includes nested dicts from arbitrary external JSON
    ) -> str | int | float | bool | dict[str, Any] | list[Any] | None:
        """Get value from nested dict using dot notation (e.g., 'new_state.state')."""
        keys = key_path.split(".")
        value = data
        for key in keys:
            if isinstance(value, dict) and key in value:
                value = value[key]
            else:
                return None
        return value

    async def _refresh_listener_cache(self) -> None:
        """Refresh the listener cache from database."""
        # Use provided db_context for tests, otherwise create new one
        query = select(event_listeners_table).where(
            event_listeners_table.c.enabled.is_(True)
        )

        if self._db_context:
            result = await self._db_context.fetch_all(query)
        elif self.get_db_context_func:
            db_ctx = self.get_db_context_func()
            result = await db_ctx.fetch_all(query)
        else:
            raise RuntimeError(
                "EventProcessor requires get_db_context_func to be provided"
            )

        new_cache: dict[str, list[EventListenerDict]] = {}
        for row in result:
            listener_dict = cast("EventListenerDict", dict(row))
            # Parse JSON fields if they're strings
            match_conditions = listener_dict.get("match_conditions") or {}
            if isinstance(match_conditions, str):
                listener_dict["match_conditions"] = json.loads(match_conditions)
            else:
                listener_dict["match_conditions"] = match_conditions

            action_config = listener_dict.get("action_config") or {}
            if isinstance(action_config, str):
                listener_dict["action_config"] = json.loads(action_config)
            else:
                listener_dict["action_config"] = action_config

            source_id = listener_dict["source_id"]
            if source_id not in new_cache:
                new_cache[source_id] = []
            new_cache[source_id].append(listener_dict)

        self._listener_cache = new_cache
        self._last_cache_refresh = time.time()
        logger.debug(
            f"Refreshed listener cache: {sum(len(v) for v in new_cache.values())} "
            f"listeners across {len(new_cache)} sources"
        )

    async def _execute_action(
        self,
        listener: EventListenerDict,
        # ast-grep-ignore: no-dict-any - event_data is arbitrary JSON from external sources with no fixed schema
        event_data: dict[str, Any],
    ) -> None:
        """Execute the action defined in the listener (opens new DB context)."""
        if self.get_db_context_func:
            db_ctx = self.get_db_context_func()
            await self._execute_action_in_context(db_ctx, listener, event_data)
        else:
            raise RuntimeError(
                "EventProcessor requires get_db_context_func to be provided"
            )

    async def _execute_action_in_context(
        self,
        db_ctx: Database | DatabaseTransaction,
        listener: EventListenerDict,
        # ast-grep-ignore: no-dict-any - event_data is arbitrary JSON from external sources with no fixed schema
        event_data: dict[str, Any],
    ) -> None:
        """Execute the action defined in the listener within existing DB context."""
        action_type = ActionType(listener["action_type"])
        action_config = listener["action_config"] or {}

        # ast-grep-ignore: no-dict-any - context dict contains mixed types including arbitrary event_data for action execution
        context: dict[str, Any] = {
            "trigger": f"Event listener '{listener['name']}' matched",
            "listener_id": listener["id"],
            "source": listener["source_id"],
        }

        if (
            action_type == ActionType.WAKE_LLM
            and action_config.get("include_event_data", True)
            or action_type == ActionType.SCRIPT
        ):
            context["event_data"] = event_data

        # Event triggers are untrusted (e.g. attacker-influenced email/webhook
        # content). A wake_llm turn processes that content and is the injectable
        # component, so it runs under the restricted "event_handler" profile
        # rather than the listener creator's (often full-trust) profile -- unless
        # the event comes from an MCP server the operator declared trusted_events
        # for. Script actions keep running under the creating profile so their
        # validated tool set matches execution.
        payload_trusted = self._payload_is_trusted(listener)
        if action_type == ActionType.WAKE_LLM:
            # The event_handler routing must not launder a wake the origin
            # profile may not perform: creation of wake_llm listeners is denied
            # for allow_wake_llm=False profiles, but pre-existing or
            # admin-created listeners bypass that, so re-check the origin here.
            # Skip this listener only — other listeners for the event still run.
            origin_profile_id = listener.get("processing_profile_id")
            if (
                origin_profile_id is not None
                and self.profile_wake_llm_flags is not None
                and self.profile_wake_llm_flags.get(origin_profile_id) is not True
            ):
                logger.error(
                    "Refusing wake_llm for event listener %s: its origin profile "
                    "'%s' is not permitted to wake the LLM (allow_wake_llm is "
                    "disabled or the profile is no longer configured).",
                    listener["id"],
                    origin_profile_id,
                )
                return
            action_profile_id: str | None = (
                origin_profile_id
                if payload_trusted and origin_profile_id is not None
                else EVENT_HANDLER_PROFILE_ID
            )
        else:
            action_profile_id = listener.get("processing_profile_id")

        await execute_action(
            db_ctx=db_ctx,  # type: ignore[arg-type] # DatabaseTransaction implements the same interface as Database
            action_type=action_type,
            action_config=cast("dict[str, Any]", action_config),
            conversation_id=listener["conversation_id"],
            interface_type=listener.get("interface_type", "telegram"),
            context=context,
            processing_profile_id=action_profile_id,
            created_by_user_id=listener.get("created_by_user_id"),
            tool_call_review_trigger_type="event_listener",
            tool_call_review_trigger_definition=(
                str(action_config.get("context"))
                if isinstance(action_config.get("context"), str)
                else listener.get("description")
            ),
            tool_call_review_trigger_payload_present="event_data" in context,
            tool_call_review_trigger_payload_trusted=payload_trusted,
        )

        logger.info(
            f"Executed {action_type.value} action for listener {listener['id']}"
        )

    def _payload_is_trusted(self, listener: EventListenerDict) -> bool:
        """Whether the listener's events come from a trusted_events MCP server."""
        if str(listener["source_id"]) != EventSourceType.mcp.value:
            return False
        server_id = (listener["match_conditions"] or {}).get(SERVER_KEY)
        return server_id in self.trusted_event_server_ids

    async def _disable_listener(self, listener_id: int) -> None:
        """Disable a one-time listener after it triggers (opens new DB context)."""
        if self.get_db_context_func:
            db_ctx = self.get_db_context_func()
            await self._disable_listener_in_context(db_ctx, listener_id)
        else:
            raise RuntimeError(
                "EventProcessor requires get_db_context_func to be provided"
            )

    async def _disable_listener_in_context(
        self, db_ctx: Database | DatabaseTransaction, listener_id: int
    ) -> None:
        """Disable a one-time listener after it triggers within existing DB context."""
        await db_ctx.execute(
            text("UPDATE event_listeners SET enabled = FALSE WHERE id = :id"),
            {"id": listener_id},
        )
        logger.info(f"Disabled one-time listener {listener_id}")

    async def get_health_status(self) -> EventProcessorHealthStatus:
        """Get health status of all event sources."""
        sources: dict[str, SourceHealthInfoDict] = {}

        for source_id, source in self.sources.items():
            if hasattr(source, "_connection_healthy"):
                sources[source_id] = SourceHealthInfoDict(
                    healthy=getattr(source, "_connection_healthy", None),
                    reconnect_attempts=getattr(source, "_reconnect_attempts", 0),
                    last_event_time=getattr(source, "_last_event_time", 0),
                )
            else:
                sources[source_id] = SourceHealthInfoDict(status="unknown")

        return EventProcessorHealthStatus(
            processor_running=self._running,
            sources=sources,
            listener_cache=ListenerCacheInfoDict(
                last_refresh=self._last_cache_refresh,
                listener_count=sum(len(v) for v in self._listener_cache.values()),
                by_source={k: len(v) for k, v in self._listener_cache.items()},
            ),
        )
