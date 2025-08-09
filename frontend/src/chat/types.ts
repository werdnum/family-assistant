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
}

export interface MessageContent {
  type: 'text' | 'tool-call';
  text?: string;
  toolCallId?: string;
  toolName?: string;
  args?: Record<string, unknown>;
  argsText?: string;
  result?: string | Record<string, unknown>;
}
