import { AssistantRuntimeProvider, useExternalStoreRuntime } from '@assistant-ui/react';
import { ArrowLeft, Menu, PanelLeftClose, PanelLeftOpen } from 'lucide-react';
import React, { Component, useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { ErrorInfo, ReactNode } from 'react';
import { useSearchParams } from 'react-router-dom';
import { Button } from '@/components/ui/button';
import { TooltipProvider } from '@/components/ui/tooltip';
import NavigationSheet from '../shared/NavigationSheet';
import { getDiagnosticsUrl } from '../utils/diagnosticsUrl';
import { parseToolArguments } from '../utils/toolUtils';
import { generateUUID } from '../utils/uuid';
import { defaultAttachmentAdapter } from './attachmentAdapter';
import ConversationSidebar from './ConversationSidebar';
import { LOADING_MARKER } from './constants';
import { NotificationSettings } from './NotificationSettings';
import { PendingConfirmationsTray } from './PendingConfirmationsTray';
import ProfileSelector from './ProfileSelector';
import { PushNotificationButton } from './PushNotificationButton';
import { ChatControlsContext } from './chatControls';
import { Thread } from './Thread';
import { ToolConfirmationProvider } from './ToolConfirmationContext';
import type { PendingToolConfirmation } from './ToolConfirmationContext';
import {
  BackendAttachment,
  BackendConversationMessage,
  ChatAppProps,
  Conversation,
  ConversationMessagesResponse,
  Message,
  MessageContent,
} from './types';
import { useLiveMessageUpdates } from './useLiveMessageUpdates';
import { useNotifications } from './useNotifications';
import { useStreamingResponse } from './useStreamingResponse';

// Error boundary to catch transient @assistant-ui/store tapClientLookup race
// condition errors during rendering (assistant-ui/assistant-ui#3395).
// Without this boundary, the error propagates up and destroys the entire
// React tree. The boundary catches the error, briefly renders null, then
// re-mounts children with a fresh key so the tap system can reinitialize.
interface ThreadErrorBoundaryState {
  hasError: boolean;
  retryKey: number;
}

class ThreadErrorBoundary extends Component<{ children: ReactNode }, ThreadErrorBoundaryState> {
  state: ThreadErrorBoundaryState = { hasError: false, retryKey: 0 };

  static getDerivedStateFromError(): Partial<ThreadErrorBoundaryState> {
    return { hasError: true };
  }

  componentDidCatch(error: Error, _info: ErrorInfo): void {
    if (!error.message?.includes('tapClientLookup')) {
      throw error;
    }
    // Schedule re-mount with a new key so children get a fresh fiber tree
    if (this.state.retryKey < 3) {
      queueMicrotask(() => {
        this.setState((s) => ({ hasError: false, retryKey: s.retryKey + 1 }));
      });
    }
  }

  render(): ReactNode {
    if (this.state.hasError) {
      return null;
    }
    return <React.Fragment key={this.state.retryKey}>{this.props.children}</React.Fragment>;
  }
}

const LIVE_CONFIRMATION_POLL_RACE_GRACE_MS = 30000;

// Fallback reconcile for a turn that gave up while still running server-side. The
// always-on follow stream is the primary completion signal, but it tails
// future-only and may not be connected, so re-poll history until the turn
// resolves. Bounded so a turn wedged "running" server-side can't poll forever.
// Exported (mutable) so tests can shrink the interval instead of waiting on it.
export const reconcilePollTuning = { intervalMs: 4000, maxPolls: 30 };

// Outcome of a history reload. The interrupted-stream give-up path distinguishes
// these: 'applied' has authoritative messages to judge a turn against; 'failed'
// (non-OK / fetch error) couldn't reconcile, so an unconfirmed-reply error must
// still surface; 'bailed' (a stream is active for the conversation, or the fetch
// was superseded) means another path will reconcile — stay silent.
type ReloadResult = 'applied' | 'bailed' | 'failed';

/** Whether the reconciled history contains a terminal assistant reply for a
 * turn. A non-empty text row, including persisted error rows rendered as
 * assistant text, counts. Tool-call-only rows count only when the backend still
 * has an explicit completed TurnRecord for that turn; otherwise they can be
 * partial rows persisted before the final reply/error committed. */
function hasTerminalReplyForTurn(
  messages: Message[],
  turnId: string,
  completedTurnIds: Set<string>
): boolean {
  return messages.some(
    (msg) =>
      msg.turnId === turnId &&
      msg.role === 'assistant' &&
      Array.isArray(msg.content) &&
      msg.content.some(
        (part) =>
          (part.type === 'text' &&
            part.text !== LOADING_MARKER &&
            Boolean((part.text ?? '').trim())) ||
          (part.type === 'tool-call' && completedTurnIds.has(turnId))
      )
  );
}

/** The "couldn't confirm the reply" placeholder shown for a turn that gave up
 * (or got a 410) and has no persisted reply. Re-derived on every reconcile, so a
 * later reply replaces it and a reload can't silently erase it. */
function buildUnconfirmedReplyError(conversationId: string, turnId: string): Message {
  const diagnosticsUrl = getDiagnosticsUrl({ conversationId });
  return {
    id: `msg_unconfirmed_${generateUUID()}`,
    role: 'assistant',
    turnId,
    content: [
      {
        type: 'text',
        text: `Sorry, I couldn't confirm the reply finished. Please try again. [View diagnostics](${diagnosticsUrl}) for debugging details.`,
      },
    ],
    createdAt: new Date(),
    status: { type: 'complete' },
  };
}

/** Place a turn's "couldn't confirm" marker into a message list: replace the
 * turn's stranded LOADING_MARKER spinner in place (so the user doesn't see a
 * spinner alongside the marker), else append. Deduped per turn. Does NOT treat
 * optimistic partial content (a streamed tool call / partial text) as a finished
 * reply — a give-up means turn_ended never arrived, so the partial is not proof.
 * Returns `messages` unchanged when a marker for the turn is already present. */
function surfaceUnconfirmedMarker(
  messages: Message[],
  conversationId: string,
  turnId: string
): Message[] {
  if (messages.some((msg) => msg.turnId === turnId && msg.id.startsWith('msg_unconfirmed_'))) {
    return messages;
  }
  const errorMessage = buildUnconfirmedReplyError(conversationId, turnId);
  const spinnerIndex = messages.findIndex(
    (msg) =>
      msg.turnId === turnId &&
      msg.role === 'assistant' &&
      Array.isArray(msg.content) &&
      msg.content.some((part) => part.type === 'text' && part.text === LOADING_MARKER)
  );
  if (spinnerIndex !== -1) {
    const next = [...messages];
    next[spinnerIndex] = errorMessage;
    return next;
  }
  return [...messages, errorMessage];
}

/** Keep a visible loading assistant row for turns that history still reports as
 * running but has not persisted any assistant row for yet. */
function preserveRunningLoadingMessages(
  reloadedMessages: Message[],
  previousMessages: Message[],
  runningTurnIds: Set<string>
): Message[] {
  let next = reloadedMessages;
  for (const turnId of runningTurnIds) {
    const hasAssistantRow = next.some((msg) => msg.turnId === turnId && msg.role === 'assistant');
    if (hasAssistantRow) {
      continue;
    }
    const previousLoading = previousMessages.find(
      (msg) =>
        msg.turnId === turnId &&
        msg.role === 'assistant' &&
        Array.isArray(msg.content) &&
        msg.content.some((part) => part.type === 'text' && part.text === LOADING_MARKER)
    );
    const loadingMessage =
      previousLoading ??
      ({
        id: `msg_loading_${turnId}`,
        role: 'assistant',
        turnId,
        content: [{ type: 'text', text: LOADING_MARKER }],
        isLoading: true,
        createdAt: new Date(),
      } satisfies Message);
    let insertAfterIndex = -1;
    for (let index = 0; index < next.length; index += 1) {
      if (next[index]?.turnId === turnId) {
        insertAfterIndex = index;
      }
    }
    if (insertAfterIndex === -1) {
      next = [...next, loadingMessage];
    } else {
      next = [
        ...next.slice(0, insertAfterIndex + 1),
        loadingMessage,
        ...next.slice(insertAfterIndex + 1),
      ];
    }
  }
  return next;
}

/** Extract plain text from a backend message's content for a notification
 * preview. Content is either a string or an array of parts; only `text` parts
 * contribute (image/tool parts have no preview text). */
function extractMessagePreview(content: BackendConversationMessage['content']): string {
  if (typeof content === 'string') {
    return content.trim();
  }
  if (Array.isArray(content)) {
    return content
      .map((part) => (part.type === 'text' ? (part as { text?: unknown }).text : null))
      .filter((text): text is string => typeof text === 'string')
      .join('\n')
      .trim();
  }
  return '';
}

function normalizeAttachmentForToolArtifact(attachment: BackendAttachment): BackendAttachment {
  const attachmentId =
    typeof attachment.attachment_id === 'string'
      ? attachment.attachment_id
      : typeof attachment.id === 'string'
        ? attachment.id
        : undefined;
  if (!attachmentId) {
    return attachment;
  }

  const contentUrl =
    typeof attachment.content_url === 'string' && attachment.content_url
      ? attachment.content_url
      : `/api/attachments/${encodeURIComponent(attachmentId)}`;
  const mimeType =
    typeof attachment.mime_type === 'string' && attachment.mime_type
      ? attachment.mime_type
      : typeof attachment.content_type === 'string' && attachment.content_type
        ? attachment.content_type
        : 'application/octet-stream';

  let attachmentType: 'image' | 'tool_result' | 'user' = 'tool_result';
  if (mimeType.startsWith('image/')) {
    attachmentType = 'image';
  } else if (
    attachment.type === 'user' &&
    typeof attachment.filename === 'string' &&
    typeof attachment.size === 'number'
  ) {
    attachmentType = 'user';
  }

  return {
    ...attachment,
    attachment_id: attachmentId,
    type: attachmentType,
    mime_type: mimeType,
    content_url: contentUrl,
  };
}

function isToolOnlyAssistantMessage(message: Message): boolean {
  return (
    message.role === 'assistant' &&
    message.content.length > 0 &&
    message.content.every((part) => part.type === 'tool-call')
  );
}

export function mergeConsecutiveToolOnlyAssistantMessages(messages: Message[]): Message[] {
  const mergedMessages: Message[] = [];

  for (const message of messages) {
    const previousMessage = mergedMessages[mergedMessages.length - 1];
    if (
      previousMessage &&
      isToolOnlyAssistantMessage(previousMessage) &&
      isToolOnlyAssistantMessage(message)
    ) {
      mergedMessages[mergedMessages.length - 1] = {
        ...previousMessage,
        content: [...previousMessage.content, ...message.content],
        status:
          previousMessage.status?.type === 'running' || message.status?.type === 'running'
            ? { type: 'running' }
            : { type: 'complete' },
      };
      continue;
    }

    mergedMessages.push(message);
  }

  return mergedMessages;
}

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

const ChatApp: React.FC<ChatAppProps> = ({ profileId = 'default_assistant' }) => {
  const [messages, setMessages] = useState<Message[]>([]);
  const [isLoading, setIsLoading] = useState<boolean>(false);
  const [sidebarOpen, setSidebarOpen] = useState<boolean>(window.innerWidth > 768);
  const [conversationId, setConversationId] = useState<string | null>(null);
  // Always-current mirror of conversationId, so a deferred follow-up timer can
  // synchronously check whether the conversation changed before it fires.
  const conversationIdRef = useRef<string | null>(null);
  conversationIdRef.current = conversationId;
  // Set to a conversation id while it has a turn that gave up but is still running
  // server-side, to drive the fallback reconcile poll. Cleared once the turn
  // resolves (reply lands or it finishes with none).
  const [pendingReconcileConvId, setPendingReconcileConvId] = useState<string | null>(null);
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [conversationsLoading, setConversationsLoading] = useState<boolean>(true);
  const [profilesLoading, setProfilesLoading] = useState<boolean>(true);
  const [isMobile, setIsMobile] = useState<boolean>(window.innerWidth <= 768);
  const [mobileShowList, setMobileShowList] = useState<boolean>(false);
  const [currentProfileId, setCurrentProfileId] = useState<string>(() => {
    // Load saved profile from localStorage, fallback to prop
    return localStorage.getItem('selectedProfileId') || profileId;
  });
  const [notificationsEnabled, setNotificationsEnabled] = useState<boolean>(() => {
    // Load notification preference from localStorage
    const saved = localStorage.getItem('notificationsEnabled');
    return saved === 'true';
  });
  const streamingMessageIdRef = useRef<string | null>(null);
  const activeStreamConversationIdRef = useRef<string | null>(null);
  const toolCallMessageIdRef = useRef<string | null>(null);
  const lastStreamingErrorRef = useRef<string | null>(null);
  // The turn id of the currently-streaming turn, used to tag mid-turn steering
  // user bubbles. Set in handleNew, cleared when the turn completes.
  const activeTurnIdRef = useRef<string | null>(null);
  // The steer-input draft, owned here so it survives the SteerBar unmounting at
  // turn end (a steer the turn never echoed is preserved for resend). Cleared
  // when the conversation changes so it doesn't leak across threads.
  const [steerDraft, setSteerDraft] = useState('');
  // Last steer error (e.g. a transient 5xx), shown in the SteerBar; the draft is
  // kept so the user can retry.
  const [steerError, setSteerError] = useState<string | null>(null);
  // Follow-up messages queued while a stream was still settling (a steer that
  // hit a finished turn, or recovered un-echoed steers). Fired one at a time
  // from the completion handler — after the ending stream's shared-ref cleanup —
  // so they don't race it or start concurrent turns.
  const pendingFollowupsRef = useRef<string[]>([]);
  // Accepted steers awaiting their user_input echo. If the turn completes
  // without echoing them (the model was in a final text-only iteration, so the
  // loop never drained them), they're recovered as normal follow-ups rather than
  // left stale in a SteerBar that's about to unmount. A list, since the user can
  // submit several steers during one long turn.
  const awaitingEchoSteersRef = useRef<string[]>([]);
  useEffect(() => {
    setSteerDraft('');
    setSteerError(null);
    // Drop any queued/awaiting steers so they can't fire into the new conversation.
    awaitingEchoSteersRef.current = [];
    pendingFollowupsRef.current = [];
  }, [conversationId]);
  // handleNew is defined after the streaming callbacks; the completion handler
  // reaches it via this ref to fire a queued follow-up.
  const handleNewRef = useRef<((message: { content: { text: string }[] }) => Promise<void>) | null>(
    null
  );
  // Set when the running turn ended because the user stopped it, so the
  // completion handler can render a "stopped" affordance instead of an empty
  // bubble (and never an error toast).
  const turnStoppedRef = useRef(false);
  const initialPromptProcessedRef = useRef(false);
  const abortControllerRef = useRef<AbortController | null>(null);
  const messagesAbortControllerRef = useRef<AbortController | null>(null);
  const resolvedConfirmationIdsRef = useRef<Set<string>>(new Set());
  // Track turn IDs originated by this tab so we can skip the live-update reload
  // when a turn_ended event for our own turn arrives (we already hold the freshest
  // state locally from streaming; reloading risks clobbering it).
  const selfTurnIdsRef = useRef<Set<string>>(new Set());
  // Turns that gave up — or got a 410 — without a confirmed reply, keyed by
  // turnId. `convId` scopes the marker to its own thread; `markIfNoReply`
  // distinguishes a true give-up (surface the "couldn't confirm" marker when the
  // turn finishes with no reply) from a 410 reconcile (assume the durably-
  // persisted reply will appear; never mark). The history reconcile
  // (loadConversationMessages) re-derives the marker from this map on EVERY
  // reload — clearing a turn once its reply lands and re-appending while a
  // gave-up turn is finished with none — so a later reply replaces the marker and
  // no reload can silently erase it.
  const unconfirmedTurnsRef = useRef<Map<string, { convId: string; markIfNoReply: boolean }>>(
    new Map()
  );
  // The hub `message`/`turn_ended` events are content-free, so the in-app
  // notification must be derived from reloaded history. We keep the latest
  // assistant message internal_id seen per conversation so a background reload
  // can tell a genuinely new out-of-band reply from an unchanged one, and a
  // ref to showNotification so the empty-dep loader can call it without
  // recreating itself on every render.
  const lastSeenAssistantIdRef = useRef<Map<string, string>>(new Map());
  const showNotificationRef = useRef<
    | ((data: {
        conversationId: string;
        messageId: string;
        preview: string;
        timestamp: string;
      }) => void)
    | null
  >(null);
  const [searchParams, setSearchParams] = useSearchParams();

  // Fetch conversations list
  const fetchConversations = useCallback(async () => {
    try {
      // Cancel previous request if it exists
      if (abortControllerRef.current) {
        abortControllerRef.current.abort();
      }

      // Create new abort controller
      const abortController = new AbortController();
      abortControllerRef.current = abortController;

      setConversationsLoading(true);
      const response = await fetch('/api/v1/chat/conversations?interface_type=web', {
        signal: abortController.signal,
      });
      if (response.ok) {
        const data = await response.json();
        setConversations(data.conversations);
      }
    } catch (error) {
      // Don't log error if request was aborted (component unmounting)
      if (error instanceof Error && error.name !== 'AbortError') {
        console.error('Error fetching conversations:', error);
      }
    } finally {
      setConversationsLoading(false);
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
        const newMap = new Map(prev);
        newMap.set(confirmationKey(request), {
          ...request,
          created_at: request.created_at ?? receivedAt,
          received_at: receivedAt,
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

  const handleConfirmation = useCallback(
    async (_toolCallId: string, requestId: string, approved: boolean) => {
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
    [conversationId, handleConfirmationResult]
  );

  const fetchPendingConfirmations = useCallback(async () => {
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
      setPendingConfirmationsError(null);
      setPendingConfirmations((prev) => {
        const receivedAt = Date.now();
        const newMap = new Map<string, PendingToolConfirmation>();
        for (const confirmation of confirmations) {
          newMap.set(confirmationKey(confirmation), {
            ...confirmation,
            received_at: receivedAt,
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
    } catch (error) {
      console.error('Error fetching pending confirmations:', error);
      setPendingConfirmationsError('Could not load pending approvals. Refresh or try again.');
    }
  }, [confirmationKey]);

  useEffect(() => {
    void fetchPendingConfirmations();
    const interval = window.setInterval(() => {
      void fetchPendingConfirmations();
    }, 15000);
    return () => window.clearInterval(interval);
  }, [fetchPendingConfirmations]);

  // Streaming callbacks
  const handleStreamingMessage = useCallback((content: string) => {
    if (!content.trim()) {
      return;
    }

    if (streamingMessageIdRef.current) {
      setMessages((prev) => {
        // Update the loading message with actual content
        return prev.map((msg) => {
          if (msg.id === streamingMessageIdRef.current) {
            // Preserve existing tool calls when updating text
            const existingContent = msg.content || [];
            const toolCalls = existingContent.filter((part) => part.type === 'tool-call');

            // Create new content array with updated text and preserved tool calls
            const newContent: MessageContent[] = [
              {
                type: 'text',
                text: content, // Use the accumulated content directly from the hook
              },
              ...toolCalls, // Preserve any existing tool calls with their status and results
            ];

            return {
              ...msg,
              content: newContent,
              isLoading: false, // Remove loading flag when content arrives
            };
          }
          return msg;
        });
      });
    }
  }, []);

  const handleStreamingError = useCallback((error: Error | string, _metadata: unknown) => {
    console.error('Streaming error:', error, _metadata);
    // Store the error but don't treat it as terminal — the stream may recover
    // with subsequent tool results or text content
    lastStreamingErrorRef.current = typeof error === 'string' ? error : error.message;
  }, []);

  const handleStreamingComplete = useCallback(
    ({
      content,
      toolCalls: _toolCalls,
      completed = true,
    }: {
      content: string;
      toolCalls: Array<Record<string, unknown>>;
      completed?: boolean;
    }) => {
      // Capture ref values locally to avoid race conditions
      const messageId = streamingMessageIdRef.current;
      const toolCallMessageId = toolCallMessageIdRef.current;
      const lastError = lastStreamingErrorRef.current;
      const wasStopped = turnStoppedRef.current;
      turnStoppedRef.current = false;

      if (messageId) {
        const hasContent = Boolean(content);
        const hasToolCalls = Boolean(toolCallMessageId);

        if (hasContent) {
          // Has text content: update message with text + preserved tool calls
          const diagnosticsUrl = lastError
            ? getDiagnosticsUrl({ conversationId: conversationId ?? undefined })
            : null;
          const errorSuffix = lastError
            ? `\n\n---\n*An error also occurred during this response. [View diagnostics](${diagnosticsUrl}) for details.*`
            : '';
          setMessages((prev) =>
            prev.map((msg) => {
              if (msg.id === messageId) {
                const existingToolCalls =
                  msg.content?.filter((part) => part.type === 'tool-call') || [];
                return {
                  ...msg,
                  content: [{ type: 'text', text: content + errorSuffix }, ...existingToolCalls],
                  status: { type: 'complete' as const },
                  isLoading: false,
                };
              }
              return msg;
            })
          );
        } else if (hasToolCalls && lastError) {
          // Tool calls present but stream ended with an error before generating
          // a text response (e.g., tool error caused a follow-up LLM failure).
          // Show the error so the user knows what happened.
          const diagnosticsUrl = getDiagnosticsUrl({
            conversationId: conversationId ?? undefined,
          });
          setMessages((prev) =>
            prev.map((msg) => {
              if (msg.id === messageId) {
                const existingToolCalls =
                  msg.content?.filter((part) => part.type === 'tool-call') || [];
                return {
                  ...msg,
                  content: [
                    {
                      type: 'text',
                      text: `Sorry, I encountered an error after running tools. [View diagnostics](${diagnosticsUrl}) for debugging details.`,
                    },
                    ...existingToolCalls,
                  ],
                  status: { type: 'complete' as const },
                  isLoading: false,
                };
              }
              return msg;
            })
          );
        } else if (hasToolCalls) {
          // No text but has tool calls, no error: just clear loading state and set complete
          setMessages((prev) =>
            prev.map((msg) => {
              if (msg.id === messageId) {
                return {
                  ...msg,
                  status: { type: 'complete' as const },
                  isLoading: false,
                };
              }
              return msg;
            })
          );
        } else if (lastError) {
          // No text, no tool calls, but had an error: show error message
          const diagnosticsUrl = getDiagnosticsUrl({
            conversationId: conversationId ?? undefined,
          });
          setMessages((prev) =>
            prev.map((msg) =>
              msg.id === messageId
                ? {
                    ...msg,
                    content: [
                      {
                        type: 'text',
                        text: `Sorry, I encountered an error processing your message. [View diagnostics](${diagnosticsUrl}) for debugging details.`,
                      },
                    ],
                    isLoading: false,
                  }
                : msg
            )
          );
        } else if (wasStopped) {
          // The user stopped the turn before any text or tool calls: show a
          // "stopped" marker rather than an empty bubble.
          setMessages((prev) =>
            prev.map((msg) =>
              msg.id === messageId
                ? {
                    ...msg,
                    content: [{ type: 'text', text: '_Stopped._' }],
                    status: { type: 'complete' as const },
                    isLoading: false,
                  }
                : msg
            )
          );
        } else {
          // No text, no tool calls, no error: just clear loading state
          setMessages((prev) =>
            prev.map((msg) => (msg.id === messageId ? { ...msg, isLoading: false } : msg))
          );
        }
      }

      // Update tool call message status when streaming completes
      if (toolCallMessageId) {
        setMessages((prev) =>
          prev.map((msg) => {
            if (msg.id === toolCallMessageId) {
              const allToolsComplete = msg.content?.every(
                (part) => part.type !== 'tool-call' || part.result !== undefined
              );
              return {
                ...msg,
                status: allToolsComplete ? { type: 'complete' } : msg.status,
              };
            }
            return msg;
          })
        );
      }

      // Always clean up refs and refresh conversations
      if (streamingMessageIdRef.current === messageId) {
        streamingMessageIdRef.current = null;
        activeStreamConversationIdRef.current = null;
        activeTurnIdRef.current = null;
      }
      if (toolCallMessageIdRef.current === toolCallMessageId) {
        toolCallMessageIdRef.current = null;
      }
      lastStreamingErrorRef.current = null;
      fetchConversations();

      // Recover/queue follow-ups only on a clean completion we actually saw end.
      // - completed === false: a local detach (cancelStream during navigation) —
      //   the server turn keeps running and may still drain the steer, so
      //   resending would duplicate it.
      // - stopped/failed: the user pressed Stop, or the turn errored. Resending
      //   an abandoned steer would restart the very interaction Stop was meant to
      //   end, so abandon any queued/awaiting steers instead.
      const cleanCompletion = completed && !wasStopped && !lastError;
      if (cleanCompletion) {
        // Accepted steers the turn never echoed (it finished a final text-only
        // iteration without draining them) would otherwise be stranded in the
        // about-to-unmount SteerBar. Recover them as normal follow-ups.
        const unEchoed = awaitingEchoSteersRef.current;
        if (unEchoed.length > 0) {
          awaitingEchoSteersRef.current = [];
          pendingFollowupsRef.current.push(...unEchoed);
          setSteerDraft((prev) => (unEchoed.some((s) => s.trim() === prev.trim()) ? '' : prev));
        }

        // Fire the next queued follow-up (a steer that hit an already-finished
        // turn, or a recovered un-echoed steer). One at a time: each turn's own
        // completion handler fires the next, so they don't start concurrent
        // turns. Defer past this hook's cleanup (which clears abortControllerRef
        // / activeTurnRef after onComplete returns) so the follow-up turn's refs
        // aren't clobbered and Stop/Steer target it correctly.
        const followup = pendingFollowupsRef.current.shift();
        if (followup) {
          const convAtSchedule = conversationIdRef.current;
          setTimeout(() => {
            // Drop it if the user switched conversations before it fired, so a
            // previous thread's steer can't be sent into the newly selected one.
            if (conversationIdRef.current !== convAtSchedule) {
              return;
            }
            void handleNewRef.current?.({ content: [{ text: followup }] });
          }, 0);
        }
      } else if (completed) {
        // Terminal but not a clean success (stopped or failed): drop any
        // queued/awaiting steers so Stop/failure doesn't auto-start a new turn.
        awaitingEchoSteersRef.current = [];
        pendingFollowupsRef.current = [];
      }
    },
    [conversationId, fetchConversations]
  );

  // A mid-turn steering message the user sent while the turn was running. Render
  // it as a user bubble just before the in-progress assistant bubble so the
  // conversation reads in order; the turn continues streaming after it.
  const handleStreamingUserInput = useCallback((content: string) => {
    const assistantId = streamingMessageIdRef.current;
    const steeringMessage: Message = {
      id: `msg_${Date.now()}_steer_${Math.random().toString(36).slice(2)}`,
      role: 'user',
      turnId: activeTurnIdRef.current ?? undefined,
      content: [{ type: 'text', text: content }],
      createdAt: new Date(),
    };
    setMessages((prev) => {
      const idx = assistantId ? prev.findIndex((m) => m.id === assistantId) : -1;
      if (idx === -1) {
        return [...prev, steeringMessage];
      }
      const next = [...prev];
      next.splice(idx, 0, steeringMessage);
      return next;
    });
    // The echo confirms the turn consumed this steer, so clear the draft if it
    // still matches what was sent (don't clobber a newer edit the user typed)
    // and drop one matching awaiting-echo entry so it isn't recovered later.
    setSteerDraft((prev) => (prev.trim() === content.trim() ? '' : prev));
    const idx = awaitingEchoSteersRef.current.findIndex((s) => s.trim() === content.trim());
    if (idx !== -1) {
      awaitingEchoSteersRef.current.splice(idx, 1);
    }
  }, []);

  // The running turn ended because the user stopped it. Flag it so the
  // completion handler renders a "stopped" affordance (and never an error).
  const handleStreamingCancelled = useCallback(() => {
    turnStoppedRef.current = true;
  }, []);

  // Handle tool calls during streaming
  const handleStreamingToolCall = useCallback((toolCalls: Array<Record<string, unknown>>) => {
    // CRITICAL FIX: Capture the ref value immediately to avoid closure issues
    // This prevents race conditions where the ref gets cleared before setState callback executes
    const targetMessageId = streamingMessageIdRef.current;

    // Check for attach_to_response specifically (removed debug logging)

    if (toolCalls && toolCalls.length > 0 && targetMessageId) {
      // Record the tool-call message id synchronously, NOT inside the
      // setMessages updater below: updater functions only run when React
      // flushes a render. When a turn's SSE events arrive in a single network
      // chunk (e.g. the hub replays an already-finished turn in one burst),
      // onComplete fires in the same task as onToolCall — before any render —
      // and a ref assigned inside the updater would still be null, sending
      // handleStreamingComplete down the wrong (no-tool-calls) branch.
      toolCallMessageIdRef.current = targetMessageId;
      setMessages((prev) => {
        const updatedMessages = prev.map((msg) => {
          if (msg.id === targetMessageId) {
            // This is the message to update.
            // It might be a 'loading' message, or it might already have text.
            const existingTextContent =
              msg.content?.filter((part) => part.type === 'text' && part.text !== LOADING_MARKER) ||
              [];

            // Create new tool parts with new object references
            const toolParts: MessageContent[] = toolCalls.map((tc) => {
              const args = parseToolArguments(tc.arguments);
              const result: MessageContent = {
                type: 'tool-call' as const,
                toolCallId: tc.id as string,
                toolName: tc.name as string,
                args: args as Record<string, unknown> | undefined,
                argsText:
                  typeof tc.arguments === 'string' ? tc.arguments : JSON.stringify(tc.arguments),
              };
              // Add result if present
              if (tc.result !== undefined) {
                result.result = tc.result as string;
              }
              // Add attachments if present
              if (tc.attachments !== undefined && Array.isArray(tc.attachments)) {
                result.attachments = [...tc.attachments];
              }
              return result;
            });

            return {
              ...msg,
              content: [...existingTextContent, ...toolParts],
              isLoading: false,
              status: { type: 'running' as const },
            };
          }
          return msg;
        });

        return updatedMessages;
      });
    }
  }, []);

  // Reload persisted history when the live stream can't show the reply itself:
  // a durably-complete turn (already_complete — never opened /stream) or a
  // dropped/interrupted stream. Deferred so it runs after onComplete clears the
  // active-stream ref; loadConversationMessages bails while that ref is set.
  // loadConversationMessages is referenced via a ref because it is declared
  // after this hook, and the streaming hook must be initialized before it.
  const loadConversationMessagesRef =
    useRef<(convId: string, background?: boolean) => Promise<ReloadResult>>(null);
  const handleReloadHistory = useCallback(
    (
      reloadConversationId: string,
      options?: { errorIfNoReply?: boolean; turnId?: string; errorOnFailedReload?: boolean }
    ) => {
      activeStreamConversationIdRef.current = null;
      // A bounded resume that gave up — or a 410 — no longer holds the turn's
      // freshest state from the stream. Mark the turn unconfirmed (keyed by
      // conversation) so the history reconcile derives/clears its "couldn't
      // confirm" marker, and drop the follow-stream self-skip so the turn's
      // eventual turn_ended triggers a reload (which self-heals the marker)
      // instead of being swallowed as "our own turn".
      if (options?.errorIfNoReply && options.turnId) {
        // errorOnFailedReload marks a TRUE give-up (resume loop exhausted): mark
        // the turn when it can't be confirmed. A 410 reconcile omits it (its
        // reply is durably persisted), so it polls-if-running / shows-if-replied
        // but never surfaces a marker.
        unconfirmedTurnsRef.current.set(options.turnId, {
          convId: reloadConversationId,
          markIfNoReply: !!options.errorOnFailedReload,
        });
        selfTurnIdsRef.current.delete(options.turnId);
      }
      setTimeout(() => {
        void (async () => {
          const result = await loadConversationMessagesRef.current?.(reloadConversationId, true);
          // 'applied' reconciled and re-derived the marker. 'bailed' means a
          // stream was active for this conversation (e.g. the user fired another
          // send during this reconcile), so the re-derive never ran — drive the
          // fallback poll to retry until that stream clears and the marker is
          // derived, otherwise the give-up could be silently swallowed. 'failed'
          // (only a true give-up, errorOnFailedReload) has no server truth, so
          // surface the marker on the current optimistic state now; the turn
          // stays unconfirmed and self-heals on the next successful reload. (A 410
          // omits errorOnFailedReload — its reply is durably persisted — so a
          // transient /messages failure there stays silent and self-heals.)
          if (options?.errorIfNoReply && options.turnId && result === 'bailed') {
            setPendingReconcileConvId(reloadConversationId);
            return;
          }
          if (
            !options?.errorIfNoReply ||
            !options.turnId ||
            result !== 'failed' ||
            !options.errorOnFailedReload
          ) {
            return;
          }
          const turnId = options.turnId;
          setMessages((prev) => surfaceUnconfirmedMarker(prev, reloadConversationId, turnId));
        })();
      }, 0);
    },
    []
  );

  // Does the server still report THIS turn running? The resume loop asks before
  // counting a held-open-but-silent leg as liveness, since the backend holds the
  // conversation stream open while ANY of the user's turns runs (e.g. a
  // concurrent tab), not just ours. limit=1 keeps the payload tiny — active_turns
  // is returned independent of message pagination. On a non-OK/failed check we
  // return false (NOT live): an unverifiable held-open leg must count toward
  // give-up rather than reset the streak forever (a persistently-failing check
  // would otherwise let a concurrent turn hold us spinning). Giving up is safe —
  // the give-up reconcile re-checks active_turns and the fallback poll still
  // recovers a genuinely-running turn's reply.
  const handleCheckTurnActive = useCallback(
    async (convId: string, turnId: string): Promise<boolean> => {
      try {
        const response = await fetch(`/api/v1/chat/conversations/${convId}/messages?limit=1`);
        if (!response.ok) {
          return false;
        }
        const data = (await response.json()) as ConversationMessagesResponse;
        return (data.active_turns ?? []).some(
          (turn) => turn.turn_id === turnId && turn.status === 'running'
        );
      } catch {
        return false;
      }
    },
    []
  );

  // Initialize streaming hook
  const { sendStreamingMessage, cancelStream, stopTurn, steerStream, isStreaming } =
    useStreamingResponse({
      onMessage: handleStreamingMessage,
      onError: handleStreamingError,
      onReloadHistory: handleReloadHistory,
      onComplete: handleStreamingComplete,
      onToolCall: handleStreamingToolCall,
      onToolConfirmationRequest: handleConfirmationRequest,
      onToolConfirmationResult: handleConfirmationResult,
      onUserInput: handleStreamingUserInput,
      onCancelled: handleStreamingCancelled,
      onCheckTurnActive: handleCheckTurnActive,
    }) as {
      sendStreamingMessage: (params: {
        prompt: string;
        conversationId: string;
        profileId: string;
        interfaceType: string;
        attachments?: Array<{ id: string; type: string; name: string; content: string }>;
        turnId?: string;
      }) => Promise<void>;
      cancelStream: () => void;
      stopTurn: () => Promise<boolean>;
      steerStream: (params: { prompt: string }) => Promise<'accepted' | 'finished' | 'error'>;
      isStreaming: boolean;
    };

  // Load messages for a conversation
  const loadConversationMessages = useCallback(async (convId: string, background = false) => {
    try {
      const streamWasActiveAtRequestStart = activeStreamConversationIdRef.current === convId;

      // Cancel previous messages request if it exists
      if (messagesAbortControllerRef.current) {
        messagesAbortControllerRef.current.abort();
      }

      // Create new abort controller for messages
      const messagesAbortController = new AbortController();
      messagesAbortControllerRef.current = messagesAbortController;

      if (!background) {
        setIsLoading(true);
      }
      const response = await fetch(`/api/v1/chat/conversations/${convId}/messages`, {
        signal: messagesAbortController.signal,
      });
      if (response.ok) {
        const data = (await response.json()) as ConversationMessagesResponse;
        if (streamWasActiveAtRequestStart || activeStreamConversationIdRef.current === convId) {
          // A stream is active for this conversation — its own completion will
          // reconcile. Don't clobber it, and treat this as a supersession.
          return 'bailed' as const;
        }

        // Turns the server still reports as running, so the unconfirmed-reply
        // reconcile below can tell an in-flight turn (partial rows, reply still
        // coming) from a finished one whose rows are authoritative.
        const runningTurnIds = new Set(
          (data.active_turns ?? [])
            .filter((turn) => turn.status === 'running')
            .map((turn) => turn.turn_id)
        );
        // Terminal turns whose persisted rows are authoritative (the reply, if
        // any, is final and won't change). A user-stopped turn ('cancelled') is
        // finished just like 'complete', so the unconfirmed-reply reconcile must
        // treat it as terminal too — otherwise stopping a turn that produced no
        // text could surface a spurious "couldn't confirm the reply" marker.
        const completedTurnIds = new Set(
          (data.active_turns ?? [])
            .filter((turn) => turn.status === 'complete' || turn.status === 'cancelled')
            .map((turn) => turn.turn_id)
        );

        const processedMessages: Message[] = [];
        const toolResponses = new Map<string, string>();
        const toolAttachments = new Map<string, BackendAttachment[]>();

        // First pass: collect tool responses and attachments
        data.messages.forEach((msg: BackendConversationMessage) => {
          if (msg.role === 'tool' && msg.tool_call_id) {
            const responseContent =
              typeof msg.content === 'string'
                ? msg.content
                : Array.isArray(msg.content)
                  ? JSON.stringify(msg.content)
                  : 'Tool executed successfully';
            toolResponses.set(msg.tool_call_id, responseContent);

            // Collect attachments from tool messages for synthesis
            const toolMessageAttachments = msg.attachments;
            if (Array.isArray(toolMessageAttachments) && toolMessageAttachments.length > 0) {
              toolAttachments.set(msg.tool_call_id, toolMessageAttachments);
            }
          }
        });

        data.messages.forEach((msg: BackendConversationMessage) => {
          if (msg.role === 'tool') {
            return;
          }

          if (msg.role === 'error') {
            let errorText = 'An error occurred while processing this message.';
            if (typeof msg.content === 'string' && msg.content.trim()) {
              errorText = msg.content;
            } else if (Array.isArray(msg.content)) {
              const textParts = msg.content
                .map((part) => (part.type === 'text' ? (part as { text?: unknown }).text : null))
                .filter((text): text is string => typeof text === 'string' && Boolean(text.trim()));
              if (textParts.length > 0) {
                errorText = textParts.join('\n\n');
              }
            }

            processedMessages.push({
              id: `msg_${msg.internal_id}`,
              role: 'assistant',
              turnId: msg.turn_id ?? undefined,
              content: [{ type: 'text', text: errorText }],
              createdAt: new Date(msg.timestamp),
              status: { type: 'complete' },
            });
            return;
          }

          if (
            msg.role === 'assistant' &&
            ((msg.tool_calls && msg.tool_calls.length > 0) ||
              (msg.metadata?.attachments && msg.metadata.attachments.length > 0) ||
              (msg.attachments && msg.attachments.length > 0))
          ) {
            const content: MessageContent[] = [];
            if (msg.content) {
              // Handle content - filter out image_url if present
              if (typeof msg.content === 'string') {
                content.push({ type: 'text', text: msg.content });
              } else if (Array.isArray(msg.content)) {
                for (const part of msg.content) {
                  if (part.type === 'text') {
                    content.push({ type: 'text', text: part.text as string });
                  }
                  // Skip image_url content types
                }
              }
            }

            // Process explicit tool calls if present

            if (Array.isArray(msg.tool_calls) && msg.tool_calls.length > 0) {
              msg.tool_calls.forEach((toolCall) => {
                const toolResponse = toolResponses.get(toolCall.id);
                const toolName = toolCall.function?.name || toolCall.name || 'unknown';
                const argumentSource = toolCall.function?.arguments ?? toolCall.arguments;
                const args = parseToolArguments(argumentSource);
                const argsText =
                  typeof argumentSource === 'string' ? argumentSource : JSON.stringify(args);

                content.push({
                  type: 'tool-call',
                  toolCallId: toolCall.id,
                  toolName: toolName,
                  args: args as Record<string, unknown>,
                  argsText: argsText,
                  result: toolResponse ?? undefined,
                });
              });
            }

            // Synthesize attach_to_response for tool call attachments
            // This extracts attachments from tool messages and associates them with the
            // assistant message that made the tool call
            // Collect all attachments from tool calls in this message
            const allToolAttachments: BackendAttachment[] = [];
            const allAttachmentIds: string[] = [];

            if (Array.isArray(msg.tool_calls) && msg.tool_calls.length > 0) {
              msg.tool_calls.forEach((toolCall) => {
                const attachments = toolAttachments.get(toolCall.id);
                if (attachments && attachments.length > 0) {
                  allToolAttachments.push(...attachments.map(normalizeAttachmentForToolArtifact));
                  attachments.forEach((att) => {
                    if (typeof att.attachment_id === 'string') {
                      allAttachmentIds.push(att.attachment_id);
                    }
                  });
                }
              });
            }

            // Also check for attachments in the assistant message's metadata
            // These are attachments queued by tools like attach_to_response
            const metadataAttachments = msg.metadata?.attachments;
            if (Array.isArray(metadataAttachments) && metadataAttachments.length > 0) {
              allToolAttachments.push(
                ...metadataAttachments.map(normalizeAttachmentForToolArtifact)
              );
              metadataAttachments.forEach((att) => {
                if (typeof att.attachment_id === 'string') {
                  allAttachmentIds.push(att.attachment_id);
                }
              });
            }

            const assistantAttachments = msg.attachments;
            if (Array.isArray(assistantAttachments) && assistantAttachments.length > 0) {
              allToolAttachments.push(
                ...assistantAttachments.map(normalizeAttachmentForToolArtifact)
              );
              assistantAttachments.forEach((att) => {
                if (typeof att.attachment_id === 'string') {
                  allAttachmentIds.push(att.attachment_id);
                }
              });
            }

            if (allToolAttachments.length > 0) {
              content.push({
                type: 'tool-call',
                toolCallId: `history_attach_tool_${msg.internal_id}`,
                toolName: 'attach_to_response',
                args: { attachment_ids: allAttachmentIds },
                argsText: JSON.stringify({ attachment_ids: allAttachmentIds }),
                result: JSON.stringify({
                  status: 'attachments_queued',
                  count: allToolAttachments.length,
                  attachments: allToolAttachments,
                }),
                attachments: allToolAttachments,
                artifact: {
                  attachments: allToolAttachments,
                },
              });
            }

            processedMessages.push({
              id: `msg_${msg.internal_id}`,
              role: 'assistant',
              turnId: msg.turn_id ?? undefined,
              content: content,
              createdAt: new Date(msg.timestamp),
              status: { type: 'complete' },
            });
            return;
          }

          // For user messages with potential attachments
          const messageContent: MessageContent[] = [];
          const attachments: Message['attachments'] = [];

          // Handle content - if it's a string, it's just text
          if (msg.content) {
            if (typeof msg.content === 'string') {
              messageContent.push({ type: 'text', text: msg.content });
            } else if (Array.isArray(msg.content)) {
              for (const part of msg.content) {
                if (part.type === 'text') {
                  const textValue = (part as { text?: unknown }).text;
                  if (typeof textValue === 'string') {
                    messageContent.push({ type: 'text', text: textValue });
                  }
                } else if (part.type === 'image_url') {
                  const imageUrl = (part as { image_url?: { url?: unknown } }).image_url?.url;
                  if (typeof imageUrl === 'string') {
                    attachments.push({
                      id: `att_${msg.internal_id}_${attachments.length}`,
                      type: 'image',
                      name: `Image ${attachments.length + 1}`,
                      content: imageUrl,
                    });
                  }
                }
              }
            }
          }

          // Handle attachments from the dedicated attachments field (new format)
          if (Array.isArray(msg.attachments)) {
            for (const attachment of msg.attachments) {
              const attachmentType =
                attachment.type === 'image' || attachment.type === 'document'
                  ? attachment.type
                  : 'file';
              const attachmentName =
                typeof attachment.name === 'string'
                  ? attachment.name
                  : `Attachment ${attachments.length + 1}`;
              const contentUrl =
                typeof attachment.content_url === 'string' ? attachment.content_url : undefined;

              attachments.push({
                id: `att_${msg.internal_id}_${attachments.length}`,
                type: attachmentType,
                name: attachmentName,
                content: contentUrl,
              });
            }
          }

          processedMessages.push({
            id: `msg_${msg.internal_id}`,
            role: msg.role,
            turnId: msg.turn_id ?? undefined,
            content: messageContent.length > 0 ? messageContent : [{ type: 'text', text: '' }],
            createdAt: new Date(msg.timestamp),
            attachments: attachments.length > 0 ? attachments : undefined,
            status: msg.role === 'assistant' ? { type: 'complete' } : undefined,
          });
        });

        // Ensure all messages have content as arrays before setting
        const messagesWithArrayContent = mergeConsecutiveToolOnlyAssistantMessages(
          processedMessages.map((msg) => ({
            ...msg,
            content: Array.isArray(msg.content) ? msg.content : msg.content ? [msg.content] : [],
          }))
        );

        // In-app notification for out-of-band assistant replies. The hub's
        // live `message`/`turn_ended` events are content-free, so the only
        // place the reply text is available is the reloaded history. A
        // background reload (the follow stream's reload path) that surfaces a
        // newly-arrived assistant message — one we hadn't seen the last time we
        // loaded this conversation — fires the notification. useNotifications
        // applies the real gates (enabled, leader tab, permission, page
        // hidden), so we just pass the message through. Skip the first load of
        // a conversation (no prior baseline) so opening it doesn't notify for
        // its existing tail.
        const latestAssistantMessage = [...data.messages]
          .reverse()
          .find((msg) => msg.role === 'assistant');
        const hadBaseline = lastSeenAssistantIdRef.current.has(convId);
        const previousAssistantId = lastSeenAssistantIdRef.current.get(convId);
        if (latestAssistantMessage) {
          lastSeenAssistantIdRef.current.set(convId, latestAssistantMessage.internal_id);
        }
        if (
          background &&
          hadBaseline &&
          latestAssistantMessage &&
          latestAssistantMessage.internal_id !== previousAssistantId
        ) {
          const preview = extractMessagePreview(latestAssistantMessage.content);
          if (preview) {
            showNotificationRef.current?.({
              conversationId: convId,
              messageId: latestAssistantMessage.internal_id,
              preview: preview.length > 100 ? `${preview.substring(0, 97)}...` : preview,
              timestamp: latestAssistantMessage.timestamp,
            });
          }
        }

        // Re-derive "couldn't confirm the reply" markers from this authoritative
        // history. For every turn that gave up (or 410'd) without a confirmed
        // reply IN THIS conversation: clear it once its reply has landed, or
        // append the marker while it is finished with no reply. A still-running
        // turn keeps no marker — its reply is still coming. Running this on every
        // reload makes the marker self-healing: a reply that arrives later (via
        // the follow stream's reload) replaces the marker, and a marker a reload
        // would otherwise wipe is re-appended.
        let hasRunningUnconfirmed = false;
        const runningUnconfirmedTurnIds = new Set<string>();
        for (const [turnId, info] of unconfirmedTurnsRef.current) {
          if (info.convId !== convId) {
            continue;
          }
          if (runningTurnIds.has(turnId)) {
            // STILL RUNNING wins before any terminal-reply judging: a partial
            // assistant row (a tool call / partial text persisted mid-turn) is
            // NOT the final reply while the server reports the turn running. No
            // marker; keep polling. The always-on follow stream is the primary
            // completion signal, but it tails future-only and may be
            // disconnected, so the fallback re-poll guarantees we reconcile.
            hasRunningUnconfirmed = true;
            // Once we've OBSERVED the turn running, its completion is now our
            // responsibility: if it later finishes with no persisted reply (the
            // producer failed before committing a row), surface the marker —
            // even for a 410, which we'd otherwise assume succeeded.
            info.markIfNoReply = true;
            runningUnconfirmedTurnIds.add(turnId);
          } else if (hasTerminalReplyForTurn(messagesWithArrayContent, turnId, completedTurnIds)) {
            // Finished with a real reply — confirmed.
            unconfirmedTurnsRef.current.delete(turnId);
          } else if (info.markIfNoReply) {
            // A true give-up finished with no reply — surface the marker.
            messagesWithArrayContent.push(buildUnconfirmedReplyError(convId, turnId));
          } else {
            // A 410 reconcile finished with no reply: the reply was durably
            // persisted before eviction (and may just be lagging) — assume
            // success, no marker, like already_complete. Clear it.
            unconfirmedTurnsRef.current.delete(turnId);
          }
        }
        // Drive the fallback reconcile poll (below) only while THIS conversation
        // has a still-running unconfirmed turn. Leave another conversation's
        // pending flag untouched.
        setPendingReconcileConvId((prev) =>
          hasRunningUnconfirmed ? convId : prev === convId ? null : prev
        );

        if (runningUnconfirmedTurnIds.size > 0) {
          setMessages((prev) =>
            preserveRunningLoadingMessages(
              messagesWithArrayContent,
              prev,
              runningUnconfirmedTurnIds
            )
          );
        } else {
          setMessages(messagesWithArrayContent);
        }
        return 'applied' as const;
      }
      // A non-OK response is a genuine reconcile failure, not a supersession.
      return 'failed' as const;
    } catch (error) {
      // An AbortError means a newer load superseded this one (bailed); any other
      // error is a genuine fetch failure.
      if (error instanceof Error && error.name === 'AbortError') {
        return 'bailed' as const;
      }
      console.error('Error loading conversation:', error);
      return 'failed' as const;
    } finally {
      if (!background) {
        setIsLoading(false);
      }
    }
  }, []);
  loadConversationMessagesRef.current = loadConversationMessages;

  // Fallback reconcile poll: while the open conversation has a turn that gave up
  // but is still running server-side, re-poll history so its eventual completion
  // is reconciled even if the always-on follow stream missed the turn_ended (it
  // tails future-only and may be disconnected). Each poll re-derives the marker
  // and clears `pendingReconcileConvId` once the turn resolves, which tears the
  // interval down. Bounded by reconcilePollTuning.maxPolls so a wedged turn can't poll
  // forever.
  useEffect(() => {
    if (!pendingReconcileConvId || pendingReconcileConvId !== conversationId) {
      return;
    }
    const convId = pendingReconcileConvId;
    let polls = 0;
    const intervalId = setInterval(() => {
      polls += 1;
      if (polls > reconcilePollTuning.maxPolls) {
        clearInterval(intervalId);
        // Don't abandon the user on a perpetual spinner: if the turn is still
        // unresolved after the poll budget (and the follow stream also missed
        // completion), surface a provisional marker and clear the pending state.
        // It self-heals — a later reload replaces it with the reply, or (if the
        // turn really is still running) removes it and resumes polling.
        setMessages((prev) => {
          let next = prev;
          for (const [turnId, info] of unconfirmedTurnsRef.current) {
            if (info.convId === convId && info.markIfNoReply) {
              next = surfaceUnconfirmedMarker(next, convId, turnId);
            }
          }
          return next;
        });
        setPendingReconcileConvId((prev) => (prev === convId ? null : prev));
        return;
      }
      void loadConversationMessagesRef.current?.(convId, true);
    }, reconcilePollTuning.intervalMs);
    return () => clearInterval(intervalId);
  }, [pendingReconcileConvId, conversationId]);

  // Handle conversation selection (defined early for use in notification callback)
  const handleConversationSelect = useCallback(
    (convId: string) => {
      // Cancel any active streaming before switching conversations
      cancelStream();

      setConversationId(convId);
      setMobileShowList(false);
      localStorage.setItem('lastConversationId', convId);
      window.history.pushState({}, '', `/chat?conversation_id=${convId}`);
      loadConversationMessages(convId);
    },
    [cancelStream, loadConversationMessages]
  );

  // Handle notification clicks - navigate to the conversation
  const handleNotificationClick = useCallback(
    (notifConversationId: string) => {
      if (notifConversationId !== conversationId) {
        handleConversationSelect(notifConversationId);
      }
    },
    [conversationId, handleConversationSelect]
  );

  // Initialize notifications (before handleLiveMessageUpdate which uses showNotification)
  const {
    isSupported: notificationsSupported,
    permission: notificationPermission,
    requestPermission: requestNotificationPermission,
    showNotification,
  } = useNotifications({
    enabled: notificationsEnabled,
    conversationId,
    onNotificationClick: handleNotificationClick,
  });

  // Keep the ref used by the empty-dep history loader pointed at the latest
  // showNotification so out-of-band replies surfaced by a background reload can
  // fire an in-app notification without rebuilding loadConversationMessages.
  useEffect(() => {
    showNotificationRef.current = showNotification;
  }, [showNotification]);

  // Handle notification preference changes
  const handleNotificationEnabledChange = useCallback((enabled: boolean) => {
    setNotificationsEnabled(enabled);
    localStorage.setItem('notificationsEnabled', String(enabled));
  }, []);

  // Cleanup effect to abort fetch requests on unmount
  useEffect(() => {
    return () => {
      // Cancel any pending fetch requests when component unmounts
      if (abortControllerRef.current) {
        abortControllerRef.current.abort();
      }
      if (messagesAbortControllerRef.current) {
        messagesAbortControllerRef.current.abort();
      }
    };
  }, []);

  // Handle window resize
  useEffect(() => {
    const handleResize = () => {
      const newIsMobile = window.innerWidth <= 768;
      setIsMobile(newIsMobile);

      // Close sidebar when switching to mobile to prevent layout issues
      if (newIsMobile) {
        setSidebarOpen(false);
      }
    };

    window.addEventListener('resize', handleResize);
    return () => window.removeEventListener('resize', handleResize);
  }, []);

  // Create a stable callback ref for SSE message updates
  const handleLiveMessageUpdate = useCallback(
    (update: {
      internal_id: string;
      timestamp: string;
      new_messages: boolean;
      role?: string;
      content?: string;
      conversation_id?: string;
      turn_id?: string;
    }) => {
      // The hub's live `message`/`turn_ended` events are content-free signals to
      // reload (no role/content/internal_id), so the in-app notification can't
      // be derived here. It's fired from the background reload in
      // loadConversationMessages, where the persisted reply text is available.

      // Skip reload if this turn was originated by this tab — we already hold
      // the freshest state from streaming; reloading risks clobbering freshly-
      // rendered content (error banners, message bubbles, attachment previews).
      if (update.turn_id && selfTurnIdsRef.current.has(update.turn_id)) {
        selfTurnIdsRef.current.delete(update.turn_id);
        return;
      }

      // Reload messages for the updated conversation
      // Skip reload if we're currently streaming to avoid race conditions
      // Use background loading to prevent disabling the input during updates
      const isActiveStreamUpdate =
        update.conversation_id === conversationId &&
        activeStreamConversationIdRef.current === update.conversation_id;

      if (update.conversation_id === conversationId && !isStreaming && !isActiveStreamUpdate) {
        loadConversationMessages(conversationId, true);
      }
    },
    [conversationId, loadConversationMessages, isStreaming]
  );

  // Set up live message updates via SSE
  useLiveMessageUpdates({
    conversationId,
    interfaceType: 'web',
    enabled: true,
    onMessageReceived: handleLiveMessageUpdate,
  });

  // Handle new chat creation
  const handleNewChat = useCallback(() => {
    // Cancel any active streaming before creating a new chat
    cancelStream();

    const newConvId = `web_conv_${generateUUID()}`;
    setConversationId(newConvId);
    setMobileShowList(false);
    setMessages([]);
    localStorage.setItem('lastConversationId', newConvId);
    window.history.pushState({}, '', `/chat?conversation_id=${newConvId}`);
  }, [cancelStream]);

  // On mobile, navigate back to conversation list
  const handleBackToList = useCallback(() => {
    cancelStream();
    setMobileShowList(true);
  }, [cancelStream]);

  // Handle profile changes
  const handleProfileChange = useCallback(
    (newProfileId: string) => {
      setCurrentProfileId(newProfileId);
      // Persist selection to localStorage
      localStorage.setItem('selectedProfileId', newProfileId);

      // Optionally start a new conversation when switching profiles
      // to maintain clear context separation
      if (currentProfileId !== newProfileId && conversationId) {
        handleNewChat();
      }
    },
    [currentProfileId, conversationId, handleNewChat]
  );

  // Handle new messages from the user
  const handleNew = useCallback(
    async (message: {
      content: { text: string }[];
      attachments?: Array<{
        id?: string;
        type?: string;
        name: string;
        content?: string;
        file?: File;
      }>;
    }) => {
      // Process attachments - they might come from the runtime with different properties.
      // In @assistant-ui/react 0.12.15+, CompleteAttachment.content is ThreadUserMessagePart[]
      // (an array of content parts) rather than a plain string URL. We extract the URL from
      // the parts so our internal Message type and backend API still use plain string URLs.
      const processedAttachments = message.attachments?.map((att) => {
        // Extract URL from content parts array (new format from attachmentAdapter.send())
        let contentUrl = '';
        const rawContent = att.content as unknown;
        if (Array.isArray(rawContent)) {
          for (const part of rawContent as Array<{
            type: string;
            image?: string;
            data?: unknown;
            name?: string;
          }>) {
            if (part.type === 'image' && typeof part.image === 'string') {
              contentUrl = part.image;
              break;
            }
            if (part.type === 'data' && part.name === 'url' && typeof part.data === 'string') {
              contentUrl = part.data;
              break;
            }
          }
        } else if (typeof rawContent === 'string') {
          contentUrl = rawContent;
        }
        return {
          id: att.id || `att_${Date.now()}_${Math.random()}`,
          type: (att.type || 'image') as 'image',
          name: att.name,
          content: contentUrl,
        };
      });

      // Tag the optimistic bubbles with this turn's id so an interrupted-stream
      // give-up can identify THIS turn's stranded loading bubble (e.g. to replace
      // a no-content spinner with an error) without touching a concurrent turn's
      // live spinner. effectiveTurnId in the hook is this same id.
      const turnId = generateUUID();
      const userMessage: Message = {
        id: `msg_${Date.now()}`,
        role: 'user',
        turnId,
        content: message.content.map((c) => ({ type: 'text' as const, text: c.text })),
        createdAt: new Date(),
        attachments: processedAttachments,
      };

      const assistantMessageId = `msg_${Date.now()}_assistant`;
      const loadingAssistantMessage: Message = {
        id: assistantMessageId,
        role: 'assistant',
        turnId,
        content: [{ type: 'text', text: LOADING_MARKER }],
        isLoading: true,
        createdAt: new Date(),
      };

      setMessages((prev) => [...prev, userMessage, loadingAssistantMessage]);

      const targetConversationId = conversationId || `web_conv_${generateUUID()}`;
      streamingMessageIdRef.current = assistantMessageId;
      activeStreamConversationIdRef.current = targetConversationId;
      activeTurnIdRef.current = turnId;
      turnStoppedRef.current = false;
      lastStreamingErrorRef.current = null;
      selfTurnIdsRef.current.add(turnId);

      await sendStreamingMessage({
        prompt: message.content[0].text,
        conversationId: targetConversationId,
        profileId: currentProfileId,
        interfaceType: 'web',
        attachments: processedAttachments,
        turnId,
      });
    },
    [conversationId, sendStreamingMessage, currentProfileId]
  );

  // Expose handleNew to the earlier-defined completion handler so it can fire a
  // queued steer follow-up after the current stream settles.
  useEffect(() => {
    handleNewRef.current = handleNew;
  }, [handleNew]);

  const convertMessage = useCallback((message: Message) => {
    // Ensure content is always an array for assistant-ui compatibility
    let content = Array.isArray(message.content)
      ? message.content
      : message.content
        ? [message.content]
        : [{ type: 'text', text: '' }];

    // TypeScript narrows the type after the ternary above, but the filter
    // result is still MessageContent[] so the assignment is valid.
    content = (content as MessageContent[]).filter((item) => item && typeof item === 'object');
    if (content.length === 0) {
      content = [{ type: 'text', text: '' }];
    }

    // @assistant-ui/react 0.12.15+ requires CompleteAttachment.content to be
    // ThreadUserMessagePart[] (an array of content parts). Our internal Message
    // type stores content as a plain string URL. Convert here for the library.
    const convertedAttachments = message.attachments?.map((att) => {
      if (typeof att.content === 'string' && att.content) {
        const url = att.content;
        const contentParts =
          att.type === 'image'
            ? [{ type: 'image' as const, image: url }]
            : [{ type: 'data' as const, name: 'url', data: url }];
        return { ...att, content: contentParts };
      }
      // Already in array format or empty - pass through
      return att;
    });

    const { attachments: _attachments, ...messageWithoutAttachments } = message;
    const convertedMessage = {
      ...messageWithoutAttachments,
      content,
    };
    if (message.role === 'user' && convertedAttachments && convertedAttachments.length > 0) {
      return { ...convertedMessage, attachments: convertedAttachments };
    }
    return convertedMessage;
  }, []);

  // The composer Stop button (onCancel) handler. Stop the turn server-side; if
  // it could not be secured (the server also rejects the turn's pending tool
  // confirmations), surface a warning so a still-approvable confirmation isn't
  // silently left behind.
  const handleStop = useCallback(async () => {
    const ok = await stopTurn();
    if (!ok) {
      setMessages((prev) => [
        ...prev,
        {
          id: `msg_stopfail_${Date.now()}`,
          role: 'assistant',
          content: [
            {
              type: 'text',
              text: '⚠️ I could not confirm the turn was fully stopped. It may still be running, and if a pending tool approval remains it could still execute — please retry, or reject any leftover approval in the pending approvals panel.',
            },
          ],
          createdAt: new Date(),
        },
      ]);
    }
  }, [stopTurn]);

  const runtime = useExternalStoreRuntime({
    messages,
    isRunning: isLoading || isStreaming,
    // @ts-expect-error - assistant-ui type mismatch with onNew handler signature
    onNew: handleNew,
    // The composer's Stop button (ComposerPrimitive.Cancel) triggers this. Stop
    // the running turn server-side; the bubble settles from the turn_ended
    // (cancelled) SSE event. handleStop surfaces a warning if the stop couldn't
    // be fully secured.
    onCancel: handleStop,
    // @ts-expect-error - assistant-ui type mismatch with convertMessage return type
    convertMessage,
    adapters: {
      // @ts-expect-error - assistant-ui type mismatch with attachment adapter
      attachments: defaultAttachmentAdapter,
    },
  });

  // Mid-run controls handed down to the Thread's composer (steering input). The
  // draft text lives here (not in the SteerBar) so a steer that the turn never
  // echoed isn't lost when the SteerBar unmounts at turn end.
  const submitSteer = useCallback(async () => {
    const prompt = steerDraft.trim();
    if (!prompt) {
      return;
    }
    setSteerError(null);
    const result = await steerStream({ prompt });
    if (result === 'accepted') {
      // Deliberately do NOT clear here: the draft is cleared only when the
      // matching user_input echo confirms the turn actually consumed it. Track
      // it as awaiting-echo so that if the turn completes without draining it
      // (a final text-only iteration), the completion handler recovers it as a
      // normal follow-up instead of losing it.
      awaitingEchoSteersRef.current.push(prompt);
      return;
    }
    if (result === 'error') {
      // The turn may still be running and the steer may even have been accepted
      // before the response failed, so DON'T auto-resend. Surface it and keep
      // the draft for a manual retry.
      setSteerError('Could not steer the assistant. Please try again.');
      return;
    }
    // 'finished': the steer targeted an already-finished turn. Don't lose the
    // text — send it as a normal follow-up. If a stream is still settling, defer
    // until its completion handler runs so we don't race its shared-ref cleanup.
    setSteerDraft('');
    if (streamingMessageIdRef.current) {
      pendingFollowupsRef.current.push(prompt);
    } else {
      await handleNew({ content: [{ text: prompt }] });
    }
  }, [steerDraft, steerStream, handleNew]);

  const chatControls = useMemo(
    () => ({
      steerText: steerDraft,
      setSteerText: (text: string) => {
        setSteerError(null);
        setSteerDraft(text);
      },
      submitSteer,
      steerError,
    }),
    [steerDraft, steerError, submitSteer]
  );

  // Initialize conversation ID from URL or localStorage
  useEffect(() => {
    fetchConversations();

    const urlParams = new URLSearchParams(window.location.search);
    const urlConversationId = urlParams.get('conversation_id');
    const initialPrompt = urlParams.get('q');
    const lastConversationId = localStorage.getItem('lastConversationId');

    if (initialPrompt && !initialPromptProcessedRef.current) {
      // If there's a prompt, start a new chat with it
      const newConvId = `web_conv_${generateUUID()}`;
      setConversationId(newConvId);
      setMessages([]);
      localStorage.setItem('lastConversationId', newConvId);
    } else if (urlConversationId) {
      setConversationId(urlConversationId);
      loadConversationMessages(urlConversationId);
    } else if (lastConversationId) {
      setConversationId(lastConversationId);
      loadConversationMessages(lastConversationId);
      window.history.replaceState({}, '', `/chat?conversation_id=${lastConversationId}`);
    } else {
      handleNewChat();
    }
  }, []);

  // Handle initial prompt from query parameter once runtime is ready
  useEffect(() => {
    const initialPrompt = searchParams.get('q');

    if (
      initialPrompt &&
      runtime &&
      !initialPromptProcessedRef.current &&
      conversationId?.startsWith('web_conv_')
    ) {
      initialPromptProcessedRef.current = true;
      handleNew({ content: [{ text: initialPrompt }] });

      // Clean up URL using React Router's setSearchParams
      setSearchParams(
        (prev) => {
          const newParams = new URLSearchParams(prev);
          newParams.delete('q');
          if (conversationId) {
            newParams.set('conversation_id', conversationId);
          }
          return newParams;
        },
        { replace: true }
      );
    }
  }, [runtime, conversationId, handleNew, searchParams, setSearchParams]);

  // Signal that app is ready (for tests)
  // Only set when runtime is ready AND initial data loading is complete
  useEffect(() => {
    if (runtime && !conversationsLoading && !profilesLoading) {
      document.documentElement.setAttribute('data-app-ready', 'true');
    } else {
      document.documentElement.removeAttribute('data-app-ready');
    }
    return () => {
      document.documentElement.removeAttribute('data-app-ready');
    };
  }, [runtime, conversationsLoading, profilesLoading]);

  const trayConfirmations = useMemo(() => {
    const visibleToolCallIds = new Set(
      messages.flatMap((message) =>
        message.content
          .filter((part) => part.type === 'tool-call' && part.toolCallId)
          .map((part) => part.toolCallId as string)
      )
    );
    return Array.from(pendingConfirmations.values()).filter(
      (confirmation) =>
        !confirmation.tool_call_id || !visibleToolCallIds.has(confirmation.tool_call_id)
    );
  }, [messages, pendingConfirmations]);
  const handleTrayConfirmation = (requestId: string, approved: boolean) =>
    handleConfirmation(requestId, requestId, approved);

  return (
    <TooltipProvider>
      <div className="flex h-screen flex-col bg-background">
        {/* Mobile: conversation list view (shown when no conversation is active) */}
        {isMobile && mobileShowList && (
          <div className="flex flex-1 flex-col min-h-0">
            <ConversationSidebar
              conversations={conversations}
              conversationsLoading={conversationsLoading}
              currentConversationId={conversationId}
              onConversationSelect={handleConversationSelect}
              onNewChat={handleNewChat}
              isOpen={true}
              onRefresh={fetchConversations}
              isMobile={isMobile}
            />
          </div>
        )}

        {/* Mobile: chat detail view (shown when not on list) */}
        {isMobile && !mobileShowList && (
          <div className="flex flex-1 flex-col min-h-0 overflow-hidden">
            {/* Header with back button */}
            <div className="flex-shrink-0 z-50 flex items-center gap-4 border-b bg-background/95 p-4 backdrop-blur supports-[backdrop-filter]:bg-background/60">
              <Button
                variant="ghost"
                size="sm"
                onClick={handleBackToList}
                aria-label="Back to conversations"
              >
                <ArrowLeft className="h-4 w-4" />
              </Button>
              <h2 className="text-xl font-semibold">Chat</h2>

              <div className="flex items-center">
                <ProfileSelector
                  selectedProfileId={currentProfileId}
                  onProfileChange={handleProfileChange}
                  disabled={isLoading}
                  onLoadingChange={setProfilesLoading}
                />
              </div>

              <div className="flex items-center gap-2 ml-auto">
                <NotificationSettings
                  enabled={notificationsEnabled}
                  onEnabledChange={handleNotificationEnabledChange}
                  permission={notificationPermission}
                  onRequestPermission={requestNotificationPermission}
                  isSupported={notificationsSupported}
                />
                <PushNotificationButton />
                <NavigationSheet currentPage="chat">
                  <Button variant="ghost" size="sm" aria-label="Open navigation">
                    <Menu className="h-4 w-4" />
                  </Button>
                </NavigationSheet>
              </div>
            </div>

            <div className="flex min-w-0 min-h-0 flex-1 flex-col">
              <PendingConfirmationsTray
                confirmations={trayConfirmations}
                loadError={pendingConfirmationsError}
                onConfirm={handleTrayConfirmation}
              />
              <main className="flex flex-1 flex-col min-h-0">
                <AssistantRuntimeProvider runtime={runtime}>
                  <ThreadErrorBoundary>
                    <ChatControlsContext.Provider value={chatControls}>
                      <ToolConfirmationProvider
                        value={{ pendingConfirmations, handleConfirmation }}
                      >
                        <Thread />
                      </ToolConfirmationProvider>
                    </ChatControlsContext.Provider>
                  </ThreadErrorBoundary>
                </AssistantRuntimeProvider>
              </main>
            </div>
          </div>
        )}

        {/* Desktop: sidebar + chat side by side */}
        {!isMobile && (
          <>
            {/* Header */}
            <div className="sticky top-0 z-50 flex items-center gap-4 border-b bg-background/95 p-4 backdrop-blur supports-[backdrop-filter]:bg-background/60">
              <Button
                variant="ghost"
                size="sm"
                onClick={() => setSidebarOpen(!sidebarOpen)}
                aria-label="Toggle sidebar"
              >
                {sidebarOpen ? (
                  <PanelLeftClose className="h-4 w-4" />
                ) : (
                  <PanelLeftOpen className="h-4 w-4" />
                )}
              </Button>
              <h2 className="text-xl font-semibold">Chat</h2>

              <div className="flex items-center">
                <ProfileSelector
                  selectedProfileId={currentProfileId}
                  onProfileChange={handleProfileChange}
                  disabled={isLoading}
                  onLoadingChange={setProfilesLoading}
                />
              </div>

              <div className="flex items-center gap-2 ml-auto">
                <NotificationSettings
                  enabled={notificationsEnabled}
                  onEnabledChange={handleNotificationEnabledChange}
                  permission={notificationPermission}
                  onRequestPermission={requestNotificationPermission}
                  isSupported={notificationsSupported}
                />
                <PushNotificationButton />
                <NavigationSheet currentPage="chat">
                  <Button variant="ghost" size="sm" aria-label="Open navigation">
                    <Menu className="h-4 w-4" />
                  </Button>
                </NavigationSheet>
              </div>
            </div>

            {/* Body */}
            <div className="flex flex-1 overflow-hidden">
              <ConversationSidebar
                conversations={conversations}
                conversationsLoading={conversationsLoading}
                currentConversationId={conversationId}
                onConversationSelect={handleConversationSelect}
                onNewChat={handleNewChat}
                isOpen={sidebarOpen}
                onRefresh={fetchConversations}
                isMobile={isMobile}
              />

              <div className="flex min-w-0 flex-1 flex-col">
                <PendingConfirmationsTray
                  confirmations={trayConfirmations}
                  loadError={pendingConfirmationsError}
                  onConfirm={handleTrayConfirmation}
                />
                <main className="flex flex-1 flex-col min-h-0">
                  <AssistantRuntimeProvider runtime={runtime}>
                    <ThreadErrorBoundary>
                      <ChatControlsContext.Provider value={chatControls}>
                        <ToolConfirmationProvider
                          value={{ pendingConfirmations, handleConfirmation }}
                        >
                          <Thread />
                        </ToolConfirmationProvider>
                      </ChatControlsContext.Provider>
                    </ThreadErrorBoundary>
                  </AssistantRuntimeProvider>
                </main>
              </div>
            </div>
          </>
        )}
      </div>
    </TooltipProvider>
  );
};

export default ChatApp;
