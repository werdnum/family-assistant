import logging
from datetime import timedelta

from opentelemetry import trace

from family_assistant.context_providers import ContextProvider, TaintedContextProvider
from family_assistant.llm.messages import (
    AssistantMessage,
    ErrorMessage,
    LLMMessage,
    ToolMessage,
    UserMessage,
)
from family_assistant.processing.message_time import with_sent_at
from family_assistant.processing.types import ContextPreparerConfig
from family_assistant.security.taint import TaintSource
from family_assistant.utils.clock import Clock

logger = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)


class ContextPreparer:
    """Prepares context for LLM interactions, including history formatting and aggregation."""

    def __init__(
        self,
        context_providers: list[ContextProvider],
        config: ContextPreparerConfig,
        clock: Clock,
    ) -> None:
        """
        Initialize the ContextPreparer.

        Args:
            context_providers: List of context providers to aggregate context from.
            config: Context-preparation configuration.
            clock: Clock instance for time operations.
        """
        self.context_providers = context_providers
        self.config = config
        self.clock = clock

    def get_history_limits(self, interface_type: str) -> tuple[int, timedelta]:
        """Get history limits based on interface type.

        Args:
            interface_type: The type of interface (e.g., "web", "telegram", "api")

        Returns:
            Tuple of (max_messages, max_age_timedelta)
        """
        if interface_type == "web":
            # Use web-specific setting if available, otherwise fall back to default
            web_max_messages = (
                self.config.web_max_history_messages
                if self.config.web_max_history_messages is not None
                else self.config.max_history_messages
            )
            web_max_age = (
                self.config.web_history_max_age_hours
                if self.config.web_history_max_age_hours is not None
                else self.config.history_max_age_hours
            )
            return web_max_messages, timedelta(hours=web_max_age)
        else:
            return self.config.max_history_messages, timedelta(
                hours=self.config.history_max_age_hours
            )

    def prepend_profile_preamble(self, system_prompt: str) -> str:
        """Prepend a profile-identification header to *system_prompt*.

        The header states which profile is active and nothing else. What the
        profile is *for* belongs in its own ``system_prompt``, which is written
        in the voice of the model that will execute it; ``config.description``
        is the caller-facing catalog entry (the delegation catalog, the slash
        command menu, the A2A agent card) and is addressed to whoever is
        choosing a profile, so it reads as contradictory instructions to the
        profile itself -- ``coder``'s tells the agent to consider
        ``spawn_worker``, a tool it does not hold.

        Nothing is asserted here about *how* the profile was reached: a
        delegated run and an explicit slash command arrive identically, so a
        claim that the user selected it would be false for the common path.
        If *system_prompt* is empty the header is returned on its own.
        """
        preamble = f"[Active Processing Profile: {self.config.id}]"
        if system_prompt:
            return preamble + "\n\n" + system_prompt
        return preamble

    async def aggregate_context(self, acting_user_id: str | None) -> str:
        """Gathers context fragments from all registered providers.

        ``acting_user_id`` is the user the turn acts for, so providers holding
        per-user data (a connected Google calendar) show that user's own; None
        for a turn with no acting user.
        """
        with tracer.start_as_current_span(
            "context.aggregate",
            attributes={
                "context.provider_count": len(self.context_providers),
            },
        ) as span:
            all_fragments: list[str] = []
            for provider in self.context_providers:
                try:
                    fragments_output = await provider.get_context_fragments(
                        acting_user_id
                    )
                except Exception as exc:
                    raise RuntimeError(
                        f"Context provider '{provider.name}' failed to provide fragments"
                    ) from exc

                all_fragments.extend(fragments_output)
            span.set_attribute("context.fragments_count", len(all_fragments))
            # Join all non-empty fragments (i.e., filter out empty strings from individual providers' lists)
            # separated by double newlines for clarity.
            return "\n\n".join(filter(None, all_fragments)).strip()

    async def aggregate_context_taint_sources(self) -> tuple[TaintSource, ...]:
        """Gather taint sources introduced by context providers."""
        sources: list[TaintSource] = []
        for provider in self.context_providers:
            if not isinstance(provider, TaintedContextProvider):
                continue
            try:
                sources.extend(await provider.get_context_taint_sources())
            except Exception as exc:
                raise RuntimeError(
                    f"Context provider '{provider.name}' failed to provide taint sources"
                ) from exc
        return tuple(sources)

    async def format_history(
        self, history_messages: list[LLMMessage]
    ) -> list[LLMMessage]:
        """
        Formats message history retrieved from the database, handling assistant tool calls correctly.

        Args:
            history_messages: List of typed LLMMessage objects from db_context.message_history.get_recent.

        Returns:
            A list of LLMMessage objects formatted for the LLM API.
        """
        messages: list[LLMMessage] = []
        # Process history messages, formatting assistant tool calls correctly
        for msg in history_messages:
            if isinstance(msg, AssistantMessage):
                # Replayed exactly as it was sent, text and tool calls together.
                # Dropping the text on later turns changed a message the model
                # had already seen, which ends the prompt-cache hit there and
                # invalidates every later Anthropic thinking block.
                messages.append(
                    AssistantMessage(
                        content=msg.content,
                        tool_calls=msg.tool_calls,
                        provider_metadata=msg.provider_metadata,
                        taint_metadata=msg.taint_metadata,
                    )
                )
            elif isinstance(msg, ToolMessage):
                # --- Format tool response messages ---
                if (
                    msg.tool_call_id
                ):  # Only include if tool_call_id is present (retrieved from DB)
                    messages.append(msg)
                else:
                    # Log a warning if a tool message is found without an ID (indicates logging issue)
                    logger.warning(
                        f"Found 'tool' role message in history without a tool_call_id: {msg}"
                    )
                    # Skip adding malformed tool message to history to avoid LLM errors
            elif isinstance(msg, ErrorMessage):
                # Include error messages as assistant messages so LLM knows it responded
                error_content = f"I encountered an error: {msg.content}"
                if msg.error_traceback:
                    error_content += f"\n\nError details: {msg.error_traceback}"
                messages.append(AssistantMessage(content=error_content))
            elif isinstance(msg, UserMessage):
                messages.append(with_sent_at(msg, self.config.timezone))
            else:
                messages.append(msg)

        logger.debug(
            f"Formatted {len(history_messages)} DB history messages into {len(messages)} LLM messages."
        )
        return messages
