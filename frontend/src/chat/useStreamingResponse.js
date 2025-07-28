import { useState, useCallback, useRef } from 'react';

/**
 * Hook for handling streaming responses from the chat API
 * @param {Function} onMessage - Callback when a content chunk is received
 * @param {Function} onToolCall - Callback when a tool call is received
 * @param {Function} onError - Callback when an error occurs
 * @param {Function} onComplete - Callback when the stream completes
 */
export const useStreamingResponse = ({
  onMessage = () => {},
  onToolCall = () => {},
  onError = () => {},
  onComplete = () => {},
} = {}) => {
  const [isStreaming, setIsStreaming] = useState(false);
  const abortControllerRef = useRef(null);
  const currentMessageRef = useRef('');
  const toolCallsRef = useRef([]);

  const sendStreamingMessage = useCallback(
    async ({ prompt, conversationId, profileId = 'default_assistant', interfaceType = 'web' }) => {
      // Reset state
      currentMessageRef.current = '';
      toolCallsRef.current = [];
      setIsStreaming(true);

      // Create abort controller for cancellation
      abortControllerRef.current = new AbortController();

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
          }),
          signal: abortControllerRef.current.signal,
        });

        if (!response.ok) {
          // Handle authentication errors
          if (response.status === 401) {
            window.location.href = `/login?next=${encodeURIComponent(window.location.pathname + window.location.search)}`;
            return;
          }
          const errorData = await response.json().catch(() => ({}));
          throw new Error(errorData.detail || `HTTP error! status: ${response.status}`);
        }

        // Process the SSE stream
        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';

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

          for (let i = 0; i < lines.length - 1; i++) {
            const line = lines[i].trim();

            if (line.startsWith('data: ')) {
              const data = line.slice(6);

              // Skip the [DONE] marker
              if (data === '[DONE]') {
                continue;
              }

              try {
                const event = JSON.parse(data);

                switch (event.type) {
                  case 'content':
                    currentMessageRef.current += event.content;
                    onMessage(currentMessageRef.current);
                    break;

                  case 'tool_call':
                    if (event.tool_call?.function?.name) {
                      toolCallsRef.current.push({
                        id: event.tool_call_id,
                        name: event.tool_call.function.name,
                        arguments: event.tool_call.function.arguments || '{}',
                      });
                      onToolCall(event.tool_call, event.tool_call_id);
                    }
                    break;

                  case 'error':
                    onError(event.error, event.metadata);
                    break;

                  case 'done':
                    onComplete({
                      content: currentMessageRef.current,
                      toolCalls: toolCallsRef.current,
                      metadata: event.metadata,
                    });
                    break;
                }
              } catch (e) {
                console.error('Error parsing SSE event:', e, data);
              }
            }
          }
        }
      } catch (error) {
        if (error.name !== 'AbortError') {
          console.error('Streaming error:', error);
          onError(error.message);
        }
      } finally {
        setIsStreaming(false);
        abortControllerRef.current = null;
      }
    },
    [onMessage, onToolCall, onError, onComplete]
  );

  const cancelStream = useCallback(() => {
    if (abortControllerRef.current) {
      abortControllerRef.current.abort();
      abortControllerRef.current = null;
      setIsStreaming(false);
    }
  }, []);

  return {
    sendStreamingMessage,
    cancelStream,
    isStreaming,
  };
};
