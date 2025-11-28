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

export interface Message {
  id: string;
  role: 'user' | 'assistant' | 'system';
  content: MessageContent[];
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
  role: 'user' | 'assistant' | 'system' | 'tool';
  timestamp: string;
  content?: string | BackendContentPart[];
  attachments?: BackendAttachment[];
  metadata?: BackendMessageMetadata;
  tool_calls?: BackendToolCall[];
  tool_call_id?: string;
}

export interface ConversationMessagesResponse {
  messages: BackendConversationMessage[];
}
