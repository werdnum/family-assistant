import { HomeIcon, MessageSquarePlusIcon, SearchIcon } from 'lucide-react';
import React, { useState } from 'react';
import { Link } from 'react-router-dom';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { ScrollArea } from '@/components/ui/scroll-area';
import { Conversation, ConversationSidebarProps } from './types';

const ConversationSidebar: React.FC<ConversationSidebarProps> = ({
  conversations = [],
  conversationsLoading = false,
  currentConversationId,
  onConversationSelect,
  onNewChat,
  isOpen,
  onRefresh: _onRefresh,
  isMobile = false,
}) => {
  const [searchQuery, setSearchQuery] = useState('');

  // Filter conversations based on search query
  const filteredConversations = conversations.filter((conv) =>
    conv.last_message.toLowerCase().includes(searchQuery.toLowerCase())
  );

  // Format timestamp for display
  const formatTimestamp = (timestamp: string): string => {
    const date = new Date(timestamp);
    const now = new Date();
    const diffMs = now.getTime() - date.getTime();
    const diffMins = Math.floor(diffMs / 60000);
    const diffHours = Math.floor(diffMs / 3600000);
    const diffDays = Math.floor(diffMs / 86400000);

    if (diffMins < 1) {
      return 'Just now';
    }
    if (diffMins < 60) {
      return `${diffMins}m ago`;
    }
    if (diffHours < 24) {
      return `${diffHours}h ago`;
    }
    if (diffDays < 7) {
      return `${diffDays}d ago`;
    }
    return date.toLocaleDateString();
  };

  const SidebarContent = () => (
    <div className="flex h-full flex-col min-h-0">
      <div className="flex items-center gap-2 p-4 pb-3">
        <Button
          asChild
          variant="ghost"
          size="sm"
          className="h-8 w-8 p-0 rounded-lg hover:bg-primary/10 hover:text-primary"
        >
          <Link to="/" aria-label="Home">
            <HomeIcon size={18} />
          </Link>
        </Button>
        <h2 className="text-sm font-semibold text-muted-foreground uppercase tracking-wider flex-1">
          Conversations
        </h2>
        <Button
          onClick={onNewChat}
          aria-label="Start new chat"
          data-testid="new-chat-button"
          variant="ghost"
          size="sm"
          className="h-8 w-8 p-0 rounded-lg hover:bg-primary/10 hover:text-primary"
        >
          <MessageSquarePlusIcon size={18} />
        </Button>
      </div>

      <div className="px-4 pb-3">
        <div className="relative">
          <SearchIcon
            size={14}
            className="absolute left-3 top-1/2 -translate-y-1/2 text-muted-foreground/50"
          />
          <input
            type="text"
            placeholder="Search..."
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            className="w-full h-8 pl-8 pr-3 text-sm rounded-lg border border-border/60 bg-muted/30 placeholder:text-muted-foreground/40 focus:outline-none focus:ring-2 focus:ring-primary/20 focus:border-primary/40 transition-all"
          />
        </div>
      </div>

      <ScrollArea className="flex-1 px-2 overflow-y-auto">
        <div className="py-1">
          {conversationsLoading ? (
            <div
              className="py-8 text-center text-sm text-muted-foreground/60"
              data-loading-indicator="true"
            >
              Loading...
            </div>
          ) : filteredConversations.length === 0 ? (
            <div className="py-8 text-center text-sm text-muted-foreground/60">
              {searchQuery ? 'No matches' : 'No conversations yet'}
            </div>
          ) : (
            <div className="space-y-0.5">
              {filteredConversations.map((conv: Conversation) => {
                const isActive = conv.conversation_id === currentConversationId;
                return (
                  <button
                    key={conv.conversation_id}
                    className={`w-full text-left px-3 py-2.5 rounded-lg transition-all duration-150 ${
                      isActive ? 'bg-primary/10 text-primary' : 'hover:bg-muted/60 text-foreground'
                    }`}
                    onClick={() => onConversationSelect(conv.conversation_id)}
                    data-testid={`conversation-item-${conv.conversation_id}`}
                    data-conversation-id={conv.conversation_id}
                  >
                    <div className="text-sm leading-snug line-clamp-2 mb-1">
                      {conv.last_message}
                    </div>
                    <div className="flex justify-between items-center">
                      <span className="text-[11px] text-muted-foreground/70">
                        {formatTimestamp(conv.last_timestamp)}
                      </span>
                      {conv.message_count > 1 && (
                        <Badge
                          variant="secondary"
                          className="text-[10px] px-1.5 py-0 h-4 font-normal"
                        >
                          {conv.message_count}
                        </Badge>
                      )}
                    </div>
                  </button>
                );
              })}
            </div>
          )}
        </div>
      </ScrollArea>
    </div>
  );

  // On mobile, render as a full-page view (parent controls visibility)
  if (isMobile) {
    return (
      <div className="h-full w-full bg-background">
        <SidebarContent />
      </div>
    );
  }

  // Desktop: collapsible sidebar panel
  return (
    <div
      className={`h-full w-72 flex-shrink-0 border-r border-border/50 bg-muted/30 transition-all duration-300 ${
        isOpen ? 'ml-0' : '-ml-72'
      }`}
    >
      <SidebarContent />
    </div>
  );
};

export default ConversationSidebar;
