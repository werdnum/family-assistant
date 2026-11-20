import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { ConversationLoadStatus, OlderMessagesStatus } from './chatControls';
import { isToolCallDisplay } from './orderedParts';
import {
  buildLiveTurnMessage,
  buildSubconversationMessages,
  type LiveSubconversationTurn,
  type LiveToolCall,
  type SubconversationMessagesResponse,
} from './subconversationMessages';
import { isTerminalToolOutcome, type ToolOutcome } from './toolOutcome';
import type {
  ActiveTurnInfo,
  BackendAttachment,
  BackendConversationMessage,
  Message,
} from './types';
import type { PendingDelegation } from './usePendingDelegations';

const TERMINAL_DELEGATION_STATUSES = new Set(['completed', 'failed']);
const POLLABLE_IDLE_STATUSES = new Set(['queued', 'awaiting_remote', 'awaiting_children']);
const PAGE_LIMIT = 50;
const STATUS_POLL_MS = 15000;
const MAX_RECONNECT_ATTEMPTS = 5;
const BASE_RECONNECT_DELAY_MS = 1000;

export interface SubconversationPeekState {
  messages: Message[];
  delegation: PendingDelegation | null;
  delegationError: string | null;
  requestText: string | null;
  childDelegations: PendingDelegation[];
  isStreaming: boolean;
  isListening: boolean;
  loadStatus: ConversationLoadStatus | null;
  hasOlderMessages: boolean;
  olderMessagesStatus: OlderMessagesStatus;
  loadOlderMessages: () => void;
  retryLoad: () => void;
}

function parseSseFrame(
  rawBlock: string
): { eventType: string; data: Record<string, unknown> } | null {
  let eventType = 'message';
  const dataLines: string[] = [];
  for (const rawLine of rawBlock.split('\n')) {
    const line = rawLine.replace(/\r$/, '');
    if (!line || line.startsWith(':')) {
      continue;
    }
    if (line.startsWith('event:')) {
      eventType = line.slice('event:'.length).trim();
    } else if (line.startsWith('data:')) {
      dataLines.push(line.slice('data:'.length).trimStart());
    }
  }
  if (dataLines.length === 0) {
    return null;
  }
  try {
    const parsed: unknown = JSON.parse(dataLines.join('\n'));
    if (parsed && typeof parsed === 'object') {
      return { eventType, data: parsed as Record<string, unknown> };
    }
  } catch {
    return null;
  }
  return null;
}

function subconversationMessagesUrl(
  conversationId: string,
  subconversationId: string,
  params?: { limit?: number; beforeId?: number }
): string {
  const qs = new URLSearchParams();
  qs.set('limit', String(params?.limit ?? PAGE_LIMIT));
  if (params?.beforeId !== undefined) {
    qs.set('before_id', String(params.beforeId));
  }
  return `/api/v1/chat/conversations/${encodeURIComponent(conversationId)}/subconversations/${encodeURIComponent(subconversationId)}/messages?${qs.toString()}`;
}

function subconversationStreamUrl(
  conversationId: string,
  subconversationId: string,
  fromSeq: number
): string {
  const qs = new URLSearchParams();
  qs.set('from_seq', String(fromSeq));
  qs.set('follow', 'true');
  return `/api/v1/chat/conversations/${encodeURIComponent(conversationId)}/subconversations/${encodeURIComponent(subconversationId)}/stream?${qs.toString()}`;
}

function parseToolArgs(raw: unknown): { args: Record<string, unknown>; argsText: string } {
  if (raw && typeof raw === 'object' && !Array.isArray(raw)) {
    const args = raw as Record<string, unknown>;
    return { args, argsText: JSON.stringify(args) };
  }
  if (typeof raw === 'string') {
    try {
      const parsed: unknown = JSON.parse(raw);
      if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
        return { args: parsed as Record<string, unknown>, argsText: raw };
      }
    } catch {
      return { args: { raw }, argsText: raw };
    }
    return { args: {}, argsText: raw };
  }
  return { args: {}, argsText: '{}' };
}

/**
 * Read-only live streaming state for peeking into a running subconversation.
 *
 * Loads persisted history for `(conversationId, subconversationId)`, replays
 * any running turn cleanly from `first_seq`, and stays subscribed over SSE
 * until the delegation reaches a terminal state or the peek view closes.
 */
export function useSubconversationPeek(
  conversationId: string | null,
  subconversationId: string | null,
  seedDelegation: PendingDelegation | null
): SubconversationPeekState {
  const [persistedRows, setPersistedRows] = useState<BackendConversationMessage[]>([]);
  const [activeTurns, setActiveTurns] = useState<ActiveTurnInfo[]>([]);
  const [liveTurn, setLiveTurn] = useState<LiveSubconversationTurn | null>(null);
  const [replayingTurnId, setReplayingTurnId] = useState<string | null>(null);
  const [delegation, setDelegation] = useState<PendingDelegation | null>(seedDelegation);
  const [delegationError, setDelegationError] = useState<string | null>(null);
  const [requestText, setRequestText] = useState<string | null>(
    seedDelegation?.request_preview ?? null
  );
  const [childDelegations, setChildDelegations] = useState<PendingDelegation[]>([]);
  const [loadStatus, setLoadStatus] = useState<ConversationLoadStatus | null>(null);
  const [hasOlderMessages, setHasOlderMessages] = useState(false);
  const [olderMessagesStatus, setOlderMessagesStatus] = useState<OlderMessagesStatus>('idle');
  const [isListening, setIsListening] = useState(false);
  const [reloadNonce, setReloadNonce] = useState(0);

  const activeTargetRef = useRef<string | null>(null);
  const streamAbortRef = useRef<AbortController | null>(null);
  const persistedRowsRef = useRef<BackendConversationMessage[]>([]);
  persistedRowsRef.current = persistedRows;
  const targetProfileIdRef = useRef<string | undefined>(seedDelegation?.target_profile_id);
  targetProfileIdRef.current = delegation?.target_profile_id ?? seedDelegation?.target_profile_id;

  const targetKey =
    conversationId && subconversationId ? `${conversationId}::${subconversationId}` : null;

  const fetchSnapshot = useCallback(
    async (
      convId: string,
      subId: string,
      signal: AbortSignal
    ): Promise<SubconversationMessagesResponse | null> => {
      const response = await fetch(subconversationMessagesUrl(convId, subId), { signal });
      if (!response.ok) {
        throw new Error(`Failed to fetch subconversation (${response.status})`);
      }
      return (await response.json()) as SubconversationMessagesResponse;
    },
    []
  );

  const applySnapshot = useCallback((snapshot: SubconversationMessagesResponse) => {
    setPersistedRows(snapshot.messages);
    setActiveTurns(snapshot.active_turns ?? []);
    setHasOlderMessages(Boolean(snapshot.has_more_before));
    setOlderMessagesStatus('idle');
    setDelegation((prev) => snapshot.delegation ?? prev);
    setDelegationError(snapshot.delegation_error ?? null);
    setRequestText((prev) => snapshot.request_text ?? prev);
    setChildDelegations(snapshot.child_delegations ?? []);
    setLoadStatus(null);
  }, []);

  useEffect(() => {
    streamAbortRef.current?.abort();
    streamAbortRef.current = null;
    activeTargetRef.current = targetKey;

    if (!conversationId || !subconversationId || !targetKey) {
      setPersistedRows([]);
      setActiveTurns([]);
      setLiveTurn(null);
      setReplayingTurnId(null);
      setDelegation(null);
      setDelegationError(null);
      setRequestText(null);
      setChildDelegations([]);
      setLoadStatus(null);
      setHasOlderMessages(false);
      setIsListening(false);
      return;
    }

    setPersistedRows([]);
    setActiveTurns([]);
    setLiveTurn(null);
    setReplayingTurnId(null);
    setDelegation(seedDelegation);
    setDelegationError(null);
    setRequestText(seedDelegation?.request_preview ?? null);
    setChildDelegations([]);
    setLoadStatus('loading');
    setHasOlderMessages(false);
    setOlderMessagesStatus('idle');

    const controller = new AbortController();
    streamAbortRef.current = controller;
    let cancelled = false;

    const isStillActive = () => !cancelled && activeTargetRef.current === targetKey;

    const startLiveTurn = (turnId: string, startedAt?: string) => {
      setLiveTurn({
        turnId,
        text: '',
        toolCalls: [],
        attachments: [],
        profileId: targetProfileIdRef.current,
        startedAt: startedAt ? new Date(startedAt) : new Date(),
      });
    };

    const handleFrame = async (
      eventType: string,
      data: Record<string, unknown>,
      onSeq: (seq: number) => void
    ): Promise<'continue' | 'stop'> => {
      if (typeof data.seq === 'number') {
        onSeq(data.seq);
      }

      if (eventType === 'heartbeat') {
        return 'continue';
      }

      if (eventType === 'stream_dropped') {
        return 'stop';
      }

      if (eventType === 'turn_started') {
        const turnId = typeof data.turn_id === 'string' ? data.turn_id : `turn_${Date.now()}`;
        setReplayingTurnId(turnId);
        startLiveTurn(turnId);
        return 'continue';
      }

      if (eventType === 'turn_ended') {
        setLiveTurn(null);
        setReplayingTurnId(null);
        try {
          const refreshed = await fetchSnapshot(
            conversationId,
            subconversationId,
            controller.signal
          );
          if (refreshed && isStillActive()) {
            applySnapshot(refreshed);
            const hasRunning = (refreshed.active_turns ?? []).some((t) => t.status === 'running');
            const isTerminal =
              refreshed.delegation !== null &&
              TERMINAL_DELEGATION_STATUSES.has(refreshed.delegation.status);
            if (!hasRunning && isTerminal) {
              return 'stop';
            }
          }
        } catch {
          // Ignore refresh failure if aborted or transient; next reconnect reconciles.
        }
        return 'continue';
      }

      if (eventType === 'message' && typeof data.reason === 'string') {
        try {
          const refreshed = await fetchSnapshot(
            conversationId,
            subconversationId,
            controller.signal
          );
          if (refreshed && isStillActive()) {
            applySnapshot(refreshed);
            const hasRunning = (refreshed.active_turns ?? []).some((t) => t.status === 'running');
            const isTerminal =
              refreshed.delegation !== null &&
              TERMINAL_DELEGATION_STATUSES.has(refreshed.delegation.status);
            if (!hasRunning && isTerminal) {
              return 'stop';
            }
          }
        } catch {
          // Ignore transient refresh failure.
        }
        return 'continue';
      }

      // Content / tool / attachment / error frames all update the active live turn.
      const frameTurnId = typeof data.turn_id === 'string' ? data.turn_id : undefined;

      if (typeof data.content === 'string') {
        const chunk = data.content;
        setLiveTurn((prev) => {
          const base: LiveSubconversationTurn = prev ?? {
            turnId: frameTurnId ?? `live_${Date.now()}`,
            text: '',
            toolCalls: [],
            attachments: [],
            profileId: targetProfileIdRef.current,
            startedAt: new Date(),
          };
          return { ...base, text: base.text + chunk };
        });
        return 'continue';
      }

      if (data.tool_call && typeof data.tool_call === 'object') {
        const tc = data.tool_call as Record<string, unknown>;
        const fn = (tc.function as Record<string, unknown> | undefined) ?? undefined;
        const callId = typeof tc.id === 'string' ? tc.id : `tc_${Date.now()}`;
        const callName =
          (typeof fn?.name === 'string' && fn.name) ||
          (typeof tc.name === 'string' && tc.name) ||
          'unknown';
        const { args, argsText } = parseToolArgs(fn?.arguments ?? tc.arguments);
        const display = isToolCallDisplay(tc.display) ? tc.display : undefined;

        setLiveTurn((prev) => {
          const base: LiveSubconversationTurn = prev ?? {
            turnId: frameTurnId ?? `live_${Date.now()}`,
            text: '',
            toolCalls: [],
            attachments: [],
            profileId: targetProfileIdRef.current,
            startedAt: new Date(),
          };
          if (base.toolCalls.some((existing) => existing.id === callId)) {
            return base;
          }
          const nextCall: LiveToolCall = {
            id: callId,
            name: callName,
            args,
            argsText,
            display,
            textOffset: base.text.length,
          };
          return { ...base, toolCalls: [...base.toolCalls, nextCall] };
        });
        return 'continue';
      }

      if (typeof data.tool_call_id === 'string' && 'result' in data) {
        const callId = data.tool_call_id;
        const resultText =
          typeof data.result === 'string' ? data.result : JSON.stringify(data.result ?? '');
        const outcome: ToolOutcome = isTerminalToolOutcome(data.outcome)
          ? data.outcome
          : 'succeeded';
        const attachments = Array.isArray(data.attachments)
          ? (data.attachments as BackendAttachment[])
          : undefined;

        setLiveTurn((prev) => {
          if (!prev) {
            return prev;
          }
          return {
            ...prev,
            toolCalls: prev.toolCalls.map((call) =>
              call.id === callId ? { ...call, result: resultText, outcome, attachments } : call
            ),
          };
        });
        return 'continue';
      }

      if (data.attachment && typeof data.attachment === 'object') {
        const attachment = data.attachment as BackendAttachment;
        setLiveTurn((prev) => {
          const base: LiveSubconversationTurn = prev ?? {
            turnId: frameTurnId ?? `live_${Date.now()}`,
            text: '',
            toolCalls: [],
            attachments: [],
            profileId: targetProfileIdRef.current,
            startedAt: new Date(),
          };
          return { ...base, attachments: [...base.attachments, attachment] };
        });
        return 'continue';
      }

      if (typeof data.error === 'string') {
        const errMsg = data.error;
        setLiveTurn((prev) => {
          const base: LiveSubconversationTurn = prev ?? {
            turnId: frameTurnId ?? `live_${Date.now()}`,
            text: '',
            toolCalls: [],
            attachments: [],
            profileId: targetProfileIdRef.current,
            startedAt: new Date(),
          };
          return { ...base, error: errMsg };
        });
      }

      return 'continue';
    };

    const run = async () => {
      let snapshot: SubconversationMessagesResponse | null;
      try {
        snapshot = await fetchSnapshot(conversationId, subconversationId, controller.signal);
      } catch (err) {
        if (!isStillActive() || (err instanceof DOMException && err.name === 'AbortError')) {
          return;
        }
        setLoadStatus('failed');
        return;
      }

      if (!snapshot || !isStillActive()) {
        return;
      }
      applySnapshot(snapshot);

      const runningTurn = (snapshot.active_turns ?? []).find((t) => t.status === 'running');
      const isTerminal =
        snapshot.delegation !== null &&
        TERMINAL_DELEGATION_STATUSES.has(snapshot.delegation.status);

      if (!runningTurn && isTerminal) {
        setIsListening(false);
        return;
      }

      let nextFromSeq = runningTurn ? (runningTurn.first_seq ?? 0) : -1;
      if (runningTurn) {
        setReplayingTurnId(runningTurn.turn_id);
        startLiveTurn(runningTurn.turn_id, runningTurn.started_at);
      }

      setIsListening(true);
      let attempt = 0;

      while (isStillActive() && attempt <= MAX_RECONNECT_ATTEMPTS) {
        try {
          const streamResponse = await fetch(
            subconversationStreamUrl(conversationId, subconversationId, nextFromSeq),
            {
              headers: { Accept: 'text/event-stream' },
              signal: controller.signal,
            }
          );

          if (streamResponse.status === 410) {
            // Buffer rotated past requested seq: reload persisted history (keeping
            // all persisted rows) and switch to live tailing.
            setReplayingTurnId(null);
            const refreshed = await fetchSnapshot(
              conversationId,
              subconversationId,
              controller.signal
            );
            if (!refreshed || !isStillActive()) {
              break;
            }
            applySnapshot(refreshed);
            const stillRunning = (refreshed.active_turns ?? []).find((t) => t.status === 'running');
            const nowTerminal =
              refreshed.delegation !== null &&
              TERMINAL_DELEGATION_STATUSES.has(refreshed.delegation.status);
            if (!stillRunning && nowTerminal) {
              setLiveTurn(null);
              break;
            }
            nextFromSeq = -1;
            attempt = 0;
            continue;
          }

          if (!streamResponse.ok || !streamResponse.body) {
            throw new Error(`Subconversation stream failed (${streamResponse.status})`);
          }

          const reader = streamResponse.body.getReader();
          const decoder = new TextDecoder();
          let buffer = '';
          let shouldStop = false;
          let receivedRealEvent = false;

          while (isStillActive()) {
            const { value, done } = await reader.read();
            if (done) {
              break;
            }
            buffer += decoder.decode(value, { stream: true });
            const blocks = buffer.split(/\r?\n\r?\n/);
            buffer = blocks.pop() ?? '';
            for (const block of blocks) {
              if (!block.trim()) {
                continue;
              }
              const parsed = parseSseFrame(block);
              if (!parsed) {
                continue;
              }
              if (parsed.eventType !== 'heartbeat' && parsed.eventType !== 'stream_dropped') {
                receivedRealEvent = true;
              }
              const action = await handleFrame(parsed.eventType, parsed.data, (seq) => {
                nextFromSeq = seq + 1;
              });
              if (action === 'stop') {
                shouldStop = true;
                break;
              }
            }
            if (shouldStop) {
              try {
                await reader.cancel();
              } catch {
                // Reader may already be closed.
              }
              break;
            }
          }

          if (receivedRealEvent) {
            attempt = 0;
          }

          if (!isStillActive()) {
            break;
          }

          let reachedTerminal = false;
          try {
            const check = await fetchSnapshot(conversationId, subconversationId, controller.signal);
            if (check && isStillActive()) {
              applySnapshot(check);
              const stillRunning = (check.active_turns ?? []).some((t) => t.status === 'running');
              const nowTerminal =
                check.delegation !== null &&
                TERMINAL_DELEGATION_STATUSES.has(check.delegation.status);
              if (!stillRunning && nowTerminal) {
                setLiveTurn(null);
                setReplayingTurnId(null);
                reachedTerminal = true;
              }
            }
          } catch {
            // Fall through to backoff reconnect.
          }

          if (reachedTerminal || !isStillActive()) {
            break;
          }

          // Stream ended while the delegation is still active (e.g. proxy drop
          // or server_shutdown); back off before reconnecting so a closed
          // stream never spins in a tight loop.
          attempt += 1;
          if (attempt > MAX_RECONNECT_ATTEMPTS) {
            break;
          }
          const eofDelay = Math.min(BASE_RECONNECT_DELAY_MS * 2 ** (attempt - 1), 10000);
          await new Promise<void>((resolve) => {
            const timer = window.setTimeout(resolve, eofDelay);
            controller.signal.addEventListener(
              'abort',
              () => {
                window.clearTimeout(timer);
                resolve();
              },
              { once: true }
            );
          });
        } catch (err) {
          if (!isStillActive() || (err instanceof DOMException && err.name === 'AbortError')) {
            break;
          }
          attempt += 1;
          if (attempt > MAX_RECONNECT_ATTEMPTS) {
            break;
          }
          const delay = Math.min(BASE_RECONNECT_DELAY_MS * 2 ** (attempt - 1), 10000);
          await new Promise<void>((resolve) => {
            const timer = window.setTimeout(resolve, delay);
            controller.signal.addEventListener(
              'abort',
              () => {
                window.clearTimeout(timer);
                resolve();
              },
              { once: true }
            );
          });
        }
      }

      if (isStillActive()) {
        setIsListening(false);
      }
    };

    void run();

    return () => {
      cancelled = true;
      controller.abort();
    };
  }, [conversationId, subconversationId, targetKey, reloadNonce, fetchSnapshot, applySnapshot]);

  // While waiting on remote or child delegations without an active local turn,
  // poll periodically so child counts and remote status stay fresh.
  const shouldPollIdleStatus =
    Boolean(conversationId && subconversationId) &&
    liveTurn === null &&
    delegation !== null &&
    POLLABLE_IDLE_STATUSES.has(delegation.status);

  useEffect(() => {
    if (!shouldPollIdleStatus || !conversationId || !subconversationId) {
      return;
    }
    const interval = window.setInterval(() => {
      const pollController = new AbortController();
      void fetchSnapshot(conversationId, subconversationId, pollController.signal)
        .then((snapshot) => {
          if (snapshot && activeTargetRef.current === `${conversationId}::${subconversationId}`) {
            applySnapshot(snapshot);
          }
        })
        .catch(() => {
          // Advisory refresh.
        });
    }, STATUS_POLL_MS);
    return () => window.clearInterval(interval);
  }, [shouldPollIdleStatus, conversationId, subconversationId, fetchSnapshot, applySnapshot]);

  const loadOlderMessages = useCallback(() => {
    if (!conversationId || !subconversationId || olderMessagesStatus === 'loading') {
      return;
    }
    const oldest = persistedRowsRef.current[0];
    if (!oldest?.internal_id) {
      return;
    }
    setOlderMessagesStatus('loading');
    const capturedKey = `${conversationId}::${subconversationId}`;
    void fetch(
      subconversationMessagesUrl(conversationId, subconversationId, {
        beforeId: oldest.internal_id,
      })
    )
      .then(async (response) => {
        if (!response.ok) {
          throw new Error(`Older messages fetch failed (${response.status})`);
        }
        return (await response.json()) as SubconversationMessagesResponse;
      })
      .then((page) => {
        if (activeTargetRef.current !== capturedKey) {
          return;
        }
        setPersistedRows((prev) => {
          const seen = new Set(prev.map((row) => row.internal_id));
          const fresh = page.messages.filter((row) => !seen.has(row.internal_id));
          return [...fresh, ...prev];
        });
        setHasOlderMessages(Boolean(page.has_more_before));
        setOlderMessagesStatus('idle');
      })
      .catch(() => {
        if (activeTargetRef.current === capturedKey) {
          setOlderMessagesStatus('failed');
        }
      });
  }, [conversationId, subconversationId, olderMessagesStatus]);

  const retryLoad = useCallback(() => {
    setReloadNonce((n) => n + 1);
  }, []);

  const messages = useMemo(() => {
    const runningIds = new Set(
      activeTurns.filter((t) => t.status === 'running').map((t) => t.turn_id)
    );
    if (liveTurn) {
      runningIds.add(liveTurn.turnId);
    }
    const replayingIds = replayingTurnId ? new Set([replayingTurnId]) : new Set<string>();
    const persisted = buildSubconversationMessages(persistedRows, runningIds, replayingIds);
    if (liveTurn) {
      return [...persisted, buildLiveTurnMessage(liveTurn)];
    }
    return persisted;
  }, [persistedRows, activeTurns, liveTurn, replayingTurnId]);

  const isStreaming = liveTurn !== null;

  return {
    messages,
    delegation,
    delegationError,
    requestText,
    childDelegations,
    isStreaming,
    isListening,
    loadStatus,
    hasOlderMessages,
    olderMessagesStatus,
    loadOlderMessages,
    retryLoad,
  };
}
