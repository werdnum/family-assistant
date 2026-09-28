import { useCallback, useEffect, useRef, useState } from 'react';

/** Mirrors `PendingDelegation` in `web/routers/chat_api.py`. */
export interface PendingDelegation {
  delegation_id: string;
  target_profile_id: string;
  status: string;
  request_preview: string;
  created_at: string;
  started_at: string | null;
  completed_at: string | null;
  cancel_requested: boolean;
  children_total: number;
  children_finished: number;
}

interface PendingDelegationsResponse {
  conversation_id: string;
  delegations: PendingDelegation[];
}

// Handoff and delivery both reach the conversation stream, which triggers a
// refresh. Status changes and child progress in between do not, so poll while
// anything is pending; this also keeps "started N min ago" current.
export const PENDING_DELEGATIONS_POLL_MS = 15000;

/**
 * Background delegations the open conversation is still waiting on.
 *
 * Call `refresh` whenever the conversation may have changed (a turn ended, a
 * live-update event arrived). Results for a conversation the user has since
 * left are dropped.
 */
export const usePendingDelegations = (
  conversationId: string | null
): { delegations: PendingDelegation[]; refresh: () => void } => {
  const [state, setState] = useState<{
    conversationId: string | null;
    delegations: PendingDelegation[];
  }>({ conversationId: null, delegations: [] });
  const conversationIdRef = useRef(conversationId);
  conversationIdRef.current = conversationId;
  const requestSeqRef = useRef(0);

  const refresh = useCallback(() => {
    const requested = conversationIdRef.current;
    if (!requested) {
      return;
    }
    requestSeqRef.current += 1;
    const seq = requestSeqRef.current;
    void (async () => {
      try {
        const response = await fetch(
          `/api/v1/chat/conversations/${encodeURIComponent(requested)}/pending-delegations`
        );
        if (!response.ok) {
          throw new Error(`Failed to fetch pending delegations: ${response.status}`);
        }
        const data = (await response.json()) as PendingDelegationsResponse;
        if (seq !== requestSeqRef.current || conversationIdRef.current !== requested) {
          return;
        }
        setState({ conversationId: requested, delegations: data.delegations });
      } catch (error) {
        // The chip is a progress hint; the result itself still arrives as a
        // message. Keep the last known list rather than flashing it away.
        console.error('Error fetching pending delegations:', error);
      }
    })();
  }, []);

  useEffect(() => {
    refresh();
  }, [conversationId, refresh]);

  const delegations = state.conversationId === conversationId ? state.delegations : [];
  const hasPending = delegations.length > 0;

  useEffect(() => {
    if (!hasPending) {
      return;
    }
    const interval = window.setInterval(refresh, PENDING_DELEGATIONS_POLL_MS);
    return () => window.clearInterval(interval);
  }, [hasPending, refresh]);

  return { delegations, refresh };
};
