"""
Event processor that routes events to storage and listeners.
"""

import asyncio
import json
import logging
import time
from collections.abc import Callable, Mapping
from datetime import tzinfo
from typing import Any, TypedDict, cast

from sqlalchemy import select, text

from family_assistant.actions import ActionType, execute_action
from family_assistant.events.condition_evaluator import EventConditionEvaluator
from family_assistant.events.sources import EventSource
from family_assistant.events.storage import EventStorage
from family_assistant.scripting import ScriptExecutionError
from family_assistant.storage.database import (
    Database,
    DatabaseTransaction,
)
from family_assistant.storage.events import (
    check_and_update_rate_limit,
    event_listeners_table,
)
from family_assistant.storage.types import (
    EventConditionEvaluatorConfig,
    EventListenerDict,
    MatchConditions,
)

logger = logging.getLogger(__name__)

# Restricted profile that event-triggered wake_llm turns run under. Event content
# is untrusted (attacker-influenced email/webhook data), and the woken LLM is the
# injectable component, so it must not run under a full-trust profile.
EVENT_HANDLER_PROFILE_ID = "event_handler"


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
        """
        self.sources = sources
        self.profile_wake_llm_flags = profile_wake_llm_flags
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

    # ast-grep-ignore: no-dict-any - event_data is arbitrary JSON from external sources (Home Assistant, webhooks) with no fixed schema
    async def process_event(self, source_id: str, event_data: dict[str, Any]) -> None:
        """Process an event from a source."""
        if not self._running:
            return

        # Use lock to prevent concurrent cache refreshes
        async with self._process_lock:
            # Refresh cache if needed
            if time.time() - self._last_cache_refresh > self._cache_refresh_interval:
                await self._refresh_listener_cache()

            # Get all active listeners for this source
            listeners = self._listener_cache.get(source_id, [])

        # Get database handle (for tests or via factory function)
        if self._db_context:
            db_ctx = self._db_context
        elif self.get_db_context_func:
            db_ctx = self.get_db_context_func()
        else:
            raise RuntimeError(
                "EventProcessor requires get_db_context_func to be provided"
            )

        triggered_listener_ids = []

        # Check each listener and process matches. Match conditions may run
        # a condition script, so this must happen outside any transaction.
        for listener in listeners:
            if await self._check_match_conditions(
                event_data,
                listener["match_conditions"],
                listener.get("condition_script"),
            ):
                # Per-listener atomicity: rate-limit update + action enqueue +
                # one-time-listener disable must be one unit, or a failed disable
                # re-fires a one-time action and a failed enqueue burns quota.
                # The closure runs before the next iteration rebinds `listener`.
                async def _process_listener(
                    txn: DatabaseTransaction,
                    listener: EventListenerDict = listener,
                ) -> bool:
                    allowed, reason = await check_and_update_rate_limit(
                        txn, listener["id"], listener["conversation_id"]
                    )
                    if not allowed:
                        logger.warning(
                            f"Listener {listener['id']} rate limited: {reason}"
                        )
                        return False

                    await self._execute_action_in_context(txn, listener, event_data)

                    # Handle one-time listeners
                    if listener.get("one_time"):
                        await self._disable_listener_in_context(txn, listener["id"])
                    return True

                try:
                    # Recorded outside the closure: atomic() replays its body on
                    # a retryable failure, and a rolled-back listener must not
                    # appear as triggered.
                    if await db_ctx.atomic(_process_listener):
                        triggered_listener_ids.append(listener["id"])
                except Exception as e:
                    logger.exception(f"Error processing listener {listener['id']}: {e}")

        # Store event for debugging/testing in its own operation
        await self.event_storage.store_event_in_context(
            db_ctx, source_id, event_data, triggered_listener_ids
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
        # rather than the listener creator's (often full-trust) profile. Script
        # actions keep running under the creating profile so their validated tool
        # set matches execution.
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
            action_profile_id: str | None = EVENT_HANDLER_PROFILE_ID
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
        )

        logger.info(
            f"Executed {action_type.value} action for listener {listener['id']}"
        )

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
