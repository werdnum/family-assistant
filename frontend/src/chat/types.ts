import type { TerminalToolOutcome } from './toolOutcome';

export interface Conversation {
  conversation_id: string;
  last_message: string;
  /** Generated one-line summary; absent until the conversation is summarized. */
  summary?: string | null;
  last_timestamp: string;
  message_count: number;
  /** Present only on search results: a snippet of a message that matched. */
  match_excerpt?: string | null;
  /** The caller's Home, a thread of it, or an ordinary conversation. */
  kind?: ConversationKind;
}

export type ConversationKind = 'home' | 'thread' | 'conversation';

/** Where a thread stands: running, waiting on the user, done, or failed. */
export type ThreadState = 'working' | 'waiting' | 'done' | 'failed';

/** One thread of a Home, as its card and the sidebar show it. */
export interface ThreadSummary {
  conversation_id: string;
  home_conversation_id: string;
  title: string;
  profile_id: string;
  state: ThreadState;
  /** Who closed the thread, while that close is what `state` shows. */
  closed_by?: 'assistant' | 'user' | null;
  closed_at?: string | null;
  /** The Home message echoing the outcome of the assistant's close. */
  echo_message_id?: number | null;
  /** Messages in the thread the caller has not read. */
  unread_count?: number;
  /** The Home tool call that started the thread, if any. */
  source_tool_call_id?: string | null;
  /** The Home message the user continued the thread from, for its "Started from" quote. */
  anchor_message_id?: number | null;
  /** The request the thread was started with. */
  seed_request?: string | null;
  /** The thread's latest visible message. */
  preview?: string | null;
  created_at: string;
  updated_at: string;
}

/** Home and its threads, as the sidebar groups them. */
export interface HomeThreadsView {
  homeConversationId: string;
  threads: ThreadSummary[];
  /** The thread list could not be fetched; `threads` may be stale or missing. */
  threadsFailed: boolean;
  onRetryThreads: () => void;
  /** Open the dialog for starting a thread on a chosen profile. */
  onNewThread: () => void;
}

export interface ConversationSidebarProps {
  conversations?: Conversation[];
  conversationsLoading?: boolean;
  currentConversationId?: string | null;
  onConversationSelect: (conversationId: string) => void;
  onNewChat: () => void;
  isOpen: boolean;
  onRefresh: () => void;
  isMobile?: boolean;
  /** Present when threads are enabled: Home is pinned and threads grouped by state. */
  home?: HomeThreadsView | null;
}

export interface ChatAppProps {
  profileId?: string;
}

/**
 * What is known about the LLM call that produced a message: the serving model
 * and, where the profile offers a choice of them, the model tier it ran at.
 * `model_tier_source` says who chose that tier — the user, the Auto router
 * (`model`), or the profile default.
 */
export interface MessageReasoningInfo {
  model?: string | null;
  model_tier?: string | null;
  model_tier_source?: 'user' | 'model' | 'default' | null;
  model_tier_requested?: string | null;
}

export interface Message {
  id: string;
  role: 'user' | 'assistant' | 'system';
  content: MessageContent[];
  // The turn that produced this message (when known from persisted history).
  // Used to match a reconciled reply to a specific turn.
  turnId?: string;
  createdAt: Date;
  isLoading?: boolean;
  status?: {
    type: 'running' | 'complete';
  };
  attachments?: Array<{
    id: string;
    type: 'image' | 'document' | 'file';
    name: string;
    content?: string;
    file?: File;
  }>;
  processing_profile_id?: string;
  reasoning_info?: MessageReasoningInfo;
  /** The stored row this message was built from; absent until it is stored. */
  internalId?: number;
  /** On a Home message echoing a thread's outcome: that thread's conversation. */
  echoed_from_thread?: string;
  /** On the Home marker left by a turn moved into a thread: that thread's conversation. */
  moved_to_thread?: string;
}

export interface MessageContent {
  type: 'text' | 'tool-call';
  text?: string;
  toolCallId?: string;
  toolName?: string;
  args?: Record<string, unknown>;
  argsText?: string;
  result?: string | Record<string, unknown>;
  outcome?: TerminalToolOutcome;
  // A history row from a turn the server still reports as running.
  awaitingResult?: boolean;
  attachments?: Array<Record<string, unknown>>;
  artifact?: {
    attachments?: Array<Record<string, unknown>>;
  };
  // How the server describes the call to the person watching.
  display?: ToolCallDisplay;
  // A streamed call: how much of the turn's text had arrived when it started.
  textOffset?: number;
}

/** The server's description of a tool call (`display` on every tool call it sends). */
export interface ToolCallDisplay {
  active: string;
  done: string;
  changes: boolean;
}

export interface BackendAttachment extends Record<string, unknown> {
  attachment_id?: string;
  name?: string;
  type?: string;
  content_url?: string;
}

export interface BackendToolCallFunction extends Record<string, unknown> {
  name?: string;
  arguments?: string | Record<string, unknown>;
}

export interface BackendToolCall extends Record<string, unknown> {
  id: string;
  type?: string;
  name?: string;
  arguments?: string | Record<string, unknown>;
  function?: BackendToolCallFunction;
  attachments?: BackendAttachment[];
  display?: ToolCallDisplay;
}

export type BackendContentPart =
  | { type: 'text'; text: string }
  | { type: 'image_url'; image_url?: { url?: string; [key: string]: unknown } }
  | { type: string; [key: string]: unknown };

export interface BackendMessageMetadata extends Record<string, unknown> {
  attachments?: BackendAttachment[];
}

export interface BackendConversationMessage extends Record<string, unknown> {
  internal_id: string;
  role: 'user' | 'assistant' | 'system' | 'tool' | 'error';
  turn_id?: string | null;
  timestamp: string;
  content?: string | BackendContentPart[];
  attachments?: BackendAttachment[];
  metadata?: BackendMessageMetadata;
  tool_calls?: BackendToolCall[];
  tool_call_id?: string;
  // For tool rows: how the call ended, classified by the backend.
  tool_outcome?: TerminalToolOutcome | null;
  processing_profile_id?: string | null;
  reasoning_info?: MessageReasoningInfo | null;
  /** On a Home message echoing a thread's outcome: that thread's conversation. */
  echoed_from_thread?: string | null;
  /** On the Home marker left by a turn moved into a thread: that thread's conversation. */
  moved_to_thread?: string | null;
}

export interface ActiveTurnInfo {
  turn_id: string;
  started_at?: string;
  latest_seq?: number;
  // 'running' | 'complete' | 'failed'
  status: string;
}

export interface ConversationMessagesResponse {
  messages: BackendConversationMessage[];
  // Profile of the most recent user message across the whole conversation,
  // independent of the returned message page. Present only when the request
  // sets include_conversation_profile=true; used to adopt the conversation's
  // profile on open.
  latest_user_profile_id?: string | null;
  conversation_model_tier?: string | null;
  // Present alongside latest_user_profile_id.
  conversation_kind?: ConversationKind | null;
  thread?: ThreadSummary | null;
  // Whether the conversation has messages older than the returned page.
  has_more_before?: boolean;
  // Recently retained turn states for this conversation, when the backend has them.
  active_turns?: ActiveTurnInfo[];
}
