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
import IntelligenceSelector from './IntelligenceSelector';
import { NotificationSettings } from './NotificationSettings';
import { PendingConfirmationsTray } from './PendingConfirmationsTray';
import ProfileSelector from './ProfileSelector';
import { type ModelTier, ProfilesProvider, useProfiles } from './profilesContext';
import { PushNotificationButton } from './PushNotificationButton';
import { ShareConversationButton } from './ShareConversationButton';
import { ChatControlsContext, type SteerResult } from './chatControls';
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
  MessageReasoningInfo,
} from './types';
import { useActivityStream } from './useActivityStream';
import { useLiveMessageUpdates } from './useLiveMessageUpdates';
import { useNotifications } from './useNotifications';
import { useStreamingResponse } from './useStreamingResponse';

// Stable empty list for profiles that offer no choice of tier, so the
// intelligence control's props keep their identity across renders.
const EMPTY_MODEL_TIERS: ModelTier[] = [];

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

function isTerminalToolPart(part: MessageContent): boolean {
  return (
    part.type !== 'tool-call' ||
    part.result !== undefined ||
    part.artifact !== undefined ||
    part.attachments !== undefined ||
    (part.toolName === 'attach_to_response' && Array.isArray(part.args?.attachment_ids))
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

const ChatAppContent: React.FC<ChatAppProps> = ({ profileId = 'default_assistant' }) => {
  const [messages, setMessages] = useState<Message[]>([]);
  const [isLoading, setIsLoading] = useState<boolean>(false);
  const [sidebarOpen, setSidebarOpen] = useState<boolean>(window.innerWidth > 768);
  const [conversationId, setConversationId] = useState<string | null>(null);
  const [persistedConversationId, setPersistedConversationId] = useState<string | null>(null);
  // Always-current mirror of conversationId, so a deferred follow-up timer can
  // synchronously check whether the conversation changed before it fires.
  const conversationIdRef = useRef<string | null>(null);
  conversationIdRef.current = conversationId;
  // True while the open conversation exists only in this client and holds no
  // turns: an id minted by handleNewChat (or at startup) that has never been
  // sent. Tracked explicitly rather than inferred from an empty `messages`,
  // which also reads empty while a real conversation's history is loading.
  const conversationIsUnsentDraftRef = useRef(true);
  // Set to a conversation id while it has a turn that gave up but is still running
  // server-side, to drive the fallback reconcile poll. Cleared once the turn
  // resolves (reply lands or it finishes with none).
  const [pendingReconcileConvId, setPendingReconcileConvId] = useState<string | null>(null);
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [conversationsLoading, setConversationsLoading] = useState<boolean>(true);
  const [profilesLoading, setProfilesLoading] = useState<boolean>(true);
  const [isMobile, setIsMobile] = useState<boolean>(window.innerWidth <= 768);
  const [mobileShowList, setMobileShowList] = useState<boolean>(false);
  // `currentProfileId` is the *active* profile: it drives the picker and is sent
  // with every turn. It is distinct from the *preferred* profile persisted in
  // localStorage ('selectedProfileId'), which seeds new chats. Opening an
  // existing conversation adopts that conversation's profile into the active
  // value (without touching the preferred one) so a follow-up turn loads the
  // matching profile-partitioned history instead of silently dropping context.
  const [currentProfileId, setCurrentProfileId] = useState<string>(() => {
    // Seed the active profile from the persisted preferred profile.
    return localStorage.getItem('selectedProfileId') || profileId;
  });
  const [notificationsEnabled, setNotificationsEnabled] = useState<boolean>(() => {
    // Load notification preference from localStorage
    const saved = localStorage.getItem('notificationsEnabled');
    return saved === 'true';
  });
  // The intelligence (model tier) choice for the profile currently selected.
  // `null` -- the usual state -- means the profile's default tier, and is the
  // only state in which a send omits `model_tier` entirely. A choice is
  // one-shot: the next send consumes it and the control returns to the default,
  // unless it is pinned, in which case it holds for the rest of this
  // conversation. Nothing about it is persisted: spending more on a request is a
  // decision about that request, not a setting that should outlive a reload.
  const [modelTierChoice, setModelTierChoice] = useState<{
    tierId: string;
    pinned: boolean;
  } | null>(null);
  // Mirror for handleNew, which must not take the choice as a dependency: its
  // identity feeds the assistant-ui runtime, and picking a tier should not
  // rebuild the send path mid-conversation.
  const modelTierChoiceRef = useRef(modelTierChoice);
  modelTierChoiceRef.current = modelTierChoice;
  const { profilesById } = useProfiles();
  const currentProfile = profilesById[currentProfileId];
  const modelTiers: ModelTier[] = currentProfile?.model_tiers ?? EMPTY_MODEL_TIERS;
  const defaultModelTier = currentProfile?.default_model_tier ?? null;
  const streamingMessageIdRef = useRef<string | null>(null);
  const activeStreamConversationIdRef = useRef<string | null>(null);
  const toolCallMessageIdRef = useRef<string | null>(null);
  const lastStreamingErrorRef = useRef<string | null>(null);
  // Whether that error was written for the user (shown verbatim) rather than
  // for a debugger (replaced by the generic line plus a diagnostics link).
  const lastStreamingErrorIsUserFacingRef = useRef(false);
  // The turn id of the currently-streaming turn, used to tag mid-turn steering
  // user bubbles. Set in handleNew, cleared when the turn completes.
  const activeTurnIdRef = useRef<string | null>(null);
  // Last steer error (e.g. a transient 5xx), shown above the composer; the
  // composer keeps its text so the user can retry.
  const [steerError, setSteerError] = useState<string | null>(null);
  // Follow-up messages queued while a stream was still settling (a steer that
  // hit a finished turn, or recovered un-echoed steers). Fired one at a time
  // from the completion handler — after the ending stream's shared-ref cleanup —
  // so they don't race it or start concurrent turns.
  const pendingFollowupsRef = useRef<string[]>([]);
  // Accepted steers awaiting their user_input echo. If the turn completes
  // without echoing them (the model was in a final text-only iteration, so the
  // loop never drained them), they're recovered as normal follow-ups rather than
  // silently lost. A list, since the user can submit several steers during one
  // long turn. Each carries the input_id it was submitted with, which is what
  // the turn's echo names when it consumes the message.
  const awaitingEchoSteersRef = useRef<{ inputId: string; prompt: string }[]>([]);
  // input_ids of steers we have SEEN the turn echo back, i.e. observed it
  // consume. Distinct from the awaiting list above, which is emptied by Stop, a
  // conversation change and the recovery drain — so "not awaiting" says nothing
  // about delivery, while an entry here is positive evidence of it. Identifying
  // submissions rather than text is what makes it evidence: an identical message
  // from another client, or from this one earlier, has a different id.
  const consumedSteerEchoesRef = useRef<string[]>([]);
  useEffect(() => {
    setSteerError(null);
    // Drop any queued/awaiting steers so they can't fire into the new conversation.
    awaitingEchoSteersRef.current = [];
    pendingFollowupsRef.current = [];
    consumedSteerEchoesRef.current = [];
  }, [conversationId]);
  // handleNew is defined after the streaming callbacks; the completion handler
  // reaches it via this ref to fire a queued follow-up.
  const handleNewRef = useRef<((message: { content: { text: string }[] }) => Promise<void>) | null>(
    null
  );
  // Same story for handleReloadHistory: the completion handler reconciles an
  // adopted turn whose output it suppressed, and that callback is defined below.
  const handleReloadHistoryRef = useRef<
    ((conversationId: string, options?: { retryIfBailed?: boolean }) => void) | null
  >(null);
  // Set when the running turn ended because the user stopped it, so the
  // completion handler can render a "stopped" affordance instead of an empty
  // bubble (and never an error toast).
  const turnStoppedRef = useRef(false);
  const initialPromptProcessedRef = useRef(false);
  const abortControllerRef = useRef<AbortController | null>(null);
  const messagesAbortControllerRef = useRef<AbortController | null>(null);
  // Optimistic conversation rows inserted on send, keyed by the owning turn id,
  // kept until that turn settles (handleStreamingComplete). Merged into every
  // fetch result so a list request already in flight when the user sent can't
  // erase the row; turn-keying stops an aborted older turn from retiring a newer
  // turn's row for the same conversation. See fetchConversations.
  const pendingOptimisticConversationsRef = useRef<Map<string, Conversation>>(new Map());
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
    // Some failures are written for the user rather than for a debugger — a send
    // refused because the conversation is still busy, which tells them what to
    // do next. Those are rendered verbatim instead of being replaced by the
    // generic error line below.
    lastStreamingErrorIsUserFacingRef.current =
      typeof error !== 'string' && (error as Error & { userFacing?: boolean }).userFacing === true;
    // The optimistic row is retired in handleStreamingComplete (always called
    // from the hook's finally, keyed by the completing turn id) — including the
    // failed-POST case — so nothing to do here.
  }, []);

  const handleStreamingComplete = useCallback(
    ({
      content,
      toolCalls: _toolCalls,
      completed = true,
      turnId,
      unconsumedAdoptedPrompt = null,
      undeliveredPrompt = null,
      adopted = false,
      kickoffFailed = false,
      reconciledWithoutEnd = false,
      reasoningInfo = null,
    }: {
      content: string;
      toolCalls: Array<Record<string, unknown>>;
      completed?: boolean;
      turnId?: string;
      unconsumedAdoptedPrompt?: string | null;
      undeliveredPrompt?: string | null;
      adopted?: boolean;
      kickoffFailed?: boolean;
      reconciledWithoutEnd?: boolean;
      reasoningInfo?: MessageReasoningInfo | null;
    }) => {
      // Capture ref values locally to avoid race conditions
      const messageId = streamingMessageIdRef.current;
      const toolCallMessageId = toolCallMessageIdRef.current;
      const lastError = lastStreamingErrorRef.current;
      const lastErrorIsUserFacing = lastStreamingErrorIsUserFacingRef.current;
      const wasStopped = turnStoppedRef.current;
      turnStoppedRef.current = false;

      // Retire this turn's optimistic row now it has settled: the server
      // reflects the send, so the fetchConversations below (and later refreshes)
      // should use the authoritative summary. Keyed by the COMPLETING turn id —
      // not activeStreamConversationIdRef, which a superseding send may have
      // already repointed — so an aborted older turn can't retire a newer turn's
      // pending row. Cleared on settle, not by a timestamp compare, so a skewed
      // clock can't pin it.
      if (turnId) {
        pendingOptimisticConversationsRef.current.delete(turnId);
      }
      if (!kickoffFailed && conversationId) {
        setPersistedConversationId(conversationId);
      }

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
          // No text, no tool calls, but had an error. A user-facing one already
          // says what happened and what to do (e.g. a send refused because the
          // conversation is still busy), so show it as written — the generic
          // line would hide the instruction and point at diagnostics for
          // something that isn't a fault.
          const diagnosticsUrl = getDiagnosticsUrl({
            conversationId: conversationId ?? undefined,
          });
          const errorText = lastErrorIsUserFacing
            ? lastError
            : `Sorry, I encountered an error processing your message. [View diagnostics](${diagnosticsUrl}) for debugging details.`;
          setMessages((prev) =>
            prev.map((msg) =>
              msg.id === messageId
                ? {
                    ...msg,
                    content: [{ type: 'text', text: errorText }],
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

      // Record what served this turn on the reply itself, so the thread can
      // name the tier it ran at without waiting for a history reload.
      if (messageId && reasoningInfo) {
        setMessages((prev) =>
          prev.map((msg) =>
            msg.id === messageId ? { ...msg, reasoning_info: reasoningInfo } : msg
          )
        );
      }

      // Update tool call message status when streaming completes
      if (toolCallMessageId) {
        setMessages((prev) =>
          prev.map((msg) => {
            if (msg.id === toolCallMessageId) {
              const allToolsComplete = msg.content?.every(isTerminalToolPart);
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
      lastStreamingErrorIsUserFacingRef.current = false;
      fetchConversations();

      // Recover/queue follow-ups on any terminal outcome except a deliberate
      // Stop, which abandons queued work by definition.
      // - completed === false: a local detach (cancelStream during navigation) —
      //   the server turn keeps running and may still drain the steer, so
      //   resending would duplicate it.
      // - stopped: the user asked for this interaction to end; restarting it is
      //   the opposite of what they pressed.
      // - failed: still recovered. Neither an accepted steer nor an adopted
      //   prompt is persisted until the LLM loop drains it and emits the echo,
      //   and the composer cleared its text on accept — so the client's queue
      //   holds the user's ONLY copy, and dropping it here deletes the message.
      //   Each recovery is resent as one new turn, so a conversation that keeps
      //   failing drains the queue rather than looping on it.
      const recoverQueued = completed && !wasStopped;
      // A recovered message's own kickoff can fail before any stream is opened
      // (a 500 from /turns), which reports completed: false. That is terminal
      // for this send, unlike a local detach (navigation), which also reports
      // false but leaves the server turn running. The hook says which happened
      // — inferring it from lastError would also catch a mid-stream error event,
      // which is explicitly non-terminal. Without this the queue stops draining
      // and the messages behind it are lost.
      const terminalKickoffFailure = kickoffFailed && !wasStopped;
      const recoverAdopted = recoverQueued && Boolean(unconsumedAdoptedPrompt);

      // A prompt that reached nothing at all — the kickoff was refused and the
      // steer it was rerouted to found the turn already gone. There is no doubt
      // about delivery, so it is recovered regardless of how this stream ended.
      if (undeliveredPrompt) {
        if (turnId) {
          setMessages((prev) => prev.filter((msg) => msg.turnId !== turnId));
        }
        // To the FRONT of the queue: this was the send that opened the stream,
        // so it predates anything already queued — a steer submitted while the
        // kickoff was still in flight lands here first, and appending would
        // replay the user's messages in the wrong order.
        pendingFollowupsRef.current.unshift(undeliveredPrompt);
      }

      // The stream stopped following this turn without seeing it end, so the
      // echo that would have settled an accepted steer will never arrive. The
      // turn may have drained it (it is persisted, and history now shows it) or
      // died first (it exists nowhere) — and the client cannot tell which, since
      // the events that would say so are exactly what it lost.
      //
      // Neither guess is safe: resending duplicates an instruction the assistant
      // may already have acted on, dropping deletes a message the composer
      // cleared. So hand the text back and let the user decide, as the ambiguous
      // steer failure does. Leaving it registered is the one clearly wrong
      // option — it fires against whatever turn completes next, out of order and
      // possibly hours later.
      //
      // The adopted prompt is in the same position and is the more damaging of
      // the two: it is not in the awaiting-echo list (the hook steered it, not
      // submitSteer), the clean-completion recovery below never runs on this
      // path, and the history reload that follows replaces its optimistic
      // bubble — so without this it exists nowhere at all. It goes first, since
      // it is the send that opened the stream.
      const unresolvedOnGiveUp = reconciledWithoutEnd
        ? [
            ...(unconsumedAdoptedPrompt ? [unconsumedAdoptedPrompt] : []),
            ...awaitingEchoSteersRef.current.map((steer) => steer.prompt),
          ]
        : [];
      if (unresolvedOnGiveUp.length > 0) {
        const unresolved = unresolvedOnGiveUp;
        awaitingEchoSteersRef.current = [];
        // Surfaced above the composer rather than as a message: this path
        // reloads persisted history immediately afterwards, which replaces the
        // thread wholesale and would drop a locally appended bubble.
        setSteerError(
          `Couldn't confirm the assistant received ${
            unresolved.length === 1 ? 'this' : 'these'
          }. Check the reply, then send again if missed: ${unresolved.join(' / ')}`
        );
      }

      // An adopted stream withheld the turn's output until it echoed our prompt,
      // so the tail of the answer it was already giving is missing from the
      // thread. Reconcile it from persisted history whenever such a stream ends
      // — not only when the prompt went unconsumed, which is the narrower case
      // handled below.
      if (adopted && conversationId) {
        // retryIfBailed: recovering a prompt fires its replacement turn right
        // after this, and an active stream makes the reconcile bail.
        void handleReloadHistoryRef.current?.(conversationId, { retryIfBailed: true });
      }

      if (recoverAdopted) {
        // Drop the turn's optimistic bubbles. The resend renders the prompt
        // again, and the assistant row holds nothing worth keeping: the stream
        // suppresses the adopted turn's output until it echoes our prompt,
        // which by definition never happened here.
        if (turnId) {
          setMessages((prev) => prev.filter((msg) => msg.turnId !== turnId));
        }
        // History reconciliation already happened above for every adopted
        // stream, which covers this case too.
      }

      if (recoverQueued) {
        // The adopted prompt goes to the front: it was the send that opened this
        // stream, so it predates every steer typed while the stream ran —
        // whether that steer is already queued here or still awaiting an echo —
        // and queueing it after them would replay the user's messages out of
        // order. It is tracked separately because it isn't in the awaiting-echo
        // list — the hook sent that steer, not submitSteer.
        if (unconsumedAdoptedPrompt) {
          pendingFollowupsRef.current.unshift(unconsumedAdoptedPrompt);
        }

        const unEchoed = awaitingEchoSteersRef.current;
        if (unEchoed.length > 0) {
          awaitingEchoSteersRef.current = [];
          pendingFollowupsRef.current.push(...unEchoed.map((steer) => steer.prompt));
        }
      } else if (completed) {
        // A deliberate Stop: drop everything queued rather than restarting the
        // interaction the user just ended.
        awaitingEchoSteersRef.current = [];
        pendingFollowupsRef.current = [];
      }

      // Fire the next queued follow-up (a steer that hit an already-finished
      // turn, a recovered un-echoed steer, or a recovered adopted prompt). One
      // at a time: each turn's own completion handler fires the next, so they
      // don't start concurrent turns. Defer past this hook's cleanup (which
      // clears abortControllerRef / activeTurnRef after onComplete returns) so
      // the follow-up turn's refs aren't clobbered and Stop/Steer target it
      // correctly.
      //
      // reconciledWithoutEnd counts too: a steer that got 404/409 while this
      // stream was still on screen was queued here as a normal follow-up, and
      // if the stream then gives up none of the other three fire. Unlike the
      // handback above, these need no user decision — a 404/409 means the steer
      // reached no turn at all, so sending it is unambiguous.
      if (recoverQueued || undeliveredPrompt || terminalKickoffFailure || reconciledWithoutEnd) {
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
      }
    },
    [conversationId, fetchConversations]
  );

  // A mid-turn steering message the user sent while the turn was running. Render
  // it as a user bubble just before the in-progress assistant bubble so the
  // conversation reads in order; the turn continues streaming after it.
  const handleStreamingUserInput = useCallback((content: string, inputId: string | null) => {
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
    // An echo with no id comes from a backend that predates the field. Match it
    // the old way — on the text — so a steer this turn really did consume isn't
    // left registered and resent as a follow-up. It can't be credited as
    // positive delivery evidence, since another client's identical message
    // looks the same.
    if (!inputId) {
      const untagged = awaitingEchoSteersRef.current.findIndex(
        (s) => s.prompt.trim() === content.trim()
      );
      if (untagged !== -1) {
        awaitingEchoSteersRef.current.splice(untagged, 1);
      }
      return;
    }
    // The echo confirms the turn consumed this steer, so drop its awaiting-echo
    // entry — it isn't recovered as a follow-up later.
    const idx = awaitingEchoSteersRef.current.findIndex((s) => s.inputId === inputId);
    if (idx !== -1) {
      awaitingEchoSteersRef.current.splice(idx, 1);
    }
    // Record it as observed-delivered, which is what lets a steer whose POST
    // response was lost report success instead of asking the user to resend
    // something the turn already acted on. Bounded: it only has to outlive an
    // in-flight steer request, so a short tail is plenty.
    consumedSteerEchoesRef.current.push(inputId);
    if (consumedSteerEchoesRef.current.length > 20) {
      consumedSteerEchoesRef.current.shift();
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
      options?: {
        errorIfNoReply?: boolean;
        turnId?: string;
        errorOnFailedReload?: boolean;
        retryIfBailed?: boolean;
      }
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
          if (
            result === 'bailed' &&
            (options?.retryIfBailed || (options?.errorIfNoReply && options.turnId))
          ) {
            // A stream was active for this conversation, so the reconcile never
            // ran. Drive the fallback poll to retry once that stream clears —
            // a recovered prompt starts its replacement turn immediately, which
            // is exactly what bails this reload.
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
        modelTier?: string;
      }) => Promise<void>;
      cancelStream: () => void;
      stopTurn: () => Promise<boolean>;
      steerStream: (params: {
        prompt: string;
        inputId: string;
      }) => Promise<'accepted' | 'finished' | 'error'>;
      isStreaming: boolean;
    };

  // Load messages for a conversation
  const loadConversationMessages = useCallback(async (convId: string, background = false) => {
    try {
      // A background reload is deferred, so the user can have moved on before it
      // runs — an aborted stream still reconciles the conversation it was for.
      // Painting that history now would replace the thread on screen with
      // another conversation's, under the current one's composer, and would also
      // abort the load the new conversation has in flight (they share an abort
      // controller). A foreground load IS the user opening this conversation, so
      // it proceeds; the state it sets is what makes the ref match.
      if (background && conversationIdRef.current !== convId) {
        return 'bailed' as ReloadResult;
      }
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
      // A foreground load is an explicit user open, where we adopt the
      // conversation's profile; ask the backend to resolve it (it's computed
      // across the whole conversation, not just the returned page, so adoption
      // is correct even when the last user message is many rows back). Background
      // reloads skip it to keep the response cheap.
      const messagesUrl = background
        ? `/api/v1/chat/conversations/${convId}/messages`
        : `/api/v1/chat/conversations/${convId}/messages?include_conversation_profile=true`;
      const response = await fetch(messagesUrl, {
        signal: messagesAbortController.signal,
      });
      if (response.ok) {
        const data = (await response.json()) as ConversationMessagesResponse;
        if (streamWasActiveAtRequestStart || activeStreamConversationIdRef.current === convId) {
          // A stream is active for this conversation — its own completion will
          // reconcile. Don't clobber it, and treat this as a supersession.
          return 'bailed' as const;
        }
        if (data.messages.length > 0 && conversationIdRef.current === convId) {
          setPersistedConversationId(convId);
        }

        // A foreground load means the user just opened this conversation (every
        // background reload passes background=true). Adopt the profile its
        // history was produced under so the follow-up turn loads the matching
        // (profile-partitioned) history instead of silently dropping context.
        // The backend resolves latest_user_profile_id from the most recent *user*
        // message across the whole conversation — not a delegated assistant row,
        // so a thread that handed off to another profile (e.g. complex_tasks
        // delegating to engineer) still resumes as the profile the user was
        // actually talking to, and not bounded by the returned page, so adoption
        // is correct even when the last user message is many rows back.
        if (!background) {
          const adoptedProfileId = data.latest_user_profile_id;
          if (typeof adoptedProfileId === 'string' && adoptedProfileId) {
            setCurrentProfileId(adoptedProfileId);
          }
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
              reasoning_info: msg.reasoning_info ?? undefined,
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

      conversationIsUnsentDraftRef.current = false;
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

  // Keep the conversation list fresh for activity OUTSIDE the open thread (a
  // new chat started in another tab/device, or a delegated/scheduled/background
  // reply) via the account-global activity stream. The per-conversation stream
  // above only sees the currently-open thread.
  useActivityStream({
    enabled: true,
    onActivity: fetchConversations,
  });

  // Handle new chat creation
  const handleNewChat = useCallback(() => {
    // Cancel any active streaming before creating a new chat
    cancelStream();

    // A new chat starts from the user's preferred profile, not whatever profile
    // an old conversation we were just viewing was adopted into.
    const preferredProfileId = localStorage.getItem('selectedProfileId') || profileId;
    setCurrentProfileId(preferredProfileId);

    const newConvId = `web_conv_${generateUUID()}`;
    conversationIsUnsentDraftRef.current = true;
    setConversationId(newConvId);
    setMobileShowList(false);
    setMessages([]);
    localStorage.setItem('lastConversationId', newConvId);
    window.history.pushState({}, '', `/chat?conversation_id=${newConvId}`);
  }, [cancelStream, profileId]);

  // On mobile, navigate back to conversation list
  const handleBackToList = useCallback(() => {
    cancelStream();
    setMobileShowList(true);
  }, [cancelStream]);

  // Set when a conversation switch is triggered by a profile change, where the
  // draft the user is composing must survive into the fresh conversation.
  const preserveComposerOnConversationSwitchRef = useRef(false);

  // Always-current mirror of "a turn is in flight", so callbacks can read it
  // without taking it as a dependency. Same value the runtime reports as
  // isRunning below.
  const turnIsRunningRef = useRef(false);
  turnIsRunningRef.current = isLoading || isStreaming;

  // A tier choice belongs to the profile and the conversation it was made in.
  // A new chat, opening another conversation, and switching profile all return
  // to the profile default rather than carrying a spend decision into somewhere
  // the user did not make it.
  useEffect(() => {
    setModelTierChoice(null);
  }, [conversationId, currentProfileId]);

  // The control reports a choice of the profile default as no choice at all, so
  // whatever is stored here differs from the default and is worth sending.
  const handleModelTierChange = useCallback((tierId: string | null, pinned: boolean) => {
    setModelTierChoice(tierId === null ? null : { tierId, pinned });
  }, []);

  // Handle profile changes
  const handleProfileChange = useCallback(
    (newProfileId: string) => {
      setCurrentProfileId(newProfileId);
      // Persist selection to localStorage
      localStorage.setItem('selectedProfileId', newProfileId);

      if (currentProfileId === newProfileId || !conversationId) {
        return;
      }
      // An unsent draft has no turns to separate from, so switching in place is
      // enough — minting a new conversation would only churn the id (and reset
      // the composer's surroundings) for no gain.
      if (conversationIsUnsentDraftRef.current) {
        return;
      }
      // Otherwise start a new conversation to keep each profile's context
      // clearly separated. The user is mid-composing the same message they'll
      // send under the new profile, so this switch must not wipe the composer
      // the way a real conversation switch does — UNLESS a turn is running, in
      // which case the composer holds steer text aimed at THAT turn (which
      // handleNewChat is about to cancel). Carrying it over would drop it into
      // an empty thread under a different profile, ready to send as a
      // standalone message: exactly the leak the clear exists to prevent.
      //
      // Read the running state from a ref, NOT from the deps: ProfileSelector
      // re-runs its profile fetch whenever onProfileChange changes identity, so
      // depending on state that flips every turn would refetch profiles mid-turn
      // and blank the picker.
      preserveComposerOnConversationSwitchRef.current = !turnIsRunningRef.current;
      handleNewChat();
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
      // A steer error from the previous turn has been read by now — the user is
      // sending again, which is what it asked them to consider.
      setSteerError(null);
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

      // The conversation now holds a turn, so it is no longer a switch-in-place
      // draft: a later profile change has real context to separate from.
      conversationIsUnsentDraftRef.current = false;

      const targetConversationId = conversationId || `web_conv_${generateUUID()}`;

      // Optimistically surface the conversation in the sidebar the instant the
      // user sends, so a brand-new chat appears immediately (and an existing one
      // bumps to the top) without waiting for the server round-trip. The
      // authoritative fetchConversations on stream completion — and the activity
      // stream — reconcile this row by conversation_id, so there's no duplicate.
      const optimisticPreview = (message.content[0]?.text ?? '').slice(0, 100);
      setConversations((prev) => {
        const existing = prev.find((c) => c.conversation_id === targetConversationId);
        const others = prev.filter((c) => c.conversation_id !== targetConversationId);
        const optimisticRow: Conversation = {
          conversation_id: targetConversationId,
          last_message: optimisticPreview,
          last_timestamp: new Date().toISOString(),
          message_count: (existing?.message_count ?? 0) + 1,
        };
        // Remember it (keyed by THIS turn) so a list fetch already in flight
        // can't erase it; retired when this turn settles (see
        // handleStreamingComplete). Keying by turn id means an aborted older turn
        // can't retire a newer turn's pending row for the same conversation.
        pendingOptimisticConversationsRef.current.set(turnId, optimisticRow);
        return [optimisticRow, ...others];
      });

      streamingMessageIdRef.current = assistantMessageId;
      activeStreamConversationIdRef.current = targetConversationId;
      activeTurnIdRef.current = turnId;
      turnStoppedRef.current = false;
      lastStreamingErrorRef.current = null;
      lastStreamingErrorIsUserFacingRef.current = false;
      selfTurnIdsRef.current.add(turnId);

      // A tier choice applies to this send. An unpinned one is spent here, so
      // the control returns to the profile default instead of quietly repricing
      // every later message in the conversation.
      const tierChoice = modelTierChoiceRef.current;
      if (tierChoice && !tierChoice.pinned) {
        setModelTierChoice(null);
      }

      await sendStreamingMessage({
        prompt: message.content[0].text,
        conversationId: targetConversationId,
        profileId: currentProfileId,
        interfaceType: 'web',
        attachments: processedAttachments,
        turnId,
        modelTier: tierChoice?.tierId,
      });
    },
    [conversationId, sendStreamingMessage, currentProfileId]
  );

  // Expose handleNew to the earlier-defined completion handler so it can fire a
  // queued steer follow-up after the current stream settles.
  useEffect(() => {
    handleNewRef.current = handleNew;
  }, [handleNew]);

  // Same for handleReloadHistory, which that handler uses to reconcile an
  // adopted turn whose output was suppressed pending an echo that never came.
  useEffect(() => {
    handleReloadHistoryRef.current = handleReloadHistory;
  }, [handleReloadHistory]);

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
      // assistant-ui rebuilds each message from the fields it knows and drops
      // the rest, so anything of ours the thread has to render — which profile
      // answered, which model tier served it — travels in metadata.custom.
      metadata: {
        custom: {
          processing_profile_id: message.processing_profile_id,
          reasoning_info: message.reasoning_info,
        },
      },
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

  // The main composer doubles as the steer input and its runtime is reused
  // across conversation changes. On a real conversation switch, clear it so
  // steer text typed for the previous turn can't leak into — and be sent in —
  // the newly selected thread. Guarded on an actual id change so an unrelated
  // runtime re-render never wipes text the user is typing. A switch caused by
  // a profile change is exempt: the user keeps composing the same draft, just
  // under a different profile.
  const prevConversationIdRef = useRef(conversationId);
  useEffect(() => {
    if (prevConversationIdRef.current !== conversationId) {
      prevConversationIdRef.current = conversationId;
      if (preserveComposerOnConversationSwitchRef.current) {
        preserveComposerOnConversationSwitchRef.current = false;
      } else {
        runtime.thread.composer.setText('');
      }
    }
  }, [conversationId, runtime]);

  // Submit a steer into the running turn. While a turn runs the main composer
  // doubles as the steer input, so the text is passed in (and owned by the
  // composer); the result tells the composer whether to clear itself.
  const submitSteer = useCallback(
    async (promptRaw: string): Promise<SteerResult> => {
      const prompt = promptRaw.trim();
      if (!prompt) {
        return 'finished';
      }
      setSteerError(null);
      // Register as awaiting-echo BEFORE the request, not after it resolves. The
      // turn can drain the steer and publish its user_input echo while the POST
      // response is still in flight; the echo handler then finds nothing to
      // remove, and registering afterwards would leave an already-consumed
      // prompt marked unconsumed — which terminal recovery resends, repeating
      // whatever the user asked for. Registered early, the echo removes it.
      // Names this submission on the wire: the turn's echo carries the id back,
      // so delivery is established by identity rather than by matching text that
      // another client — or this one, earlier — could have sent too.
      const inputId = generateUUID();
      awaitingEchoSteersRef.current.push({ inputId, prompt });
      const result = await steerStream({ prompt, inputId });
      if (result === 'accepted') {
        // Left registered (or already removed by its echo): if the turn ends
        // without draining it, the completion handler recovers it as a normal
        // follow-up instead of losing it.
        return 'accepted';
      }
      const echoedIdx = consumedSteerEchoesRef.current.indexOf(inputId);
      if (echoedIdx !== -1) {
        // We SAW the turn echo this submission back, so it was delivered and
        // only the response was lost. Report acceptance so the composer clears;
        // keeping the text would invite a retry that sends the instruction a
        // second time.
        //
        // This covers 'finished' as well as 'error': steerStream retries a lost
        // 5xx with the same body, and the turn can drain the steer and end in
        // the meantime, so the retry sees 409. Resending on that would repeat an
        // instruction the assistant already acted on.
        //
        // This keys off having observed the echo, not off the registration
        // being absent: Stop, a conversation change and the recovery drain all
        // empty that registry too, so absence would read a message the turn
        // never saw as delivered and drop it.
        consumedSteerEchoesRef.current.splice(echoedIdx, 1);
        return 'accepted';
      }
      const registeredIdx = awaitingEchoSteersRef.current.findIndex(
        (steer) => steer.inputId === inputId
      );
      // Otherwise nothing will echo it, so drop the registration again: the
      // caller handles this prompt itself (resending a 'finished' one as a
      // normal message, keeping an 'error' one in the composer), and leaving it
      // registered would have recovery send it a second time.
      if (registeredIdx !== -1) {
        awaitingEchoSteersRef.current.splice(registeredIdx, 1);
      }
      if (result === 'error') {
        // The turn may still be running and the steer may even have been
        // accepted before the response failed, so DON'T auto-resend. Surface it
        // and let the composer keep the text for a manual retry.
        setSteerError('Could not steer the assistant. Please try again.');
        return 'error';
      }
      // 'finished': the steer targeted an already-finished turn. Don't lose the
      // text — send it as a normal follow-up. If a stream is still settling,
      // defer until its completion handler runs so we don't race its shared-ref
      // cleanup.
      if (streamingMessageIdRef.current) {
        pendingFollowupsRef.current.push(prompt);
      } else {
        // Kick off the follow-up without awaiting its full stream: submitSteer
        // must resolve promptly so the composer clears and the action reverts to
        // Stop for the new turn, instead of staying stuck on a disabled Steer
        // button with stale text for the whole response. handleNew surfaces its
        // own streaming errors, matching the queued-follow-up path above.
        void handleNew({ content: [{ text: prompt }] });
      }
      return 'finished';
    },
    [steerStream, handleNew]
  );

  const chatControls = useMemo(
    () => ({
      submitSteer,
      steerError,
    }),
    [steerError, submitSteer]
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
      conversationIsUnsentDraftRef.current = false;
      setConversationId(urlConversationId);
      loadConversationMessages(urlConversationId);
    } else if (lastConversationId) {
      conversationIsUnsentDraftRef.current = false;
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

              <div className="flex items-center gap-2">
                <ProfileSelector
                  selectedProfileId={currentProfileId}
                  onProfileChange={handleProfileChange}
                  disabled={isLoading}
                  onLoadingChange={setProfilesLoading}
                />
                <IntelligenceSelector
                  tiers={modelTiers}
                  defaultTierId={defaultModelTier}
                  selectedTierId={modelTierChoice?.tierId ?? null}
                  pinned={modelTierChoice?.pinned ?? false}
                  onChange={handleModelTierChange}
                  disabled={isLoading}
                />
              </div>

              <div className="flex items-center gap-2 ml-auto">
                <ShareConversationButton
                  conversationId={conversationId}
                  hasPersistedMessages={
                    messages.length > 0 && persistedConversationId === conversationId
                  }
                />
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

              <div className="flex items-center gap-2">
                <ProfileSelector
                  selectedProfileId={currentProfileId}
                  onProfileChange={handleProfileChange}
                  disabled={isLoading}
                  onLoadingChange={setProfilesLoading}
                />
                <IntelligenceSelector
                  tiers={modelTiers}
                  defaultTierId={defaultModelTier}
                  selectedTierId={modelTierChoice?.tierId ?? null}
                  pinned={modelTierChoice?.pinned ?? false}
                  onChange={handleModelTierChange}
                  disabled={isLoading}
                />
              </div>

              <div className="flex items-center gap-2 ml-auto">
                <ShareConversationButton
                  conversationId={conversationId}
                  hasPersistedMessages={
                    messages.length > 0 && persistedConversationId === conversationId
                  }
                />
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

// One profile fetch for the whole page: the picker, the intelligence control
// and the per-message badges in the thread all read the same list.
const ChatApp: React.FC<ChatAppProps> = (props) => (
  <ProfilesProvider>
    <ChatAppContent {...props} />
  </ProfilesProvider>
);

export default ChatApp;
