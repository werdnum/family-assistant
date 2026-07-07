import logging
from datetime import timedelta

from opentelemetry import trace

from family_assistant.context_providers import ContextProvider, TaintedContextProvider
from family_assistant.llm.messages import (
    AssistantMessage,
    ErrorMessage,
    LLMMessage,
    ToolMessage,
)
from family_assistant.processing.types import ContextPreparerConfig
from family_assistant.security.taint import TaintSource
from family_assistant.utils.clock import Clock

from .utils import assistant_message_has_thought_signature

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
        """Prepend a profile-identification preamble to *system_prompt*.

        The preamble tells the model which processing profile is active and
        that the user explicitly selected it.  If *system_prompt* is empty the
        preamble is returned without a trailing newline.
        """
        profile_id = self.config.id
        description = self.config.description
        lines = [
            f"[Active Processing Profile: {profile_id}]",
            f'The user has explicitly selected the "{profile_id}" processing profile.',
        ]
        if description:
            lines.append(f"Profile purpose: {description}")
        lines.append(
            "Your available tools and capabilities are specific to this profile. "
            "Do not attempt actions outside your profile's scope."
        )
        preamble = "\n".join(lines)
        if system_prompt:
            return preamble + "\n\n" + system_prompt
        return preamble

    async def aggregate_context(self) -> str:
        """Gathers context fragments from all registered providers."""
        with tracer.start_as_current_span(
            "context.aggregate",
            attributes={
                "context.provider_count": len(self.context_providers),
            },
        ) as span:
            all_fragments: list[str] = []
            for provider in self.context_providers:
                try:
                    fragments_output = await provider.get_context_fragments()
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
                has_thought_signature = assistant_message_has_thought_signature(msg)

                # Strip text content from messages with tool calls UNLESS they have thought signatures
                # Thought signatures are cryptographically tied to exact conversation context
                # So if a thought signature is present, we MUST preserve the original text content exactly.
                final_content: str | None = msg.content

                if msg.tool_calls and msg.content and not has_thought_signature:
                    # Only strip text if NO thought signature is present, to avoid redundancy/partial response issues
                    # with other providers. But for Google with signatures, we keep it.
                    final_content = None
                    logger.debug(
                        f"Stripped text content from assistant message with tool calls (no signature). Original content: {msg.content[:100]}..."
                    )

                # Create new AssistantMessage with potentially modified content
                assistant_msg = AssistantMessage(
                    content=final_content,
                    tool_calls=msg.tool_calls,
                    provider_metadata=msg.provider_metadata,
                )
                messages.append(assistant_msg)
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
            else:
                # SystemMessage, UserMessage, or other message types - pass through as-is
                messages.append(msg)

        logger.debug(
            f"Formatted {len(history_messages)} DB history messages into {len(messages)} LLM messages."
        )
        return messages
