import React, { useState } from 'react';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Card } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { ScrollArea } from '@/components/ui/scroll-area';
import { Separator } from '@/components/ui/separator';
import { ConversationSidebarProps, Conversation } from './types';

const ConversationSidebar: React.FC<ConversationSidebarProps> = ({
  conversations = [],
  conversationsLoading = false,
  currentConversationId,
  onConversationSelect,
  onNewChat,
  isOpen,
  onRefresh: _onRefresh,
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
    <div className="flex h-full flex-col">
      <div className="flex flex-col gap-3 p-6 pb-4">
        <h2 className="text-lg font-semibold">Conversations</h2>
        <Button
          onClick={onNewChat}
          aria-label="Start new chat"
          data-testid="new-chat-button"
          className="w-full"
        >
          <span className="mr-2 text-lg">+</span> New Chat
        </Button>
      </div>

      <div className="px-6 pb-4">
        <Input
          type="text"
          placeholder="Search conversations..."
          value={searchQuery}
          onChange={(e) => setSearchQuery(e.target.value)}
          className="w-full"
        />
      </div>

      <Separator />

      <ScrollArea className="flex-1 px-6">
        <div className="py-4">
          {conversationsLoading ? (
            <div className="py-8 text-center text-sm text-muted-foreground italic">
              Loading conversations...
            </div>
          ) : filteredConversations.length === 0 ? (
            <div className="py-8 text-center text-sm text-muted-foreground italic">
              {searchQuery ? 'No conversations match your search' : 'No conversations yet'}
            </div>
          ) : (
            <div className="space-y-2">
              {filteredConversations.map((conv: Conversation) => (
                <Card
                  key={conv.conversation_id}
                  className={`cursor-pointer transition-all duration-200 ${
                    conv.conversation_id === currentConversationId
                      ? 'bg-primary text-primary-foreground border-primary'
                      : 'hover:bg-accent hover:border-accent-foreground/20'
                  }`}
                  onClick={() => onConversationSelect(conv.conversation_id)}
                  data-testid={`conversation-item-${conv.conversation_id}`}
                  data-conversation-id={conv.conversation_id}
                >
                  <div className="p-4">
                    <div className="text-sm leading-relaxed mb-2 line-clamp-2 overflow-hidden">
                      {conv.last_message}
                    </div>
                    <div className="flex justify-between items-center">
                      <span className="text-xs opacity-90 whitespace-nowrap">
                        {formatTimestamp(conv.last_timestamp)}
                      </span>
                      <Badge variant="secondary" className="text-xs whitespace-nowrap">
                        {conv.message_count} messages
                      </Badge>
                    </div>
                  </div>
                </Card>
              ))}
            </div>
          )}
        </div>
      </ScrollArea>
    </div>
  );

  // Always render the desktop version - mobile rendering is handled by ChatApp
  return (
    <div
      className={`h-full w-80 flex-shrink-0 border-r bg-background transition-all duration-300 ${
        isOpen ? 'ml-0' : '-ml-80'
      }`}
    >
      <SidebarContent />
    </div>
  );
};

export default ConversationSidebar;
