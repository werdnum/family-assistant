import React, { useState } from 'react';
import { Button } from '@/components/ui/button';
import './ConversationSidebar.css';

const ConversationSidebar = ({
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
  const formatTimestamp = (timestamp) => {
    const date = new Date(timestamp);
    const now = new Date();
    const diffMs = now - date;
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

  return (
    <div className={`conversation-sidebar ${isOpen ? 'open' : ''}`}>
      <div className="sidebar-header">
        <h2>Conversations</h2>
        <Button onClick={onNewChat} aria-label="Start new chat" data-testid="new-chat-button">
          <span className="plus-icon">+</span> New Chat
        </Button>
      </div>

      <div className="sidebar-search">
        <input
          type="text"
          placeholder="Search conversations..."
          value={searchQuery}
          onChange={(e) => setSearchQuery(e.target.value)}
          className="search-input"
        />
      </div>

      <div className="conversations-list">
        {conversationsLoading ? (
          <div className="loading-message">Loading conversations...</div>
        ) : filteredConversations.length === 0 ? (
          <div className="empty-message">
            {searchQuery ? 'No conversations match your search' : 'No conversations yet'}
          </div>
        ) : (
          filteredConversations.map((conv) => (
            <div
              key={conv.conversation_id}
              className={`conversation-item ${
                conv.conversation_id === currentConversationId ? 'active' : ''
              }`}
              onClick={() => onConversationSelect(conv.conversation_id)}
              data-testid={`conversation-item-${conv.conversation_id}`}
              data-conversation-id={conv.conversation_id}
            >
              <div className="conversation-preview">{conv.last_message}</div>
              <div className="conversation-meta">
                <span className="timestamp">{formatTimestamp(conv.last_timestamp)}</span>
                <span className="message-count">{conv.message_count} messages</span>
              </div>
            </div>
          ))
        )}
      </div>
    </div>
  );
};

export default ConversationSidebar;
