import json
import logging
from typing import TYPE_CHECKING, Any

# Removed aiofiles and base64 as file handling is delegated to LLMClient
from family_assistant.indexing.pipeline import ContentProcessor, IndexableContent
from family_assistant.llm import LLMInterface

if TYPE_CHECKING:
    from family_assistant.storage.vector import Document
    from family_assistant.tools.types import ToolExecutionContext


logger = logging.getLogger(__name__)


class LLMIntelligenceProcessor(ContentProcessor):
    """
    A content processor that uses an LLM to extract structured information
    (e.g., summary, categories, specific fields) from content.
    """

    def __init__(
        self,
        llm_client: LLMInterface,
        system_prompt_template: str,
        output_schema: dict[str, Any],  # JSON schema for the function call
        target_embedding_type: str,  # The embedding_type for the output IndexableContent
        input_content_types: list[
            str
        ],  # List of embedding_types this processor should act upon
        tool_name: str = "extract_information",  # Name for the tool the LLM will call
        max_content_length: int | None = None,  # Applies to purely textual content
    ) -> None:
        self.llm_client = llm_client
        self.system_prompt_template = system_prompt_template
        self.output_schema = output_schema
        self.target_embedding_type = target_embedding_type
        self.input_content_types = set(
            input_content_types
        )  # Use a set for faster lookups
        self.tool_name = tool_name
        self.max_content_length = max_content_length

        if not self.input_content_types:
            raise ValueError("input_content_types cannot be empty.")
        if not self.target_embedding_type:
            raise ValueError("target_embedding_type cannot be empty.")
        if not self.system_prompt_template:
            raise ValueError("system_prompt_template cannot be empty.")
        if not self.output_schema:
            raise ValueError("output_schema cannot be empty.")

    @property
    def name(self) -> str:
        return f"LLMIntelligenceProcessor_{self.target_embedding_type}"

    # _prepare_llm_input_content method is removed as its logic is now in LLMClient

    async def process(
        self,
        current_items: list[IndexableContent],
        original_document: "Document",  # noqa: ARG002
        initial_content_ref: IndexableContent,  # noqa: ARG002
        context: "ToolExecutionContext",
    ) -> list[IndexableContent]:
        processed_items: list[IndexableContent] = []
        newly_created_items: list[IndexableContent] = []

        for item in current_items:
            if item.embedding_type not in self.input_content_types:
                processed_items.append(item)
                continue

            logger.info(
                f"Processor '{self.name}': Processing item with embedding_type '{item.embedding_type}' from '{item.source_processor}'."
            )

            # Extract necessary details from IndexableContent for the LLM client
            prompt_text: str | None = item.content
            file_path: str | None = item.ref
            mime_type: str | None = item.mime_type

            if not prompt_text and not file_path:
                logger.warning(
                    f"Processor '{self.name}': Skipping item {item.embedding_type} as it has no text content and no file reference."
                )
                processed_items.append(item)
                continue

            if file_path and not mime_type:
                logger.warning(
                    f"Processor '{self.name}': Skipping item {item.embedding_type} with file_path '{file_path}' due to missing mime_type."
                )
                processed_items.append(item)
                continue

            system_prompt = self.system_prompt_template
            # Future: system_prompt = self.system_prompt_template.format(title=original_document.title, ...)

            tools = [
                {
                    "type": "function",
                    "function": {
                        "name": self.tool_name,
                        "description": (
                            f"Extracts information according to the schema. Schema description: {self.output_schema.get('description', 'User-defined schema')}"
                        ),
                        "parameters": self.output_schema,
                    },
                }
            ]
            tool_choice_for_llm = {
                "type": "function",
                "function": {"name": self.tool_name},
            }

            try:
                logger.debug(
                    f"Processor '{self.name}': Formatting user message for item type '{item.embedding_type}'. "
                    f"Prompt text provided: {bool(prompt_text)}. File path provided: {file_path} ({mime_type})."
                )
                user_message = await self.llm_client.format_user_message_with_file(
                    prompt_text=prompt_text,
                    file_path=file_path,
                    mime_type=mime_type,
                    max_text_length=self.max_content_length,
                )

                messages = [
                    {"role": "system", "content": system_prompt},
                    user_message,  # Add the formatted user message
                ]

                logger.debug(
                    f"Processor '{self.name}': Sending request to LLM. System prompt: '{system_prompt[:100]}...'. User message: {json.dumps(user_message, default=str)[:200]}..."
                )
                llm_response = await self.llm_client.generate_response(
                    messages=messages,
                    tools=tools,
                    tool_choice=tool_choice_for_llm,
                )

                if llm_response.tool_calls:
                    for tool_call in llm_response.tool_calls:
                        if tool_call.get("function", {}).get("name") == self.tool_name:
                            try:
                                arguments_str = tool_call.get("function", {}).get(
                                    "arguments", "{}"
                                )
                                extracted_data = json.loads(arguments_str)

                                new_item_content = json.dumps(extracted_data, indent=2)
                                new_item = IndexableContent(
                                    content=new_item_content,
                                    embedding_type=self.target_embedding_type,
                                    mime_type="application/json",
                                    source_processor=self.name,
                                    metadata={
                                        "original_item_embedding_type": (
                                            item.embedding_type
                                        ),
                                        "original_item_source_processor": (
                                            item.source_processor
                                        ),
                                        "llm_model_used": getattr(
                                            self.llm_client, "model", "unknown"
                                        ),
                                    },
                                )
                                newly_created_items.append(new_item)
                                logger.info(
                                    f"Processor '{self.name}': Successfully extracted information, created new item type '{self.target_embedding_type}'."
                                )
                            except json.JSONDecodeError as e:
                                logger.error(
                                    f"Processor '{self.name}': Failed to parse LLM tool call arguments: {arguments_str}. Error: {e}"
                                )
                            except Exception as e:
                                logger.error(
                                    f"Processor '{self.name}': Error processing LLM tool call: {e}",
                                    exc_info=True,
                                )
                        else:
                            logger.warning(
                                f"Processor '{self.name}': LLM called unexpected tool: {tool_call.get('function', {}).get('name')}"
                            )
                elif llm_response.content:
                    logger.warning(
                        f"Processor '{self.name}': LLM did not use the tool, returned text content: {llm_response.content[:200]}..."
                    )
                else:
                    logger.warning(
                        f"Processor '{self.name}': LLM response had no tool calls and no content."
                    )

            except Exception as e:
                logger.error(
                    f"Processor '{self.name}': Error during LLM call or processing response: {e}",
                    exc_info=True,
                )
                processed_items.append(item)

        return newly_created_items + processed_items
