import { useCallback, useEffect, useRef, useState } from 'react';

import { conversationStreamUrl } from './conversationStream';

export interface LiveMessageUpdate {
  internal_id: string;
  timestamp: string;
  new_messages: boolean;
  role?: string;
  content?: string;
  conversation_id?: string;
}

export interface UseLiveMessageUpdatesOptions {
  conversationId: string | null;
  interfaceType?: string;
  enabled?: boolean;
  onMessageReceived?: (update: LiveMessageUpdate) => void;
}

/**
 * Hook to establish SSE connection for live message updates
 */
export function useLiveMessageUpdates({
  conversationId,
  interfaceType = 'web',
  enabled = true,
  onMessageReceived,
}: UseLiveMessageUpdatesOptions) {
  const eventSourceRef = useRef<EventSource | null>(null);
  const reconnectTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const callbackRef = useRef(onMessageReceived);
  const [reconnectTrigger, setReconnectTrigger] = useState(0);
  // Highest seq this follow stream has acked for the current conversation.
  // Passed as ?ack_seq= on (re)subscribe so the hub can mark covered turns
  // delivered (suppressing the redundant disconnect push), and updated after
  // each turn_ended ack. Reset when the conversation changes.
  const ackedSeqRef = useRef<number>(-1);

  // Update callback ref when it changes
  useEffect(() => {
    callbackRef.current = onMessageReceived;
  }, [onMessageReceived]);

  // Reset the acked-seq baseline when switching conversations. Keyed only on
  // conversationId (not reconnectTrigger) so a reconnect keeps the prior ack
  // position rather than re-acking from scratch.
  useEffect(() => {
    ackedSeqRef.current = -1;
  }, [conversationId]);

  const cleanup = useCallback(() => {
    if (eventSourceRef.current) {
      eventSourceRef.current.close();
      eventSourceRef.current = null;
    }
    if (reconnectTimeoutRef.current) {
      clearTimeout(reconnectTimeoutRef.current);
      reconnectTimeoutRef.current = null;
    }
  }, []);

  useEffect(() => {
    // Don't connect if disabled or no conversation ID
    if (!enabled || !conversationId) {
      cleanup();
      return undefined;
    }

    // Wait for page load to complete before connecting SSE
    // This ensures the page reaches "networkidle" state in tests
    const connect = () => {
      // EventSource is not available in Node.js test environment
      if (typeof EventSource === 'undefined') {
        return;
      }

      // Subscribe to the conversation event stream in follow mode so the
      // connection stays open across turns and surfaces turns started from
      // other devices/tabs. (The send-and-watch flow uses a separate
      // follow=false subscription via useStreamingResponse.)
      // follow=true keeps the connection open across turns and surfaces turns
      // started from other devices/tabs. Tail from the current head
      // (from_seq=-1): a follow-only listener wants future events, not a replay
      // — subscribing from 0 would 410 once the conversation's hub buffer has
      // rotated, trapping EventSource in a permanent reconnect loop.
      //
      // event_types=message,turn_ended: this listener only reloads history on
      // those frames, so skip the token firehose (the server still always
      // delivers heartbeat/stream_dropped/turn_ended control frames).
      //
      // ack_seq: tell the hub the highest seq we've already acked so it can
      // suppress the redundant disconnect push for turns we've seen.
      const url = conversationStreamUrl(conversationId, {
        follow: true,
        fromSeq: -1,
        ackSeq: ackedSeqRef.current,
        eventTypes: 'message,turn_ended',
      });

      // Create EventSource connection
      const eventSource = new EventSource(url);
      eventSourceRef.current = eventSource;

      // Acknowledge a handled turn so the server suppresses the disconnect
      // push. Best-effort: a failed ack must not break the live stream.
      const ackConversation = (ackSeq: number) => {
        ackedSeqRef.current = Math.max(ackedSeqRef.current, ackSeq);
        void fetch('/api/v1/chat/ack', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ conversation_id: conversationId, ack_seq: ackSeq }),
        })
          .then((response) => {
            if (!response.ok) {
              console.warn(`Live-update ack failed: HTTP ${response.status}`);
            }
          })
          .catch((error) => {
            console.warn('Live-update ack request failed:', error);
          });
      };

      // A completed turn means new persisted messages are available. The hub
      // stream doesn't carry full message rows, so signal a reload rather
      // than the message body; ChatApp re-fetches conversation history.
      const notifyTurnEnded = () => {
        callbackRef.current?.({
          internal_id: '',
          timestamp: new Date().toISOString(),
          new_messages: true,
          conversation_id: conversationId,
        });
      };

      // On every (re)connect, trigger a history reload. Because we tail from
      // the live head (from_seq=-1), the browser's native EventSource
      // auto-reconnect after a network blip resumes at the new head and drops
      // any events published while offline; reloading on open closes that gap
      // since message content is always read from persisted history, not the
      // (content-free) live event.
      eventSource.addEventListener('open', notifyTurnEnded);

      eventSource.addEventListener('turn_ended', (event) => {
        notifyTurnEnded();
        // Ack the completed turn so the disconnect push is suppressed. Every
        // SSE event carries its seq in the data payload.
        try {
          const data = JSON.parse((event as MessageEvent).data) as { seq?: unknown };
          if (typeof data.seq === 'number') {
            ackConversation(data.seq);
          }
        } catch {
          // A malformed payload only costs us the ack; the reload still fired.
        }
      });
      // Out-of-band assistant messages (scheduled callbacks, task-worker flows)
      // arrive as a `message` event. This EventSource is scoped to a single
      // conversation, so force the bound conversationId regardless of the
      // payload — ChatApp keys its reload on conversation_id and an out-of-band
      // tickle may omit it.
      eventSource.addEventListener('message', (event) => {
        try {
          const update: LiveMessageUpdate = JSON.parse(event.data);
          callbackRef.current?.({ ...update, conversation_id: conversationId });
        } catch {
          notifyTurnEnded();
        }
      });

      eventSource.addEventListener('heartbeat', () => {
        // Heartbeat to keep connection alive
      });

      eventSource.onerror = (error) => {
        // Log as warning since SSE disconnections are expected during normal operation
        // (server restarts, network issues, etc.) and we handle reconnection automatically
        console.warn('[SSE] Connection error, will attempt to reconnect:', error);
        eventSource.close();

        // Attempt reconnection after 5 seconds by triggering useEffect
        reconnectTimeoutRef.current = setTimeout(() => {
          setReconnectTrigger((prev) => prev + 1);
        }, 5000);
      };
    };

    // Wait for browser to be idle before connecting SSE
    // A small delay is necessary to allow Playwright test fixtures to achieve "networkidle"
    // state before the persistent SSE connection is established. This is a pragmatic trade-off
    // between test reliability and user experience - the 1.5s delay happens after page load,
    // so UX impact is minimal while ensuring tests work correctly.
    const scheduleConnect = () => {
      if ('requestIdleCallback' in window) {
        // Use requestIdleCallback + timeout to ensure networkidle is achieved
        window.requestIdleCallback(
          () => {
            setTimeout(connect, 1500);
          },
          { timeout: 500 }
        );
      } else {
        // Fallback for browsers without requestIdleCallback
        setTimeout(connect, 1500);
      }
    };

    if (document.readyState === 'complete') {
      // Page already loaded, schedule connection
      scheduleConnect();
    } else {
      // Wait for load event, then schedule connection
      window.addEventListener('load', scheduleConnect);
    }

    // Cleanup on unmount or when conversation changes
    return () => {
      window.removeEventListener('load', scheduleConnect);
      cleanup();
    };
  }, [conversationId, interfaceType, enabled, cleanup, reconnectTrigger]);

  return {
    disconnect: cleanup,
  };
}
