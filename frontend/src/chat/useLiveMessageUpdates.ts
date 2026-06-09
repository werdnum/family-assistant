import { useCallback, useEffect, useRef, useState } from 'react';

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

  // Update callback ref when it changes
  useEffect(() => {
    callbackRef.current = onMessageReceived;
  }, [onMessageReceived]);

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
      const url = new URL(
        `/api/v1/chat/conversations/${encodeURIComponent(conversationId)}/stream`,
        window.location.origin
      );
      url.searchParams.set('follow', 'true');
      // Tail from the current head (from_seq=-1): a follow-only listener wants
      // future events, not a replay. Subscribing from 0 would 410 once the
      // conversation's hub buffer has rotated, trapping EventSource in a
      // permanent reconnect loop.
      url.searchParams.set('from_seq', '-1');

      // Create EventSource connection
      const eventSource = new EventSource(url.toString());
      eventSourceRef.current = eventSource;

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

      eventSource.addEventListener('turn_ended', notifyTurnEnded);
      // Backwards-compatible: some flows may still emit a `message` event.
      eventSource.addEventListener('message', (event) => {
        try {
          const update: LiveMessageUpdate = JSON.parse(event.data);
          callbackRef.current?.(update);
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
