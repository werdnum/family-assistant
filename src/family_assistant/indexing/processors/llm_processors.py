import json
import logging
from typing import TYPE_CHECKING, Any

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
        max_content_length: int | None = None, # Applies to purely textual content
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

    async def _prepare_llm_input_content(
        self, item: IndexableContent, context: "ToolExecutionContext"  # noqa: ARG002
    ) -> str | list[dict[str, Any]]:
        """
        Prepares the content from an IndexableContent item for the LLM.
        Handles text directly. File references and image handling are future work.
        """
        content_to_process: str | None = None

        if item.content:
            content_to_process = item.content
        elif item.ref:
            # TODO: Implement robust async file reading for item.ref based on mime_type.
            # This could involve calling other utility functions or services.
            # For images, this might involve base64 encoding or providing a URL if the LLM supports it.
            # For PDFs, it might involve prior text extraction if not already done.
            logger.warning(
                f"Processor '{self.name}': Item has a 'ref' ({item.ref}) for mime_type '{item.mime_type}'. "
                "Content extraction from 'ref' is a placeholder. Assuming text content for now if direct content is missing."
            )
            # Placeholder: if it's a ref, and we expect text, this part needs to read the file.
            # For now, we'll rely on item.content primarily.
            # If item.content is None and item.ref exists, this indicates a missing step or
            # that this processor is not yet equipped to handle this item.ref type.
            if item.mime_type and "text" in item.mime_type:
                logger.error(
                    f"Processor '{self.name}': Text file reading from ref '{item.ref}' not implemented yet. Content will be empty."
                )
            else:
                logger.warning(
                    f"Processor '{self.name}': Cannot process non-text ref '{item.ref}' with mime_type '{item.mime_type}' yet."
                )
            return ""  # Return empty or raise error if content cannot be prepared

        if not content_to_process:
            logger.warning(
                f"Processor '{self.name}': No textual content found for item: {item.embedding_type} from {item.source_processor}"
            )
            return ""

        if (
            self.max_content_length
            and len(content_to_process) > self.max_content_length
        ):
            logger.info(
                f"Processor '{self.name}': Content for {item.embedding_type} is too long ({len(content_to_process)} chars), "
                f"truncating to {self.max_content_length} chars."
            )
            content_to_process = content_to_process[: self.max_content_length]

        # For now, assuming text content. Multimodal would return a list of dicts.
        # e.g., [{"type": "text", "text": "Describe this"}, {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,..."}}]
        return content_to_process

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

            llm_input_content = await self._prepare_llm_input_content(item, context)

            if not llm_input_content:
                logger.warning(
                    f"Processor '{self.name}': Skipping item due to empty prepared content: {item.embedding_type}"
                )
                processed_items.append(item)
                continue

            system_prompt = self.system_prompt_template
            # If system_prompt_template needs formatting with item-specific data:
            # system_prompt = self.system_prompt_template.format(title=original_document.title, ...)

            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": llm_input_content},
            ]

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

            try:
                logger.debug(
                    f"Processor '{self.name}': Sending request to LLM. System prompt: '{system_prompt[:100]}...', User content length: {len(str(llm_input_content))}"
                )
                llm_response = await self.llm_client.generate_response(
                    messages=messages,
                    tools=tools,
                    tool_choice={
                        "type": "function",
                        "function": {"name": self.tool_name},
                    },
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
