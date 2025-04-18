import logging

from typing import List, Dict, Any
from litellm import acompletion

logger = logging.getLogger(__name__)

async def get_llm_response(messages: List[Dict[str, Any]], model: str) -> str | None:
    """
    Sends the conversation history to the specified LLM model via LiteLLM/OpenRouter
    and returns the response content.

    Args:
        messages: A list of message dictionaries, e.g., [{"role": "user", "content": "..."}]
        model: The identifier of the LLM model to use.

    Returns:
        The response content string from the LLM, or None if an error occurs.
    """
    logger.info(f"Sending {len(messages)} messages to LLM ({model}). Last message: {messages[-1]['content'][:100]}...")

    try:
        response = await acompletion(
            model=model,
            messages=messages,
            # Add any other necessary parameters like temperature, max_tokens, etc.
            # temperature=0.7,
        )
        # Extract the response content
        if response.choices and response.choices[0].message and response.choices[0].message.content:
            response_content = response.choices[0].message.content
            logger.info(f"Received LLM response: {response_content[:100]}...") # Log snippet
            return response_content.strip()
        else:
            logger.warning(f"LLM response structure unexpected or empty: {response}")
            return None
    except Exception as e:
        logger.error(f"Error calling LiteLLM completion: {e}", exc_info=True)
        return None
