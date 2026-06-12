import { useCallback, useRef, useState } from 'react';

import { conversationStreamUrl, redirectToLogin } from './conversationStream';

/**
 * Hook for handling streaming responses from the chat API
 * @param {Object} options - Hook options
 * @param {Function} options.onMessage - Callback when content is streamed (receives accumulated content)
 * @param {Function} options.onToolCall - Callback when tool calls are updated (receives array of all tool calls)
 * @param {Function} options.onToolConfirmationRequest - Callback when tool confirmation is requested
 * @param {Function} options.onToolConfirmationResult - Callback when tool confirmation result is received
 * @param {Function} options.onError - Callback when an error occurs (receives Error object)
 * @param {Function} options.onComplete - Callback when stream completes (receives { content, toolCalls })
 * @param {Function} options.onReloadHistory - Callback to reload persisted history (receives conversationId). Used when the reply is durably complete but not replayable from the live stream (already_complete) or when a dropped stream can't be resumed.
 * @returns {Object} { sendStreamingMessage, cancelStream, isStreaming }
 */
export const useStreamingResponse = ({
  onMessage = () => {},
  onToolCall = () => {},
  onToolConfirmationRequest = () => {},
  onToolConfirmationResult = () => {},
  onError = () => {},
  onComplete = () => {},
  onReloadHistory = () => {},
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
      // Highest seq applied from this conversation's stream. Used to resubscribe
      // (?from_seq=) after a dropped stream and as the ack_seq once the turn ends.
      let lastSeq = -1;
      // Detail of the most recent failed (5xx) subscribe attempt, surfaced if
      // every retry is exhausted.
      let lastSubscribeErrorDetail = null;
      // Set once we observe the terminal turn_ended for our turn. If the stream
      // closes without it, the message is interrupted (not cleanly complete).
      let turnEnded = false;
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
            redirectToLogin();
            return;
          }
          const errorData = await startResponse.json().catch(() => ({}));
          throw new Error(errorData.detail || `HTTP error! status: ${startResponse.status}`);
        }

        const {
          conversation_id: streamConversationId,
          first_seq: firstSeq,
          already_complete: alreadyComplete,
        } = await startResponse.json();
        const resolvedConversationId = streamConversationId || conversationId;

        // The turn was resolved from the durable record (turn_id found in the
        // DB but not in the in-memory hub: restart / pruned / evicted). It has
        // already finished and is NOT replayable from the hub, so don't open
        // /stream — reload persisted history to surface the reply.
        if (alreadyComplete) {
          onReloadHistory(resolvedConversationId);
          return;
        }

        // Step 2: subscribe to the conversation event stream and consume it.
        // Returns one of: 'completed' (turn_ended seen, stream done),
        // 'dropped' (server sent stream_dropped — caller may resubscribe), or
        // 'interrupted' (stream closed without turn_ended). `fromSeq` is the
        // seq to (re)subscribe from; `ackSeq` is the highest seq already
        // applied so the hub can mark covered turns delivered.
        const consumeStream = async (fromSeq) => {
          const streamUrl = conversationStreamUrl(resolvedConversationId, {
            fromSeq,
            ackSeq: lastSeq,
          });

          const response = await fetch(streamUrl, {
            method: 'GET',
            headers: {
              Accept: 'text/event-stream',
            },
            signal: abortControllerRef.current.signal,
          });

          if (!response.ok) {
            if (response.status === 401) {
              redirectToLogin();
              return 'completed';
            }
            if (response.status === 410) {
              // The turn's events have rotated out of the hub buffer (or the
              // conversation was evicted). There's nothing to replay, but the
              // reply is durably persisted — reload history to surface it
              // rather than showing an error for a turn that likely succeeded.
              onReloadHistory(resolvedConversationId);
              return 'completed';
            }
            const errorData = await response.json().catch(() => ({}));
            const detail = errorData.detail || `HTTP error! status: ${response.status}`;
            if (response.status >= 500) {
              // A transient server error on the subscribe GET doesn't stop the
              // turn — the producer keeps running and the hub retains the
              // events for replay. Report it as retryable so the caller can
              // resubscribe instead of declaring the whole turn failed.
              lastSubscribeErrorDetail = detail;
              return 'subscribe_failed';
            }
            throw new Error(detail);
          }

          const reader = response.body.getReader();
          const decoder = new TextDecoder();
          // The SSE event name (`event:` line) must persist across read chunks:
          // an event can be split so its `event:` line lands in one chunk and
          // its `data:` line in the next. Reset after each event is handled.
          let currentEventType = null;
          let buffer = '';

          while (true) {
            const { done, value } = await reader.read();
            if (done) {
              break;
            }

            buffer += decoder.decode(value, { stream: true });

            const lines = buffer.split('\n');
            buffer = lines[lines.length - 1]; // Keep incomplete line in buffer

            for (let i = 0; i < lines.length - 1; i++) {
              const line = lines[i].trim();

              if (line.startsWith('event: ')) {
                currentEventType = line.slice(7);
                continue;
              }

              if (!line.startsWith('data: ')) {
                continue;
              }

              const data = line.slice(6);

              // Consume the pending SSE event name for THIS data line and clear
              // the shared one immediately. The name only needs to persist
              // across a chunk boundary until its data line arrives; clearing
              // here means a branch that `continue`s can't leak its type onto
              // the next event.
              const eventType = currentEventType;
              currentEventType = null;

              let payload;
              try {
                payload = JSON.parse(data);
              } catch (e) {
                console.error('Failed to parse SSE event:', e, 'Data:', data);
                continue;
              }

              // The hub dropped this subscriber (queue overflow) or the server
              // is shutting down. Bail out so the caller can resubscribe from
              // lastSeq (or mark the turn interrupted if resume isn't feasible).
              if (eventType === 'stream_dropped') {
                return 'dropped';
              }

              // Track the highest seq we've applied so we can resume after a
              // drop and ack the right position at turn_ended.
              if (typeof payload.seq === 'number' && payload.seq > lastSeq) {
                lastSeq = payload.seq;
              }

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
              // signalled both by the SSE event name and a terminal payload
              // status. Match only the known terminal statuses (not any truthy
              // `status` field) so a tool result that happens to carry a status
              // can't end the stream mid-turn.
              if (
                eventType === 'turn_ended' ||
                payload.status === 'complete' ||
                payload.status === 'failed'
              ) {
                turnEnded = true;
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
                return 'completed';
              }
              // turn_started / heartbeat carry no renderable content.
              if (eventType === 'turn_started' || eventType === 'heartbeat') {
                continue;
              }

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

              // Accumulate per-file `attachment` events for the assistant's
              // response only. The producer republishes the user's own uploads
              // as `attachment` events tagged `source: "trigger"`; those already
              // render on the user bubble, so ignore them here and only collect
              // `source: "response"` (or unset, treated as response defensively).
              // The collected events are materialized into a synthetic
              // attach_to_response tool call at turn_ended, unless the assistant
              // already made a real attach_to_response call.
              if (
                payload.type === 'attachment' &&
                payload.attachment_id &&
                (payload.source === undefined || payload.source === 'response')
              ) {
                autoAttachments.push({
                  type: 'attachment',
                  attachment_id: payload.attachment_id,
                  content_url: payload.content_url,
                  mime_type: payload.mime_type,
                  description: payload.description,
                  size: payload.size,
                });
              }

              // Handle error
              if (payload.error) {
                onError(new Error(payload.error || 'Unknown error'));
              }
            }
          }

          // Stream closed without a stream_dropped or turn_ended frame.
          return turnEnded ? 'completed' : 'interrupted';
        };

        // Subscribe to the turn's event stream, retrying briefly on transient
        // server errors (5xx). The producer keeps running whether or not this
        // subscriber is connected, so failing the whole turn over one failed
        // subscribe would show a spurious error for a reply that completes
        // normally. This mirrors the follow stream's reconnect-on-error
        // behavior. `firstSeq` seeds the very first subscription; retries
        // resume from the last applied seq.
        let outcome = await consumeStream(firstSeq ?? 0);
        for (let retry = 0; outcome === 'subscribe_failed' && retry < 2; retry++) {
          await new Promise((resolve) => setTimeout(resolve, 250 * (retry + 1)));
          outcome = await consumeStream(lastSeq >= 0 ? lastSeq : (firstSeq ?? 0));
        }
        if (outcome === 'subscribe_failed') {
          throw new Error(lastSubscribeErrorDetail || 'Failed to subscribe to the reply stream.');
        }
        // Resubscribe at most once on a dropped stream (queue overflow / server
        // shutdown). A single bounded retry resumes from the last applied seq;
        // if it drops again, fall through to the interrupted handling rather
        // than spinning.
        if (outcome === 'dropped') {
          outcome = await consumeStream(lastSeq >= 0 ? lastSeq : (firstSeq ?? 0));
        }

        // A dropped-then-dropped (or stream that closed mid-turn) means we
        // never saw turn_ended. The reply may still have been persisted, so
        // reload history rather than leaving the assistant bubble stuck
        // loading, and surface that the live render was interrupted.
        if (outcome !== 'completed' && !turnEnded) {
          onReloadHistory(resolvedConversationId);
          onError(new Error('The connection was interrupted before the reply finished.'));
        }
      } catch (error) {
        if (error.name !== 'AbortError') {
          onError(error instanceof Error ? error : new Error(String(error)));
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
      onReloadHistory,
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
