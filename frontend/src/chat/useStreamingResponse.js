import { useCallback, useRef, useState } from 'react';

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
      turnId = undefined,
    }) => {
      setIsStreaming(true);
      abortControllerRef.current = new AbortController();

      let currentMessage = '';
      const toolCalls = [];
      let buffer = '';
      // Per-file `attachment` events are accumulated into a single synthetic
      // attach_to_response tool call so queued attachments render in the UI.
      const autoAttachments = [];

      // Client-generated turn id makes the kickoff idempotent: a retried POST
      // with the same id returns the existing turn instead of starting a
      // second producer.
      const effectiveTurnId =
        turnId ||
        (typeof crypto !== 'undefined' && crypto.randomUUID
          ? crypto.randomUUID()
          : `turn_${Date.now()}_${Math.random().toString(36).slice(2)}`);

      try {
        // Step 1: kick off the turn. This persists the user message, seeds the
        // turn_started event, and starts the background producer.
        const startResponse = await fetch('/api/v1/chat/turns', {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
          },
          body: JSON.stringify({
            turn_id: effectiveTurnId,
            prompt,
            conversation_id: conversationId,
            profile_id: profileId,
            interface_type: interfaceType,
            attachments: attachments,
          }),
          signal: abortControllerRef.current.signal,
        });

        if (!startResponse.ok) {
          if (startResponse.status === 401) {
            window.location.href = `/login?next=${encodeURIComponent(window.location.pathname + window.location.search)}`;
            return;
          }
          const errorData = await startResponse.json().catch(() => ({}));
          throw new Error(errorData.detail || `HTTP error! status: ${startResponse.status}`);
        }

        const { conversation_id: streamConversationId, first_seq: firstSeq } =
          await startResponse.json();
        const resolvedConversationId = streamConversationId || conversationId;

        // Step 2: subscribe to the conversation event stream from the turn's
        // first seq. follow=false closes the stream once the turn completes.
        const streamUrl =
          `/api/v1/chat/conversations/${encodeURIComponent(resolvedConversationId)}/stream` +
          `?from_seq=${encodeURIComponent(String(firstSeq ?? 0))}`;

        const response = await fetch(streamUrl, {
          method: 'GET',
          headers: {
            Accept: 'text/event-stream',
          },
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

        // Acknowledge a processed turn so the server can suppress the disconnect
        // push. Best-effort: a failed ack must not break stream completion.
        const ackTurn = async (ackConversationId, ackSeq) => {
          try {
            const ackResponse = await fetch('/api/v1/chat/ack', {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({
                conversation_id: ackConversationId,
                ack_seq: ackSeq,
              }),
            });
            // fetch only rejects on network errors, so surface HTTP error
            // statuses too instead of swallowing a misbehaving ack endpoint.
            if (!ackResponse.ok) {
              console.warn(`Turn ack failed: HTTP ${ackResponse.status}`);
            }
          } catch (e) {
            // A missed ack only means a possibly-redundant push; never fatal.
            console.warn('Turn ack request failed:', e);
          }
        };

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

                // The conversation stream carries events for every turn in the
                // conversation. In this send-and-watch flow we only care about
                // the turn we just started: ignore events tagged with a
                // different turn_id (e.g. a turn started concurrently from
                // another tab/device). Connection-level events (heartbeat,
                // stream_dropped) carry no turn_id and fall through.
                if (payload.turn_id && payload.turn_id !== effectiveTurnId) {
                  continue;
                }

                // The turn_ended event terminates this turn's stream. Done is
                // signalled both by the SSE event name and the payload status.
                if (currentEventType === 'turn_ended' || payload.status) {
                  // Explicitly acknowledge receipt so the server suppresses the
                  // "you have a new reply" disconnect push. The server never
                  // treats writing the SSE chunk as delivery — only this ack
                  // (sent after we've actually processed turn_ended) counts.
                  // Fire-and-forget: never block UI completion on the ack.
                  if (typeof payload.seq === 'number') {
                    void ackTurn(resolvedConversationId, payload.seq);
                  }
                  // Materialize accumulated per-file attachments into a single
                  // synthetic tool call, but only if the assistant didn't make a
                  // real attach_to_response call (which already renders them).
                  if (
                    autoAttachments.length > 0 &&
                    !toolCalls.some((tc) => tc.name === 'attach_to_response')
                  ) {
                    toolCalls.push({
                      id: `web_attach_${Date.now()}_${Math.random().toString(36).substr(2, 9)}`,
                      name: 'attach_to_response',
                      arguments: JSON.stringify({
                        attachment_ids: autoAttachments.map((a) => a.attachment_id),
                      }),
                      result: JSON.stringify({
                        status: 'attachments_queued',
                        count: autoAttachments.length,
                        attachments: autoAttachments,
                      }),
                      attachments: [...autoAttachments],
                      _synthetic: true,
                    });
                    onToolCall([...toolCalls]);
                  }
                  // A failed turn carries its error on the terminal event;
                  // surface it before exiting so the UI shows the error rather
                  // than silently clearing the loading state.
                  if (payload.status === 'failed' || payload.error) {
                    onError(new Error(payload.error || 'The assistant stopped unexpectedly.'));
                  }
                  return;
                }
                // turn_started / heartbeat carry no renderable content.
                if (currentEventType === 'turn_started' || currentEventType === 'heartbeat') {
                  continue;
                }

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
                    // Create a new tool call object to ensure React detects the change
                    const updatedToolCall = {
                      ...toolCalls[toolCallIndex],
                      result: payload.result,
                      attachments: payload.attachments || toolCalls[toolCallIndex].attachments,
                    };

                    // Create new array with the updated tool call
                    const newToolCalls = [...toolCalls];
                    newToolCalls[toolCallIndex] = updatedToolCall;

                    // Update the toolCalls array reference
                    toolCalls.length = 0;
                    toolCalls.push(...newToolCalls);

                    // Trigger the callback with the new array
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

                // Accumulate per-file `attachment` events. The turn producer
                // emits one per queued attachment; they're materialized into a
                // synthetic attach_to_response tool call at turn_ended, but only
                // when the assistant didn't already make a real attach_to_response
                // call (otherwise the same files would render twice).
                if (payload.type === 'attachment' && payload.attachment_id) {
                  autoAttachments.push({
                    type: 'attachment',
                    attachment_id: payload.attachment_id,
                    url: payload.url,
                    content_url: payload.content_url,
                    mime_type: payload.mime_type,
                    description: payload.description,
                    size: payload.size,
                  });
                }

                // Handle done event with auto-attachments
                // When tools return attachments without explicit attach_to_response call,
                // synthesize a tool call to display them in the UI
                if (
                  payload.attachment_ids &&
                  payload.attachments &&
                  Array.isArray(payload.attachments)
                ) {
                  const syntheticToolCall = {
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
