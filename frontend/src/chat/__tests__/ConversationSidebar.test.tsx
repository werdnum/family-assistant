import { render, screen, fireEvent } from '@testing-library/react';
import React from 'react';
import ConversationSidebar from '../ConversationSidebar';
import { describe, it, expect, vi } from 'vitest';

const sampleConversations = [
  {
    conversation_id: 'web_conv_1',
    last_message: 'Hello world',
    last_timestamp: new Date().toISOString(),
    message_count: 1,
  },
  {
    conversation_id: 'web_conv_2',
    last_message: 'Testing sidebar',
    last_timestamp: new Date().toISOString(),
    message_count: 3,
  },
];

describe('ConversationSidebar', () => {
  it('calls callbacks for new chat and conversation selection', () => {
    const onNewChat = vi.fn();
    const onSelect = vi.fn();

    render(
      <ConversationSidebar
        conversations={sampleConversations}
        currentConversationId="web_conv_1"
        onNewChat={onNewChat}
        onConversationSelect={onSelect}
        isOpen
      />
    );

    fireEvent.click(screen.getByTestId('new-chat-button'));
    expect(onNewChat).toHaveBeenCalledTimes(1);

    fireEvent.click(screen.getByTestId('conversation-item-web_conv_2'));
    expect(onSelect).toHaveBeenCalledWith('web_conv_2');
  });

  it('filters conversations by search text', () => {
    render(
      <ConversationSidebar
        conversations={sampleConversations}
        currentConversationId="web_conv_1"
        onNewChat={() => {}}
        onConversationSelect={() => {}}
        isOpen
      />
    );

    const search = screen.getByPlaceholderText('Search conversations...');
    fireEvent.change(search, { target: { value: 'Testing' } });

    expect(screen.queryByTestId('conversation-item-web_conv_1')).not.toBeInTheDocument();
    expect(screen.getByTestId('conversation-item-web_conv_2')).toBeInTheDocument();
  });
});
