"""
LLM client that provides retry and fallback capabilities.

This implementation mimics LiteLLM's simple retry strategy:
- Attempt 1: Primary model
- Attempt 2: Retry primary model (if Attempt 1 was a retriable error)
- Attempt 3: Fallback model (if configured and previous attempts failed)
"""

import asyncio
import logging
from collections.abc import AsyncIterator, Sequence
from typing import TYPE_CHECKING, Any, TypeVar

from pydantic import BaseModel

if TYPE_CHECKING:
    from family_assistant.tools.types import ToolAttachment, ToolDefinition

from . import LLMInterface, LLMMessage, LLMOutput, LLMStreamEvent, StructuredOutputError
from .base import (
    LLMProviderError,
    ProviderConnectionError,
    ProviderTimeoutError,
    RateLimitError,
    ServiceUnavailableError,
)
from .messages import UserMessage

T = TypeVar("T", bound=BaseModel)

logger = logging.getLogger(__name__)


class RetryingLLMClient:
    """
    LLM client that provides retry and fallback capabilities.

    This wrapper mimics LiteLLM's simple retry logic:
    - One retry on primary model for retriable errors
    - Fallback to alternative model if all primary attempts fail
    """

    def __init__(
        self,
        primary_client: "LLMInterface",
        primary_model: str,
        fallback_client: "LLMInterface | None" = None,
        fallback_model: str | None = None,
    ) -> None:
        """
        Initialize the retrying client.

        Args:
            primary_client: The primary LLM client
            primary_model: Model name for the primary client (for logging)
            fallback_client: Optional fallback LLM client
            fallback_model: Model name for the fallback client (for logging)
        """
        self.primary_client = primary_client
        self.primary_model = primary_model
        self.fallback_client = fallback_client
        self.fallback_model = fallback_model or "openai/gpt-5.2"  # Default fallback

        logger.info(
            f"RetryingLLMClient initialized with primary model: {primary_model}, "
            f"fallback model: {fallback_model if fallback_client else 'None'}"
        )

    async def generate_response(
        self,
        messages: Sequence[LLMMessage],
        tools: "list[ToolDefinition] | None" = None,
        tool_choice: str | None = "auto",
    ) -> "LLMOutput":
        """Generate response with retry and fallback logic."""
        # Define retriable errors - matching LiteLLM's logic
        retriable_errors = (
            ProviderConnectionError,
            ProviderTimeoutError,
            RateLimitError,
            ServiceUnavailableError,
        )

        last_exception: Exception | None = None

        # Attempt 1: Primary model
        try:
            logger.info(f"Attempt 1: Primary model ({self.primary_model})")
            return await self.primary_client.generate_response(
                messages=messages,
                tools=tools,
                tool_choice=tool_choice,
            )
        except retriable_errors as e:
            logger.warning(
                f"Attempt 1 (Primary model {self.primary_model}) failed with retriable error: {e}. "
                f"Retrying primary model."
            )
            last_exception = e
        except LLMProviderError as e:  # Non-retriable provider error
            logger.warning(
                f"Attempt 1 (Primary model {self.primary_model}) failed with provider error: {e}. "
                f"Proceeding to fallback."
            )
            last_exception = e
        except Exception as e:
            logger.error(
                f"Attempt 1 (Primary model {self.primary_model}) failed with unexpected error: {e}",
                exc_info=True,
            )
            last_exception = e

        # Attempt 2: Retry Primary model (if Attempt 1 was a retriable error)
        if isinstance(last_exception, retriable_errors):
            try:
                logger.info(f"Attempt 2: Retrying primary model ({self.primary_model})")
                return await self.primary_client.generate_response(
                    messages=messages,
                    tools=tools,
                    tool_choice=tool_choice,
                )
            except retriable_errors as e:
                logger.warning(
                    f"Attempt 2 (Retry Primary model {self.primary_model}) failed with retriable error: {e}. "
                    f"Proceeding to fallback."
                )
                last_exception = e
            except LLMProviderError as e:  # Non-retriable provider error on retry
                logger.warning(
                    f"Attempt 2 (Retry Primary model {self.primary_model}) failed with provider error: {e}. "
                    f"Proceeding to fallback."
                )
                last_exception = e
            except Exception as e:
                logger.error(
                    f"Attempt 2 (Retry Primary model {self.primary_model}) failed with unexpected error: {e}",
                    exc_info=True,
                )
                last_exception = e

        # Attempt 3: Fallback model (if configured and primary attempts failed)
        if self.fallback_client and last_exception:
            # Check if fallback model is same as primary
            if self.fallback_model == self.primary_model:
                logger.warning(
                    f"Fallback model '{self.fallback_model}' is the same as the primary model '{self.primary_model}'. "
                    f"Skipping fallback."
                )
                if last_exception:
                    raise last_exception
                # This case should ideally not happen
                raise LLMProviderError(
                    message="All attempts failed without a specific error to raise.",
                    provider="unknown",
                    model=self.primary_model,
                )

            logger.info(f"Attempt 3: Fallback model ({self.fallback_model})")
            try:
                return await self.fallback_client.generate_response(
                    messages=messages,
                    tools=tools,
                    tool_choice=tool_choice,
                )
            except Exception as e:
                logger.error(
                    f"Attempt 3 (Fallback model {self.fallback_model}) also failed: {e}",
                    exc_info=True,
                )
                # Re-raise the last exception from primary attempts as it's likely more informative
                raise last_exception from e

        # If all attempts failed, raise the last exception
        if last_exception:
            logger.error(
                f"All LLM attempts failed. Raising last recorded exception: {last_exception}"
            )
            raise last_exception
        else:
            # Should not be reached if logic is correct, but as a safeguard:
            logger.error(
                "All LLM attempts failed without a specific exception captured."
            )
            raise LLMProviderError(
                message="All LLM attempts failed without a specific exception.",
                provider="unknown",
                model=self.primary_model,
            )

    async def generate_response_stream(
        self,
        messages: Sequence[LLMMessage],
        tools: "list[ToolDefinition] | None" = None,
        tool_choice: str | None = "auto",
    ) -> AsyncIterator[LLMStreamEvent]:
        """Generate streaming response with retry and fallback logic."""
        logger.info(f"Streaming from primary model ({self.primary_model})")

        events_yielded = False
        try:
            async for event in self.primary_client.generate_response_stream(
                messages=messages,
                tools=tools,
                tool_choice=tool_choice,
            ):
                yield event
                events_yielded = True
        except Exception as e:
            logger.error(
                f"Streaming from primary model {self.primary_model} failed: {e}",
                exc_info=True,
            )

            # Only retry/fallback if no content has been sent to the client
            if not events_yielded:
                # For rate limits with a short retry_after, wait and retry on primary
                if (
                    isinstance(e, RateLimitError)
                    and e.retry_after is not None
                    and e.retry_after <= 60
                ):
                    logger.info(
                        f"Rate limited. Waiting {e.retry_after:.0f}s before retrying primary model."
                    )
                    # ast-grep-ignore: no-asyncio-sleep-in-tests - Production retry delay, not test code
                    await asyncio.sleep(e.retry_after)
                    try:
                        async for event in self.primary_client.generate_response_stream(
                            messages=messages,
                            tools=tools,
                            tool_choice=tool_choice,
                        ):
                            events_yielded = True
                            yield event
                        return
                    except Exception as retry_err:
                        logger.warning(
                            f"Retry after rate limit delay also failed: {retry_err}"
                        )
                        if events_yielded:
                            raise retry_err
                        # Fall through to fallback logic below

                if self.fallback_client:
                    # Check if fallback model is same as primary
                    if self.fallback_model == self.primary_model:
                        logger.warning(
                            f"Fallback model '{self.fallback_model}' is the same as the primary model '{self.primary_model}'. "
                            f"Skipping fallback."
                        )
                        raise

                    logger.info(
                        f"Falling back to {self.fallback_model} (no content emitted yet)"
                    )
                    try:
                        async for (
                            event
                        ) in self.fallback_client.generate_response_stream(
                            messages=messages,
                            tools=tools,
                            tool_choice=tool_choice,
                        ):
                            yield event
                    except Exception as fallback_error:
                        logger.error(
                            f"Fallback streaming model {self.fallback_model} also failed: {fallback_error}",
                            exc_info=True,
                        )
                        # Raise the original error as it's likely more relevant
                        raise e from fallback_error
                else:
                    logger.warning("Cannot fallback: No fallback client configured.")
                    raise
            else:
                logger.warning("Cannot fallback: Content already emitted to stream.")
                raise

    async def format_user_message_with_file(
        self,
        prompt_text: str | None,
        file_path: str | None,
        mime_type: str | None,
        max_text_length: int | None,
        # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
    ) -> dict[str, Any]:
        """Format user message with file - delegates to primary client."""
        return await self.primary_client.format_user_message_with_file(
            prompt_text, file_path, mime_type, max_text_length
        )

    def create_attachment_injection(
        self,
        attachment: "ToolAttachment",
    ) -> UserMessage:
        """Create attachment injection - delegates to primary client."""
        return self.primary_client.create_attachment_injection(attachment)

    async def generate_structured(
        self,
        messages: Sequence[LLMMessage],
        response_model: type[T],
    ) -> T:
        """Generate structured response with retry and fallback logic.

        Note: The underlying client handles validation retries. This method only
        handles provider-level retries (connection errors, rate limits, etc.).
        """
        retriable_errors = (
            ProviderConnectionError,
            ProviderTimeoutError,
            RateLimitError,
            ServiceUnavailableError,
        )

        last_exception: Exception | None = None

        # Attempt 1: Primary model
        try:
            logger.info(
                f"Structured output attempt 1: Primary model ({self.primary_model})"
            )
            return await self.primary_client.generate_structured(
                messages=messages,
                response_model=response_model,
            )
        except retriable_errors as e:
            logger.warning(
                f"Structured output attempt 1 (Primary model {self.primary_model}) "
                f"failed with retriable error: {e}. Retrying primary model."
            )
            last_exception = e
        except StructuredOutputError as e:
            # Structured output errors are not retriable at this level
            # The underlying client already retried validation errors
            logger.warning(
                f"Structured output attempt 1 (Primary model {self.primary_model}) "
                f"failed with structured output error: {e}. Proceeding to fallback."
            )
            last_exception = e
        except LLMProviderError as e:
            logger.warning(
                f"Structured output attempt 1 (Primary model {self.primary_model}) "
                f"failed with provider error: {e}. Proceeding to fallback."
            )
            last_exception = e
        except Exception as e:
            logger.error(
                f"Structured output attempt 1 (Primary model {self.primary_model}) "
                f"failed with unexpected error: {e}",
                exc_info=True,
            )
            last_exception = e

        # Attempt 2: Retry Primary model (if Attempt 1 was a retriable error)
        if isinstance(last_exception, retriable_errors):
            try:
                logger.info(
                    f"Structured output attempt 2: Retrying primary model ({self.primary_model})"
                )
                return await self.primary_client.generate_structured(
                    messages=messages,
                    response_model=response_model,
                )
            except retriable_errors as e:
                logger.warning(
                    f"Structured output attempt 2 (Retry Primary model {self.primary_model}) "
                    f"failed with retriable error: {e}. Proceeding to fallback."
                )
                last_exception = e
            except LLMProviderError as e:
                logger.warning(
                    f"Structured output attempt 2 (Retry Primary model {self.primary_model}) "
                    f"failed with provider error: {e}. Proceeding to fallback."
                )
                last_exception = e
            except Exception as e:
                logger.error(
                    f"Structured output attempt 2 (Retry Primary model {self.primary_model}) "
                    f"failed with unexpected error: {e}",
                    exc_info=True,
                )
                last_exception = e

        # Attempt 3: Fallback model (if configured and primary attempts failed)
        if self.fallback_client and last_exception:
            if self.fallback_model == self.primary_model:
                logger.warning(
                    f"Fallback model '{self.fallback_model}' is the same as the primary model "
                    f"'{self.primary_model}'. Skipping fallback."
                )
                raise last_exception

            logger.info(
                f"Structured output attempt 3: Fallback model ({self.fallback_model})"
            )
            try:
                return await self.fallback_client.generate_structured(
                    messages=messages,
                    response_model=response_model,
                )
            except Exception as e:
                logger.error(
                    f"Structured output attempt 3 (Fallback model {self.fallback_model}) "
                    f"also failed: {e}",
                    exc_info=True,
                )
                raise last_exception from e

        # If all attempts failed, raise the last exception
        if last_exception:
            logger.error(
                f"All structured output attempts failed. "
                f"Raising last recorded exception: {last_exception}"
            )
            raise last_exception
        else:
            raise LLMProviderError(
                message="All structured output attempts failed without a specific exception.",
                provider="unknown",
                model=self.primary_model,
            )
