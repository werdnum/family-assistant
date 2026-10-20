import {
  AssistantRuntimeProvider,
  type Attachment,
  type CreateAttachment,
  MessageNotSentError,
  useExternalStoreRuntime,
} from '@assistant-ui/react';
import { MessagesSquare } from 'lucide-react';
import React, {
  Component,
  useCallback,
  useEffect,
  useImperativeHandle,
  useMemo,
  useRef,
  useState,
} from 'react';
import type { ErrorInfo, ReactNode } from 'react';
import { Button } from '@/components/ui/button';
import { AppTopBar } from '../shared/AppShell';
import { getDiagnosticsUrl } from '../utils/diagnosticsUrl';
import { parseToolArguments } from '../utils/toolUtils';
import { generateUUID } from '../utils/uuid';
import { defaultAttachmentAdapter } from './attachmentAdapter';
import { type ConversationDraft, useChatShell } from './chatShell';
import { LOADING_MARKER } from './constants';
import IntelligenceSelector from './IntelligenceSelector';
import { getProfileDisplayName } from './profileNames';
import ProfileSelector from './ProfileSelector';
import { type ModelTier, useProfiles } from './profilesContext';
import { ShareConversationButton } from './ShareConversationButton';
import { useConversationShareLink } from './useConversationShareLink';
import {
  ChatControlsContext,
  type ConversationLoadStatus,
  type OlderMessagesStatus,
  type SteerResult,
} from './chatControls';
import { interleaveTextAndToolCalls, isToolCallDisplay, toolCallParts } from './orderedParts';
import { Thread } from './Thread';
import {
  type HomeTurnActions,
  HomeTurnActionsContext,
  type HomeTurnTarget,
} from './homeTurnActions';
import { MoveToThreadDialog } from './MoveToThreadDialog';
import { type ContinueAnchor, StartThreadDialog, threadProfiles } from './StartThreadDialog';
import { ThreadBanner } from './ThreadBanner';
import { ThreadsContext } from './threadsContext';
import { ThreadStateDot } from './ThreadStateDot';
import { markThreadDone, type ThreadAttachment } from './threadsApi';
import { ToolConfirmationProvider } from './ToolConfirmationContext';
import { isTerminalToolOutcome, type TerminalToolOutcome } from './toolOutcome';
import {
  BackendAttachment,
  BackendConversationMessage,
  ConversationMessagesResponse,
  Message,
  MessageContent,
  MessageReasoningInfo,
  ThreadSummary,
} from './types';
import { useLiveMessageUpdates } from './useLiveMessageUpdates';
import { useMarkRead } from './useMarkRead';
import { usePendingDelegations } from './usePendingDelegations';
import { useStreamingResponse } from './useStreamingResponse';

// Stable empty list for profiles that offer no choice of tier, so the
// intelligence control's props keep their identity across renders.
const EMPTY_MODEL_TIERS: ModelTier[] = [];

/** The model tier one conversation is on, under one profile. */
interface ConversationModelTier {
  conversationId: string | null;
  profileId: string;
  /**
   * Whether the conversation has been on a chosen tier. From then on every send
   * names its tier, the default included: a send naming none continues whatever
   * the server last recorded, which a failed send could leave different from
   * what the control shows.
   */
  namesTier: boolean;
  /** The tier the control shows; null for the profile default. */
  selected: string | null;
}

/** *state* if it belongs to this conversation and profile, else null (the default). */
function modelTierFor(
  state: ConversationModelTier | null,
  conversationId: string | null,
  profileId: string
): ConversationModelTier | null {
  return state && state.conversationId === conversationId && state.profileId === profileId
    ? state
    : null;
}

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
    if (
      !error.message?.includes('tapClientLookup') &&
      !error.message?.includes('useClientLookup')
    ) {
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
    id: `msg_unconfirmed_${turnId}`,
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

/** Carry the ids this tab already shows over onto a reload's server-derived
 * messages, so a reload doesn't remount them. A message this tab sent gets a
 * client id; history names the same message `msg_<internal_id>`, and adopting
 * that id would remount the bubble, replay its entrance animation and drop UI
 * state such as tool-group expansion. Within each turn, the previous messages
 * whose id the reload doesn't contain are paired in order, per role, with the
 * reloaded messages whose id wasn't already on screen. Ids the two lists share
 * are left alone, which keeps a turn the history window cuts in half from
 * shifting its ids when an older page widens the window.
 *
 * Pairing from the front is only one-to-one when the reload holds the whole
 * turn. The history window drops a turn's oldest rows first, and a turn opens
 * with its user message, so a turn the reload holds fewer user rows for than
 * the screen does has been clipped; its ids are left as the reload names them. */
export function adoptPreviousMessageIds(
  reloadedMessages: Message[],
  previousMessages: Message[]
): Message[] {
  const countUserRowsByTurn = (messages: Message[]): Map<string, number> => {
    const counts = new Map<string, number>();
    for (const msg of messages) {
      if (msg.turnId && msg.role === 'user') {
        counts.set(msg.turnId, (counts.get(msg.turnId) ?? 0) + 1);
      }
    }
    return counts;
  };
  const reloadedUserRows = countUserRowsByTurn(reloadedMessages);
  const previousUserRows = countUserRowsByTurn(previousMessages);
  const reloadedIds = new Set(reloadedMessages.map((msg) => msg.id));
  const previousIds = new Set(previousMessages.map((msg) => msg.id));
  const unmatchedPrevious = new Map<string, string[]>();
  for (const msg of previousMessages) {
    if (!msg.turnId || reloadedIds.has(msg.id)) {
      continue;
    }
    const shownUserRows = previousUserRows.get(msg.turnId) ?? 0;
    if (shownUserRows === 0 || (reloadedUserRows.get(msg.turnId) ?? 0) < shownUserRows) {
      continue;
    }
    const key = `${msg.role}\u0000${msg.turnId}`;
    const ids = unmatchedPrevious.get(key);
    if (ids) {
      ids.push(msg.id);
    } else {
      unmatchedPrevious.set(key, [msg.id]);
    }
  }
  if (unmatchedPrevious.size === 0) {
    return reloadedMessages;
  }
  return reloadedMessages.map((msg) => {
    if (!msg.turnId || previousIds.has(msg.id)) {
      return msg;
    }
    const previousId = unmatchedPrevious.get(`${msg.role}\u0000${msg.turnId}`)?.shift();
    return previousId ? { ...msg, id: previousId } : msg;
  });
}

/** Extract plain text from a backend message's content for a notification
 * preview. Content is either a string or an array of parts; only `text` parts
 * contribute (image/tool parts have no preview text). */
/** A message's text, as a quote of it shows. */
function messageText(message: Message): string {
  return message.content
    .filter((part) => part.type === 'text' && part.text && part.text !== LOADING_MARKER)
    .map((part) => part.text)
    .join('\n')
    .trim();
}

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
        internalId: message.internalId ?? previousMessage.internalId,
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

// Rows of history fetched when a conversation opens, and added per "load earlier".
const HISTORY_PAGE_SIZE = 50;

// A file the user picked goes back through the attachment adapter, so it is
// validated and uploaded at send exactly as when it was first added; anything
// already carrying its content is put back as it is.
function restorableAttachment(attachment: Attachment): File | CreateAttachment {
  if (attachment.file) {
    return attachment.file;
  }
  return {
    id: attachment.id,
    type: attachment.type,
    name: attachment.name,
    contentType: attachment.contentType,
    content: attachment.content ?? [],
  };
}

/** How a pane came to show another conversation. */
export type ConversationChangeReason =
  /** It opened one that exists. */
  | 'open'
  /** It started a new, unsent one. */
  | 'new'
  /** A send minted the conversation it went to. */
  | 'send';

/** What a pane tells the chat about the conversation it shows. */
export interface PaneReport {
  conversationId: string | null;
  /** Tool calls rendered in it, whose approvals show inline rather than in the tray. */
  toolCallIds: ReadonlySet<string>;
  isStreaming: boolean;
}

export interface ConversationPaneHandle {
  /** Show this conversation. False when the pane refused, mid-send. */
  openConversation: (conversationId: string) => boolean;
  /** Start a new, unsent conversation on the preferred profile. */
  newChat: () => boolean;
  /** Send a message into the conversation on screen, as if typed. */
  send: (text: string) => void;
}

export interface ConversationPaneProps {
  ref?: React.Ref<ConversationPaneHandle>;
  /** The profile a new conversation starts on when none was chosen. */
  profileId: string;
  layout: 'mobile' | 'desktop';
  /**
   * Where the pane sits: the main column, or the right-hand panel beside it.
   * A thread in the main column offers the way back to Home; in the panel,
   * Home is already beside it.
   */
  presentation: 'main' | 'side';
  /**
   * A hidden pane keeps its conversation, stream and draft, renders nothing,
   * and marks nothing read.
   */
  hidden: boolean;
  /** Called synchronously whenever the pane moves to another conversation. */
  onConversationChange: (conversationId: string, reason: ConversationChangeReason) => void;
  onReport: (report: PaneReport) => void;
  /** Leading header controls the chat supplies (the sidebar toggle). */
  headerStart?: ReactNode;
  /** Trailing header controls the chat supplies. */
  headerEnd?: ReactNode;
  /** Shown between the header and the transcript (alerts, the approvals tray). */
  topSlot?: ReactNode;
  /** The mobile header's way back to the conversation list. */
  onBackToList?: () => void;
}

/**
 * One conversation on screen: its transcript, live stream, composer, turn
 * state, profile and tier, thread banner and read marker. Everything here is
 * per pane, so two panes can show two conversations side by side; what is
 * shared between them comes from the chat shell.
 */
export const ConversationPane: React.FC<ConversationPaneProps> = ({
  ref,
  profileId,
  layout,
  presentation,
  hidden,
  onConversationChange,
  onReport,
  headerStart,
  headerEnd,
  topSlot,
  onBackToList,
}) => {
  const isMobile = layout === 'mobile';
  const {
    homeConversationId,
    homeThreads,
    updateHomeThread,
    refreshHomeThreads,
    openConversation,
    noteTurnStarted,
    noteTurnSettled,
    pendingConfirmations,
    onConfirmationRequest,
    onConfirmationResult,
    confirmTool,
    showNotification,
    drafts,
    startThread,
    takeTurnToFollow,
    homeQuotes,
    reportHomeQuotes,
  } = useChatShell();
  const onConversationChangeRef = useRef(onConversationChange);
  onConversationChangeRef.current = onConversationChange;
  const [messages, setMessages] = useState<Message[]>([]);
  const [isLoading, setIsLoading] = useState<boolean>(false);
  // Which rows every load of the open conversation fetches. It starts as the
  // latest page; loading earlier history widens it and pins it to the oldest
  // row loaded (floorId), so later reloads (after a turn, or from the follow
  // stream) keep that history as new rows arrive, and always return a
  // contiguous tail with no gap to stitch.
  const historyWindowRef = useRef<{
    convId: string | null;
    limit: number;
    floorId: string | null;
  }>({
    convId: null,
    limit: HISTORY_PAGE_SIZE,
    floorId: null,
  });
  const [olderHistory, setOlderHistory] = useState<{ convId: string; hasMore: boolean } | null>(
    null
  );
  const [olderMessagesStatus, setOlderMessagesStatus] = useState<OlderMessagesStatus>('idle');
  // The open of a conversation that has not yet produced its history. Keyed by
  // conversation so a result can only ever describe the conversation it was
  // fetched for; while it is set for the selected conversation the thread shows
  // its status instead of a transcript, and sending is refused.
  const [conversationLoad, setConversationLoad] = useState<{
    convId: string;
    status: ConversationLoadStatus;
  } | null>(null);
  const conversationLoadRef = useRef(conversationLoad);
  conversationLoadRef.current = conversationLoad;
  const [conversationId, setConversationId] = useState<string | null>(null);
  const shareLink = useConversationShareLink(conversationId);
  const { delegations: pendingDelegations, refresh: refreshPendingDelegations } =
    usePendingDelegations(conversationId);
  const [persistedConversationId, setPersistedConversationId] = useState<string | null>(null);
  // Always-current mirror of conversationId, so a deferred follow-up timer can
  // synchronously check whether the conversation changed before it fires.
  const conversationIdRef = useRef<string | null>(null);
  conversationIdRef.current = conversationId;
  const homeConversationIdRef = useRef<string | null>(null);
  homeConversationIdRef.current = homeConversationId;
  // The thread a foreground load reported the open conversation to be, for a
  // thread that is not (yet) in Home's list.
  // The latest turn sent from here that ended in the conversation on screen.
  const [localTurnEnded, setLocalTurnEnded] = useState<{ convId: string; seq: number } | null>(
    null
  );
  const [openedThread, setOpenedThread] = useState<{
    convId: string;
    thread: ThreadSummary;
  } | null>(null);
  // True while the open conversation exists only in this client and holds no
  // turns: an id minted by handleNewChat (or at startup) that has never been
  // sent. Tracked explicitly rather than inferred from an empty `messages`,
  // which also reads empty while a real conversation's history is loading.
  const conversationIsUnsentDraftRef = useRef(true);
  // Set to a conversation id while it has a turn that gave up but is still running
  // server-side, to drive the fallback reconcile poll. Cleared once the turn
  // resolves (reply lands or it finishes with none).
  const [pendingReconcileConvId, setPendingReconcileConvId] = useState<string | null>(null);
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
  // The intelligence (model tier) the conversation is on. A choice is sticky:
  // the server carries it to every later turn that names no tier, so it holds
  // until the user picks again. `selected` is what the control shows, `null`
  // for the profile's default; `namesTier` says whether a send has to name the
  // default, which it must once the conversation has been on a chosen tier. Keyed by conversation and profile, so a new chat, another
  // conversation and another profile all read as the default without an effect
  // racing the adoption of a reopened conversation's tier.
  const [modelTierState, setModelTierState] = useState<ConversationModelTier | null>(null);
  // Mirror for handleNew, which must not take the choice as a dependency: its
  // identity feeds the assistant-ui runtime, and picking a tier should not
  // rebuild the send path mid-conversation.
  const modelTierStateRef = useRef(modelTierState);
  modelTierStateRef.current = modelTierState;
  const { profilesById, defaultProfileId } = useProfiles();
  const isHome = homeConversationId !== null && conversationId === homeConversationId;
  const currentThread: ThreadSummary | null = conversationId
    ? (homeThreads.find((thread) => thread.conversation_id === conversationId) ??
      (openedThread?.convId === conversationId ? openedThread.thread : null))
    : null;
  // Home always runs on the default profile and a thread on its own, so the
  // picker is locked there: changing profile would otherwise start a new
  // conversation, leaving Home or the thread behind.
  const lockedProfileId = currentThread
    ? currentThread.profile_id
    : isHome
      ? (defaultProfileId ?? profileId)
      : null;
  const profileLockedRef = useRef(false);
  profileLockedRef.current = lockedProfileId !== null;
  useEffect(() => {
    if (lockedProfileId !== null && lockedProfileId !== currentProfileId) {
      setCurrentProfileId(lockedProfileId);
    }
  }, [lockedProfileId, currentProfileId]);
  const currentProfile = profilesById[currentProfileId];
  const modelTiers: ModelTier[] = currentProfile?.model_tiers ?? EMPTY_MODEL_TIERS;
  const defaultModelTier = currentProfile?.default_model_tier ?? null;
  const defaultModelTierRef = useRef(defaultModelTier);
  defaultModelTierRef.current = defaultModelTier;
  const selectedModelTierId =
    modelTierFor(modelTierState, conversationId, currentProfileId)?.selected ?? null;
  const streamingMessageIdRef = useRef<string | null>(null);
  const activeStreamConversationIdRef = useRef<string | null>(null);
  // Follows the first turn of a thread the user just started, once the thread's
  // history has loaded here; defined below, beside the send path.
  const followTurnRef = useRef<(conversationId: string) => void>(() => {});
  // The profile the next send goes to as a new thread's opening message, set
  // by the composer's send-as-thread menu just before it sends.
  const sendAsThreadProfileRef = useRef<string | null>(null);
  const sendAsThreadRef = useRef<
    (profileId: string, text: string, attachments: ThreadAttachment[]) => Promise<void>
  >(async () => {});
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
  const messagesAbortControllerRef = useRef<AbortController | null>(null);
  // The conversation whose open (foreground load) is in flight, if any.
  const openInFlightConvIdRef = useRef<string | null>(null);
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
  const showNotificationRef = useRef(showNotification);
  showNotificationRef.current = showNotification;

  const handleConfirmation = useCallback(
    (_toolCallId: string, requestId: string, approved: boolean) =>
      confirmTool(requestId, approved, conversationId),
    [confirmTool, conversationId]
  );

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
            return {
              ...msg,
              content: interleaveTextAndToolCalls(content, toolCallParts(msg.content)),
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

      // The turn may have handed work off to the background.
      refreshPendingDelegations();
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
                return {
                  ...msg,
                  content: interleaveTextAndToolCalls(
                    content + errorSuffix,
                    toolCallParts(msg.content)
                  ),
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
                // The apology follows the tools that ran before the failure.
                return {
                  ...msg,
                  content: [
                    ...toolCallParts(msg.content),
                    {
                      type: 'text',
                      text: `Sorry, I encountered an error after running tools. [View diagnostics](${diagnosticsUrl}) for debugging details.`,
                    },
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

      // A turn that ran to its end in the conversation on screen was read as
      // it streamed, though none of its rows have stored ids here yet.
      const streamedConversationId = activeStreamConversationIdRef.current;
      if (
        completed &&
        !kickoffFailed &&
        streamedConversationId &&
        streamedConversationId === conversationIdRef.current
      ) {
        setLocalTurnEnded((prev) => ({
          convId: streamedConversationId,
          seq: (prev?.seq ?? 0) + 1,
        }));
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
      // Retire this turn's optimistic list row and refetch the list and Home's
      // threads: the turn may have started a thread, or been one. Keyed by the
      // COMPLETING turn id — not activeStreamConversationIdRef, which a
      // superseding send may have already repointed — so an aborted older turn
      // can't retire a newer turn's pending row.
      noteTurnSettled(turnId);

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
    [conversationId, noteTurnSettled, refreshPendingDelegations]
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
  const handleStreamingToolCall = useCallback(
    (toolCalls: Array<Record<string, unknown>>, streamedText = '') => {
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
                if (isTerminalToolOutcome(tc.outcome)) {
                  result.outcome = tc.outcome;
                }
                // Add attachments if present
                if (tc.attachments !== undefined && Array.isArray(tc.attachments)) {
                  result.attachments = [...tc.attachments];
                }
                if (isToolCallDisplay(tc.display)) {
                  result.display = tc.display;
                }
                if (typeof tc.textOffset === 'number') {
                  result.textOffset = tc.textOffset;
                }
                return result;
              });

              return {
                ...msg,
                content: interleaveTextAndToolCalls(streamedText, toolParts),
                isLoading: false,
                status: { type: 'running' as const },
              };
            }
            return msg;
          });

          return updatedMessages;
        });
      }
    },
    []
  );

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
      onToolConfirmationRequest: onConfirmationRequest,
      onToolConfirmationResult: onConfirmationResult,
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
        existingTurn?: { firstSeq: number; alreadyComplete?: boolean; incomplete?: boolean };
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
  const loadConversationMessages = useCallback(async (convId: string, reload = false) => {
    // Every load aborts the one before it (they share an abort controller), so
    // a reload that lands while this conversation is still opening ends that
    // open. It takes the open over, so the open still settles — with history,
    // the adopted profile, or an error and Retry — instead of being left
    // loading forever.
    const background = reload && openInFlightConvIdRef.current !== convId;
    const clearConversationLoad = (id: string) =>
      setConversationLoad((prev) => (prev?.convId === id ? null : prev));
    const failConversationLoad = (id: string) =>
      setConversationLoad((prev) =>
        prev?.convId === id ? { convId: id, status: 'failed' } : prev
      );
    let messagesAbortController: AbortController | null = null;
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
      messagesAbortController = new AbortController();
      messagesAbortControllerRef.current = messagesAbortController;

      if (!background) {
        openInFlightConvIdRef.current = convId;
        setIsLoading(true);
        setConversationLoad({ convId, status: 'loading' });
      }
      // Opening a conversation starts from one page of history; a background
      // reload of the same conversation keeps whatever window the user has
      // widened it to.
      if (!background || historyWindowRef.current.convId !== convId) {
        historyWindowRef.current = { convId, limit: HISTORY_PAGE_SIZE, floorId: null };
      }
      const requestWindow = historyWindowRef.current;
      // A pinned window asks for a page of headroom, so rows that arrived since
      // the last load can't push its oldest row out of the response.
      const requestLimit =
        requestWindow.floorId === null
          ? requestWindow.limit
          : requestWindow.limit + HISTORY_PAGE_SIZE;
      const params = new URLSearchParams({ limit: String(requestLimit) });
      // A foreground load is an explicit user open, where we adopt the
      // conversation's profile; ask the backend to resolve it (it's computed
      // across the whole conversation, not just the returned page, so adoption
      // is correct even when the last user message is many rows back). Background
      // reloads skip it to keep the response cheap.
      if (!background) {
        params.set('include_conversation_profile', 'true');
      }
      const response = await fetch(`/api/v1/chat/conversations/${convId}/messages?${params}`, {
        signal: messagesAbortController.signal,
      });
      if (response.ok) {
        const data = (await response.json()) as ConversationMessagesResponse;
        if (streamWasActiveAtRequestStart || activeStreamConversationIdRef.current === convId) {
          // A stream is active for this conversation — its own completion will
          // reconcile. Don't clobber it, and treat this as a supersession. The
          // stream owns what the thread shows, so the open is over.
          clearConversationLoad(convId);
          return 'bailed' as const;
        }
        // The user can have left this conversation while the request was in
        // flight; its history must not replace the one now on screen.
        if (conversationIdRef.current !== convId) {
          return 'bailed' as const;
        }

        // Trim the headroom back to the oldest row the user had loaded, and
        // keep the window pinned there so the next load covers the same range
        // plus whatever arrives in the meantime.
        const { messages: fetchedRows, has_more_before: fetchedHasMore } = data;
        const floorIndex =
          requestWindow.floorId === null
            ? -1
            : fetchedRows.findIndex((row) => row.internal_id === requestWindow.floorId);
        const historyRows = floorIndex > 0 ? fetchedRows.slice(floorIndex) : fetchedRows;
        const historyHasMore = floorIndex > 0 || fetchedHasMore === true;
        const windowIsWidened =
          requestWindow.floorId !== null || requestWindow.limit > HISTORY_PAGE_SIZE;
        if (windowIsWidened && historyWindowRef.current === requestWindow) {
          historyWindowRef.current = {
            convId,
            limit: Math.max(historyRows.length, HISTORY_PAGE_SIZE),
            floorId: historyRows[0]?.internal_id ?? null,
          };
        }

        if (historyRows.length > 0 && conversationIdRef.current === convId) {
          setPersistedConversationId(convId);
        }
        setOlderHistory({ convId, hasMore: historyHasMore });
        setOlderMessagesStatus((prev) => (prev === 'failed' ? 'idle' : prev));
        clearConversationLoad(convId);

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
          setOpenedThread(data.thread ? { convId, thread: data.thread } : null);
          const adoptedProfileId = data.latest_user_profile_id;
          if (typeof adoptedProfileId === 'string' && adoptedProfileId) {
            setCurrentProfileId(adoptedProfileId);
            // The tier the conversation is on comes with it, so the control
            // shows what the next turn will actually run at.
            const conversationTier = data.conversation_model_tier ?? null;
            setModelTierState({
              conversationId: convId,
              profileId: adoptedProfileId,
              namesTier: conversationTier !== null,
              selected: conversationTier,
            });
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
        const toolOutcomes = new Map<string, TerminalToolOutcome>();
        const toolAttachments = new Map<string, BackendAttachment[]>();

        // First pass: collect tool responses and attachments
        historyRows.forEach((msg: BackendConversationMessage) => {
          if (msg.role === 'tool' && msg.tool_call_id) {
            const responseContent =
              typeof msg.content === 'string'
                ? msg.content
                : Array.isArray(msg.content)
                  ? JSON.stringify(msg.content)
                  : 'Tool executed successfully';
            toolResponses.set(msg.tool_call_id, responseContent);
            if (isTerminalToolOutcome(msg.tool_outcome)) {
              toolOutcomes.set(msg.tool_call_id, msg.tool_outcome);
            }

            // Collect attachments from tool messages for synthesis
            const toolMessageAttachments = msg.attachments;
            if (Array.isArray(toolMessageAttachments) && toolMessageAttachments.length > 0) {
              toolAttachments.set(msg.tool_call_id, toolMessageAttachments);
            }
          }
        });

        historyRows.forEach((msg: BackendConversationMessage) => {
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
              internalId: Number(msg.internal_id),
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
                  outcome: toolOutcomes.get(toolCall.id),
                  awaitingResult:
                    toolResponse === undefined &&
                    Boolean(msg.turn_id && runningTurnIds.has(msg.turn_id)),
                  display: isToolCallDisplay(toolCall.display) ? toolCall.display : undefined,
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
              internalId: Number(msg.internal_id),
              echoed_from_thread: msg.echoed_from_thread ?? undefined,
              moved_to_thread: msg.moved_to_thread ?? undefined,
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
            internalId: Number(msg.internal_id),
            echoed_from_thread: msg.echoed_from_thread ?? undefined,
            moved_to_thread: msg.moved_to_thread ?? undefined,
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
        const latestAssistantMessage = [...historyRows]
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

        setMessages((prev) => {
          const reloaded = adoptPreviousMessageIds(messagesWithArrayContent, prev);
          return runningUnconfirmedTurnIds.size > 0
            ? preserveRunningLoadingMessages(reloaded, prev, runningUnconfirmedTurnIds)
            : reloaded;
        });
        return 'applied' as const;
      }
      // A non-OK response is a genuine reconcile failure, not a supersession.
      if (!background) {
        failConversationLoad(convId);
      }
      return 'failed' as const;
    } catch (error) {
      // An AbortError means a newer load superseded this one (bailed); any other
      // error is a genuine fetch failure.
      if (error instanceof Error && error.name === 'AbortError') {
        return 'bailed' as const;
      }
      console.error('Error loading conversation:', error);
      if (!background) {
        failConversationLoad(convId);
      }
      return 'failed' as const;
    } finally {
      // A superseded open leaves the flag to the load that replaced it.
      if (!background && messagesAbortControllerRef.current === messagesAbortController) {
        openInFlightConvIdRef.current = null;
        setIsLoading(false);
      }
    }
  }, []);
  loadConversationMessagesRef.current = loadConversationMessages;

  // Widen the open conversation's history window by a page and reload it. A
  // reload this supersedes (or that supersedes it) reads the widened window
  // too, so a 'bailed' result still ends with the older rows shown.
  const loadOlderMessages = useCallback(async () => {
    const convId = conversationIdRef.current;
    if (!convId) {
      return;
    }
    const current = historyWindowRef.current;
    const limit =
      (current.convId === convId ? current.limit : HISTORY_PAGE_SIZE) + HISTORY_PAGE_SIZE;
    historyWindowRef.current = { convId, limit, floorId: null };
    setOlderMessagesStatus('loading');
    const result = await loadConversationMessages(convId, true);
    setOlderMessagesStatus(result === 'failed' ? 'failed' : 'idle');
  }, [loadConversationMessages]);

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

  // Whether the composer is mid-send: its text has gone out but its
  // attachments are still uploading. The runtime hands the finished message to
  // whichever conversation is open when the upload completes, so switching in
  // that window would post this conversation's message into another one.
  const composerSendingRef = useRef(false);

  // Open a conversation in this pane.
  const handleConversationSelect = useCallback(
    (convId: string): boolean => {
      if (composerSendingRef.current) {
        return false;
      }
      // Reopening the conversation already on screen (back to the list and into
      // it again on mobile, or its own row on desktop) keeps the transcript, the
      // widened history window and any live stream, and catches up quietly
      // instead of reopening it from scratch behind a loading placeholder.
      if (convId === conversationIdRef.current && conversationLoadRef.current === null) {
        if (activeStreamConversationIdRef.current !== convId) {
          void loadConversationMessages(convId, true).then(() => followTurnRef.current(convId));
        }
        return true;
      }
      // Cancel any active streaming before switching conversations
      cancelStream();

      conversationIsUnsentDraftRef.current = false;
      // The transcript on screen belongs to the conversation being left; it
      // must not stand in for this one while its history loads, or if it fails.
      if (convId !== conversationIdRef.current) {
        setMessages([]);
      }
      // Called outside a React event (a retried Home lookup), the render that
      // updates the mirror can come after the history response, which would
      // then look superseded.
      conversationIdRef.current = convId;
      setConversationId(convId);
      onConversationChangeRef.current(convId, 'open');
      void loadConversationMessages(convId).then((result) => {
        if (result === 'applied') {
          followTurnRef.current(convId);
        }
      });
      return true;
    },
    [cancelStream, loadConversationMessages]
  );

  // Cleanup effect to abort fetch requests on unmount
  useEffect(() => {
    return () => {
      if (messagesAbortControllerRef.current) {
        messagesAbortControllerRef.current.abort();
      }
    };
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

      // Before the self-turn and streaming skips below: a delegation handed off
      // or delivered changes the pending list even when history needs no reload.
      if (update.conversation_id === conversationId) {
        refreshPendingDelegations();
      }

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
    [conversationId, loadConversationMessages, isStreaming, refreshPendingDelegations]
  );

  // Set up live message updates via SSE
  useLiveMessageUpdates({
    conversationId,
    interfaceType: 'web',
    enabled: true,
    onMessageReceived: handleLiveMessageUpdate,
  });

  // Home and threads keep a read marker per viewer. The open one is marked
  // read through the newest stored message on screen, once its history has
  // loaded and while the chat (not the mobile list) is showing, scrolled down
  // to that message.
  const [viewportAtBottom, setViewportAtBottom] = useState(true);
  const latestStoredMessageId = useMemo(() => {
    let latest: number | null = null;
    for (const message of messages) {
      const id = message.internalId;
      if (typeof id === 'number' && Number.isFinite(id) && (latest === null || id > latest)) {
        latest = id;
      }
    }
    return latest;
  }, [messages]);
  const readableConversationId =
    conversationId &&
    (isHome || currentThread) &&
    conversationLoad?.convId !== conversationId &&
    viewportAtBottom &&
    !hidden
      ? conversationId
      : null;
  const handleMarkedRead = useCallback(
    (readConversationId: string, unreadCount: number) =>
      updateHomeThread(readConversationId, { unread_count: unreadCount }),
    [updateHomeThread]
  );
  useMarkRead({
    conversationId: readableConversationId,
    throughMessageId: latestStoredMessageId,
    localTurnEnd:
      localTurnEnded && localTurnEnded.convId === readableConversationId
        ? localTurnEnded.seq
        : null,
    onMarked: handleMarkedRead,
  });

  // A thread the user is marking done, or failed to.
  const [markDoneStatus, setMarkDoneStatus] = useState<{
    convId: string;
    status: 'pending' | 'failed';
  } | null>(null);
  const handleMarkThreadDone = useCallback(
    async (threadConversationId: string) => {
      setMarkDoneStatus({ convId: threadConversationId, status: 'pending' });
      try {
        const closed = await markThreadDone(threadConversationId);
        updateHomeThread(threadConversationId, closed);
        setOpenedThread((prev) =>
          prev?.convId === threadConversationId ? { convId: prev.convId, thread: closed } : prev
        );
        setMarkDoneStatus((prev) => (prev?.convId === threadConversationId ? null : prev));
        refreshHomeThreads();
      } catch (error) {
        console.error('Error marking thread done:', error);
        setMarkDoneStatus((prev) =>
          prev?.convId === threadConversationId
            ? { convId: threadConversationId, status: 'failed' }
            : prev
        );
      }
    },
    [updateHomeThread, refreshHomeThreads]
  );

  // Handle new chat creation
  const handleNewChat = useCallback(() => {
    if (composerSendingRef.current) {
      return false;
    }
    // Cancel any active streaming before creating a new chat
    cancelStream();
    // A history load still in flight belongs to the conversation being left.
    messagesAbortControllerRef.current?.abort();
    openInFlightConvIdRef.current = null;
    setIsLoading(false);
    setConversationLoad(null);

    // A new chat starts from the user's preferred profile, not whatever profile
    // an old conversation we were just viewing was adopted into.
    const preferredProfileId = localStorage.getItem('selectedProfileId') || profileId;
    setCurrentProfileId(preferredProfileId);

    const newConvId = `web_conv_${generateUUID()}`;
    conversationIsUnsentDraftRef.current = true;
    setConversationId(newConvId);
    setMessages([]);
    onConversationChangeRef.current(newConvId, 'new');
    return true;
  }, [cancelStream, profileId]);

  // On mobile, navigate back to conversation list
  const handleBackToList = useCallback(() => {
    cancelStream();
    onBackToList?.();
  }, [cancelStream, onBackToList]);

  // Set when a conversation switch is triggered by a profile change, where the
  // draft the user is composing must survive into the fresh conversation.
  const preserveComposerOnConversationSwitchRef = useRef(false);

  // Always-current mirror of "a turn is in flight", so callbacks can read it
  // without taking it as a dependency. Same value the runtime reports as
  // isRunning below.
  const turnIsRunningRef = useRef(false);
  turnIsRunningRef.current = isLoading || isStreaming;

  // The control reports a choice of the profile default as `null`. The choice
  // belongs to this conversation and profile; what the server holds for them
  // is kept, since leaving a chosen tier is a change that has to be sent.
  const handleModelTierChange = useCallback(
    (tierId: string | null) => {
      setModelTierState((prev) => ({
        conversationId,
        profileId: currentProfileId,
        namesTier: modelTierFor(prev, conversationId, currentProfileId)?.namesTier ?? false,
        selected: tierId,
      }));
    },
    [conversationId, currentProfileId]
  );

  // Handle profile changes
  const handleProfileChange = useCallback(
    (newProfileId: string) => {
      // The message being sent was written for this profile and conversation;
      // changing either before the runtime hands it over would send it under
      // the new one.
      // Read from a ref rather than the deps, so the callback keeps its identity
      // (see the note on ProfileSelector below).
      if (composerSendingRef.current || profileLockedRef.current) {
        return;
      }
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
        status?: { type: string; reason?: string };
      }>;
    }) => {
      // Taken first, so a send refused below can't leave it set for the next.
      const threadProfileId = sendAsThreadProfileRef.current;
      sendAsThreadProfileRef.current = null;
      // An attachment whose upload failed at send has no content to send. The
      // runtime puts a draft refused with MessageNotSentError back into the
      // composer, where the attachment renders its upload error.
      if (
        message.attachments?.some(
          (att) => att.status?.type === 'incomplete' && att.status.reason === 'error'
        )
      ) {
        throw new MessageNotSentError('An attachment failed to upload.');
      }

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

      if (threadProfileId) {
        await sendAsThreadRef.current(
          threadProfileId,
          message.content.map((part) => part.text).join('\n'),
          processedAttachments ?? []
        );
        return;
      }

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
      if (!conversationId) {
        setConversationId(targetConversationId);
        onConversationChangeRef.current(targetConversationId, 'send');
      }

      // Optimistically surface the conversation in the sidebar the instant the
      // user sends, so a brand-new chat appears immediately (and an existing one
      // bumps to the top) without waiting for the server round-trip. The
      // authoritative fetchConversations on stream completion — and the activity
      // stream — reconcile this row by conversation_id, so there's no duplicate.
      noteTurnStarted(turnId, targetConversationId, (message.content[0]?.text ?? '').slice(0, 100));

      streamingMessageIdRef.current = assistantMessageId;
      activeStreamConversationIdRef.current = targetConversationId;
      activeTurnIdRef.current = turnId;
      turnStoppedRef.current = false;
      lastStreamingErrorRef.current = null;
      lastStreamingErrorIsUserFacingRef.current = false;
      selfTurnIdsRef.current.add(turnId);

      // A chosen tier goes on every send, so the turn runs at what the control
      // shows whatever the server last recorded. The default is sent as nothing
      // until the conversation has been on a chosen tier; after that it is
      // named, which is how the server is told to leave one.
      const tierState = modelTierFor(modelTierStateRef.current, conversationId, currentProfileId);
      const selectedTier = tierState?.selected ?? null;
      const modelTier =
        selectedTier ??
        (tierState?.namesTier ? (defaultModelTierRef.current ?? undefined) : undefined);
      if (targetConversationId === homeConversationIdRef.current) {
        // A tier chosen in Home is for this turn only; the server doesn't carry
        // it forward, so the control goes back to the default.
        setModelTierState(null);
      } else if (tierState) {
        setModelTierState({
          ...tierState,
          conversationId: targetConversationId,
          namesTier: tierState.namesTier || selectedTier !== null,
        });
      }

      await sendStreamingMessage({
        prompt: message.content[0].text,
        conversationId: targetConversationId,
        profileId: currentProfileId,
        interfaceType: 'web',
        attachments: processedAttachments,
        turnId,
        modelTier,
      });
    },
    [conversationId, sendStreamingMessage, currentProfileId, noteTurnStarted]
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

  // A thread the user just started opens here with its first turn already
  // running. Its history (the opening message, and any Home turns it continued)
  // has loaded by now, so only the reply is left to stream: from where the turn
  // began, replacing whatever of it the history caught mid-way.
  followTurnRef.current = (convId: string) => {
    if (conversationIdRef.current !== convId || activeStreamConversationIdRef.current !== null) {
      return;
    }
    const turn = takeTurnToFollow(convId);
    if (!turn) {
      return;
    }
    const assistantMessageId = `msg_${Date.now()}_assistant`;
    setMessages((prev) => [
      ...prev.filter((msg) => !(msg.turnId === turn.turnId && msg.role === 'assistant')),
      {
        id: assistantMessageId,
        role: 'assistant',
        turnId: turn.turnId,
        content: [{ type: 'text', text: LOADING_MARKER }],
        isLoading: true,
        createdAt: new Date(),
      },
    ]);
    streamingMessageIdRef.current = assistantMessageId;
    activeStreamConversationIdRef.current = convId;
    activeTurnIdRef.current = turn.turnId;
    turnStoppedRef.current = false;
    lastStreamingErrorRef.current = null;
    lastStreamingErrorIsUserFacingRef.current = false;
    selfTurnIdsRef.current.add(turn.turnId);
    void sendStreamingMessage({
      prompt: '',
      conversationId: convId,
      profileId: currentProfileId,
      interfaceType: 'web',
      turnId: turn.turnId,
      existingTurn: {
        firstSeq: turn.firstSeq,
        alreadyComplete: turn.alreadyComplete,
        incomplete: turn.incomplete,
      },
    });
  };

  // Send-as-thread: the composed message opens a new thread on the chosen
  // profile instead of becoming a Home turn. A tier chosen here goes with it
  // where that profile offers the tier. A retry of the same message reuses the
  // start's id, so a start whose answer was lost isn't made twice.
  const [sendAsThreadError, setSendAsThreadError] = useState<string | null>(null);
  const pendingThreadStartRef = useRef<{ key: string; turnId: string } | null>(null);
  sendAsThreadRef.current = async (
    threadProfileId: string,
    text: string,
    attachments: ThreadAttachment[]
  ) => {
    const tierState = modelTierFor(modelTierStateRef.current, conversationId, currentProfileId);
    const chosenTier = tierState?.selected ?? null;
    const offersTier =
      chosenTier !== null &&
      (profilesById[threadProfileId]?.model_tiers ?? []).some((tier) => tier.id === chosenTier);
    const modelTier = offersTier && chosenTier ? chosenTier : null;
    const key = JSON.stringify([
      threadProfileId,
      text,
      attachments.map((attachment) => attachment.id),
      modelTier,
    ]);
    const pending = pendingThreadStartRef.current;
    const turnId = pending?.key === key ? pending.turnId : generateUUID();
    pendingThreadStartRef.current = { key, turnId };
    setSendAsThreadError(null);
    try {
      await startThread({
        profile_id: threadProfileId,
        prompt: text,
        turn_id: turnId,
        ...(attachments.length > 0 ? { attachments } : {}),
        ...(modelTier ? { model_tier: modelTier } : {}),
      });
    } catch (error) {
      const reason =
        error instanceof Error ? error.message : "Couldn't start the thread. Try again.";
      setSendAsThreadError(reason);
      throw new MessageNotSentError(reason);
    }
    pendingThreadStartRef.current = null;
    // A tier chosen in Home is for one send, which this was.
    setModelTierState(null);
  };
  useEffect(() => {
    setSendAsThreadError(null);
    pendingThreadStartRef.current = null;
  }, [conversationId]);

  const sendAsThreadProfiles = useMemo(
    () =>
      threadProfiles(Object.values(profilesById)).map((profile) => ({
        id: profile.id,
        label: getProfileDisplayName(profile.id),
      })),
    [profilesById]
  );

  // Home's messages as text, by stored id, for the "Started from" quote a
  // thread continued from one of them shows.
  const messagesRef = useRef(messages);
  messagesRef.current = messages;
  const homeQuoteKey = isHome
    ? messages
        .filter((msg) => msg.internalId !== undefined && !msg.isLoading)
        .map((msg) => `${msg.internalId}:${msg.content.length}`)
        .join(',')
    : null;
  useEffect(() => {
    if (homeQuoteKey === null) {
      return;
    }
    const quotes = new Map<number, string>();
    for (const msg of messagesRef.current) {
      const text = messageText(msg);
      if (msg.internalId !== undefined && !msg.moved_to_thread && text) {
        quotes.set(msg.internalId, text);
      }
    }
    reportHomeQuotes(quotes);
  }, [homeQuoteKey, reportHomeQuotes]);

  // Home's actions on its messages, and the dialog each opens.
  const [continueTarget, setContinueTarget] = useState<HomeTurnTarget | null>(null);
  const [moveTarget, setMoveTarget] = useState<HomeTurnTarget | null>(null);
  const userTurnIdsKey = messages
    .filter((msg) => msg.role === 'user' && msg.turnId)
    .map((msg) => msg.turnId)
    .join(',');
  const homeTurnActions = useMemo<HomeTurnActions | null>(() => {
    if (!isHome) {
      return null;
    }
    const userTurnIds = new Set(userTurnIdsKey.split(','));
    return {
      continueInThread: setContinueTarget,
      moveToThread: setMoveTarget,
      turnHasUserMessage: (turnId) => turnId !== undefined && userTurnIds.has(turnId),
    };
  }, [isHome, userTurnIdsKey]);
  // A reply streamed here has no stored id yet; the server names it by turn.
  const resolveHomeMessageId = useCallback(async (target: HomeTurnTarget): Promise<number> => {
    if (target.internalId !== undefined) {
      return target.internalId;
    }
    const convId = conversationIdRef.current;
    if (target.turnId && convId) {
      const response = await fetch(
        `/api/v1/chat/conversations/${encodeURIComponent(convId)}/messages?limit=${HISTORY_PAGE_SIZE}`
      );
      if (response.ok) {
        const data = (await response.json()) as ConversationMessagesResponse;
        // The turn's reply, which is what the user picked; any row of the turn
        // names it to the server, but the anchor is quoted.
        const turnRows = data.messages.filter((msg) => msg.turn_id === target.turnId);
        const row =
          [...turnRows]
            .reverse()
            .find((msg) => msg.role === 'assistant' && !msg.tool_calls?.length && msg.content) ??
          turnRows[0];
        if (row) {
          return Number(row.internal_id);
        }
      }
    }
    throw new Error("This message isn't saved yet. Try again in a moment.");
  }, []);
  const continueAnchor = useMemo<ContinueAnchor | null>(
    () =>
      continueTarget
        ? {
            quote: continueTarget.quote,
            resolveMessageId: () => resolveHomeMessageId(continueTarget),
          }
        : null,
    [continueTarget, resolveHomeMessageId]
  );
  const handleTurnMoved = useCallback(() => {
    const convId = conversationIdRef.current;
    if (convId) {
      void loadConversationMessages(convId, true);
    }
  }, [loadConversationMessages]);

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
          echoed_from_thread: message.echoed_from_thread,
          moved_to_thread: message.moved_to_thread,
          internal_id: message.internalId,
          turn_id: message.turnId,
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

  const retryConversationLoad = useCallback(() => {
    const convId = conversationIdRef.current;
    if (convId) {
      void loadConversationMessages(convId).then((result) => {
        if (result === 'applied') {
          followTurnRef.current(convId);
        }
      });
    }
  }, [loadConversationMessages]);

  const conversationLoadStatus =
    conversationLoad && conversationLoad.convId === conversationId ? conversationLoad.status : null;

  const runtime = useExternalStoreRuntime({
    messages,
    // Opening a conversation is not a turn: the composer stays a composer (not a
    // steer input) and is held back by isSendDisabled until the history lands.
    isRunning: isStreaming,
    isSendDisabled: conversationLoadStatus !== null,
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

  const sendDisabledRef = useRef(false);
  sendDisabledRef.current = conversationLoadStatus !== null;
  useEffect(() => {
    const composer = runtime.thread.composer;
    // While sending, the composer has emptied its text but still holds the
    // attachments it is uploading, so it is non-empty yet unable to send for a
    // reason other than the open conversation's history still loading.
    return composer.subscribe(() => {
      const state = composer.getState();
      composerSendingRef.current = !state.canSend && !state.isEmpty && !sendDisabledRef.current;
    });
  }, [runtime]);

  // The composer and its runtime are shared by every conversation, so on a real
  // conversation switch its contents are set aside with the conversation they
  // were written in, and whatever was set aside for the destination comes back.
  // Text and attachments move together: neither is lost on leaving, and neither
  // is carried into a conversation it was not written for. That includes steer
  // text, which stays with the conversation whose turn it was aimed at. Guarded
  // on an actual id change so an unrelated runtime re-render never touches text
  // the user is typing. A switch caused by a profile change is exempt: the user
  // keeps composing the same draft, just under a different profile.
  const prevConversationIdRef = useRef(conversationId);
  // A restored draft's attachments go back into the composer one at a time; a
  // switch before they are all back must set aside the whole draft, not the
  // part of it the composer holds so far.
  const draftRestoreRef = useRef<{ convId: string; draft: ConversationDraft } | null>(null);
  useEffect(() => {
    const outgoingId = prevConversationIdRef.current;
    if (outgoingId === conversationId) {
      return;
    }
    prevConversationIdRef.current = conversationId;
    // Nothing was open yet: whatever the user typed while the landing view was
    // still being decided was written for the conversation that opens. A pane
    // that opens with an empty composer takes up the draft set aside for it.
    if (outgoingId === null) {
      const { text, attachments } = runtime.thread.composer.getState();
      if (text || attachments.length > 0) {
        return;
      }
    }
    if (preserveComposerOnConversationSwitchRef.current) {
      preserveComposerOnConversationSwitchRef.current = false;
      return;
    }
    const composer = runtime.thread.composer;
    const { text } = composer.getState();
    const unfinishedRestore = draftRestoreRef.current;
    const attachments =
      unfinishedRestore?.convId === outgoingId
        ? unfinishedRestore.draft.attachments
        : composer.getState().attachments;
    if (outgoingId) {
      if (text || attachments.length > 0) {
        drafts.set(outgoingId, { text, attachments });
      } else {
        drafts.delete(outgoingId);
      }
    }
    const incoming = conversationId ? drafts.get(conversationId) : undefined;
    if (conversationId) {
      drafts.delete(conversationId);
    }
    const restore =
      conversationId && incoming && incoming.attachments.length > 0
        ? { convId: conversationId, draft: incoming }
        : null;
    draftRestoreRef.current = restore;
    composer.setText(incoming?.text ?? '');
    void composer.clearAttachments().then(async () => {
      for (const attachment of restore?.draft.attachments ?? []) {
        // A later switch owns the composer now; what is left of this draft
        // would land in the wrong conversation.
        if (draftRestoreRef.current !== restore) {
          return;
        }
        await composer.addAttachment(restorableAttachment(attachment));
      }
      if (restore && draftRestoreRef.current === restore) {
        draftRestoreRef.current = null;
      }
    });
  }, [conversationId, runtime, drafts]);

  // A pane that closes sets its draft aside for its conversation, and stops
  // following the turn it was streaming, which carries on server-side.
  const runtimeRef = useRef(runtime);
  runtimeRef.current = runtime;
  const cancelStreamRef = useRef(cancelStream);
  cancelStreamRef.current = cancelStream;
  useEffect(
    () => () => {
      const convId = conversationIdRef.current;
      // Mid-send, what the composer still holds belongs to the message going
      // out, not to a draft.
      if (convId && !composerSendingRef.current) {
        const { text, attachments } = runtimeRef.current.thread.composer.getState();
        const unfinishedRestore = draftRestoreRef.current;
        const draftAttachments =
          unfinishedRestore?.convId === convId ? unfinishedRestore.draft.attachments : attachments;
        if (text || draftAttachments.length > 0) {
          drafts.set(convId, { text, attachments: draftAttachments });
        }
      }
      cancelStreamRef.current();
    },
    [drafts]
  );

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
      hasOlderMessages: olderHistory?.convId === conversationId && olderHistory.hasMore,
      olderMessagesStatus,
      loadOlderMessages: () => void loadOlderMessages(),
      pendingDelegations,
      conversationLoadStatus,
      retryConversationLoad,
      sendAsThread: isHome
        ? {
            profiles: sendAsThreadProfiles,
            prepare: (threadProfileId: string) => {
              sendAsThreadProfileRef.current = threadProfileId;
            },
            error: sendAsThreadError,
          }
        : null,
    }),
    [
      conversationLoadStatus,
      retryConversationLoad,
      isHome,
      sendAsThreadProfiles,
      sendAsThreadError,
      steerError,
      submitSteer,
      olderHistory,
      conversationId,
      olderMessagesStatus,
      loadOlderMessages,
      pendingDelegations,
    ]
  );

  const threadsContextValue = useMemo(() => {
    if (!homeConversationId) {
      return null;
    }
    const bySourceToolCallId = new Map<string, ThreadSummary>();
    const byConversationId = new Map<string, ThreadSummary>();
    for (const thread of homeThreads) {
      byConversationId.set(thread.conversation_id, thread);
      if (thread.source_tool_call_id) {
        bySourceToolCallId.set(thread.source_tool_call_id, thread);
      }
    }
    return {
      threadsBySourceToolCallId: bySourceToolCallId,
      threadsByConversationId: byConversationId,
      openConversation,
      setViewportAtBottom,
    };
  }, [homeConversationId, homeThreads, openConversation, setViewportAtBottom]);

  const conversationTitle = isHome ? 'Home' : (currentThread?.title ?? null);

  const anchorId = currentThread?.anchor_message_id;
  const threadBanner = currentThread && (
    <ThreadBanner
      thread={currentThread}
      anchorQuote={typeof anchorId === 'number' ? (homeQuotes.get(anchorId) ?? null) : null}
      onOpenHome={openConversation}
      compact={presentation === 'side'}
      onMarkDone={(threadConversationId) => void handleMarkThreadDone(threadConversationId)}
      markDoneStatus={
        markDoneStatus?.convId === currentThread.conversation_id ? markDoneStatus.status : null
      }
    />
  );

  const threadView = (
    <AssistantRuntimeProvider runtime={runtime}>
      <ThreadErrorBoundary>
        <ChatControlsContext.Provider value={chatControls}>
          <ToolConfirmationProvider value={{ pendingConfirmations, handleConfirmation }}>
            <ThreadsContext.Provider value={threadsContextValue}>
              <HomeTurnActionsContext.Provider value={homeTurnActions}>
                <Thread />
              </HomeTurnActionsContext.Provider>
            </ThreadsContext.Provider>
          </ToolConfirmationProvider>
        </ChatControlsContext.Provider>
      </ThreadErrorBoundary>
    </AssistantRuntimeProvider>
  );

  // Keyed on the ids themselves, so streamed text doesn't re-report an
  // unchanged set to the shell on every token.
  const toolCallIdsKey = useMemo(
    () =>
      messages
        .flatMap((message) =>
          message.content
            .filter((part) => part.type === 'tool-call' && part.toolCallId)
            .map((part) => part.toolCallId as string)
        )
        .join('\u0000'),
    [messages]
  );
  const toolCallIds = useMemo(
    () => new Set(toolCallIdsKey ? toolCallIdsKey.split('\u0000') : []),
    [toolCallIdsKey]
  );
  useEffect(() => {
    onReport({ conversationId, toolCallIds, isStreaming });
  }, [onReport, conversationId, toolCallIds, isStreaming]);

  useImperativeHandle(
    ref,
    () => ({
      openConversation: handleConversationSelect,
      newChat: handleNewChat,
      send: (text: string) => void handleNew({ content: [{ text }] }),
    }),
    [handleConversationSelect, handleNewChat, handleNew]
  );

  if (hidden) {
    return null;
  }

  const shareButton = (
    <ShareConversationButton
      shareLink={shareLink}
      conversationId={conversationId}
      hasPersistedMessages={messages.length > 0 && persistedConversationId === conversationId}
    />
  );
  const profileSelector = (
    <ProfileSelector
      selectedProfileId={currentProfileId}
      onProfileChange={handleProfileChange}
      disabled={isLoading || lockedProfileId !== null}
    />
  );
  const intelligenceSelector = (
    <IntelligenceSelector
      tiers={modelTiers}
      defaultTierId={defaultModelTier}
      selectedTierId={selectedModelTierId}
      onChange={handleModelTierChange}
      disabled={isLoading}
    />
  );
  const body = (
    <div className="flex min-w-0 min-h-0 flex-1 flex-col">
      {topSlot}
      {threadBanner}
      <div className="flex flex-1 flex-col min-h-0">{threadView}</div>
      {homeTurnActions && (
        <>
          <StartThreadDialog
            open={continueTarget !== null}
            onOpenChange={(open) => !open && setContinueTarget(null)}
            anchor={continueAnchor}
          />
          <MoveToThreadDialog
            open={moveTarget !== null}
            onOpenChange={(open) => !open && setMoveTarget(null)}
            quote={moveTarget?.quote ?? ''}
            resolveMessageId={() =>
              moveTarget ? resolveHomeMessageId(moveTarget) : Promise.reject(new Error('No turn'))
            }
            onMoved={handleTurnMoved}
          />
        </>
      )}
    </div>
  );

  if (isMobile) {
    return (
      <div
        className="flex flex-1 flex-col min-h-0 overflow-hidden"
        data-testid="conversation-pane"
        data-presentation={presentation}
        data-pane-conversation-id={conversationId ?? undefined}
      >
        {/* The shared top bar carries navigation and the conversation
            actions; the profile/intelligence selectors sit on a second
            row that wraps, so nothing is pushed past the viewport at
            narrow widths or zoom. */}
        <div
          className="flex-shrink-0 z-40 flex flex-col border-b bg-background/95 backdrop-blur supports-[backdrop-filter]:bg-background/60"
          data-testid="mobile-chat-header"
        >
          <AppTopBar
            title={conversationTitle ?? 'Chat'}
            className="static border-b-0 bg-transparent backdrop-blur-none"
            actions={
              <>
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={handleBackToList}
                  aria-label="Back to conversations"
                >
                  <MessagesSquare className="h-4 w-4" />
                </Button>
                {shareButton}
                {headerEnd}
              </>
            }
          />

          <div className="flex min-w-0 flex-wrap items-center gap-2 px-3 pb-2">
            {profileSelector}
            {intelligenceSelector}
          </div>
        </div>
        {body}
      </div>
    );
  }

  if (presentation === 'side') {
    return (
      <section
        className="flex min-h-0 w-[38%] min-w-[22rem] max-w-[40rem] flex-shrink-0 flex-col border-l"
        aria-label={conversationTitle ?? 'Thread'}
        data-testid="conversation-pane"
        data-presentation={presentation}
        data-pane-conversation-id={conversationId ?? undefined}
      >
        <div className="flex flex-shrink-0 flex-col gap-1 border-b bg-background/95 px-4 py-2">
          <div className="flex min-h-10 min-w-0 items-center gap-2">
            {currentThread && <ThreadStateDot state={currentThread.state} />}
            {conversationTitle && (
              <h2
                className="min-w-0 flex-1 truncate text-base font-semibold"
                data-testid="conversation-title"
              >
                {conversationTitle}
              </h2>
            )}
            <div className="ml-auto flex shrink-0 items-center gap-1">
              {shareButton}
              {headerEnd}
            </div>
          </div>
          <div className="flex min-w-0 flex-wrap items-center gap-2">
            {profileSelector}
            {intelligenceSelector}
          </div>
        </div>
        {body}
      </section>
    );
  }

  return (
    <div
      className="flex min-w-0 min-h-0 flex-1 flex-col"
      data-testid="conversation-pane"
      data-presentation={presentation}
      data-pane-conversation-id={conversationId ?? undefined}
    >
      <div className="sticky top-0 z-40 flex min-h-14 flex-wrap items-center gap-x-3 gap-y-2 border-b bg-background/95 px-4 py-2 backdrop-blur supports-[backdrop-filter]:bg-background/60">
        {headerStart}

        {conversationTitle && (
          <h1
            className="min-w-0 max-w-xs truncate text-base font-semibold"
            data-testid="conversation-title"
          >
            {conversationTitle}
          </h1>
        )}

        <div className="flex items-center gap-2">
          {profileSelector}
          {intelligenceSelector}
        </div>

        <div className="flex items-center gap-2 ml-auto">
          {shareButton}
          {headerEnd}
        </div>
      </div>
      {body}
    </div>
  );
};
