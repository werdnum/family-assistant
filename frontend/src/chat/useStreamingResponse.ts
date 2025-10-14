import { useState, useCallback, useRef } from 'react';

// Interfaces for data structures
interface Attachment {
  attachment_id: string;
  [key: string]: any;
}

export interface ToolCall {
  id: string;
  name: string;
  arguments: string;
  result?: string;
  attachments?: Attachment[];
  _synthetic?: boolean;
}

export interface ToolConfirmationRequest {
  request_id: string;
  tool_name: string;
  tool_call_id: string;
  confirmation_prompt: string;
  timeout_seconds: number;
  args: any;
  created_at: string;
}

export interface ToolConfirmationResult {
  request_id: string;
  approved: boolean;
}

interface SendMessageParams {
  prompt: string;
  conversationId?: string;
  profileId?: string;
  interfaceType?: string;
  attachments?: any[];
}

interface StreamingResponseOptions {
  onMessage?: (content: string) => void;
  onToolCall?: (toolCalls: ToolCall[]) => void;
  onToolConfirmationRequest?: (request: ToolConfirmationRequest) => void;
  onToolConfirmationResult?: (result: ToolConfirmationResult) => void;
  onError?: (error: string | Error) => void;
  onComplete?: (result: { content: string; toolCalls: ToolCall[] }) => void;
}

interface StreamingResponse {
  sendStreamingMessage: (params: SendMessageParams) => Promise<void>;
  cancelStream: () => void;
  isStreaming: boolean;
}

/**
 * Hook for handling streaming responses from the chat API
 * @param {StreamingResponseOptions} options - Hook options
 * @returns {StreamingResponse} { sendStreamingMessage, cancelStream, isStreaming }
 */
export const useStreamingResponse = ({
  onMessage = () => {},
  onToolCall = () => {},
  onToolConfirmationRequest = () => {},
  onToolConfirmationResult = () => {},
  onError = () => {},
  onComplete = () => {},
}: StreamingResponseOptions = {}): StreamingResponse => {
  const [isStreaming, setIsStreaming] = useState(false);
  const abortControllerRef = useRef<AbortController | null>(null);

  const sendStreamingMessage = useCallback(
    async ({
      prompt,
      conversationId,
      profileId = 'default_assistant',
      interfaceType = 'web',
      attachments = undefined,
    }: SendMessageParams): Promise<void> => {
      setIsStreaming(true);
      abortControllerRef.current = new AbortController();

      let currentMessage = '';
      const toolCalls: ToolCall[] = [];
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

        const reader = response.body!.getReader();
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

          let currentEventType: string | null = null;

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

                // Handle text content
                if (payload.content) {
                  currentMessage += payload.content;
                  onMessage(currentMessage);
                }

                // Handle a single tool call
                if (payload.tool_call) {
                  const newToolCall: ToolCall = {
                    id: payload.tool_call.id,
                    name: payload.tool_call.function.name,
                    arguments: payload.tool_call.function.arguments || '{}',
                  };
                  toolCalls.push(newToolCall);
                  onToolCall([...toolCalls]);
                }

                // Handle an array of tool calls
                if (payload.tool_calls) {
                  payload.tool_calls.forEach((tc: any) => {
                    const newToolCall: ToolCall = {
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
                    const updatedToolCall: ToolCall = {
                      ...toolCalls[toolCallIndex],
                      result: payload.result,
                      attachments: payload.attachments || toolCalls[toolCallIndex].attachments,
                    };

                    const newToolCalls = [...toolCalls];
                    newToolCalls[toolCallIndex] = updatedToolCall;

                    toolCalls.length = 0;
                    toolCalls.push(...newToolCalls);
                    onToolCall(newToolCalls);
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

                // Handle done event with auto-attachments
                if (
                  payload.attachment_ids &&
                  payload.attachments &&
                  Array.isArray(payload.attachments)
                ) {
                  const syntheticToolCall: ToolCall = {
                    id: `web_attach_${Date.now()}_${Math.random().toString(36).substr(2, 9)}`,
                    name: 'attach_to_response',
                    arguments: JSON.stringify({ attachment_ids: payload.attachment_ids }),
                    result: JSON.stringify({
                      status: 'attachments_queued',
                      count: payload.attachments.length,
                      attachments: payload.attachments,
                    }),
                    attachments: payload.attachments,
                    _synthetic: true,
                  };

                  toolCalls.push(syntheticToolCall);
                  onToolCall([...toolCalls]);
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
      } catch (error: any) {
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
