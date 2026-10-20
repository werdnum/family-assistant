import { useCallback, useEffect, useRef, useState } from 'react';
import { fetchHomeThreads } from './threadsApi';
import type { ThreadSummary } from './types';

const NO_THREADS: ThreadSummary[] = [];

/**
 * The threads of Home, newest first. Fetched once Home is known (null while
 * threads are disabled or Home is still being looked up); `refresh` refetches,
 * superseding any request still in flight, and is stable so it can be handed
 * to activity pings. `updateThread` patches one thread from what an action on
 * it returned, ahead of the refetch the action's activity ping causes, and
 * `addThread` puts a thread the user just started at the top of the list.
 */
export function useHomeThreads(homeConversationId: string | null): {
  threads: ThreadSummary[];
  /** Whether the latest fetch failed, leaving `threads` as they were. */
  failed: boolean;
  refresh: () => void;
  updateThread: (conversationId: string, update: Partial<ThreadSummary>) => void;
  addThread: (thread: ThreadSummary) => void;
} {
  const [threads, setThreads] = useState<ThreadSummary[]>(NO_THREADS);
  const [failed, setFailed] = useState(false);
  const abortRef = useRef<AbortController | null>(null);
  const homeRef = useRef(homeConversationId);
  homeRef.current = homeConversationId;

  const refresh = useCallback(() => {
    const home = homeRef.current;
    if (!home) {
      return;
    }
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;
    void (async () => {
      try {
        const data = await fetchHomeThreads(controller.signal);
        if (controller.signal.aborted || homeRef.current !== home) {
          return;
        }
        setThreads(data.threads);
        setFailed(false);
      } catch (error) {
        if (!controller.signal.aborted && homeRef.current === home) {
          console.error('Error fetching threads:', error);
          setFailed(true);
        }
      }
    })();
  }, []);

  const updateThread = useCallback((conversationId: string, update: Partial<ThreadSummary>) => {
    setThreads((current) =>
      current.some((thread) => thread.conversation_id === conversationId)
        ? current.map((thread) =>
            thread.conversation_id === conversationId ? { ...thread, ...update } : thread
          )
        : current
    );
  }, []);

  const addThread = useCallback((thread: ThreadSummary) => {
    setThreads((current) => [
      thread,
      ...current.filter((entry) => entry.conversation_id !== thread.conversation_id),
    ]);
  }, []);

  useEffect(() => {
    if (!homeConversationId) {
      setThreads(NO_THREADS);
      setFailed(false);
      return undefined;
    }
    refresh();
    return () => abortRef.current?.abort();
  }, [homeConversationId, refresh]);

  return { threads, failed, refresh, updateThread, addThread };
}
