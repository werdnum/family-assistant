import { useState, useCallback, useRef } from 'react';

/**
 * Hook for handling streaming responses from the chat API
 * @param {Object} options - Hook options
 * @param {Function} options.onMessage - Callback when content is streamed (receives accumulated content)
 * @param {Function} options.onToolCall - Callback when tool calls are updated (receives array of all tool calls)
 * @param {Function} options.onToolConfirmationRequest - Callback when tool confirmation is requested
 * @param {Function} options.onToolConfirmationResult - Callback when tool confirmation result is received
 * @param {Function} options.onError - Callback when an error occurs (receives Error object)
 * @param {Function} options.onComplete - Callback when stream completes (receives { content, toolCalls })
 * @returns {Object} { sendStreamingMessage, cancelStream, isStreaming }
 */
export const useStreamingResponse = ({
  onMessage = () => {},
  onToolCall = () => {},
  onToolConfirmationRequest = () => {},
  onToolConfirmationResult = () => {},
  onError = () => {},
  onComplete = () => {},
} = {}) => {
  const [isStreaming, setIsStreaming] = useState(false);
  const abortControllerRef = useRef(null);

  const sendStreamingMessage = useCallback(
    async ({
      prompt,
      conversationId,
      profileId = 'default_assistant',
      interfaceType = 'web',
      attachments = undefined,
    }) => {
      setIsStreaming(true);
      abortControllerRef.current = new AbortController();

      let currentMessage = '';
      const toolCalls = [];
      let buffer = '';

      try {
        const response = await fetch('/api/v1/chat/send_message_stream', {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
          },
          body: JSON.stringify({
            prompt,
            conversation_id: conversationId,
            profile_id: profileId,
            interface_type: interfaceType,
            attachments: attachments,
          }),
          signal: abortControllerRef.current.signal,
        });

        if (!response.ok) {
          if (response.status === 401) {
            window.location.href = `/login?next=${encodeURIComponent(window.location.pathname + window.location.search)}`;
            return;
          }
          const errorData = await response.json().catch(() => ({}));
          throw new Error(errorData.detail || `HTTP error! status: ${response.status}`);
        }

        const reader = response.body.getReader();
        const decoder = new TextDecoder();

        while (true) {
          const { done, value } = await reader.read();
          if (done) {
            break;
          }

          // Decode chunk and add to buffer
          buffer += decoder.decode(value, { stream: true });

          // Process complete SSE messages from buffer
          const lines = buffer.split('\n');
          buffer = lines[lines.length - 1]; // Keep incomplete line in buffer

          let currentEventType = null;

          for (let i = 0; i < lines.length - 1; i++) {
            const line = lines[i].trim();

            // Handle SSE event lines
            if (line.startsWith('event: ')) {
              currentEventType = line.slice(7);
              if (currentEventType === 'close') {
                // Server closed the stream
                return;
              }
              continue;
            }

            // Handle data lines
            if (line.startsWith('data: ')) {
              const data = line.slice(6);

              // Skip the [DONE] marker
              if (data === '[DONE]') {
                continue;
              }

              try {
                const payload = JSON.parse(data);

                // A single payload can contain multiple parts.
                // We don't rely on `currentEventType` but inspect the payload directly.

                // Handle text content
                if (payload.content) {
                  currentMessage += payload.content;
                  onMessage(currentMessage);
                }

                // Handle a single tool call
                if (payload.tool_call) {
                  const newToolCall = {
                    id: payload.tool_call.id,
                    name: payload.tool_call.function.name,
                    arguments: payload.tool_call.function.arguments || '{}',
                  };
                  toolCalls.push(newToolCall);
                  onToolCall([...toolCalls]);
                }

                // Handle an array of tool calls
                if (payload.tool_calls) {
                  payload.tool_calls.forEach((tc) => {
                    const newToolCall = {
                      id: tc.id,
                      name: tc.function.name,
                      arguments: tc.function.arguments || '{}',
                    };
                    toolCalls.push(newToolCall);
                  });
                  onToolCall([...toolCalls]);
                }

                // Handle tool result
                if (payload.tool_call_id && payload.result) {
                  const toolCallIndex = toolCalls.findIndex((tc) => tc.id === payload.tool_call_id);
                  if (toolCallIndex !== -1) {
                    toolCalls[toolCallIndex].result = payload.result;
                    if (payload.attachments && payload.attachments.length > 0) {
                      toolCalls[toolCallIndex].attachments = payload.attachments;
                    }
                    onToolCall([...toolCalls]);
                  }
                }

                // Handle tool confirmation request
                if (payload.request_id && payload.tool_name) {
                  onToolConfirmationRequest({
                    request_id: payload.request_id,
                    tool_name: payload.tool_name,
                    tool_call_id: payload.tool_call_id,
                    confirmation_prompt: payload.confirmation_prompt,
                    timeout_seconds: payload.timeout_seconds,
                    args: payload.args,
                    created_at: new Date().toISOString(),
                  });
                }

                // Handle tool confirmation result
                if (payload.request_id !== undefined && payload.approved !== undefined) {
                  onToolConfirmationResult({
                    request_id: payload.request_id,
                    approved: payload.approved,
                  });
                }

                // Handle error
                if (payload.error) {
                  onError(new Error(payload.error || 'Unknown error'));
                }
              } catch (e) {
                console.error('Failed to parse SSE event:', e, 'Data:', data);
              }

              // Reset event type after processing
              currentEventType = null;
            }
          }
        }
      } catch (error) {
        if (error.name !== 'AbortError') {
          onError(error.message || error.toString());
        }
      } finally {
        setIsStreaming(false);
        onComplete({ content: currentMessage, toolCalls });
        abortControllerRef.current = null;
      }
    },
    [
      onMessage,
      onToolCall,
      onToolConfirmationRequest,
      onToolConfirmationResult,
      onError,
      onComplete,
    ]
  );

  const cancelStream = useCallback(() => {
    if (abortControllerRef.current) {
      abortControllerRef.current.abort();
    }
  }, []);

  return {
    sendStreamingMessage,
    cancelStream,
    isStreaming,
  };
};
