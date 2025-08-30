import { describe, it, expect, beforeEach, vi, afterEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import React from 'react';
import ChatApp from '../ChatApp';

// Mock the streaming hook to avoid network calls and allow test control
const sendStreamingMessageMock = vi.fn();

vi.mock('../useStreamingResponse', () => ({
  useStreamingResponse: (opts: Record<string, unknown>) => ({
    sendStreamingMessage: (args: unknown) => sendStreamingMessageMock(args, opts),
    cancelStream: vi.fn(),
    isStreaming: false,
  }),
}));

// Helper to create a minimal fetch response
const createJsonResponse = (data: unknown) =>
  Promise.resolve({ ok: true, json: async () => data } as Response);

describe('ChatApp', () => {
  beforeEach(() => {
    localStorage.clear();
    sendStreamingMessageMock.mockReset();
    // Default to desktop width unless overridden in individual tests
    Object.defineProperty(window, 'innerWidth', {
      writable: true,
      configurable: true,
      value: 1024,
    });

    global.fetch = vi.fn((url: RequestInfo) => {
      if (typeof url === 'string') {
        if (url === '/api/v1/chat/conversations') {
          return createJsonResponse({ conversations: [] });
        }
        if (url === '/api/v1/profiles') {
          return createJsonResponse({
            profiles: [
              {
                id: 'default_assistant',
                description: 'Default Profile',
                available_tools: [],
                enabled_mcp_servers: [],
              },
            ],
            default_profile_id: 'default_assistant',
          });
        }
        if (url.startsWith('/api/v1/chat/conversations/') && url.endsWith('/messages')) {
          return createJsonResponse({ messages: [] });
        }
        if (url === '/api/v1/chat/confirm_tool') {
          return createJsonResponse({});
        }
      }
      return createJsonResponse({});
    }) as unknown as typeof fetch;
  });

  afterEach(() => {
    vi.clearAllMocks();
  });

  it('initialises new conversation when none exists', async () => {
    render(<ChatApp />);

    // Wait for chat input to ensure component has mounted
    const input = await screen.findByTestId('chat-input');

    // Conversation ID should be shown in header
    expect(screen.getByText(/Conversation: web_conv_/)).toBeInTheDocument();

    // Chat input should be enabled
    expect(input.disabled).toBeFalsy();
  });

  it('toggles mobile sidebar when toggle button is pressed', async () => {
    Object.defineProperty(window, 'innerWidth', { writable: true, configurable: true, value: 500 });

    render(<ChatApp />);

    // Wait for input to ensure component has mounted
    await screen.findByTestId('chat-input');

    // Sidebar should be closed initially on mobile
    expect(screen.queryByText('Conversations')).not.toBeInTheDocument();

    const toggleButton = await screen.findByLabelText('Toggle sidebar');
    fireEvent.click(toggleButton);

    // Sidebar content should appear after toggle
    expect(await screen.findByText('Conversations')).toBeInTheDocument();
  });

  it('handles basic message flow', async () => {
    sendStreamingMessageMock.mockImplementation(async (_args, opts) => {
      opts.onMessage('Hello! I am a bot.');
      opts.onComplete({ content: 'Hello! I am a bot.', toolCalls: [] });
    });

    render(<ChatApp />);

    const input = await screen.findByTestId('chat-input');
    fireEvent.input(input, { target: { value: 'Hi there' } });
    fireEvent.click(await screen.findByTestId('send-button'));

    // Assistant message should appear with streamed content
    await screen.findByText('Hello! I am a bot.');

    const userMessages = screen.getAllByTestId('user-message-content');
    expect(userMessages[userMessages.length - 1]).toHaveTextContent('Hi there');

    const assistantMessages = screen.getAllByTestId('assistant-message-content');
    expect(assistantMessages[assistantMessages.length - 1]).toHaveTextContent('Hello! I am a bot.');
  });

  it('supports multiple sequential messages', async () => {
    type StreamingOpts = {
      onMessage: (content: string) => void;
      onComplete: (data: { content: string; toolCalls: unknown[] }) => void;
    };

    sendStreamingMessageMock.mockImplementation(
      (_args: Record<string, unknown>, opts: StreamingOpts) => {
        const prompt = _args.prompt as string;
        const content = prompt.includes('First') ? 'First response' : 'Second response';
        opts.onMessage(content);
        opts.onComplete({ content, toolCalls: [] });
      }
    );

    render(<ChatApp />);

    const input = await screen.findByTestId('chat-input');

    fireEvent.input(input, { target: { value: 'First message' } });
    fireEvent.click(await screen.findByTestId('send-button'));
    await screen.findByText('First response');

    fireEvent.input(input, { target: { value: 'Second message' } });
    fireEvent.click(await screen.findByTestId('send-button'));
    await screen.findByText('Second response');

    const userMessages = screen.getAllByTestId('user-message-content');
    expect(userMessages.map((m) => m.textContent)).toEqual(
      expect.arrayContaining(['First message', 'Second message'])
    );

    const assistantMessages = screen.getAllByTestId('assistant-message-content');
    expect(assistantMessages.map((m) => m.textContent)).toEqual(
      expect.arrayContaining(['First response', 'Second response'])
    );
  });

  it('renders tool call confirmation and sends approval', async () => {
    sendStreamingMessageMock.mockImplementation(async (_args, opts) => {
      opts.onToolCall([
        {
          id: 'call-1',
          name: 'add_or_update_note',
          arguments: JSON.stringify({ title: 'Test', content: 'Body' }),
        },
      ]);
      opts.onToolConfirmationRequest({
        tool_call_id: 'call-1',
        request_id: 'req-1',
        confirmation_prompt: 'Allow note creation?',
        timeout_seconds: 30,
        created_at: new Date().toISOString(),
        args: {},
      });
      opts.onComplete({ content: '', toolCalls: [] });
    });

    render(<ChatApp />);

    const input = await screen.findByTestId('chat-input');
    fireEvent.input(input, { target: { value: 'Use tool' } });
    fireEvent.click(await screen.findByTestId('send-button'));

    await screen.findByText('Allow note creation?');

    const approveBtn = screen.getByRole('button', { name: 'Approve' });
    fireEvent.click(approveBtn);

    const confirmCall = (fetch as vi.Mock).mock.calls.find(
      (c) => c[0] === '/api/v1/chat/confirm_tool'
    );
    expect(confirmCall).toBeTruthy();
    expect(JSON.parse(confirmCall[1].body)).toMatchObject({
      request_id: 'req-1',
      approved: true,
    });
  });

  it('loads conversations and switches between them', async () => {
    (global.fetch as vi.Mock).mockImplementation((url: RequestInfo) => {
      const requestUrl = typeof url === 'string' ? url : url.url;
      if (
        requestUrl &&
        requestUrl.includes('/api/v1/chat/conversations') &&
        !requestUrl.includes('/messages')
      ) {
        return createJsonResponse({
          conversations: [
            {
              conversation_id: 'conv1',
              last_message: 'first',
              last_timestamp: '2024-01-01T00:00:00Z',
              message_count: 2,
            },
            {
              conversation_id: 'conv2',
              last_message: 'second',
              last_timestamp: '2024-01-01T00:00:00Z',
              message_count: 2,
            },
          ],
        });
      }
      if (requestUrl && requestUrl.includes('/api/v1/profiles')) {
        return createJsonResponse({
          profiles: [
            {
              id: 'default_assistant',
              description: 'Default Profile',
              available_tools: [],
              enabled_mcp_servers: [],
            },
          ],
          default_profile_id: 'default_assistant',
        });
      }
      if (requestUrl && requestUrl.includes('/api/v1/chat/conversations/conv1/messages')) {
        return createJsonResponse({
          messages: [
            {
              internal_id: 1,
              role: 'user',
              content: 'Hello from conv1',
              timestamp: '2024-01-01T00:00:00Z',
            },
            {
              internal_id: 2,
              role: 'assistant',
              content: 'Hi from conv1',
              timestamp: '2024-01-01T00:00:01Z',
            },
          ],
        });
      }
      if (requestUrl && requestUrl.includes('/api/v1/chat/conversations/conv2/messages')) {
        return createJsonResponse({
          messages: [
            {
              internal_id: 3,
              role: 'user',
              content: 'Hi from conv2',
              timestamp: '2024-01-01T00:00:00Z',
            },
            {
              internal_id: 4,
              role: 'assistant',
              content: 'Tool response',
              tool_calls: [
                {
                  id: 'tool123',
                  type: 'function',
                  function: {
                    name: 'add_or_update_note',
                    arguments: JSON.stringify({ title: 't', content: 'c' }),
                  },
                },
              ],
              timestamp: '2024-01-01T00:00:01Z',
            },
            {
              internal_id: 5,
              role: 'tool',
              tool_call_id: 'tool123',
              content: 'done',
              timestamp: '2024-01-01T00:00:02Z',
            },
          ],
        });
      }
      return createJsonResponse({});
    });

    localStorage.setItem('lastConversationId', 'conv1');

    render(<ChatApp />);

    const conv2Item = await screen.findByTestId('conversation-item-conv2');
    fireEvent.click(conv2Item);

    await screen.findByText('Tool response');
    await screen.findByText('done');
  });
});
