import { Maximize2, Minimize2, PanelLeftClose, PanelLeftOpen, X } from 'lucide-react';
import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { Button } from '@/components/ui/button';
import { TooltipProvider } from '@/components/ui/tooltip';
import { AppTopBar } from '../shared/AppShell';
import {
  ChatShellContext,
  type ChatShellValue,
  type ConversationDraft,
  type NotificationRequest,
  type TurnToFollow,
} from './chatShell';
import {
  type ConversationChangeReason,
  ConversationPane,
  type ConversationPaneHandle,
  type PaneReport,
} from './ConversationPane';
import ConversationSidebar from './ConversationSidebar';
import { NotificationSettings } from './NotificationSettings';
import { PendingConfirmationsTray } from './PendingConfirmationsTray';
import { ProfilesProvider, useProfiles } from './profilesContext';
import { PushNotificationButton } from './PushNotificationButton';
import { StartThreadDialog } from './StartThreadDialog';
import { fetchHome, type StartThreadRequest, startHomeThread } from './threadsApi';
import type { PendingToolConfirmation } from './ToolConfirmationContext';
import { ChatAppProps, Conversation, type ThreadSummary } from './types';
import { useActivityStream } from './useActivityStream';
import { useHomeThreads } from './useHomeThreads';
import { useNotifications } from './useNotifications';

const LIVE_CONFIRMATION_POLL_RACE_GRACE_MS = 30000;
const PENDING_CONFIRMATIONS_POLL_MS = 15000;
// One failed poll is retried after this delay; only a second consecutive
// failure is shown, so a blip doesn't push the thread down until the next poll.
const PENDING_CONFIRMATIONS_RETRY_MS = 3000;
// How long a confirmation for this conversation may wait for its tool call to
// render (and so show inline) before it falls back to the tray.
const INLINE_CONFIRMATION_GRACE_MS = 5000;

function parseConfirmationTimestamp(value: unknown): number | null {
  if (typeof value !== 'string' && typeof value !== 'number') {
    return null;
  }
  const timestamp = new Date(value).getTime();
  return Number.isNaN(timestamp) ? null : timestamp;
}

function localConfirmationExpiry(confirmation: PendingToolConfirmation): number | null {
  const durationSeconds = confirmation.time_remaining_seconds ?? confirmation.timeout_seconds;
  if (typeof durationSeconds !== 'number') {
    return null;
  }
  const receivedAt = parseConfirmationTimestamp(confirmation.received_at);
  if (receivedAt === null) {
    return null;
  }
  return receivedAt + durationSeconds * 1000;
}

function isFreshLiveConfirmation(confirmation: PendingToolConfirmation): boolean {
  const receivedAt = parseConfirmationTimestamp(confirmation.received_at);
  if (receivedAt === null) {
    return false;
  }
  const now = Date.now();
  const expiresAt = localConfirmationExpiry(confirmation);
  return (
    (expiresAt === null || expiresAt > now) &&
    now - receivedAt < LIVE_CONFIRMATION_POLL_RACE_GRACE_MS
  );
}

function confirmationFingerprint(confirmation: PendingToolConfirmation): string {
  return JSON.stringify({
    request_id: confirmation.request_id,
    tool_name: confirmation.tool_name,
    tool_call_id: confirmation.tool_call_id,
    confirmation_prompt: confirmation.confirmation_prompt,
    args: confirmation.args,
    created_at: confirmation.created_at,
    expires_at: confirmation.expires_at,
    timeout_seconds: confirmation.timeout_seconds,
    time_remaining_seconds: confirmation.time_remaining_seconds,
  });
}

export function confirmationMapsEqual(
  left: Map<string, PendingToolConfirmation>,
  right: Map<string, PendingToolConfirmation>
): boolean {
  if (left.size !== right.size) {
    return false;
  }
  for (const [key, leftValue] of left.entries()) {
    const rightValue = right.get(key);
    if (!rightValue || confirmationFingerprint(leftValue) !== confirmationFingerprint(rightValue)) {
      return false;
    }
  }
  return true;
}

/** How a navigation is recorded in the browser history. */
type HistoryMode = 'push' | 'replace' | 'none';

type PaneSlot = 'center' | 'panel';

/** A thread open beside the centre, or in its place once expanded. */
interface PanelState {
  threadId: string;
  expanded: boolean;
}

// Below this width the panel would squeeze Home and the thread, so a thread
// opens in place of Home, as on a phone.
const PANEL_MIN_WIDTH = 1440;

/** The chat's address: what the centre shows, and the thread open beside it. */
function chatUrl(centerConversationId: string | null, panel: PanelState | null): string {
  const params: string[] = [];
  if (centerConversationId) {
    params.push(`conversation_id=${encodeURIComponent(centerConversationId)}`);
  }
  if (panel) {
    params.push(`thread=${encodeURIComponent(panel.threadId)}`);
    if (panel.expanded) {
      params.push('expanded=1');
    }
  }
  return params.length > 0 ? `/chat?${params.join('&')}` : '/chat';
}

function panelFromUrl(search: string): PanelState | null {
  const params = new URLSearchParams(search);
  const threadId = params.get('thread');
  return threadId ? { threadId, expanded: params.get('expanded') === '1' } : null;
}

const ChatAppContent: React.FC<ChatAppProps> = ({ profileId = 'default_assistant' }) => {
  const [sidebarOpen, setSidebarOpen] = useState<boolean>(true);
  const [isMobile, setIsMobile] = useState<boolean>(window.innerWidth < 768);
  const [isWide, setIsWide] = useState<boolean>(window.innerWidth >= PANEL_MIN_WIDTH);
  const [mobileShowList, setMobileShowList] = useState<boolean>(false);
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [conversationsLoading, setConversationsLoading] = useState<boolean>(true);
  const conversationsLoadedRef = useRef(false);
  const abortControllerRef = useRef<AbortController | null>(null);
  // Optimistic conversation rows inserted on send, keyed by the owning turn id,
  // kept until that turn settles (noteTurnSettled). Merged into every fetch
  // result so a list request already in flight when the user sent can't erase
  // the row; turn-keying stops an aborted older turn from retiring a newer
  // turn's row for the same conversation. See fetchConversations.
  const pendingOptimisticConversationsRef = useRef<Map<string, Conversation>>(new Map());
  const resolvedConfirmationIdsRef = useRef<Set<string>>(new Set());
  const { isLoading: profilesLoading } = useProfiles();
  const [notificationsEnabled, setNotificationsEnabled] = useState<boolean>(() => {
    // Load notification preference from localStorage
    const saved = localStorage.getItem('notificationsEnabled');
    return saved === 'true';
  });
  // The caller's Home while threads are enabled; null while they are disabled
  // or not yet known, in which case the chat behaves as a list of conversations.
  const [homeConversationId, setHomeConversationId] = useState<string | null>(null);
  const homeConversationIdRef = useRef<string | null>(null);
  homeConversationIdRef.current = homeConversationId;
  const {
    threads: homeThreads,
    failed: homeThreadsFailed,
    refresh: refreshHomeThreads,
    updateThread: updateHomeThread,
    addThread: addHomeThread,
  } = useHomeThreads(homeConversationId);
  // Set once anything decides what the chat shows (opening a conversation, a
  // new chat, a send), so a landing on Home that resolves later doesn't
  // replace it.
  const landingSettledRef = useRef(false);
  const initialPromptProcessedRef = useRef(false);
  const [searchParams, setSearchParams] = useSearchParams();
  const [drafts] = useState(() => new Map<string, ConversationDraft>());

  const centerRef = useRef<ConversationPaneHandle>(null);
  // The conversation the centre pane shows, as it last reported.
  const [centerConversationId, setCenterConversationId] = useState<string | null>(null);
  const [paneReports, setPaneReports] = useState<Partial<Record<PaneSlot, PaneReport>>>({});
  const handleCenterReport = useCallback(
    (report: PaneReport) => setPaneReports((prev) => ({ ...prev, center: report })),
    []
  );
  const centerConversationIdRef = useRef<string | null>(null);

  const panelRef = useRef<ConversationPaneHandle>(null);
  const [panel, setPanel] = useState<PanelState | null>(null);
  const panelStateRef = useRef<PanelState | null>(null);
  // The conversation the panel pane shows, as it last reported.
  const panelConversationIdRef = useRef<string | null>(null);
  const handlePanelReport = useCallback(
    (report: PaneReport) => setPaneReports((prev) => ({ ...prev, panel: report })),
    []
  );
  const handlePanelConversationChange = useCallback((convId: string) => {
    panelConversationIdRef.current = convId;
  }, []);
  // Whether the thread is shown in place of the centre: expanded, or on a
  // window too narrow for both.
  const panelCoversCenter = panel !== null && (panel.expanded || isMobile || !isWide);
  const panelCoversCenterRef = useRef(panelCoversCenter);
  panelCoversCenterRef.current = panelCoversCenter;

  const writeHistory = useCallback((mode: HistoryMode) => {
    const url = chatUrl(centerConversationIdRef.current, panelStateRef.current);
    if (mode === 'push') {
      window.history.pushState({}, '', url);
    } else if (mode === 'replace') {
      window.history.replaceState({}, '', url);
    }
  }, []);

  const setPanelState = useCallback(
    (next: PanelState | null, mode: HistoryMode) => {
      panelStateRef.current = next;
      setPanel(next);
      if (!next) {
        panelConversationIdRef.current = null;
        setPaneReports(({ panel: _closed, ...rest }) => rest);
      }
      writeHistory(mode);
    },
    [writeHistory]
  );

  // How the next conversation change a pane reports is recorded in history:
  // pushed for a user's navigation, replaced or left alone while landing.
  const historyModeRef = useRef<HistoryMode>('push');
  const withHistoryMode = useCallback(<T,>(mode: HistoryMode, navigate: () => T): T => {
    historyModeRef.current = mode;
    try {
      return navigate();
    } finally {
      historyModeRef.current = 'push';
    }
  }, []);

  const handleCenterConversationChange = useCallback(
    (convId: string, reason: ConversationChangeReason) => {
      landingSettledRef.current = true;
      centerConversationIdRef.current = convId;
      setCenterConversationId(convId);
      // A send that minted its conversation replaces the entry it was typed in.
      const mode = reason === 'send' ? 'replace' : historyModeRef.current;
      // A landing open restores where the user was; it is not a new choice.
      if (mode === 'push' || reason !== 'open') {
        localStorage.setItem('lastConversationId', convId);
      }
      writeHistory(mode);
    },
    [writeHistory]
  );

  // Fetch conversations list
  const fetchConversations = useCallback(async () => {
    // Cancel previous request if it exists
    abortControllerRef.current?.abort();
    const abortController = new AbortController();
    abortControllerRef.current = abortController;
    try {
      // Only the first load shows the sidebar's placeholder. Later refreshes
      // (on every send, stream completion and activity ping) keep the current
      // list on screen until the new one arrives, rather than blanking it.
      if (!conversationsLoadedRef.current) {
        setConversationsLoading(true);
      }
      // Home and its threads are listed from their own endpoint.
      const kindFilter = homeConversationIdRef.current ? '&kind=ordinary' : '';
      const response = await fetch(`/api/v1/chat/conversations?interface_type=web${kindFilter}`, {
        signal: abortController.signal,
      });
      if (response.ok) {
        const data = await response.json();
        const serverList: Conversation[] = data.conversations;
        // Prefer optimistic rows that are still pending (their send hasn't
        // settled). Pending is keyed off the turn lifecycle — set on send,
        // cleared when the turn completes/fails (see handleStreamingComplete /
        // handleStreamingError) — NOT off a client/server timestamp comparison,
        // which a skewed client clock could keep "newest" forever and so pin a
        // stale preview indefinitely. So a stale in-flight fetch can't undo an
        // optimistic insert/bump while the turn is in flight, and once it settles
        // the server row is authoritative.
        // pending is keyed by turn id; a conversation is pending if any in-flight
        // turn holds an optimistic row for it. Collapse to one row per
        // conversation (a superseding re-send can leave two in-flight turns for
        // the same conversation), keeping the newest — Map.values() is insertion
        // order, so a later turn overwrites the earlier one.
        const pending = pendingOptimisticConversationsRef.current;
        const pendingByConversation = new Map<string, Conversation>();
        for (const row of pending.values()) {
          pendingByConversation.set(row.conversation_id, row);
        }
        const pendingRows = [...pendingByConversation.values()];
        const pendingIds = new Set(pendingByConversation.keys());
        // Sort newest-first for display order only (not retirement); pending rows
        // otherwise follow Map insertion (oldest-first) order.
        const merged = [
          ...pendingRows,
          ...serverList.filter((c) => !pendingIds.has(c.conversation_id)),
        ].sort((a, b) => Date.parse(b.last_timestamp) - Date.parse(a.last_timestamp));
        setConversations(merged);
        conversationsLoadedRef.current = true;
      }
    } catch (error) {
      // Don't log error if request was aborted (component unmounting)
      if (error instanceof Error && error.name !== 'AbortError') {
        console.error('Error fetching conversations:', error);
      }
    } finally {
      // A request superseded by a newer one leaves the loading state to it, so
      // an aborted first load can't flash "No conversations yet".
      if (abortControllerRef.current === abortController) {
        setConversationsLoading(false);
      }
    }
  }, []);

  const confirmationKey = useCallback((confirmation: PendingToolConfirmation) => {
    return confirmation.tool_call_id || confirmation.request_id;
  }, []);

  // Track pending confirmations by tool call ID when available, otherwise request ID.
  const [pendingConfirmations, setPendingConfirmations] = useState<
    Map<string, PendingToolConfirmation>
  >(new Map());
  const [pendingConfirmationsError, setPendingConfirmationsError] = useState<string | null>(null);

  const handleConfirmationRequest = useCallback(
    (request: PendingToolConfirmation) => {
      resolvedConfirmationIdsRef.current.delete(request.request_id);
      setPendingConfirmations((prev) => {
        const receivedAt = Date.now();
        const key = confirmationKey(request);
        const newMap = new Map(prev);
        newMap.set(key, {
          ...request,
          created_at: request.created_at ?? receivedAt,
          received_at: receivedAt,
          first_seen_at: prev.get(key)?.first_seen_at ?? receivedAt,
          received_via_sse: true,
        });
        return newMap;
      });
    },
    [confirmationKey]
  );

  const handleConfirmationResult = useCallback(
    (result: { request_id: string; [key: string]: unknown }) => {
      resolvedConfirmationIdsRef.current.add(result.request_id);
      setPendingConfirmations((prev) => {
        const newMap = new Map(prev);
        for (const [key, value] of newMap.entries()) {
          if (value.request_id === result.request_id) {
            newMap.delete(key);
          }
        }
        return newMap;
      });
    },
    []
  );

  const confirmTool = useCallback(
    async (requestId: string, approved: boolean, conversationId: string | null) => {
      try {
        const response = await fetch('/api/v1/chat/confirm_tool', {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
          },
          body: JSON.stringify({
            request_id: requestId,
            approved: approved,
            conversation_id: conversationId,
          }),
        });

        if (!response.ok) {
          throw new Error(`Failed to send confirmation: ${response.status}`);
        }

        const responseBody = (await response.json()) as {
          success?: boolean;
          error?: string;
          message?: string;
        };
        if (responseBody.success === false) {
          throw new Error(
            responseBody.error ??
              responseBody.message ??
              'Confirmation request was rejected by the server'
          );
        }
        handleConfirmationResult({ request_id: requestId });
      } catch (error) {
        console.error('Error sending confirmation:', error);
        throw error;
      }
    },
    [handleConfirmationResult]
  );

  // Resolves true when the pending list was loaded, false when it could not be.
  const fetchPendingConfirmations = useCallback(async (): Promise<boolean> => {
    try {
      const response = await fetch('/api/v1/chat/confirmations/pending');
      if (!response.ok) {
        throw new Error(`Failed to fetch pending confirmations: ${response.status}`);
      }
      const data = (await response.json()) as unknown;
      if (!data || typeof data !== 'object') {
        throw new Error('Pending confirmations response did not contain an object');
      }
      const pendingConfirmationsResponse = data as { confirmations?: unknown };
      if (!Array.isArray(pendingConfirmationsResponse.confirmations)) {
        throw new Error('Pending confirmations response did not contain a confirmation list');
      }
      const fetchedConfirmations =
        pendingConfirmationsResponse.confirmations as PendingToolConfirmation[];
      const confirmations = fetchedConfirmations.filter(
        (confirmation) => !resolvedConfirmationIdsRef.current.has(confirmation.request_id)
      );
      setPendingConfirmations((prev) => {
        const receivedAt = Date.now();
        const newMap = new Map<string, PendingToolConfirmation>();
        for (const confirmation of confirmations) {
          const key = confirmationKey(confirmation);
          newMap.set(key, {
            ...confirmation,
            received_at: receivedAt,
            first_seen_at: prev.get(key)?.first_seen_at ?? receivedAt,
          });
        }
        for (const [key, confirmation] of prev.entries()) {
          if (
            !newMap.has(key) &&
            !resolvedConfirmationIdsRef.current.has(confirmation.request_id) &&
            confirmation.received_via_sse === true &&
            isFreshLiveConfirmation(confirmation)
          ) {
            newMap.set(key, confirmation);
          }
        }
        if (confirmationMapsEqual(prev, newMap)) {
          return prev;
        }
        return newMap;
      });
      return true;
    } catch (error) {
      console.error('Error fetching pending confirmations:', error);
      return false;
    }
  }, [confirmationKey]);

  useEffect(() => {
    let cancelled = false;
    let consecutiveFailures = 0;
    let retryTimer: number | undefined;
    const poll = async () => {
      window.clearTimeout(retryTimer);
      const loaded = await fetchPendingConfirmations();
      if (cancelled) {
        return;
      }
      if (loaded) {
        consecutiveFailures = 0;
        setPendingConfirmationsError(null);
        return;
      }
      consecutiveFailures += 1;
      if (consecutiveFailures === 1) {
        retryTimer = window.setTimeout(() => void poll(), PENDING_CONFIRMATIONS_RETRY_MS);
      } else {
        setPendingConfirmationsError("Couldn't check for pending approvals. Retrying…");
      }
    };
    void poll();
    const interval = window.setInterval(() => void poll(), PENDING_CONFIRMATIONS_POLL_MS);
    return () => {
      cancelled = true;
      window.clearInterval(interval);
      window.clearTimeout(retryTimer);
    };
  }, [fetchPendingConfirmations]);

  const noteTurnStarted = useCallback(
    (turnId: string, targetConversationId: string, preview: string) => {
      setConversations((prev) => {
        const existing = prev.find((c) => c.conversation_id === targetConversationId);
        const others = prev.filter((c) => c.conversation_id !== targetConversationId);
        const optimisticRow: Conversation = {
          conversation_id: targetConversationId,
          last_message: preview,
          // Keep the existing label while the bump is pending; the sweep
          // refreshes it once the conversation goes quiet.
          summary: existing?.summary,
          last_timestamp: new Date().toISOString(),
          message_count: (existing?.message_count ?? 0) + 1,
        };
        // Remember it (keyed by THIS turn) so a list fetch already in flight
        // can't erase it; retired when this turn settles (see
        // noteTurnSettled). Keying by turn id means an aborted older turn
        // can't retire a newer turn's pending row for the same conversation.
        pendingOptimisticConversationsRef.current.set(turnId, optimisticRow);
        return [optimisticRow, ...others];
      });
    },
    []
  );

  // The server reflects a settled turn, so the list it returns is
  // authoritative again. Cleared on settle, not by a timestamp compare, so a
  // skewed clock can't pin an optimistic row.
  const noteTurnSettled = useCallback(
    (turnId: string | undefined) => {
      if (turnId) {
        pendingOptimisticConversationsRef.current.delete(turnId);
      }
      fetchConversations();
      refreshHomeThreads();
    },
    [fetchConversations, refreshHomeThreads]
  );

  // Navigation. Every way into a conversation — the sidebar, a card, an echo,
  // the breadcrumb, a notification, the approvals tray — comes through here.
  // A thread of Home opens in the panel; Home and ordinary conversations open
  // in the centre. A thread covering the centre gives way to what opens there,
  // and one beside Home stays open while Home is chosen again.
  const homeThreadsRef = useRef(homeThreads);
  homeThreadsRef.current = homeThreads;
  const openConversation = useCallback(
    (convId: string) => {
      const home = homeConversationIdRef.current;
      const panelState = panelStateRef.current;
      if (
        home &&
        convId !== home &&
        homeThreadsRef.current.some((thread) => thread.conversation_id === convId)
      ) {
        const next = { threadId: convId, expanded: panelState?.expanded ?? false };
        if (centerConversationIdRef.current === convId) {
          // The thread is in the centre already: Home takes the centre back.
          panelStateRef.current = next;
          if (centerRef.current?.openConversation(home)) {
            setPanel(next);
          } else {
            panelStateRef.current = panelState;
          }
        } else if (panelState?.threadId !== convId) {
          setPanelState(next, 'push');
        }
        setMobileShowList(false);
        return;
      }
      const closesPanel = panelState !== null && (convId !== home || panelCoversCenterRef.current);
      if (closesPanel && centerConversationIdRef.current === convId) {
        setPanelState(null, 'push');
        setMobileShowList(false);
        return;
      }
      // Closed first, so the centre's own history entry is the one recorded.
      if (closesPanel) {
        panelStateRef.current = null;
      }
      if (centerRef.current?.openConversation(convId)) {
        if (closesPanel) {
          setPanelState(null, 'none');
        }
        setMobileShowList(false);
      } else if (closesPanel) {
        panelStateRef.current = panelState;
      }
    },
    [setPanelState]
  );

  // The first turn of each thread the user started here, until the pane that
  // opens the thread takes it to follow.
  const turnsToFollowRef = useRef(new Map<string, TurnToFollow>());
  const takeTurnToFollow = useCallback((conversationId: string): TurnToFollow | null => {
    const turn = turnsToFollowRef.current.get(conversationId) ?? null;
    turnsToFollowRef.current.delete(conversationId);
    return turn;
  }, []);

  const startThread = useCallback(
    async (request: StartThreadRequest): Promise<ThreadSummary> => {
      const { thread, turn } = await startHomeThread(request);
      if (turn) {
        turnsToFollowRef.current.set(thread.conversation_id, {
          turnId: turn.turn_id,
          firstSeq: turn.first_seq,
          alreadyComplete: turn.already_complete === true,
          incomplete: turn.incomplete === true,
        });
      }
      addHomeThread(thread);
      // Known as a thread before the list re-renders, so it opens in the panel.
      homeThreadsRef.current = [
        thread,
        ...homeThreadsRef.current.filter(
          (entry) => entry.conversation_id !== thread.conversation_id
        ),
      ];
      openConversation(thread.conversation_id);
      refreshHomeThreads();
      return thread;
    },
    [addHomeThread, openConversation, refreshHomeThreads]
  );

  const [newThreadOpen, setNewThreadOpen] = useState(false);
  const openNewThread = useCallback(() => setNewThreadOpen(true), []);

  const [homeQuotes, setHomeQuotes] = useState<ReadonlyMap<number, string>>(() => new Map());
  const reportHomeQuotes = useCallback(
    (quotes: ReadonlyMap<number, string>) => setHomeQuotes(quotes),
    []
  );

  const handleNewChat = useCallback(() => {
    const panelState = panelStateRef.current;
    panelStateRef.current = null;
    if (centerRef.current?.newChat()) {
      if (panelState) {
        setPanelState(null, 'none');
      }
      setMobileShowList(false);
    } else {
      panelStateRef.current = panelState;
    }
  }, [setPanelState]);

  const closePanel = useCallback(() => setPanelState(null, 'push'), [setPanelState]);
  const setPanelExpanded = useCallback(
    (expanded: boolean) => {
      const panelState = panelStateRef.current;
      if (panelState) {
        setPanelState({ ...panelState, expanded }, 'replace');
      }
    },
    [setPanelState]
  );

  // Back and forward move between the places the chat has been.
  useEffect(() => {
    const handlePopState = () => {
      const urlCenter = new URLSearchParams(window.location.search).get('conversation_id');
      const urlPanel = panelFromUrl(window.location.search);
      if (
        urlCenter &&
        urlCenter !== centerConversationIdRef.current &&
        !withHistoryMode('none', () => centerRef.current?.openConversation(urlCenter))
      ) {
        // The centre is mid-send and stays where it is, so the address goes
        // back to saying so.
        writeHistory('push');
        return;
      }
      const current = panelStateRef.current;
      if (urlPanel?.threadId !== current?.threadId || urlPanel?.expanded !== current?.expanded) {
        setPanelState(urlPanel, 'none');
      }
      setMobileShowList(false);
    };
    window.addEventListener('popstate', handlePopState);
    return () => window.removeEventListener('popstate', handlePopState);
  }, [setPanelState, withHistoryMode, writeHistory]);

  // The panel pane follows the thread the panel is for. It refuses to move
  // mid-send, and then the panel stays on what it shows.
  useEffect(() => {
    if (!panel || panelConversationIdRef.current === panel.threadId) {
      return;
    }
    const pane = panelRef.current;
    if (pane && !pane.openConversation(panel.threadId) && panelConversationIdRef.current) {
      setPanelState({ ...panel, threadId: panelConversationIdRef.current }, 'replace');
    }
  }, [panel, setPanelState]);

  const handleBackToList = useCallback(() => setMobileShowList(true), []);

  const showingMobileList = isMobile && mobileShowList;
  const centerHidden = showingMobileList || panelCoversCenter;
  // The conversations on screen right now, one per visible pane.
  const visibleConversationIds = new Set<string>();
  if (!centerHidden && centerConversationId) {
    visibleConversationIds.add(centerConversationId);
  }
  if (panel && !showingMobileList) {
    visibleConversationIds.add(panel.threadId);
  }
  const visibleConversationIdsRef = useRef(visibleConversationIds);
  visibleConversationIdsRef.current = visibleConversationIds;

  // Handle notification clicks - navigate to the conversation
  const handleNotificationClick = useCallback(
    (notifConversationId: string) => {
      if (!visibleConversationIdsRef.current.has(notifConversationId)) {
        openConversation(notifConversationId);
      }
    },
    [openConversation]
  );

  const {
    isSupported: notificationsSupported,
    permission: notificationPermission,
    requestPermission: requestNotificationPermission,
    showNotification: showAnyNotification,
  } = useNotifications({
    enabled: notificationsEnabled,
    // Which conversations are on screen is decided below, across both panes.
    conversationId: null,
    onNotificationClick: handleNotificationClick,
  });
  // A reply in a conversation the reader is looking at needs no notification.
  const showNotification = useCallback(
    (request: NotificationRequest) => {
      if (
        document.visibilityState === 'visible' &&
        visibleConversationIdsRef.current.has(request.conversationId)
      ) {
        return;
      }
      showAnyNotification(request);
    },
    [showAnyNotification]
  );

  // Handle notification preference changes
  const handleNotificationEnabledChange = useCallback((enabled: boolean) => {
    setNotificationsEnabled(enabled);
    localStorage.setItem('notificationsEnabled', String(enabled));
  }, []);

  // Cleanup effect to abort the list fetch on unmount
  useEffect(() => {
    return () => {
      abortControllerRef.current?.abort();
    };
  }, []);

  // Handle window resize
  useEffect(() => {
    // The mobile layout doesn't render the desktop sidebar, so its open state
    // is left as the user set it for when the window widens again.
    const handleResize = () => {
      setIsMobile(window.innerWidth < 768);
      setIsWide(window.innerWidth >= PANEL_MIN_WIDTH);
    };

    window.addEventListener('resize', handleResize);
    return () => window.removeEventListener('resize', handleResize);
  }, []);

  // Keep the conversation list fresh for activity OUTSIDE the open thread (a
  // new chat started in another tab/device, or a delegated/scheduled/background
  // reply) via the account-global activity stream. Each pane's own stream only
  // sees the conversation it shows.
  // A thread's run starting or ending is activity too, and changes its state.
  const handleActivity = useCallback(() => {
    fetchConversations();
    refreshHomeThreads();
  }, [fetchConversations, refreshHomeThreads]);
  useActivityStream({
    enabled: true,
    onActivity: handleActivity,
  });

  // Home is known after the first list load, which then left Home and its
  // threads in; list again without them.
  useEffect(() => {
    if (homeConversationId) {
      fetchConversations();
    }
  }, [homeConversationId, fetchConversations]);

  const homeSidebar = useMemo(
    () =>
      homeConversationId
        ? {
            homeConversationId,
            threads: homeThreads,
            threadsFailed: homeThreadsFailed,
            onRetryThreads: refreshHomeThreads,
            onNewThread: openNewThread,
          }
        : null,
    [homeConversationId, homeThreads, homeThreadsFailed, refreshHomeThreads, openNewThread]
  );

  const [homeLookupFailed, setHomeLookupFailed] = useState(false);
  // Home's id, null while threads are disabled, or 'failed' when it could not
  // be looked up.
  const lookUpHome = useCallback(async (): Promise<string | null | 'failed'> => {
    try {
      const home = await fetchHome();
      setHomeLookupFailed(false);
      return home.enabled ? home.conversation_id : null;
    } catch (error) {
      console.error('Error fetching Home:', error);
      setHomeLookupFailed(true);
      return 'failed';
    }
  }, []);

  const retryHomeLookup = useCallback(async () => {
    const lookup = await lookUpHome();
    if (lookup !== 'failed' && lookup !== null) {
      setHomeConversationId(lookup);
      openConversation(lookup);
    }
  }, [lookUpHome, openConversation]);

  // Initialize conversation ID from URL or localStorage
  useEffect(() => {
    fetchConversations();

    const urlParams = new URLSearchParams(window.location.search);
    const urlConversationId = urlParams.get('conversation_id');
    const initialPrompt = urlParams.get('q');
    const lastConversationId = localStorage.getItem('lastConversationId');
    const opensLanding =
      !(initialPrompt && !initialPromptProcessedRef.current) && !urlConversationId;
    const urlPanel = initialPrompt ? null : panelFromUrl(window.location.search);
    if (urlPanel) {
      setPanelState(urlPanel, 'none');
    }

    if (initialPrompt && !initialPromptProcessedRef.current) {
      // If there's a prompt, start a new chat with it
      withHistoryMode('none', () => centerRef.current?.newChat());
    } else if (urlConversationId) {
      withHistoryMode('none', () => centerRef.current?.openConversation(urlConversationId));
    }

    // With threads enabled the chat lands on Home; otherwise on the last
    // conversation, or a new one. A failed lookup lands as if threads were off,
    // so the chat stays usable, and says so with a way to retry.
    let cancelled = false;
    void lookUpHome().then((lookup) => {
      if (cancelled) {
        return;
      }
      const home = lookup === 'failed' ? null : lookup;
      setHomeConversationId(home);
      if (!opensLanding || landingSettledRef.current) {
        return;
      }
      const landingConversationId = home ?? lastConversationId;
      if (landingConversationId) {
        withHistoryMode('replace', () =>
          centerRef.current?.openConversation(landingConversationId)
        );
      } else {
        withHistoryMode('replace', handleNewChat);
      }
    });
    return () => {
      cancelled = true;
    };
  }, []);

  // A thread reached by a link to it, or opened before Home's threads were
  // known, lands in the centre; once it is known to be a thread, it moves to
  // the panel with Home in the centre, as if opened from Home.
  useEffect(() => {
    if (
      !homeConversationId ||
      !centerConversationId ||
      centerConversationId === homeConversationId ||
      panelStateRef.current !== null ||
      !homeThreads.some((thread) => thread.conversation_id === centerConversationId)
    ) {
      return;
    }
    const promoted = { threadId: centerConversationId, expanded: false };
    panelStateRef.current = promoted;
    if (withHistoryMode('replace', () => centerRef.current?.openConversation(homeConversationId))) {
      setPanel(promoted);
    } else {
      panelStateRef.current = null;
    }
  }, [homeConversationId, homeThreads, centerConversationId, withHistoryMode]);

  // Handle initial prompt from query parameter once the centre pane is on the
  // new conversation it is for
  useEffect(() => {
    const initialPrompt = searchParams.get('q');

    if (
      initialPrompt &&
      !initialPromptProcessedRef.current &&
      centerConversationId?.startsWith('web_conv_')
    ) {
      initialPromptProcessedRef.current = true;
      centerRef.current?.send(initialPrompt);

      // Clean up URL using React Router's setSearchParams
      setSearchParams(
        (prev) => {
          const newParams = new URLSearchParams(prev);
          newParams.delete('q');
          newParams.set('conversation_id', centerConversationId);
          return newParams;
        },
        { replace: true }
      );
    }
  }, [centerConversationId, searchParams, setSearchParams]);

  // Signal that app is ready (for tests)
  // Only set when initial data loading is complete
  useEffect(() => {
    if (!conversationsLoading && !profilesLoading) {
      document.documentElement.setAttribute('data-app-ready', 'true');
    } else {
      document.documentElement.removeAttribute('data-app-ready');
    }
    return () => {
      document.documentElement.removeAttribute('data-app-ready');
    };
  }, [conversationsLoading, profilesLoading]);

  // Bumped when a held-back confirmation's inline grace runs out, so the tray
  // re-evaluates it.
  const [trayClock, setTrayClock] = useState(0);
  const { trayConfirmations, nextTrayRelease } = useMemo(() => {
    void trayClock;
    const panes = [
      centerHidden ? undefined : paneReports.center,
      panel && !showingMobileList ? paneReports.panel : undefined,
    ].filter((pane): pane is PaneReport => pane !== undefined);
    const now = Date.now();
    let nextRelease: number | null = null;
    const inTray = Array.from(pendingConfirmations.values()).filter((confirmation) => {
      if (!confirmation.tool_call_id) {
        return true;
      }
      const toolCallId = confirmation.tool_call_id;
      if (panes.some((pane) => pane.toolCallIds.has(toolCallId))) {
        return false;
      }
      // A confirmation for a turn in flight on screen can land just before its
      // tool call renders; give it a moment to show inline rather than
      // flashing through the tray first.
      const belongsToLiveTurn = panes.some(
        (pane) =>
          pane.isStreaming &&
          (confirmation.received_via_sse === true ||
            (typeof confirmation.conversation_id === 'string' &&
              confirmation.conversation_id === pane.conversationId))
      );
      const firstSeenAt = confirmation.first_seen_at;
      if (belongsToLiveTurn && typeof firstSeenAt === 'number') {
        const releaseAt = firstSeenAt + INLINE_CONFIRMATION_GRACE_MS;
        if (releaseAt > now) {
          nextRelease = nextRelease === null ? releaseAt : Math.min(nextRelease, releaseAt);
          return false;
        }
      }
      return true;
    });
    return { trayConfirmations: inTray, nextTrayRelease: nextRelease };
  }, [paneReports, pendingConfirmations, trayClock, centerHidden, panel, showingMobileList]);

  useEffect(() => {
    if (nextTrayRelease === null) {
      return;
    }
    const timer = window.setTimeout(
      () => setTrayClock((value) => value + 1),
      Math.max(0, nextTrayRelease - Date.now())
    );
    return () => window.clearTimeout(timer);
  }, [nextTrayRelease]);

  const handleTrayConfirmation = (requestId: string, approved: boolean) =>
    confirmTool(requestId, approved, centerConversationId);

  const shell = useMemo<ChatShellValue>(
    () => ({
      homeConversationId,
      homeThreads,
      updateHomeThread,
      refreshHomeThreads,
      openConversation,
      noteTurnStarted,
      noteTurnSettled,
      pendingConfirmations,
      onConfirmationRequest: handleConfirmationRequest,
      onConfirmationResult: handleConfirmationResult,
      confirmTool,
      showNotification,
      drafts,
      startThread,
      takeTurnToFollow,
      openNewThread,
      homeQuotes,
      reportHomeQuotes,
    }),
    [
      homeConversationId,
      homeThreads,
      updateHomeThread,
      refreshHomeThreads,
      openConversation,
      noteTurnStarted,
      noteTurnSettled,
      pendingConfirmations,
      handleConfirmationRequest,
      handleConfirmationResult,
      confirmTool,
      showNotification,
      drafts,
      startThread,
      takeTurnToFollow,
      openNewThread,
      homeQuotes,
      reportHomeQuotes,
    ]
  );

  const notificationControls = (
    <>
      <NotificationSettings
        enabled={notificationsEnabled}
        onEnabledChange={handleNotificationEnabledChange}
        permission={notificationPermission}
        onRequestPermission={requestNotificationPermission}
        isSupported={notificationsSupported}
      />
      <PushNotificationButton />
    </>
  );

  const sidebarToggle = isMobile ? null : (
    <Button
      variant="ghost"
      size="sm"
      onClick={() => setSidebarOpen(!sidebarOpen)}
      aria-label="Toggle conversation list"
      aria-expanded={sidebarOpen}
    >
      {sidebarOpen ? <PanelLeftClose className="h-4 w-4" /> : <PanelLeftOpen className="h-4 w-4" />}
    </Button>
  );

  const alerts = (
    <>
      <PendingConfirmationsTray
        confirmations={trayConfirmations}
        loadError={pendingConfirmationsError}
        onConfirm={handleTrayConfirmation}
        currentConversationId={centerConversationId}
        onOpenConversation={openConversation}
      />
      {homeLookupFailed && (
        <div
          role="alert"
          className="flex flex-shrink-0 items-center gap-2 border-b border-destructive/30 bg-destructive/5 px-4 py-2 text-sm text-destructive md:px-6"
          data-testid="home-lookup-failed"
        >
          <span className="flex-1">Couldn't load Home.</span>
          <Button variant="outline" size="sm" onClick={() => void retryHomeLookup()}>
            Retry
          </Button>
        </div>
      )}
    </>
  );

  const panelControls = (
    <>
      <Button
        variant="ghost"
        size="sm"
        className="h-8 w-8 p-0"
        onClick={() => setPanelExpanded(true)}
        aria-label="Expand thread"
        title="Expand thread"
        data-testid="panel-expand"
      >
        <Maximize2 className="h-4 w-4" />
      </Button>
      <Button
        variant="ghost"
        size="sm"
        className="h-8 w-8 p-0"
        onClick={closePanel}
        aria-label="Close thread"
        title="Close thread"
        data-testid="panel-close"
      >
        <X className="h-4 w-4" />
      </Button>
    </>
  );
  const coveringPanelControls = (
    <>
      {panel?.expanded && isWide && !isMobile && (
        <Button
          variant="ghost"
          size="sm"
          onClick={() => setPanelExpanded(false)}
          aria-label="Collapse thread to the side panel"
          title="Collapse thread to the side panel"
          data-testid="panel-collapse"
        >
          <Minimize2 className="h-4 w-4" />
        </Button>
      )}
      {notificationControls}
    </>
  );
  const sidebarConversationId = panel?.threadId ?? centerConversationId;

  return (
    <ChatShellContext.Provider value={shell}>
      <TooltipProvider>
        <div className="flex h-dvh flex-col bg-background">
          {/* Mobile: the conversation list, in place of the chat */}
          {showingMobileList && (
            <div className="flex flex-1 flex-col min-h-0">
              <AppTopBar title="Chat" />
              <ConversationSidebar
                conversations={conversations}
                conversationsLoading={conversationsLoading}
                currentConversationId={sidebarConversationId}
                onConversationSelect={openConversation}
                onNewChat={handleNewChat}
                isOpen={true}
                onRefresh={fetchConversations}
                isMobile={isMobile}
                home={homeSidebar}
              />
            </div>
          )}

          <div className={showingMobileList ? 'hidden' : 'flex min-h-0 flex-1 overflow-hidden'}>
            {!isMobile && (
              <ConversationSidebar
                conversations={conversations}
                conversationsLoading={conversationsLoading}
                currentConversationId={sidebarConversationId}
                onConversationSelect={openConversation}
                onNewChat={handleNewChat}
                isOpen={sidebarOpen}
                onRefresh={fetchConversations}
                isMobile={isMobile}
                home={homeSidebar}
              />
            )}
            <ConversationPane
              ref={centerRef}
              profileId={profileId}
              layout={isMobile ? 'mobile' : 'desktop'}
              presentation="main"
              hidden={centerHidden}
              onConversationChange={handleCenterConversationChange}
              onReport={handleCenterReport}
              headerStart={sidebarToggle}
              headerEnd={notificationControls}
              topSlot={panelCoversCenter ? undefined : alerts}
              onBackToList={handleBackToList}
            />
            {panel && (
              <ConversationPane
                ref={panelRef}
                profileId={profileId}
                layout={isMobile ? 'mobile' : 'desktop'}
                presentation={panelCoversCenter ? 'main' : 'side'}
                hidden={showingMobileList}
                onConversationChange={handlePanelConversationChange}
                onReport={handlePanelReport}
                headerStart={panelCoversCenter ? sidebarToggle : undefined}
                headerEnd={panelCoversCenter ? coveringPanelControls : panelControls}
                topSlot={panelCoversCenter ? alerts : undefined}
                onBackToList={handleBackToList}
              />
            )}
          </div>
        </div>
        {homeConversationId && (
          <StartThreadDialog open={newThreadOpen} onOpenChange={setNewThreadOpen} />
        )}
      </TooltipProvider>
    </ChatShellContext.Provider>
  );
};

// One profile fetch for the whole page: the picker, the intelligence control
// and the per-message badges in the thread all read the same list.
const ChatApp: React.FC<ChatAppProps> = (props) => (
  <ProfilesProvider>
    <ChatAppContent {...props} />
  </ProfilesProvider>
);

export default ChatApp;
