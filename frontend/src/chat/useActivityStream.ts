import { useCallback, useEffect, useRef, useState } from 'react';

export interface UseActivityStreamOptions {
  enabled?: boolean;
  onActivity?: () => void;
}

/**
 * Subscribe to the account-global conversation-activity stream
 * (`GET /api/v1/chat/activity/stream`) and invoke `onActivity` whenever any
 * conversation the user owns changes.
 *
 * This is what keeps the sidebar fresh for activity OUTSIDE the open thread — a
 * brand-new chat (including one started in another tab/device), or a
 * delegated/scheduled/background reply landing in a conversation the user isn't
 * currently viewing — none of which the per-conversation follow stream
 * (`useLiveMessageUpdates`) observes. The frame is advisory: every ping just
 * triggers the caller's authoritative conversation-list refetch, and a genuine
 * reconnect refreshes once to close any gap missed while disconnected.
 */
export function useActivityStream({ enabled = true, onActivity }: UseActivityStreamOptions) {
  const eventSourceRef = useRef<EventSource | null>(null);
  const reconnectTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const callbackRef = useRef(onActivity);
  const [reconnectTrigger, setReconnectTrigger] = useState(0);

  useEffect(() => {
    callbackRef.current = onActivity;
  }, [onActivity]);

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
    if (!enabled) {
      cleanup();
      return undefined;
    }

    const connect = () => {
      // EventSource is not available in the Node.js test environment.
      if (typeof EventSource === 'undefined') {
        return;
      }

      const eventSource = new EventSource('/api/v1/chat/activity/stream');
      eventSourceRef.current = eventSource;

      // Refresh on every open, including the first. Activity pings are
      // ephemeral (no replay), so anything that changed in the gap between
      // ChatApp's mount fetch and this deferred connect — or while offline
      // before a reconnect — is only caught by refreshing on open.
      eventSource.addEventListener('open', () => {
        callbackRef.current?.();
      });

      // Any activity ping means some conversation the user owns changed; the
      // caller refetches the authoritative list.
      eventSource.addEventListener('conversation_activity', () => {
        callbackRef.current?.();
      });

      eventSource.addEventListener('heartbeat', () => {
        // Keep-alive only.
      });

      eventSource.onerror = (error) => {
        // SSE disconnects are routine (server restarts, idle proxies); reconnect.
        console.warn('[activity-sse] Connection error, will reconnect:', error);
        eventSource.close();
        reconnectTimeoutRef.current = setTimeout(() => {
          setReconnectTrigger((prev) => prev + 1);
        }, 5000);
      };
    };

    // Mirror useLiveMessageUpdates: defer the persistent connection until the
    // page is idle so Playwright fixtures can reach "networkidle".
    const scheduleConnect = () => {
      if ('requestIdleCallback' in window) {
        window.requestIdleCallback(
          () => {
            setTimeout(connect, 1500);
          },
          { timeout: 500 }
        );
      } else {
        setTimeout(connect, 1500);
      }
    };

    if (document.readyState === 'complete') {
      scheduleConnect();
    } else {
      window.addEventListener('load', scheduleConnect);
    }

    return () => {
      window.removeEventListener('load', scheduleConnect);
      cleanup();
    };
  }, [enabled, cleanup, reconnectTrigger]);

  return {
    disconnect: cleanup,
  };
}
