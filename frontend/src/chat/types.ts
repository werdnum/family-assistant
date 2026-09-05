export interface Conversation {
  conversation_id: string;
  last_message: string;
  last_timestamp: string;
  message_count: number;
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
}

export interface MessageContent {
  type: 'text' | 'tool-call';
  text?: string;
  toolCallId?: string;
  toolName?: string;
  args?: Record<string, unknown>;
  argsText?: string;
  result?: string | Record<string, unknown>;
  attachments?: Array<Record<string, unknown>>;
  artifact?: {
    attachments?: Array<Record<string, unknown>>;
  };
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
  processing_profile_id?: string | null;
  reasoning_info?: MessageReasoningInfo | null;
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
  // Recently retained turn states for this conversation, when the backend has them.
  active_turns?: ActiveTurnInfo[];
}
