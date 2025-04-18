import logging

from litellm import acompletion

logger = logging.getLogger(__name__)

async def get_llm_response(user_message: str, model: str) -> str | None:
    """
    Sends the user message to the specified LLM model via LiteLLM/OpenRouter
    and returns the response content.
    """
    messages = [{"role": "user", "content": user_message}]
    logger.info(f"Sending message to LLM ({model}): {user_message}")

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
