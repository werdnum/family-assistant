# Streaming Response Support for Chat UI

## Overview

This document outlines the design for adding streaming response support to the Family Assistant Chat
UI. The implementation will use Server-Sent Events (SSE) to stream responses from the backend to the
frontend, providing real-time feedback for both text generation and tool execution.

## Goals

1. **Real-time Response Streaming**: Users see assistant responses as they're generated
2. **Tool Execution Transparency**: Real-time visibility into tool calls and their results
3. **Backward Compatibility**: All existing interfaces (Telegram, API, tests) continue working
   unchanged
4. **Minimal Code Duplication**: Reuse existing processing logic with streaming wrapper
5. **Progressive Enhancement**: Graceful fallback to non-streaming mode when needed

## Non-Goals

1. WebSocket implementation (SSE is simpler and sufficient)
2. Streaming for Telegram interface (would require significant bot API changes)
3. Changing existing LLMInterface implementations (streaming is opt-in)

## Architecture

### Backend Architecture

#### 1. Streaming Data Types

```python
from dataclasses import dataclass
from typing import Literal, Any, AsyncGenerator
import asyncio
import json
import uuid

@dataclass
class LLMStreamEvent:
    """Represents a single event in the LLM response stream."""
    type: Literal["text", "tool_call", "tool_result", "end", "error"]
    content: str | None = None
    tool_call: ToolCallItem | None = None
    tool_call_id: str | None = None  # For correlating tool results with calls
    tool_result: dict[str, Any] | None = None
    reasoning_info: dict[str, Any] | None = None
    error: str | None = None
```

#### 2. Extended LLM Interface

```python
class LLMInterface(Protocol):
    """Extended protocol with optional streaming support."""
    
    async def generate_response(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | None = "auto",
    ) -> LLMOutput:
        """Existing method - all implementations must provide this."""
        ...
    
    async def generate_response_stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | None = "auto",
    ) -> AsyncGenerator[LLMStreamEvent, None]:
        """Optional streaming method - implementations can opt-in."""
        # Default implementation chunks the non-streaming response
        result = await self.generate_response(messages, tools, tool_choice)
        if result.content:
            # Chunk text content for pseudo-streaming
            chunk_size = 100  # Characters per chunk
            for i in range(0, len(result.content), chunk_size):
                chunk = result.content[i:i + chunk_size]
                yield LLMStreamEvent(type="text", content=chunk)
                await asyncio.sleep(0.01)  # Small delay to simulate streaming
        if result.tool_calls:
            for tool_call in result.tool_calls:
                yield LLMStreamEvent(type="tool_call", tool_call=tool_call)
        yield LLMStreamEvent(type="end", reasoning_info=result.reasoning_info)
```

#### 3. ProcessingService Refactoring

The core processing logic will be extracted into a generator function:

```python
class ProcessingService:
    async def _process_interaction_generator(
        self,
        db_context: DatabaseContext,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        # ... other parameters
    ) -> AsyncGenerator[LLMStreamEvent, None]:
        """Core processing logic as a generator."""
        
        while True:
            tool_calls_in_turn = []
            current_text = ""
            reasoning_info = None
            
            # Always use generate_response_stream - LLM clients without native
            # streaming will use the default implementation that chunks the response
            async for event in self.llm_client.generate_response_stream(messages, tools):
                yield event
                
                # Collect tool calls and text
                if event.type == "tool_call":
                    tool_calls_in_turn.append(event.tool_call)
                elif event.type == "text":
                    current_text += event.content or ""
                elif event.type == "end":
                    reasoning_info = event.reasoning_info
            
            # If no tool calls, we're done
            if not tool_calls_in_turn:
                break
            
            # Execute tools and yield results
            tool_results = []
            for tool_call in tool_calls_in_turn:
                result = await self._execute_tool(tool_call)
                tool_results.append({
                    "tool_call_id": tool_call.id,
                    "result": result
                })
                yield LLMStreamEvent(
                    type="tool_result", 
                    tool_call_id=tool_call.id,
                    tool_result=result
                )
            
            # Add assistant message with tool calls to history
            messages.append({
                "role": "assistant",
                "content": current_text if current_text else None,
                "tool_calls": [self._serialize_tool_call(tc) for tc in tool_calls_in_turn]
            })
            
            # Add tool results to history
            for tool_result in tool_results:
                messages.append({
                    "role": "tool",
                    "content": json.dumps(tool_result["result"]),
                    "tool_call_id": tool_result["tool_call_id"]
                })
            
            # Continue loop for next LLM call
    
    # Existing method for backward compatibility
    async def handle_chat_interaction(self, ...) -> tuple[str | None, ...]:
        """Existing method consumes the generator."""
        final_content = []
        tool_results = []
        
        async for event in self._process_interaction_generator(...):
            if event.type == "text":
                final_content.append(event.content)
            elif event.type == "tool_result":
                tool_results.append(event.tool_result)
            # ... handle other event types
        
        return ''.join(final_content), ...
    
    # New streaming method
    async def handle_chat_interaction_stream(self, ...) -> AsyncGenerator[LLMStreamEvent, None]:
        """New method returns the generator directly."""
        async for event in self._process_interaction_generator(...):
            yield event
```

#### 4. SSE API Endpoint

Since EventSource only supports GET requests, we'll use a POST endpoint with fetch API and handle
the streaming response manually:

```python
@chat_api_router.post("/v1/chat/send_message_stream")
async def api_chat_send_message_stream(
    payload: ChatPromptRequest,
    processing_service: Annotated[ProcessingService, Depends(get_processing_service)],
    db_context: Annotated[DatabaseContext, Depends(get_db)],
):
    """Stream chat responses using Server-Sent Events format."""
    
    async def event_generator():
        try:
            async for event in processing_service.handle_chat_interaction_stream(...):
                # Format as SSE with event types
                if event.type == "text":
                    yield f"event: text\ndata: {json.dumps({'content': event.content})}\n\n"
                elif event.type == "tool_call":
                    # Convert tool_call to dict for JSON serialization
                    tool_call_dict = {
                        "id": event.tool_call.id,
                        "function": {
                            "name": event.tool_call.function.name,
                            "arguments": event.tool_call.function.arguments
                        }
                    }
                    yield f"event: tool_call\ndata: {json.dumps({'tool_call': tool_call_dict})}\n\n"
                elif event.type == "tool_result":
                    # Include tool_call_id for correlation
                    yield f"event: tool_result\ndata: {json.dumps({'tool_call_id': event.tool_call_id, 'result': event.tool_result})}\n\n"
                elif event.type == "end":
                    yield f"event: end\ndata: {json.dumps({'reasoning_info': event.reasoning_info})}\n\n"
                # ... handle other event types
        except Exception as e:
            error_id = str(uuid.uuid4())
            logger.error(f"Streaming error {error_id}: {e}", exc_info=True)
            yield f"event: error\ndata: {json.dumps({'error': 'An error occurred while processing your request', 'error_id': error_id})}\n\n"
        finally:
            # Connection will be closed after generator completes
            pass
    
    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # Disable Nginx buffering
        }
    )
```

### Frontend Architecture

#### 1. Streaming Runtime Hook

```javascript
// frontend/src/chat/hooks/useStreamingRuntime.js
import { useCallback, useRef, useState, useEffect } from 'react';

// Helper to parse SSE stream from fetch response
async function* parseSSEStream(response) {
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    
    buffer += decoder.decode(value, { stream: true });
    const events = buffer.split('\n\n');
    buffer = events.pop() || '';
    
    for (const event of events) {
      if (!event.trim()) continue;
      
      const lines = event.split('\n');
      let eventType = 'message';
      const dataLines = [];
      
      for (const line of lines) {
        if (line.startsWith('event: ')) {
          eventType = line.slice(7);
        } else if (line.startsWith('data: ')) {
          dataLines.push(line.slice(6));
        }
      }
      
      if (dataLines.length > 0) {
        // Join multiple data lines per SSE spec
        const data = dataLines.join('\n');
        try {
          const eventData = JSON.parse(data);
          yield { type: eventType, ...eventData };
        } catch (e) {
          console.error('Failed to parse SSE data:', e);
        }
      }
    }
  }
}

export function useStreamingRuntime({ conversationId, initialMessages = [], onUpdate }) {
  const [messages, setMessages] = useState(initialMessages);
  const [isRunning, setIsRunning] = useState(false);
  const abortControllerRef = useRef(null);
  
  // Update messages when initialMessages change (e.g., conversation switch)
  useEffect(() => {
    setMessages(initialMessages);
  }, [initialMessages]);
  
  const startStreaming = useCallback(async (userMessage) => {
    setIsRunning(true);
    
    // Add user message and assistant placeholder in one update
    const userMsg = {
      id: `msg_${Date.now()}`,
      role: 'user',
      content: userMessage.content,
      createdAt: new Date()
    };
    
    const assistantId = `msg_${Date.now()}_assistant`;
    const assistantMsg = {
      id: assistantId,
      role: 'assistant',
      content: [],
      createdAt: new Date()
    };
    
    setMessages(prev => [...prev, userMsg, assistantMsg]);
    
    // Create abort controller for cancellation
    const abortController = new AbortController();
    abortControllerRef.current = abortController;
    
    try {
      // Use fetch with POST for streaming
      const response = await fetch('/api/v1/chat/send_message_stream', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({
          prompt: userMessage.content[0].text,
          conversation_id: conversationId,
          profile_id: 'default_assistant',
          interface_type: 'web'
        }),
        signal: abortController.signal
      });
      
      if (!response.ok) {
        throw new Error(`HTTP error! status: ${response.status}`);
      }
      
      let currentText = '';
      const toolCalls = new Map(); // Use Map for easier lookup
      
      // Process the SSE stream
      for await (const event of parseSSEStream(response)) {
        switch (event.type) {
          case 'text':
            currentText += event.content;
            setMessages(prev => prev.map(msg => 
              msg.id === assistantId 
                ? { ...msg, content: [{ type: 'text', text: currentText }] }
                : msg
            ));
            break;
            
          case 'tool_call':
            const toolCall = event.tool_call;
            toolCalls.set(toolCall.id, {
              type: 'tool-call',
              toolCallId: toolCall.id,
              toolName: toolCall.function.name,
              args: toolCall.function.arguments,
              result: null
            });
            
            // Update message with all tool calls
            setMessages(prev => prev.map(msg => 
              msg.id === assistantId 
                ? { 
                    ...msg, 
                    content: [
                      { type: 'text', text: currentText },
                      ...Array.from(toolCalls.values())
                    ]
                  }
                : msg
            ));
            break;
            
          case 'tool_result':
            // Update the specific tool call with its result
            const existingToolCall = toolCalls.get(event.tool_call_id);
            if (existingToolCall) {
              existingToolCall.result = event.result;
              
              // Update message with updated tool calls
              setMessages(prev => prev.map(msg => 
                msg.id === assistantId 
                  ? { 
                      ...msg, 
                      content: [
                        { type: 'text', text: currentText },
                        ...Array.from(toolCalls.values())
                      ]
                    }
                  : msg
              ));
            }
            break;
            
          case 'end':
            // Could store reasoning info if needed
            break;
            
          case 'error':
            console.error('Stream error:', event.error);
            // Could show error in UI
            break;
        }
      }
    } catch (error) {
      if (error.name !== 'AbortError') {
        console.error('Streaming error:', error);
        // Handle error - could show error message
      }
    } finally {
      setIsRunning(false);
      abortControllerRef.current = null;
      onUpdate?.(); // Refresh conversations
    }
  }, [conversationId, onUpdate]);
  
  const cancel = useCallback(() => {
    if (abortControllerRef.current) {
      abortControllerRef.current.abort();
    }
  }, []);
  
  return {
    messages,
    isRunning,
    onNew: startStreaming,
    cancel,
    // Compatible with assistant-ui runtime interface
  };
}
```

#### 2. Updated ChatApp Component

```javascript
// Modify ChatApp.jsx to support streaming
const ChatApp = () => {
  const [streamingEnabled, setStreamingEnabled] = useState(true);
  // ... existing state
  
  // Existing runtime for non-streaming
  const externalRuntime = useExternalStoreRuntime({
    messages,
    isRunning: isLoading || !conversationId,
    onNew: handleNew,
    convertMessage,
  });
  
  // New streaming runtime
  const streamingRuntime = useStreamingRuntime({
    conversationId,
    initialMessages: messages,
    onUpdate: fetchConversations, // Refresh sidebar after messages
  });
  
  // Choose runtime based on streaming preference
  const runtime = streamingEnabled ? streamingRuntime : externalRuntime;
  
  return (
    <div className="chat-app-wrapper">
      {/* Add streaming toggle in settings */}
      {/* Rest of the UI remains the same */}
      <AssistantRuntimeProvider runtime={runtime}>
        <Thread />
      </AssistantRuntimeProvider>
    </div>
  );
};
```

## Implementation Plan

### Phase 1: Backend Infrastructure

1. Define streaming data types and protocols
2. Extend LLMInterface with optional streaming
3. Implement streaming in LiteLLM clients
4. Refactor ProcessingService to generator pattern
5. Add SSE endpoint with error handling

### Phase 2: Frontend Integration

1. Create SSE client utilities
2. Implement useStreamingRuntime hook
3. Update ChatApp with streaming toggle
4. Add streaming state indicators
5. Test error recovery and reconnection

### Phase 3: Tool Call Streaming

1. Stream tool call notifications
2. Stream tool execution progress
3. Handle tool confirmation in streaming
4. Update UI for tool execution visibility
5. Add tool result streaming

### Phase 4: Testing & Polish

1. Comprehensive testing of streaming flow
2. Performance optimization
3. Error handling improvements
4. Documentation updates
5. Migration guide for LLM providers

## Testing Strategy

### Unit Tests

- Mock streaming LLM responses
- Test event parsing and formatting
- Verify backward compatibility

### Integration Tests

- Test SSE endpoint with various scenarios
- Verify streaming with tool calls
- Test error conditions and recovery

### E2E Tests

- Full chat flow with streaming
- Tool execution visibility
- Error handling and fallback

## Rollout Strategy

1. **Feature Flag**: Add streaming as opt-in feature
2. **Gradual Rollout**: Enable for subset of users
3. **Monitoring**: Track streaming performance metrics
4. **Fallback**: Automatic fallback to non-streaming on errors

## Security Considerations

1. **Rate Limiting**: Apply same limits as non-streaming endpoint
2. **Connection Limits**: Limit concurrent SSE connections per user
3. **Timeout Handling**: Close idle connections after timeout
4. **Input Validation**: Same validation as existing endpoint

## Performance Considerations

1. **Memory Usage**: Stream processing uses less memory than buffering
2. **Connection Pooling**: Reuse connections where possible
3. **Compression**: Enable compression for SSE responses
4. **Caching**: Cache static parts of responses

## Future Enhancements

1. **WebSocket Support**: For bidirectional streaming
2. **Partial Tool Results**: Stream long-running tool outputs
3. **Streaming File Uploads**: Progress for document processing
4. **Multi-modal Streaming**: Support for image/audio generation

## Conclusion

This design provides a robust foundation for streaming responses while maintaining full backward
compatibility. The implementation can be done incrementally with minimal risk to existing
functionality.
