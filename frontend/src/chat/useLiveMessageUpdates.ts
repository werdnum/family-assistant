import { useEffect, useRef, useCallback, useState } from 'react';

export interface LiveMessageUpdate {
  internal_id: string;
  timestamp: string;
  new_messages: boolean;
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

      // Construct SSE endpoint URL
      const url = new URL('/api/v1/chat/events', window.location.origin);
      url.searchParams.set('conversation_id', conversationId);
      url.searchParams.set('interface_type', interfaceType);

      // Create EventSource connection
      const eventSource = new EventSource(url.toString());
      eventSourceRef.current = eventSource;

      eventSource.addEventListener('connected', () => {
        // Successfully connected to SSE
      });

      eventSource.addEventListener('message', (event) => {
        try {
          const update: LiveMessageUpdate = JSON.parse(event.data);
          callbackRef.current?.(update);
        } catch (error) {
          console.error('[SSE] Failed to parse message update:', error);
        }
      });

      eventSource.addEventListener('heartbeat', () => {
        // Heartbeat to keep connection alive
      });

      eventSource.onerror = (error) => {
        console.error('[SSE] Connection error:', error);
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
