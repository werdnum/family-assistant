import { useCallback, useRef, useState } from 'react';

import { conversationStreamUrl, redirectToLogin } from './conversationStream';

// Resume tuning for a dropped/interrupted reply stream. The producer keeps
// running server-side and the hub replays events from any seq, so a drop is
// resumed rather than failed. Give-up is liveness-aware: only an instant
// drain-and-close that applies nothing counts toward the bound, so a long,
// quiet tool call (held open by the server) streams to completion.
//
// Exported as a mutable object so tests can shrink the timings (e.g. set
// livenessMs to 0) and exercise the give-up/liveness logic without sleeping
// past real wall-clock thresholds. Production code never mutates it.
//
// - initialDelayMs / maxDelayMs: exponential backoff between no-progress closes.
// - livenessMs: a resume held open at least this long proves the turn is still
//   running (follow=false blocks only while live; the edge proxy caps a live
//   connection well above this), so it doesn't count as a no-progress attempt.
//   An instant empty close (a finished/evicted turn) stays well under it.
// - maxNoProgressResumes: consecutive instant no-progress resumes before giving
//   up and reconciling from persisted history.
export const streamResumeTuning = {
  initialDelayMs: 250,
  maxDelayMs: 1000,
  livenessMs: 1500,
  maxNoProgressResumes: 4,
};

/**
 * Hook for handling streaming responses from the chat API
 * @param {Object} options - Hook options
 * @param {Function} options.onMessage - Callback when content is streamed (receives accumulated content)
 * @param {Function} options.onToolCall - Callback when tool calls are updated (receives array of all tool calls)
 * @param {Function} options.onToolConfirmationRequest - Callback when tool confirmation is requested
 * @param {Function} options.onToolConfirmationResult - Callback when tool confirmation result is received
 * @param {Function} options.onError - Callback when an error occurs (receives Error object)
 * @param {Function} options.onComplete - Callback when stream completes (receives { content, toolCalls })
 * @param {Function} options.onUserInput - Callback when a mid-turn steering message is injected into the running turn (receives the message content). Lets the UI render the steering message as a user bubble while the turn continues.
 * @param {Function} options.onCancelled - Callback when the turn ends because the user stopped it (no payload). Distinct from onError: a stop is not a failure, so the UI should mark the bubble "stopped" without an error toast.
 * @param {Function} options.onReloadHistory - Callback to reload persisted history (receives conversationId, options). Used when the reply is durably complete but not replayable from the live stream (already_complete/410) or when a bounded resume gives up. Pass `{ errorIfNoReply: true }` on a give-up with no durable completion signal so the caller surfaces an error if the reconciled turn has no terminal reply.
 * @param {Function} options.onCheckTurnActive - Async (conversationId, turnId) => boolean: whether the server still reports this turn running. Used so a held-open resume leg held open by a concurrent turn doesn't reset the give-up streak.
 * @returns {Object} { sendStreamingMessage, cancelStream, isStreaming }
 */
export const useStreamingResponse = ({
  onMessage = () => {},
  onToolCall = () => {},
  onToolConfirmationRequest = () => {},
  onToolConfirmationResult = () => {},
  onError = () => {},
  onComplete = () => {},
  onUserInput = () => {},
  onCancelled = () => {},
  onReloadHistory = () => {},
  // Async (conversationId, turnId) => boolean: does the server still report THIS
  // turn running? Used to decide whether a held-open resume leg that delivered
  // nothing for our turn is real liveness or a concurrent turn holding the shared
  // conversation stream open. The default keeps the prior held-open-is-liveness
  // behavior when no checker is wired.
  onCheckTurnActive = () => true,
} = {}) => {
  const [isStreaming, setIsStreaming] = useState(false);
  const abortControllerRef = useRef(null);
  // Identity of the turn currently streaming, so cancelStream/steerStream can
  // target the live server-side turn. Set once the turn is kicked off and
  // cleared when the stream settles.
  const activeTurnRef = useRef(null);

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
      // REPLAY cursor: highest seq seen on the conversation stream (ANY turn).
      // Drives ?from_seq= on resume, so it tracks the conversation-wide buffer
      // head and a resume isn't evicted (410) by a concurrent turn's events.
      let lastSeq = -1;
      // ACK cursor: highest seq of OUR turn only. Sent as ack_seq so a resume
      // never acknowledges a concurrent turn's turn_ended seq.
      let ackSeq = -1;
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

      // Record the live turn up front (the turn id and conversation id are
      // already known) so a Stop/Steer click during the kickoff POST — the
      // composer shows Stop as soon as isStreaming flips true — targets this
      // turn instead of no-op'ing. Refined below if the server resolves a
      // different conversation id (only when conversationId was omitted).
      activeTurnRef.current = {
        turnId: effectiveTurnId,
        conversationId,
      };

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
        // Refine the live-turn record now that the server has resolved the
        // conversation id (it generates one when the client omits it).
        activeTurnRef.current = {
          turnId: effectiveTurnId,
          conversationId: resolvedConversationId,
        };

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
            ackSeq,
          });

          let response;
          try {
            response = await fetch(streamUrl, {
              method: 'GET',
              headers: {
                Accept: 'text/event-stream',
              },
              signal: abortControllerRef.current.signal,
            });
          } catch (fetchError) {
            if (fetchError?.name === 'AbortError') {
              throw fetchError;
            }
            // A network error establishing the subscription (DNS, refused,
            // proxy reset before headers). The producer keeps running, so treat
            // it as resumable rather than failing the whole turn.
            return 'subscribe_failed';
          }

          if (!response.ok) {
            if (response.status === 401) {
              redirectToLogin();
              return 'completed';
            }
            if (response.status === 410) {
              // The turn's events have rotated out of the hub buffer (or the
              // conversation was evicted). There's nothing to replay. Usually the
              // reply is durably persisted, so reloading history surfaces it. But
              // a 410 can also fire while the turn is STILL running (its early
              // events were evicted), so reconcile through the unconfirmed-reply
              // path (errorIfNoReply + turnId): if active_turns shows it running
              // we wait for its turn_ended, if a reply is persisted we show it,
              // and only a finished turn with no reply surfaces the marker.
              onReloadHistory(resolvedConversationId, {
                errorIfNoReply: true,
                turnId: effectiveTurnId,
              });
              return 'completed';
            }
            const errorData = await response.json().catch(() => ({}));
            const detail = errorData.detail || `HTTP error! status: ${response.status}`;
            if (response.status >= 500) {
              // A transient server error on the subscribe GET doesn't stop the
              // turn — the producer keeps running and the hub retains the
              // events for replay. Report it as resumable so the caller resumes
              // instead of declaring the whole turn failed.
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
            let readResult;
            try {
              readResult = await reader.read();
            } catch (readError) {
              if (readError?.name === 'AbortError') {
                throw readError;
              }
              // A proxy reset / network drop errors the read instead of yielding
              // a clean EOF or a stream_dropped frame. Treat it as a resumable
              // interruption so the loop resumes from lastSeq + 1 rather than
              // surfacing a spurious error for a turn that is still running.
              return 'interrupted';
            }
            const { done, value } = readResult;
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

              // REPLAY cursor: highest seq seen on the stream, for ANY turn.
              // Resume continues from lastSeq + 1, so it must track the
              // conversation-wide buffer head — advancing it for ignored
              // concurrent-turn events too. Otherwise our from_seq stays pinned
              // behind those seqs and the bounded buffer can evict it, making the
              // next resume 410 and treat this (possibly still-running) turn as
              // gone. Re-skipping the other turn's events below is cheap.
              if (typeof payload.seq === 'number' && payload.seq > lastSeq) {
                lastSeq = payload.seq;
              }

              // The conversation stream carries events for every turn in the
              // conversation. In this send-and-watch flow we only care about
              // the turn we just started: ignore events tagged with a
              // different turn_id (e.g. a turn started concurrently from
              // another tab/device). Connection-level events (heartbeat,
              // stream_dropped, message) carry no turn_id and fall through for
              // rendering decisions, but they must not advance ackSeq below.
              if (payload.turn_id && payload.turn_id !== effectiveTurnId) {
                continue;
              }

              // ACK cursor: highest seq of OUR turn only. ack_seq is sent from
              // this, so a resume never acknowledges a concurrent turn's
              // turn_ended seq (which would make the backend suppress that turn's
              // disconnect push for a reply this client never handled).
              if (
                payload.turn_id === effectiveTurnId &&
                typeof payload.seq === 'number' &&
                payload.seq > ackSeq
              ) {
                ackSeq = payload.seq;
              }

              // The turn_ended event terminates this turn's stream. Done is
              // signalled both by the SSE event name and a terminal payload
              // status. Match only the known terminal statuses (not any truthy
              // `status` field) so a tool result that happens to carry a status
              // can't end the stream mid-turn.
              if (
                eventType === 'turn_ended' ||
                payload.status === 'complete' ||
                payload.status === 'failed' ||
                payload.status === 'cancelled'
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
                // A user-requested stop ends the turn as 'cancelled'. That's not
                // an error — surface it via onCancelled so the UI marks the
                // bubble "stopped" without an error toast. A genuine failure
                // ('failed', or any terminal event carrying an error) still
                // routes through onError.
                if (payload.status === 'cancelled') {
                  onCancelled();
                } else if (payload.status === 'failed' || payload.error) {
                  onError(new Error(payload.error || 'The assistant stopped unexpectedly.'));
                }
                return 'completed';
              }
              // turn_started / heartbeat carry no renderable content.
              if (eventType === 'turn_started' || eventType === 'heartbeat') {
                continue;
              }

              // A mid-turn steering message the user sent while this turn was
              // running. Render it as a user bubble; the turn continues. Handled
              // before the generic `payload.content` text branch below so it
              // isn't appended to the assistant's streamed reply.
              if (eventType === 'user_input' || payload.type === 'user_input') {
                if (payload.content) {
                  onUserInput(payload.content);
                }
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

        // `firstSeq` seeds the very first subscription. The hub replays events
        // with seq >= from_seq, so a resume uses lastSeq + 1 to continue just
        // past the last applied event without re-applying it.
        const resumeFromLastSeq = () => consumeStream(lastSeq >= 0 ? lastSeq + 1 : (firstSeq ?? 0));

        // Subscribe, then RESUME the stream on any non-clean exit (a proxy-cut
        // EOF, a hub stream_dropped, or a transient 5xx subscribe). The producer
        // keeps running server-side regardless of this subscriber and the hub
        // retains the events, so resuming continues the live token stream (the
        // final text and turn_ended) exactly where it left off — a drop after
        // tool calls finishes cleanly instead of surfacing a spurious error.
        //
        // Give-up is liveness-aware: a resume that applies a new seq OR is held
        // open for at least the liveness window is proof the turn is still
        // running, so it resets the no-progress streak. Only an instant
        // drain-and-close that applies nothing — how follow=false reports a
        // finished or evicted turn — counts toward the bound. This keeps a long,
        // quiet tool call streaming to completion while still bounding a
        // truly-gone turn. Modeled on the iOS client's resume loop.
        //
        // A held-open leg therefore keeps the loop alive indefinitely while the
        // server still reports the turn as running (follow=false closes promptly
        // once no turn is running, so a finished/crashed producer yields instant
        // closes and is bounded). The bubble's spinner honestly reflects "still
        // working", and the user can cancel — preferred over giving up on a slow
        // legitimate tool call.
        let outcome = await consumeStream(firstSeq ?? 0);
        let noProgressResumes = 0;
        let resumeDelayMs = streamResumeTuning.initialDelayMs;
        while (
          !turnEnded &&
          (outcome === 'dropped' || outcome === 'interrupted' || outcome === 'subscribe_failed') &&
          noProgressResumes < streamResumeTuning.maxNoProgressResumes
        ) {
          if (noProgressResumes > 0) {
            await new Promise((resolve) => setTimeout(resolve, resumeDelayMs));
            if (abortControllerRef.current?.signal.aborted) {
              return;
            }
          }
          const ackBeforeResume = ackSeq;
          const resumeStartedAt = Date.now();
          outcome = await resumeFromLastSeq();
          // Progress is measured by OUR turn's ack cursor, not the conversation
          // -wide lastSeq: events from a concurrent turn (another tab) advance
          // lastSeq but are not proof THIS turn is alive.
          const ourTurnProgressed = ackSeq !== ackBeforeResume;
          // A leg that subscribed (got 200) and stayed open past the liveness
          // window proves the conversation's stream is live — but the backend
          // holds a non-follow stream open while ANY of the user's turns in the
          // conversation is running, not just ours. So a held-open leg that
          // delivered nothing for OUR turn is ambiguous: our own quiet (long tool
          // call) turn, or a concurrent turn in another tab. Only count it as
          // liveness if the server still reports OUR turn running; otherwise this
          // is a gone turn that must progress toward give-up rather than spin
          // until the unrelated turn finishes. (A slow 5xx subscribe_failed is
          // never liveness — it must keep counting toward give-up.)
          const heldOpen =
            outcome !== 'subscribe_failed' &&
            Date.now() - resumeStartedAt >= streamResumeTuning.livenessMs;
          let stillLive = ourTurnProgressed;
          if (!stillLive && heldOpen) {
            stillLive = await onCheckTurnActive(resolvedConversationId, effectiveTurnId);
          }
          if (stillLive) {
            noProgressResumes = 0;
            resumeDelayMs = streamResumeTuning.initialDelayMs;
          } else {
            noProgressResumes += 1;
            resumeDelayMs = Math.min(resumeDelayMs * 2, streamResumeTuning.maxDelayMs);
          }
        }

        // Bounded resume gave up without observing turn_ended. We have NO
        // durable proof the reply was committed: a dropped/interrupted give-up
        // (stream_dropped is also emitted on server shutdown / queue overflow
        // before turn_ended) and a persistent 5xx subscribe are both ambiguous.
        // So reconcile from persisted history and have the caller surface an
        // error IF the reconciled turn produced no terminal reply — showing the
        // reply when it did persist, an error when it did not, never silently
        // clearing the bubble. (Durable signals — turn_ended status=complete via
        // the loop exit, status=failed / explicit error events, 410, and
        // already_complete — are all handled before reaching here, and reconcile
        // without this flag where appropriate.)
        if (!turnEnded && outcome !== 'completed') {
          // A true give-up: the resume loop was exhausted. errorOnFailedReload
          // tells the client to surface the "couldn't confirm" marker even if the
          // reconcile fetch itself fails — we have tried hard and the optimistic
          // partial (if any) is not proof the reply finished. (A 410 reconcile,
          // by contrast, did NOT exhaust the loop and routes without this flag.)
          onReloadHistory(resolvedConversationId, {
            errorIfNoReply: true,
            turnId: effectiveTurnId,
            errorOnFailedReload: true,
          });
        }
      } catch (error) {
        if (error.name !== 'AbortError') {
          onError(error instanceof Error ? error : new Error(String(error)));
        }
      } finally {
        setIsStreaming(false);
        onComplete({ content: currentMessage, toolCalls });
        abortControllerRef.current = null;
        activeTurnRef.current = null;
      }
    },
    [
      onMessage,
      onToolCall,
      onToolConfirmationRequest,
      onToolConfirmationResult,
      onError,
      onComplete,
      onUserInput,
      onCancelled,
      onReloadHistory,
      onCheckTurnActive,
    ]
  );

  // Detach THIS browser from the live stream without touching the server-side
  // turn. Used when navigating away (switching conversation, new chat): the
  // producer keeps running and the turn stays resumable. NOT the Stop button —
  // that is stopTurn below.
  const cancelStream = useCallback(() => {
    if (abortControllerRef.current) {
      abortControllerRef.current.abort();
    }
  }, []);

  // Stop the running turn server-side (the "Stop generating" button). The
  // backend halts the producer (graceful interrupt then hard cancel) and ends
  // the turn as 'cancelled'. We deliberately do NOT abort the local stream: the
  // authoritative turn_ended(cancelled) event then arrives over SSE and drives
  // onCancelled, so the bubble settles from the server's truth rather than a
  // local race. Best-effort: a failed cancel POST must not throw to the caller.
  const stopTurn = useCallback(async () => {
    const active = activeTurnRef.current;
    if (!active) {
      return;
    }
    const url = `/api/v1/chat/turns/${encodeURIComponent(active.turnId)}/cancel`;
    const body = JSON.stringify({ conversation_id: active.conversationId });
    // Stop must fully secure the turn (the server also rejects the turn's
    // pending tool confirmations, returning 503 if it can't). Retry a transient
    // 5xx/network failure a few times; the server side is idempotent.
    for (let attempt = 0; attempt < 3; attempt++) {
      try {
        const response = await fetch(url, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body,
        });
        if (response.status === 401) {
          redirectToLogin();
          return;
        }
        if (response.ok) {
          return;
        }
        if (response.status < 500) {
          console.warn(`Turn cancel failed: HTTP ${response.status}`);
          return;
        }
        console.warn(`Turn cancel failed (attempt ${attempt + 1}): HTTP ${response.status}`);
      } catch (e) {
        console.warn(`Turn cancel request failed (attempt ${attempt + 1}):`, e);
      }
    }
  }, []);

  // Inject a steering message into the running turn without starting a new one.
  // Returns true if accepted. On 409 (turn already finished / not steerable) the
  // caller should fall back to sending a normal new message. Returns false for
  // "no active turn" so the caller can do the same.
  const steerStream = useCallback(async ({ prompt }) => {
    const active = activeTurnRef.current;
    if (!active) {
      return false;
    }
    try {
      const response = await fetch(
        `/api/v1/chat/turns/${encodeURIComponent(active.turnId)}/steer`,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            conversation_id: active.conversationId,
            prompt,
          }),
        }
      );
      if (response.status === 401) {
        redirectToLogin();
        return false;
      }
      if (response.status === 409) {
        // Turn finished between the user typing and submitting; not steerable.
        return false;
      }
      if (!response.ok) {
        console.warn(`Turn steer failed: HTTP ${response.status}`);
        return false;
      }
      return true;
    } catch (e) {
      console.warn('Turn steer request failed:', e);
      return false;
    }
  }, []);

  return {
    sendStreamingMessage,
    cancelStream,
    stopTurn,
    steerStream,
    isStreaming,
  };
};
