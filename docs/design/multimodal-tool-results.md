# Multimodal Tool Results Design Document

## Executive Summary

This document describes the design for enabling tools to return multimodal content (images, PDFs,
documents) to LLMs in the Family Assistant system. Due to varying provider support for multimodal
tool responses, we implement a unified approach that handles provider-specific capabilities
transparently.

## Problem Statement

### Current Limitations

- Tools can only return text strings to LLMs
- Document tools extract text, losing formatting and visual information
- PDFs, images, and other files cannot be passed back to the LLM in their original form
- Different LLM providers have varying support for multimodal content in tool responses

### Desired Capabilities

- Tools should be able to return files (PDFs, images, documents) to the LLM
- The LLM should receive files in a format it can process natively
- Implementation should be transparent to tool developers
- System should gracefully handle provider limitations

## Current Architecture Analysis

### Tool System

```python
# Current tool signature
async def tool_name(exec_context: ToolExecutionContext, ...) -> str:
    return "text result"
```

Tools return strings that are passed to the LLM as:

```python
{"role": "tool", "tool_call_id": "...", "content": "text result"}
```

### LLM Provider Stack

1. **Primary**: `GoogleGenAIClient` - Native Google genai SDK implementation
2. **Secondary**: `OpenAIClient` - Native OpenAI implementation
3. **Fallback**: `LiteLLMClient` - Used for edge cases and Claude support

### Message History

- Already supports `attachments` field (JSON) for storing metadata
- Stores attachment references, not inline content
- Retrieval reconstructs messages with attachments

## Provider Capabilities Matrix

| Provider   | Library      | Tool Response Support | Workaround Required     |
| ---------- | ------------ | --------------------- | ----------------------- |
| **Gemini** | google-genai | JSON only             | Yes - Message injection |
| **OpenAI** | openai       | Text/JSON only        | Yes - Message injection |
| **Claude** | LiteLLM      | Full multimodal       | No - Native support     |

### Detailed Provider Analysis

#### Google Gemini (google-genai SDK)

- **Function responses**: JSON-serializable dictionaries only
- **No support for**: inline_data, file URIs, or binary content in responses
- **Workaround**: Inject synthetic user message after tool response

#### OpenAI

- **Tool messages**: String content only
- **No support for**: Images or files in tool responses
- **Workaround**: Inject synthetic user message after tool response

#### Claude (via LiteLLM)

- **Full support**: Tool responses can contain text and image content blocks
- **Format**: `{"type": "tool_result", "content": [{"type": "text", ...}, {"type": "image", ...}]}`
- **No workaround needed**: Use native capability when available

## Proposed Solution Architecture

### Core Design Principles

1. **Encapsulation**: Multimodal handling isolated within LLM provider implementations
2. **Backward Compatibility**: Tools can return strings or enhanced results
3. **Provider Transparency**: Tools don't need to know about provider limitations
4. **Future-Ready**: Easy to adapt when providers add native support

### 1. Enhanced Tool Result Type

```python
# src/family_assistant/tools/types.py
from dataclasses import dataclass
from typing import Optional

@dataclass
class ToolAttachment:
    """File attachment for tool results"""
    mime_type: str
    content: Optional[bytes] = None
    file_path: Optional[str] = None
    description: str = ""
    
    def get_content_as_base64(self) -> Optional[str]:
        """Get content as base64 string for embedding in messages"""
        if self.content:
            import base64
            return base64.b64encode(self.content).decode()
        return None
    
@dataclass
class ToolResult:
    """Enhanced tool result supporting multimodal content"""
    text: str  # Primary text response
    attachment: Optional[ToolAttachment] = None
    
    def to_string(self) -> str:
        """Convert to string for backward compatibility"""
        return self.text  # Message injection handled by providers
```

### 2. Provider Implementation Strategy

#### Message Rewriting Approach

Each LLM provider implementation will handle tool response rewriting internally:

```python
# In LLMInterface implementations
def _process_tool_messages(self, messages: list[dict]) -> list[dict]:
    """Process messages, handling tool attachments"""
    processed = []
    pending_attachment = None
    
    for msg in messages:
        if msg.get("role") == "tool" and msg.get("_attachment"):
            # Store attachment for injection
            pending_attachment = msg.pop("_attachment")
            msg["content"] = msg.get("content", "") + "\n[File content in following message]"
        
        processed.append(msg)
        
        # Inject attachment after tool message if needed
        if pending_attachment and not self._supports_multimodal_tools():
            injection_msg = self._create_attachment_injection(pending_attachment)
            processed.append(injection_msg)
            pending_attachment = None
    
    return processed
```

### 3. Provider-Specific Implementations

#### GoogleGenAIClient

```python
class GoogleGenAIClient(LLMInterface):
    
    def _supports_multimodal_tools(self) -> bool:
        """Gemini doesn't support multimodal tool responses"""
        return False
    
    def _create_attachment_injection(self, attachment: ToolAttachment) -> dict:
        """Create user message with attachment for Gemini"""
        parts = [{"text": "[System: File from previous tool response]"}]
        
        if attachment.content and attachment.mime_type.startswith("image/"):
            # Add as inline_data
            parts.append({
                "inline_data": {
                    "mime_type": attachment.mime_type,
                    "data": attachment.content
                }
            })
        elif attachment.file_path:
            # TODO: For PDFs, consider using Gemini File API or extracting text content
            parts.append({"text": f"[File: {attachment.file_path} - Note: File content not accessible to model]"})
            
        return {"role": "user", "parts": parts}
    
    def _convert_messages_to_genai_format(self, messages: list[dict]) -> list:
        """Convert messages, handling tool attachments"""
        # First process any tool attachments
        messages = self._process_tool_messages(messages)
        
        # Then do normal conversion
        contents = []
        for msg in messages:
            # ... existing conversion logic ...
```

#### OpenAIClient

```python
class OpenAIClient(LLMInterface):
    
    def _supports_multimodal_tools(self) -> bool:
        """OpenAI doesn't support multimodal tool responses"""
        return False
    
    def _create_attachment_injection(self, attachment: ToolAttachment) -> dict:
        """Create user message with attachment for OpenAI"""
        content = [{"type": "text", "text": "[System: File from previous tool response]"}]
        
        if attachment.content and attachment.mime_type.startswith("image/"):
            # Use helper method for base64 encoding
            b64_data = attachment.get_content_as_base64()
            if b64_data:
                content.append({
                    "type": "image_url",
                    "image_url": {"url": f"data:{attachment.mime_type};base64,{b64_data}"}
                })
            
        return {"role": "user", "content": content}
    
    async def generate_response(self, messages: list[dict], ...) -> LLMOutput:
        """Generate response, handling tool attachments"""
        # Process tool attachments before sending
        messages = self._process_tool_messages(messages)
        
        # ... rest of existing logic ...
```

#### LiteLLMClient (for Claude)

```python
class LiteLLMClient(LLMInterface):
    
    def _supports_multimodal_tools(self) -> bool:
        """Check if model supports multimodal tool responses"""
        return self.model.startswith("claude")
    
    def _process_tool_messages(self, messages: list[dict]) -> list[dict]:
        """Process messages, using native support when available"""
        if not self._supports_multimodal_tools():
            return super()._process_tool_messages(messages)
        
        # Claude supports multimodal natively
        processed = []
        for msg in messages:
            if msg.get("role") == "tool" and msg.get("_attachment"):
                attachment = msg.pop("_attachment")
                # Convert to Claude's format
                content = [
                    {"type": "text", "text": msg.get("content", "")},
                ]
                if attachment.content and attachment.mime_type.startswith("image/"):
                    # Use helper method for base64 encoding
                    b64_data = attachment.get_content_as_base64()
                    if b64_data:
                        content.append({
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": attachment.mime_type,
                                "data": b64_data
                            }
                        })
                msg["content"] = content
            processed.append(msg)
        return processed
```

### 4. Processing Pipeline Integration

In `processing.py`, minimal changes to handle `ToolResult`:

```python
async def _execute_single_tool(self, ...):
    """Execute tool and handle enhanced results"""
    
    result = await self.tools_provider.execute_tool(...)
    
    # Handle both string and ToolResult
    if isinstance(result, ToolResult):
        # Extract attachment for provider handling
        tool_response_message = {
            "role": "tool",
            "tool_call_id": call_id,
            "content": result.text,
        }
        
        # Add attachment metadata for provider to handle
        if result.attachment:
            tool_response_message["_attachment"] = result.attachment
            
            # Store in history metadata
            tool_response_message["attachments"] = [{
                "type": "tool_result",
                "mime_type": result.attachment.mime_type,
                "description": result.attachment.description
            }]
    else:
        # Backward compatible string handling
        tool_response_message = {
            "role": "tool",
            "tool_call_id": call_id,
            "content": str(result)
        }
    
    return tool_response_message, ...
```

### 5. Tool Migration Example

```python
async def get_full_document_content_tool(
    exec_context: ToolExecutionContext,
    document_id: int,
    return_original: bool = False,  # New parameter
) -> Union[str, ToolResult]:
    """Get document content, optionally with original file"""
    
    # ... existing document retrieval logic ...
    
    if return_original and document.mime_type in ["application/pdf", "image/png", "image/jpeg"]:
        # Read original file (TODO: Stream large files to avoid memory issues)
        async with aiofiles.open(document.file_path, "rb") as f:
            content = await f.read()
            
        return ToolResult(
            text=f"Retrieved {document.type} document: {document.name}",
            attachment=ToolAttachment(
                mime_type=document.mime_type,
                content=content,
                description=f"Original {document.type} file"
            )
        )
    else:
        # Return extracted text as before
        return extracted_text  # Backward compatible
```

## Storage & History Considerations

### Message History Storage

- Tool messages with attachments store metadata in `attachments` field
- Attachment content not stored inline (reference only)
- Provider-injected messages marked appropriately

### Retrieval and Reconstruction

- When loading history, `_attachment` fields reconstructed from metadata
- Provider reprocessing ensures correct format for each LLM

## Migration Path

### Phase 1: Core Infrastructure

1. Add `ToolResult` and `ToolAttachment` types
2. Update `_execute_single_tool` to handle both return types
3. Add `_process_tool_messages` base implementation to providers

### Phase 2: Provider Implementations

1. Implement message rewriting in `GoogleGenAIClient`
2. Implement message rewriting in `OpenAIClient`
3. Update `LiteLLMClient` for Claude native support

### Phase 3: Tool Updates

1. Update `get_full_document_content_tool` with `return_original` parameter
2. Migrate other document tools progressively
3. Maintain backward compatibility throughout

### Phase 4: Testing & Validation

1. Unit tests for each provider's handling
2. End-to-end tests with real documents
3. Verify history storage and retrieval

## Testing Strategy

### Unit Tests

```python
def test_tool_result_conversion():
    """Test ToolResult to string conversion"""
    result = ToolResult(
        text="Document retrieved",
        attachment=ToolAttachment(mime_type="application/pdf", ...)
    )
    assert "[File content follows" in result.to_string()

def test_provider_message_rewriting():
    """Test each provider's message rewriting"""
    # Test Gemini injection
    # Test OpenAI injection  
    # Test Claude native handling
```

### Integration Tests

- Test tool returning `ToolResult` with attachment
- Verify provider-specific message transformation
- Ensure LLM receives and processes files correctly
- Validate history storage with attachments

### End-to-End Tests

- Upload PDF → Tool retrieves with attachment → LLM analyzes
- Test with each provider (Gemini, OpenAI, Claude via LiteLLM)
- Verify degradation when attachment fails

## Future Evolution

When providers add native support:

1. Update `_supports_multimodal_tools()` to detect capability
2. Switch from injection to native format
3. No changes needed to tools or core pipeline

Example future detection:

```python
def _supports_multimodal_tools(self) -> bool:
    """Check for multimodal tool support"""
    # Future: Check API version or capability
    if hasattr(self, 'api_version') and self.api_version >= "2025-06":
        return True
    return False
```

## Security Considerations

1. **File Size Limits**: Enforce maximum attachment size (e.g., 20MB)
2. **MIME Type Validation**: Verify file types before processing
3. **Memory Management**: Stream large files rather than loading entirely
4. **Access Control**: Ensure tools only access authorized files

## Performance Considerations

1. **Lazy Loading**: Don't load file content until needed
2. **Caching**: Cache processed attachments for repeated access
3. **Compression**: Consider compressing large attachments
4. **Provider Limits**: Respect provider-specific size/rate limits

## Open Questions

1. **Multiple Attachments**: Should we support multiple files per tool response?
2. **Attachment Storage**: Should we store attachment content separately from message history?
3. **Error Recovery**: How to handle attachment injection failures gracefully?
4. **User Visibility**: Should users see the injection messages in the UI?

## Conclusion

This design provides a clean, provider-agnostic approach to multimodal tool results that:

- Works within current provider limitations
- Maintains backward compatibility
- Isolates complexity within provider implementations
- Prepares for future native support
- Leverages existing attachment storage infrastructure

The message rewriting approach ensures tools can return rich content while each provider handles the
complexity of its specific requirements transparently.
