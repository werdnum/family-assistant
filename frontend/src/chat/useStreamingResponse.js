import { useCallback, useRef, useState } from 'react';

import { generateUUID } from '../utils/uuid';
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
 * @param {Function} [options.onMessage] - Callback when content is streamed (receives accumulated content)
 * @param {Function} [options.onToolCall] - Callback when tool calls are updated (receives array of all tool calls)
 * @param {Function} [options.onToolConfirmationRequest] - Callback when tool confirmation is requested
 * @param {Function} [options.onToolConfirmationResult] - Callback when tool confirmation result is received
 * @param {Function} [options.onError] - Callback when an error occurs (receives Error object)
 * @param {Function} [options.onComplete] - Callback when stream completes (receives { content, toolCalls, completed, turnId, streamedTurnId }). `turnId` is the id this send was kicked off with, so caller-side bookkeeping keyed at send time still matches; `streamedTurnId` is the turn actually followed, which differs only when a refused send adopted the conversation's running turn.
 * @param {Function} [options.onUserInput] - Callback when a mid-turn steering message is injected into the running turn (receives the message content and the submitting client's `input_id`, or null when it sent none). Lets the UI render the steering message as a user bubble while the turn continues, and tells a sender that this turn consumed its own submission.
 * @param {Function} [options.onCancelled] - Callback when the turn ends because the user stopped it (no payload). Distinct from onError: a stop is not a failure, so the UI should mark the bubble "stopped" without an error toast.
 * @param {Function} [options.onReloadHistory] - Callback to reload persisted history (receives conversationId, options). Used when the reply is durably complete but not replayable from the live stream (already_complete/410) or when a bounded resume gives up. Pass `{ errorIfNoReply: true }` on a give-up with no durable completion signal so the caller surfaces an error if the reconciled turn has no terminal reply.
 * @param {Function} [options.onCheckTurnActive] - Async (conversationId, turnId) => boolean: whether the server still reports this turn running. Used so a held-open resume leg held open by a concurrent turn doesn't reset the give-up streak.
 * @returns {{sendStreamingMessage: Function, cancelStream: Function, stopTurn: Function, steerStream: Function, isStreaming: boolean}}
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
  // Set once the user asks to stop the live turn, cleared when a new send
  // begins. A send still placing itself when Stop lands has no stream to carry
  // the turn_ended(cancelled) event, so this is the only record that the user
  // asked for the interaction to end.
  const stopRequestedRef = useRef(false);
  // The one identity change a request in flight is allowed to follow: a kickoff
  // refused with 409 continues as the conversation's running turn. Recorded as
  // the exact pair so a retry follows THAT transition and nothing else — a
  // later, unrelated turn in the same conversation must not inherit a Stop.
  const adoptionRedirectRef = useRef(null);

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
      stopRequestedRef.current = false;
      adoptionRedirectRef.current = null;
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

      // The echo of a prompt delivered to an adopted turn, cleared once that
      // echo arrives. Declared out here so the completion report below can see
      // whether it ever did: a turn that was already streaming its final,
      // tool-free response never drains what the steer queued, and an adopted
      // prompt left unconsumed at turn_ended has to be resent or it is simply
      // lost. `{ prompt, afterSeq }` — see the adoption branch.
      let pendingAdoptedEcho = null;
      // Set when a message reached nothing at all: the kickoff was refused and
      // the steer it was rerouted to was then rejected outright. The caller
      // resends it unconditionally — unlike the cases below there is no doubt
      // about delivery, so a resend cannot duplicate it.
      let undeliveredPrompt = null;
      // True once the turn's event stream has been opened at least once. Lets
      // the completion report distinguish a send that died during setup (the
      // kickoff POST failed) from one that was detached mid-stream — both end
      // with completed: false, but only the first means nothing is running
      // server-side and the caller's queue should keep moving.
      let streamStarted = false;
      // Set when the stream stopped following this turn without ever seeing it
      // end — a bounded resume gave up, or the hub had evicted its events (410).
      // Persisted history becomes the record of what happened, so anything the
      // client was still waiting to observe on the stream will never be observed.
      let reconciledWithoutEnd = false;

      // Client-generated turn id makes the kickoff idempotent: a retried POST
      // with the same id returns the existing turn instead of starting a
      // second producer.
      // Reassigned only when the server refuses this turn because the
      // conversation already has one running and we adopt that turn instead.
      const kickoffTurnId =
        turnId ||
        (typeof crypto !== 'undefined' && crypto.randomUUID
          ? crypto.randomUUID()
          : `turn_${Date.now()}_${Math.random().toString(36).slice(2)}`);
      let effectiveTurnId = kickoffTurnId;

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

        // A conversation runs one turn at a time, so the server refuses this one
        // if it already has a running turn (409) and names the turn instead. We
        // only get here having believed nothing was running — the composer falls
        // back to a plain send when it has lost its turn handle (its stream
        // dropped, the tab was hidden and resumed) — so this is that same
        // message arriving at the wrong door. Deliver it to the running turn as
        // a steer and follow that turn, rather than failing in front of the user.
        let adoptedSteer = null;
        if (startResponse.status === 409) {
          const conflict = await startResponse.json().catch(() => ({}));
          const runningTurnId = conflict?.detail?.active_turn_id;
          // Steering carries text only, so a refused send that had files cannot
          // be adopted: the running turn would answer a prompt whose
          // attachments were never uploaded or associated with the history.
          // Surface it as a failed send — which names the running turn as the
          // reason and asks for a resend — rather than quietly delivering a
          // message with its files stripped off.
          if (runningTurnId && attachments?.length) {
            const refusal = new Error(
              'This conversation is still working on the previous message, so the ' +
                'attachments could not be sent. Wait for it to finish, then send them again.'
            );
            // Tells the caller to show this text as-is. It is an instruction to
            // the user, not a fault report, so replacing it with the generic
            // "I encountered an error" line would hide the only thing that says
            // what to do — and point at diagnostics for a non-bug.
            refusal.userFacing = true;
            throw refusal;
          }
          if (runningTurnId) {
            adoptedSteer = {
              turnId: runningTurnId,
              // Subscribe from where the running turn began, not seq 0, so its
              // own events replay without dragging in earlier turns'.
              fromSeq: conflict.detail.active_turn_first_seq ?? 0,
              prompt,
            };
          }
        }

        if (!startResponse.ok && !adoptedSteer) {
          if (startResponse.status === 401) {
            redirectToLogin();
            return;
          }
          const errorData = await startResponse.json().catch(() => ({}));
          throw new Error(errorData.detail || `HTTP error! status: ${startResponse.status}`);
        }

        let resolvedConversationId;
        let firstSeq;
        // Set only on the normal kickoff path; an adopted turn is live in the
        // hub by definition, so it is never resolved from the durable record.
        let durableTurnState = null;
        // `pendingAdoptedEcho` (declared above the try) is the one steer echo to
        // swallow, because the caller already rendered this prompt optimistically
        // as a normal send. afterSeq is the stream head when the steer was
        // queued, so the echo is published above it.
        if (adoptedSteer) {
          effectiveTurnId = adoptedSteer.turnId;
          resolvedConversationId = conversationId;
          activeTurnRef.current = {
            turnId: effectiveTurnId,
            conversationId: resolvedConversationId,
          };
          adoptionRedirectRef.current = {
            from: kickoffTurnId,
            to: effectiveTurnId,
            conversationId: resolvedConversationId,
          };
          // The text has to come back with any ambiguous failure: the kickoff
          // was refused, the composer has cleared, and nothing persisted this
          // prompt, so an error without it leaves the message only in a bubble
          // that a refresh discards.
          const ambiguousDeliveryError = () => {
            const ambiguous = new Error(
              "Your message may not have reached the assistant, and it's still working on the " +
                'previous one. Check the reply, then send it again if it was missed:\n\n' +
                `> ${adoptedSteer.prompt}`
            );
            ambiguous.userFacing = true;
            return ambiguous;
          };
          const steerUrl = `/api/v1/chat/turns/${encodeURIComponent(effectiveTurnId)}/steer`;
          // Identifies this submission on the echo the turn publishes when it
          // consumes the message, so the echo is recognised as ours rather than
          // by matching text that another client could have sent too.
          const steerInputId = generateUUID();
          let steerResponse;
          try {
            steerResponse = await fetch(steerUrl, {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({
                conversation_id: resolvedConversationId,
                prompt: adoptedSteer.prompt,
                input_id: steerInputId,
              }),
              signal: abortControllerRef.current.signal,
            });
          } catch (steerFetchError) {
            if (steerFetchError?.name === 'AbortError') {
              throw steerFetchError;
            }
            // A dropped connection is as ambiguous as a 5xx — the request may
            // have reached the server — and it is the likeliest failure here,
            // since a flaky connection is what makes a client lose its turn
            // handle and land on this path at all.
            throw ambiguousDeliveryError();
          }
          if (!steerResponse.ok) {
            if (steerResponse.status === 401) {
              redirectToLogin();
              return;
            }
            if (steerResponse.status === 404 || steerResponse.status === 409) {
              // The turn finished in the gap between the kickoff's 409 and this
              // steer, so it is definitively gone. The prompt reached nothing:
              // the kickoff was refused, the steer was rejected, and the
              // composer has already cleared. Report it as undelivered so the
              // caller starts it as its own turn — the conversation is idle now,
              // so that kickoff will be accepted. Throwing here would leave the
              // message existing nowhere at all.
              //
              // Unless the user pressed Stop while this send was still placing
              // itself: the cancel is why the turn is gone, and resending would
              // restart the interaction they just ended. No stream ever opened
              // here, so no turn_ended(cancelled) will say so — only this flag.
              if (!stopRequestedRef.current) {
                undeliveredPrompt = adoptedSteer.prompt;
              }
              return;
            }
            // A 5xx is ambiguous — the steer may have been queued before the
            // response failed — so it must NOT be auto-resent.
            throw ambiguousDeliveryError();
          }
          const steerBody = await steerResponse.json().catch(() => ({}));
          pendingAdoptedEcho = {
            prompt: adoptedSteer.prompt,
            inputId: steerInputId,
            // Absent (an older backend): fall back to the turn's start, which
            // restores the previous replay-the-whole-turn behaviour.
            afterSeq:
              typeof steerBody.queued_after_seq === 'number'
                ? steerBody.queued_after_seq
                : adoptedSteer.fromSeq - 1,
          };
          // Follow the adopted turn from just after our message was queued, NOT
          // from where the turn began. The turn has usually been working for a
          // while — its earlier text and tool calls would replay into the bubble
          // the composer just opened for THIS message, showing the previous
          // answer again underneath it (and twice over, when history had already
          // rendered that turn before the tab was resumed). What the turn does
          // in response to this message starts here; the rest is reconciled from
          // persisted history when the turn ends.
          firstSeq = pendingAdoptedEcho.afterSeq + 1;
        } else {
          const {
            conversation_id: streamConversationId,
            first_seq: startFirstSeq,
            already_complete: alreadyComplete,
            incomplete: durableIncomplete,
          } = await startResponse.json();
          resolvedConversationId = streamConversationId || conversationId;
          firstSeq = startFirstSeq;
          durableTurnState = { alreadyComplete, durableIncomplete };
          // Refine the live-turn record now that the server has resolved the
          // conversation id (it generates one when the client omits it).
          activeTurnRef.current = {
            turnId: effectiveTurnId,
            conversationId: resolvedConversationId,
          };
        }

        // The turn was resolved from the durable record (turn_id found in the
        // DB but not in the in-memory hub: restart / pruned / evicted). It is
        // NOT replayable from the hub, so don't open /stream — reload persisted
        // history. If the durable record has no reply (incomplete: the turn was
        // interrupted by a crash/restart mid-turn), surface a recovery error
        // instead of silently showing the prompt alone.
        if (durableTurnState?.alreadyComplete) {
          if (durableTurnState.durableIncomplete) {
            onReloadHistory(resolvedConversationId, {
              errorIfNoReply: true,
              turnId: effectiveTurnId,
              errorOnFailedReload: true,
            });
          } else {
            onReloadHistory(resolvedConversationId);
          }
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
              reconciledWithoutEnd = true;
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
                  // A prompt we sent as a plain message and then re-routed into
                  // the running turn echoes back here, but the caller already
                  // rendered it optimistically when the send began. Swallow that
                  // one echo so it doesn't appear twice; every other input on
                  // this turn — later steers, and anything another client sent —
                  // carries a different id and still shows.
                  //
                  // An echo with no id at all comes from a backend that predates
                  // the field (the same skew `afterSeq` covers above). Fall back
                  // to the old text-and-cursor match there: failing to swallow it
                  // would hold back the whole adopted reply and resend the prompt.
                  const isOurAdoptedEcho =
                    pendingAdoptedEcho !== null &&
                    (payload.input_id
                      ? payload.input_id === pendingAdoptedEcho.inputId
                      : payload.content.trim() === pendingAdoptedEcho.prompt.trim() &&
                        typeof payload.seq === 'number' &&
                        payload.seq > pendingAdoptedEcho.afterSeq);
                  if (isOurAdoptedEcho) {
                    pendingAdoptedEcho = null;
                  } else {
                    onUserInput(payload.content, payload.input_id ?? null);
                  }
                }
                continue;
              }

              // Everything below renders into the assistant bubble the composer
              // opened for THIS message. While an adopted turn has not yet
              // echoed our prompt it is still finishing the response that was
              // already in flight — `queued_after_seq` marks when the steer was
              // queued, not when the turn got to it — so those events belong to
              // the previous answer and would appear beneath a message they do
              // not answer. Skip them; the turn's real reply starts after the
              // echo. If turn_ended arrives first the prompt was never consumed
              // at all, and the caller resends it (see unconsumedAdoptedPrompt)
              // and reconciles the old turn from persisted history.
              if (pendingAdoptedEcho !== null) {
                // Errors and tool confirmations still pass: a confirmation is a
                // live prompt whose tool stalls without an answer, and an error
                // is never silently dropped.
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
                if (payload.request_id !== undefined && payload.approved !== undefined) {
                  onToolConfirmationResult({
                    request_id: payload.request_id,
                    approved: payload.approved,
                  });
                }
                if (payload.error) {
                  onError(new Error(payload.error || 'Unknown error'));
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
                // `tool_result` (not the event's own `attachment`) so the entry
                // satisfies isAttachment() and DynamicToolUI forwards it to the
                // attachments tool UI. An unrecognized type is dropped there,
                // which sent that UI down its metadata-refetch fallback: one full
                // GET of every attachment just to read its response headers.
                autoAttachments.push({
                  type: 'tool_result',
                  attachment_id: payload.attachment_id,
                  content_url:
                    payload.content_url ||
                    `/api/attachments/${encodeURIComponent(payload.attachment_id)}`,
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
        streamStarted = true;
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
          reconciledWithoutEnd = true;
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
        // `completed` distinguishes a turn we saw end (turn_ended observed) from
        // a local detach (e.g. cancelStream on navigation) where the server turn
        // keeps running. Steer recovery keys off this so a local abort doesn't
        // resend a steer the original turn may still drain.
        onComplete({
          content: currentMessage,
          toolCalls,
          completed: turnEnded,
          // The KICKOFF id, not the streamed one: the caller keyed this send's
          // optimistic conversation row by the id it passed in (or, for a
          // generated id, tracks nothing), so adopting a running turn must not
          // move the key out from under it and strand the row for the session.
          // The turn actually followed is reported separately.
          turnId: kickoffTurnId,
          streamedTurnId: effectiveTurnId,
          // Still set means the adopted turn ended without ever consuming this
          // prompt — it was already streaming a final, tool-free response when
          // the steer landed, so the loop broke before its drain. Nothing else
          // tracks it (the caller's awaiting-echo list only covers steers it
          // sent itself), so the caller must resend it as a new turn or the
          // message is lost — the exact failure this whole change is about.
          unconsumedAdoptedPrompt: pendingAdoptedEcho?.prompt ?? null,
          // A prompt that reached nothing: recover it whatever the outcome,
          // since no turn ever saw it and no stream was followed.
          undeliveredPrompt,
          // The send failed before any stream was followed, so there is no
          // server-side turn to wait on — terminal for this send, unlike a
          // local detach.
          kickoffFailed: !streamStarted && !turnEnded,
          // True when this send followed a turn it adopted rather than one it
          // started. The stream withholds that turn's output until it echoes
          // our prompt, so its earlier tail is missing from the thread and has
          // to be reconciled from persisted history when the stream ends.
          adopted: effectiveTurnId !== kickoffTurnId,
          // The stream stopped following this turn without seeing it end, so
          // nothing it was still waiting to observe (a steer's echo) ever will
          // be. The caller has to settle that here rather than carry it into an
          // unrelated later turn.
          reconciledWithoutEnd,
        });
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
  // Resolves true if the server acknowledged the stop (so the turn — and any
  // pending tool confirmations — is secured), false if it could not be secured
  // after retries. The caller should surface a false so the user knows a pending
  // approval may still be live.
  const stopTurn = useCallback(async () => {
    stopRequestedRef.current = true;
    let active = activeTurnRef.current;
    if (!active) {
      return true;
    }
    // Stop must fully secure the turn (the server also rejects the turn's
    // pending tool confirmations, returning 503 if it can't). Retry with a small
    // backoff on transient 5xx/network failures AND on 404: clicking Stop right
    // after Send can race the kickoff POST that registers the turn in the hub,
    // and the turn doesn't exist there for a brief window. The server side is
    // idempotent.
    const attempts = 5;
    for (let attempt = 0; attempt < attempts; attempt++) {
      // Re-read the live identity every attempt rather than capturing it once:
      // a kickoff still in flight when Stop was clicked can be refused (409)
      // and adopt the conversation's running turn, which repoints this ref.
      // Cancelling the client-generated id we started with would then 404
      // forever while the real turn kept running.
      //
      // Follow ONLY that adoption, identified by the exact turn pair. Following
      // the live ref generally would be wrong twice over: the user can switch
      // conversations during the backoff, and — even in this conversation — the
      // turn being cancelled can finish and the user start another, which a
      // pending Stop would then cancel instead. Keep the last known identity in
      // both cases, and when the ref has been cleared (the stream settled).
      const redirect = adoptionRedirectRef.current;
      if (
        redirect &&
        redirect.from === active.turnId &&
        redirect.conversationId === active.conversationId
      ) {
        active = { turnId: redirect.to, conversationId: redirect.conversationId };
      }
      const url = `/api/v1/chat/turns/${encodeURIComponent(active.turnId)}/cancel`;
      const body = JSON.stringify({ conversation_id: active.conversationId });
      let retryable = false;
      try {
        const response = await fetch(url, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body,
        });
        if (response.status === 401) {
          redirectToLogin();
          return false;
        }
        if (response.ok) {
          return true;
        }
        // 404: the kickoff may not have registered the turn yet — retry briefly.
        // Other 4xx are permanent client errors; 5xx are transient.
        retryable = response.status === 404 || response.status >= 500;
        if (!retryable) {
          console.warn(`Turn cancel failed: HTTP ${response.status}`);
          return false;
        }
        console.warn(`Turn cancel failed (attempt ${attempt + 1}): HTTP ${response.status}`);
      } catch (e) {
        retryable = true;
        console.warn(`Turn cancel request failed (attempt ${attempt + 1}):`, e);
      }
      if (retryable && attempt < attempts - 1) {
        await new Promise((resolve) => setTimeout(resolve, 150 * (attempt + 1)));
      }
    }
    return false;
  }, []);

  // Inject a steering message into the running turn. Returns a status:
  //  - 'accepted'  : queued (200); the caller keeps the draft until echoed.
  //  - 'finished'  : 409 / no active turn — steer the wrong target; the caller
  //                  may safely resend it as a normal follow-up.
  //  - 'error'     : transient failure (5xx/network). The turn may still be
  //                  running and the steer may even have been accepted, so the
  //                  caller must NOT auto-resend; surface it and keep the draft.
  const steerStream = useCallback(async ({ prompt, inputId }) => {
    const active = activeTurnRef.current;
    if (!active) {
      return 'finished';
    }
    // Retry 404 (the kickoff POST may not have registered the turn yet — same
    // race as Stop) and transient 5xx/network with a small backoff. A 404 that
    // persists means the turn really finished → 'finished' (resend as a normal
    // message); a persistent 5xx/network is 'error' (don't auto-resend).
    const attempts = 5;
    for (let attempt = 0; attempt < attempts; attempt++) {
      // Deliberately pinned to the turn this steer was submitted against, NOT
      // re-read from activeTurnRef the way Stop is. The ref moves on for
      // reasons that have nothing to do with adoption: a turn ending drains the
      // recovery queue, which starts a NEW turn carrying this very prompt as
      // its kickoff — following the ref there would steer the same text into
      // the turn already sending it, and the user would see it twice.
      //
      // The cost is that a steer submitted while a kickoff is still in flight
      // 404s to exhaustion and comes back 'finished', so the caller resends it
      // as a normal follow-up. The message still gets through; it just arrives
      // as its own turn instead of steering the running one.
      const url = `/api/v1/chat/turns/${encodeURIComponent(active.turnId)}/steer`;
      // The same input_id on every attempt: a retry after an ambiguous failure
      // must be recognisable as the same submission, not a second one.
      const body = JSON.stringify({
        conversation_id: active.conversationId,
        prompt,
        input_id: inputId,
      });
      let lastWas404 = false;
      try {
        const response = await fetch(url, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body,
        });
        if (response.status === 401) {
          redirectToLogin();
          return 'error';
        }
        if (response.ok) {
          return 'accepted';
        }
        if (response.status === 409) {
          // Turn finished between typing and submitting; not steerable.
          return 'finished';
        }
        if (response.status === 404) {
          lastWas404 = true;
        } else if (response.status < 500) {
          console.warn(`Turn steer failed: HTTP ${response.status}`);
          return 'error';
        } else {
          console.warn(`Turn steer failed (attempt ${attempt + 1}): HTTP ${response.status}`);
        }
      } catch (e) {
        console.warn(`Turn steer request failed (attempt ${attempt + 1}):`, e);
      }
      if (attempt < attempts - 1) {
        await new Promise((resolve) => setTimeout(resolve, 150 * (attempt + 1)));
      } else if (lastWas404) {
        // The turn never registered before we gave up: treat as finished.
        return 'finished';
      }
    }
    return 'error';
  }, []);

  return {
    sendStreamingMessage,
    cancelStream,
    stopTurn,
    steerStream,
    isStreaming,
  };
};
