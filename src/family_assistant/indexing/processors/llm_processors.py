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
        initial_content_ref: IndexableContent | None,  # noqa: ARG002
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
            # tool_choice_for_llm was assigned but not used.
            # tool_choice="required" is used in generate_response call directly.

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
                    tool_choice="required",  # Ensure LLM calls the specified tool
                )

                if llm_response.tool_calls:
                    for tool_call in llm_response.tool_calls:
                        if tool_call.get("function", {}).get("name") == self.tool_name:
                            arguments_str = (
                                "{}"  # Initialize for safety in error logging
                            )
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
                    f"Processor '{self.name}': Error during LLM call or processing response for item type '{item.embedding_type}': {e}",
                    exc_info=True,
                )
                # Original item is already in output_items if we adopt the new strategy below.
                # If not, and an error occurs, the original item might be lost if not re-added.
                # For now, assuming errors mean no new item is generated, original passes.

        # Ensure all original items are passed through, plus any newly created items.
        # The original `current_items` should be the base for `output_items` if they are always passed.
        # Let's adjust the logic to explicitly build the output list.
        final_output_items = list(current_items)  # Start with all original items
        final_output_items.extend(newly_created_items)  # Add any new items generated

        return final_output_items


# --- Default Summary Generation Configuration ---
DEFAULT_SUMMARY_SYSTEM_PROMPT_TEMPLATE = """You are an expert at summarizing documents.
Your task is to provide a concise one or two sentence summary of the document content presented to you.
The summary should capture the main essence of the document.
Examples of good summaries:
- "A receipt for in flight wifi from a united airlines flight 870 from Sydney to San Francisco on 12 May 2025"
- "a pharmacy receipt from Sampletown pharmacy on 8 November 2024 for Espomeprazole 20mg"
- "a confirmation from National Australia Bank (NAB) that the interest rate on a mortgage of 25 Example Ave Sampletown has changed. It's dated 15 January 2024"

Please use the 'extract_summary' tool to provide your summary based on the document content.
"""

DEFAULT_SUMMARY_OUTPUT_SCHEMA = {
    "type": "object",
    "description": "Schema for extracting a concise document summary.",
    "properties": {
        "summary": {
            "type": "string",
            "description": (
                "A concise one or two sentence summary of the document's content."
            ),
        }
    },
    "required": ["summary"],
}


class LLMSummaryGeneratorProcessor(LLMIntelligenceProcessor):
    """
    A specialized LLM processor that generates a concise summary for input content.
    It uses a predefined system prompt and output schema tailored for summarization.
    """

    def __init__(
        self,
        llm_client: LLMInterface,
        input_content_types: list[str],
        target_embedding_type: str = "document_summary",
        max_content_length: int | None = None,
        # Allow overriding defaults for advanced use cases, but provide strong defaults
        system_prompt_template: str = DEFAULT_SUMMARY_SYSTEM_PROMPT_TEMPLATE,
        output_schema: dict[str, Any] = DEFAULT_SUMMARY_OUTPUT_SCHEMA,
        tool_name: str = "extract_summary",
    ) -> None:
        """
        Initializes the LLMSummaryGeneratorProcessor.

        Args:
            llm_client: The LLM client instance.
            input_content_types: List of embedding_types this processor should act upon
                                 (e.g., ["original_document_file", "raw_body_text"]).
            target_embedding_type: The embedding_type for the output summary IndexableContent.
                                   Defaults to "document_summary".
            max_content_length: Maximum length for purely textual content to be processed.
            system_prompt_template: The system prompt template for the LLM.
            output_schema: The JSON schema for the LLM function call.
            tool_name: The name for the tool the LLM will call.
        """
        super().__init__(
            llm_client=llm_client,
            system_prompt_template=system_prompt_template,
            output_schema=output_schema,
            target_embedding_type=target_embedding_type,
            input_content_types=input_content_types,
            tool_name=tool_name,
            max_content_length=max_content_length,
        )
        logger.info(
            f"LLMSummaryGeneratorProcessor initialized for target_embedding_type '{target_embedding_type}' on input types: {input_content_types}"
        )

    @property
    def name(self) -> str:
        # Override name to be more specific for this processor type
        return f"LLMSummaryGeneratorProcessor_{self.target_embedding_type}"


# --- Default Primary Link Extraction Configuration ---
DEFAULT_PRIMARY_LINK_SYSTEM_PROMPT_TEMPLATE = """Analyze the provided email content.
If the email's sole and primary purpose is to direct the recipient to visit a specific URL, use the 'extract_primary_link' tool to provide that URL.
If the email has other significant purposes, or if no single primary URL is identifiable as the main call to action, do not call the tool or indicate that no primary link is applicable.
The URL should be a fully qualified URL.
"""

DEFAULT_PRIMARY_LINK_OUTPUT_SCHEMA = {
    "type": "object",
    "description": (
        "Schema for extracting the primary call-to-action URL from an email if its sole purpose is to direct to that URL."
    ),
    "properties": {
        "primary_url": {
            "type": "string",
            "format": "uri",
            "description": (
                "The single, primary URL the email is directing the user to visit. Null if not applicable."
            ),
        },
        "is_primary_link_email": {
            "type": "boolean",
            "description": (
                "True if the email's sole primary purpose is to share this link, false otherwise."
            ),
        },
    },
    "required": [
        "is_primary_link_email"
    ],  # primary_url is not required if is_primary_link_email is false
}

DEFAULT_PRIMARY_LINK_TOOL_NAME = "extract_primary_link"


class LLMPrimaryLinkExtractorProcessor(LLMIntelligenceProcessor):
    """
    A specialized LLM processor that extracts a primary call-to-action URL
    from content if the content's main purpose is to direct to that URL.
    The extracted URL is intended to be fetched by a subsequent processor like WebFetcherProcessor.
    """

    def __init__(
        self,
        llm_client: LLMInterface,
        input_content_types: list[str],
        target_embedding_type: str = "raw_url",  # Output type for WebFetcherProcessor
        max_content_length: int | None = None,
        system_prompt_template: str = DEFAULT_PRIMARY_LINK_SYSTEM_PROMPT_TEMPLATE,
        output_schema: dict[str, Any] = DEFAULT_PRIMARY_LINK_OUTPUT_SCHEMA,
        tool_name: str = DEFAULT_PRIMARY_LINK_TOOL_NAME,
    ) -> None:
        """
        Initializes the LLMPrimaryLinkExtractorProcessor.

        Args:
            llm_client: The LLM client instance.
            input_content_types: List of embedding_types this processor should act upon
                                 (e.g., ["raw_body_text"]).
            target_embedding_type: The embedding_type for the output IndexableContent
                                   containing the URL. Defaults to "raw_url".
            max_content_length: Maximum length for purely textual content to be processed.
            system_prompt_template: The system prompt template for the LLM.
            output_schema: The JSON schema for the LLM function call.
            tool_name: The name for the tool the LLM will call.
        """
        super().__init__(
            llm_client=llm_client,
            system_prompt_template=system_prompt_template,
            output_schema=output_schema,
            target_embedding_type=target_embedding_type,
            input_content_types=input_content_types,
            tool_name=tool_name,
            max_content_length=max_content_length,
        )
        logger.info(
            f"LLMPrimaryLinkExtractorProcessor initialized for target_embedding_type '{target_embedding_type}' on input types: {input_content_types}"
        )

    @property
    def name(self) -> str:
        # Override name to be more specific for this processor type
        return f"LLMPrimaryLinkExtractorProcessor_{self.target_embedding_type}"

    async def process(
        self,
        current_items: list[IndexableContent],
        original_document: "Document",
        initial_content_ref: IndexableContent | None,
        context: "ToolExecutionContext",
    ) -> list[IndexableContent]:
        processed_items: list[IndexableContent] = []
        newly_created_items: list[IndexableContent] = []

        for item in current_items:
            if item.embedding_type not in self.input_content_types:
                processed_items.append(item)
                continue

            logger.info(
                f"Processor '{self.name}': Processing item with embedding_type '{item.embedding_type}' from '{item.source_processor}' for primary link extraction."
            )

            prompt_text: str | None = item.content
            file_path: str | None = item.ref
            mime_type: str | None = item.mime_type

            if not prompt_text and not file_path:
                logger.warning(
                    f"Processor '{self.name}': Skipping item {item.embedding_type} for link extraction as it has no text content and no file reference."
                )
                processed_items.append(item)
                continue

            if (
                file_path and not mime_type
            ):  # Should not happen if mime_type is always set for files
                logger.warning(
                    f"Processor '{self.name}': Skipping item {item.embedding_type} with file_path '{file_path}' for link extraction due to missing mime_type."
                )
                processed_items.append(item)
                continue

            system_prompt = self.system_prompt_template
            tools = [
                {
                    "type": "function",
                    "function": {
                        "name": self.tool_name,
                        "description": self.output_schema.get(
                            "description", "Extract primary link."
                        ),
                        "parameters": self.output_schema,
                    },
                }
            ]
            # tool_choice_for_llm was assigned but not used.
            # tool_choice="required" is used in generate_response call directly.

            try:
                user_message = await self.llm_client.format_user_message_with_file(
                    prompt_text=prompt_text,
                    file_path=file_path,
                    mime_type=mime_type,
                    max_text_length=self.max_content_length,
                )
                messages = [{"role": "system", "content": system_prompt}, user_message]

                llm_response = await self.llm_client.generate_response(
                    messages=messages, tools=tools, tool_choice="required"
                )

                if llm_response.tool_calls:
                    for tool_call in llm_response.tool_calls:
                        if tool_call.get("function", {}).get("name") == self.tool_name:
                            arguments_str = (
                                "{}"  # Initialize for safety in error logging
                            )
                            try:
                                arguments_str = tool_call.get("function", {}).get(
                                    "arguments", "{}"
                                )
                                extracted_data = json.loads(arguments_str)

                                primary_url = extracted_data.get("primary_url")
                                is_primary = extracted_data.get(
                                    "is_primary_link_email", False
                                )

                                if (
                                    is_primary
                                    and primary_url
                                    and isinstance(primary_url, str)
                                ):
                                    new_url_item = IndexableContent(
                                        content=primary_url,  # The URL string is the content
                                        embedding_type=self.target_embedding_type,  # e.g., "raw_url"
                                        mime_type="text/uri-list",  # Standard MIME type for a list of URIs (here, one URI)
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
                                            "original_document_source_id": (
                                                original_document.source_id
                                            ),
                                        },
                                    )
                                    newly_created_items.append(new_url_item)
                                    logger.info(
                                        f"Processor '{self.name}': Extracted primary URL '{primary_url}' from item type '{item.embedding_type}'. Created new item with type '{self.target_embedding_type}'."
                                    )
                                elif is_primary and not primary_url:
                                    logger.info(
                                        f"Processor '{self.name}': LLM indicated it's a primary link email but did not provide a URL for item type '{item.embedding_type}'."
                                    )
                                else:  # Not a primary link email, or no URL provided
                                    logger.info(
                                        f"Processor '{self.name}': LLM determined item type '{item.embedding_type}' is not a primary link email or no URL found."
                                    )
                            except json.JSONDecodeError as e:
                                logger.error(
                                    f"Processor '{self.name}': Failed to parse LLM tool call arguments for link extraction: {arguments_str}. Error: {e}"
                                )
                            except (
                                Exception
                            ) as e:  # Catch other errors during argument processing
                                logger.error(
                                    f"Processor '{self.name}': Error processing LLM tool call arguments for link extraction: {e}",
                                    exc_info=True,
                                )
                        else:  # LLM called an unexpected tool
                            logger.warning(
                                f"Processor '{self.name}': LLM called unexpected tool '{tool_call.get('function', {}).get('name')}' during link extraction."
                            )
                elif llm_response.content:
                    logger.warning(
                        f"Processor '{self.name}': LLM did not use tool for link extraction, returned text content: {llm_response.content[:200]}..."
                    )
                else:  # No tool calls and no content
                    logger.warning(
                        f"Processor '{self.name}': LLM response for link extraction had no tool calls and no content for item type '{item.embedding_type}'."
                    )
            except Exception as e:
                logger.error(
                    f"Processor '{self.name}': Error during LLM call or response processing for link extraction from item type '{item.embedding_type}': {e}",
                    exc_info=True,
                )

            processed_items.append(item)  # Always pass through the original item

        final_output_items = processed_items
        final_output_items.extend(newly_created_items)

        return final_output_items
