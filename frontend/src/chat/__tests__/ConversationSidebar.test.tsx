import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { HttpResponse, http } from 'msw';
import { vi } from 'vitest';
import { mockLocalStorage, resetLocalStorageMock } from '../../test/mocks/localStorageMock';
import { server } from '../../test/setup.js';
import { renderChatApp } from '../../test/utils/renderChatApp';

describe('ConversationSidebar', () => {
  beforeEach(() => {
    resetLocalStorageMock();
    vi.clearAllMocks();

    // Clean up URL state from previous tests
    window.history.replaceState({}, '', '/');

    // Clean up DOM attributes
    document.documentElement.removeAttribute('data-app-ready');
  });

  afterEach(() => {
    Object.defineProperty(window, 'innerWidth', {
      writable: true,
      configurable: true,
      value: 1024,
    });
  });

  const renderedConversationIds = (): (string | null)[] =>
    screen
      .queryAllByTestId(/^conversation-item-/)
      .map((item) => item.getAttribute('data-conversation-id'));

  const serveConversationHistories = (): void => {
    server.use(
      http.get('/api/v1/chat/conversations', () =>
        HttpResponse.json({
          conversations: [
            {
              conversation_id: 'conv-1',
              last_message: 'Preview of conv-1',
              last_timestamp: '2025-01-01T10:00:00Z',
              message_count: 1,
            },
            {
              conversation_id: 'conv-2',
              last_message: 'Preview of conv-2',
              last_timestamp: '2025-01-01T09:00:00Z',
              message_count: 1,
            },
          ],
          count: 2,
        })
      ),
      http.get('/api/v1/chat/conversations/:conversationId/messages', ({ params }) => {
        const conversationId = String(params.conversationId);
        if (conversationId !== 'conv-1' && conversationId !== 'conv-2') {
          return HttpResponse.json({ messages: [] });
        }
        return HttpResponse.json({
          messages: [
            {
              internal_id: `${conversationId}-msg-1`,
              role: 'user',
              content: `Hello from ${conversationId}`,
              timestamp: '2025-01-01T09:00:00Z',
            },
          ],
        });
      })
    );
  };

  const lastConversationIdWrites = (): unknown[] =>
    mockLocalStorage.setItem.mock.calls
      .filter(([key]) => key === 'lastConversationId')
      .map(([, value]) => value);

  it('displays conversation list', async () => {
    // Mock conversations endpoint with existing conversations
    server.use(
      http.get('/api/v1/chat/conversations', () => {
        return HttpResponse.json({
          conversations: [
            {
              conversation_id: 'conv-1',
              last_message: 'First conversation message',
              last_timestamp: '2025-01-01T10:00:00Z',
              message_count: 2,
            },
            {
              conversation_id: 'conv-2',
              last_message: 'Second conversation message',
              last_timestamp: '2025-01-01T09:00:00Z',
              message_count: 3,
            },
          ],
          count: 2,
        });
      })
    );

    await renderChatApp({ waitForReady: true });

    await screen.findByTestId('conversation-item-conv-1');
    expect(renderedConversationIds()).toEqual(['conv-1', 'conv-2']);
  });

  it('allows switching between conversations', async () => {
    const user = userEvent.setup();
    serveConversationHistories();

    await renderChatApp({ waitForReady: true });

    await user.click(await screen.findByTestId('conversation-item-conv-1'));
    expect(await screen.findByText('Hello from conv-1')).toBeInTheDocument();

    await user.click(screen.getByTestId('conversation-item-conv-2'));
    expect(await screen.findByText('Hello from conv-2')).toBeInTheDocument();
    expect(screen.queryByText('Hello from conv-1')).not.toBeInTheDocument();
    expect(lastConversationIdWrites().slice(-2)).toEqual(['conv-1', 'conv-2']);
    expect(window.location.search).toBe('?conversation_id=conv-2');
  });

  it('reopens the conversation already on screen without a loading placeholder', async () => {
    const user = userEvent.setup();
    serveConversationHistories();

    await renderChatApp({ waitForReady: true });
    await user.click(await screen.findByTestId('conversation-item-conv-1'));
    expect(await screen.findByText('Hello from conv-1')).toBeInTheDocument();

    let releaseReload: (() => void) | undefined;
    const reloads: URL[] = [];
    server.use(
      http.get('/api/v1/chat/conversations/:conversationId/messages', async ({ request }) => {
        reloads.push(new URL(request.url));
        await new Promise<void>((resolve) => {
          releaseReload = resolve;
        });
        return HttpResponse.json({
          messages: [
            {
              internal_id: 'conv-1-msg-1',
              role: 'user',
              content: 'Hello from conv-1',
              timestamp: '2025-01-01T09:00:00Z',
            },
          ],
        });
      })
    );

    await user.click(screen.getByTestId('conversation-item-conv-1'));
    await waitFor(() => expect(releaseReload).toBeDefined());

    expect(screen.queryByTestId('conversation-loading')).not.toBeInTheDocument();
    expect(screen.getByText('Hello from conv-1')).toBeInTheDocument();
    expect(reloads[0].searchParams.has('include_conversation_profile')).toBe(false);
    releaseReload?.();
  });

  it('keeps the previous matches on screen while a refined search runs', async () => {
    const user = userEvent.setup();
    let releaseRefinedSearch: (() => void) | undefined;
    server.use(
      http.get('/api/v1/chat/conversations', async ({ request }) => {
        const query = new URL(request.url).searchParams.get('q');
        if (query === null) {
          return HttpResponse.json({ conversations: [], count: 0 });
        }
        if (query !== 'pass') {
          await new Promise<void>((resolve) => {
            releaseRefinedSearch = resolve;
          });
        }
        return HttpResponse.json({
          conversations: [
            {
              conversation_id: 'conv-passport',
              last_message: 'You are welcome',
              last_timestamp: '2025-01-01T09:00:00Z',
              message_count: 6,
              match_excerpt: 'renew the passport before the trip',
            },
          ],
          count: 1,
        });
      })
    );

    await renderChatApp({ waitForReady: true });
    const searchBox = screen.getByPlaceholderText('Search...');
    await user.type(searchBox, 'pass');
    expect(await screen.findByTestId('conversation-item-conv-passport')).toBeInTheDocument();

    await user.type(searchBox, 'port');
    await waitFor(() => expect(releaseRefinedSearch).toBeDefined());
    expect(screen.getByTestId('conversation-item-conv-passport')).toBeInTheDocument();
    expect(screen.queryByText('Searching...')).not.toBeInTheDocument();
    releaseRefinedSearch?.();
  });

  it('toggles sidebar open/closed on desktop', async () => {
    const user = userEvent.setup();
    await renderChatApp({ waitForReady: true });

    // On desktop, the sidebar toggle button is present
    const toggleButton = screen.getByLabelText('Toggle sidebar');

    // Sidebar should be open by default on desktop (w-72 panel visible)
    const sidebar = document.querySelector('.w-72.flex-shrink-0.border-r');
    expect(sidebar).toBeInTheDocument();
    expect(sidebar?.className).not.toContain('-ml-72');

    // Click to close sidebar
    await user.click(toggleButton);

    await waitFor(() => {
      expect(sidebar?.className).toContain('-ml-72');
    });

    // Click again to re-open
    await user.click(toggleButton);

    await waitFor(() => {
      expect(sidebar?.className).not.toContain('-ml-72');
    });
  });

  it('creates new conversation from sidebar', async () => {
    const user = userEvent.setup();
    serveConversationHistories();
    mockLocalStorage.getItem.mockImplementation((key: string) =>
      key === 'lastConversationId' ? 'conv-1' : null
    );

    await renderChatApp({ waitForReady: true });
    expect(await screen.findByText('Hello from conv-1')).toBeInTheDocument();
    expect(lastConversationIdWrites()).toEqual([]);

    await user.click(screen.getByTestId('new-chat-button'));

    expect(await screen.findByText('How can I help you?')).toBeInTheDocument();
    expect(screen.queryByText('Hello from conv-1')).not.toBeInTheDocument();
    const writes = lastConversationIdWrites();
    expect(writes).toHaveLength(1);
    expect(writes[0]).toMatch(/^web_conv_/);
    expect(window.location.search).toBe(`?conversation_id=${String(writes[0])}`);
  });

  it('searches conversations on the server and shows where each matched', async () => {
    const user = userEvent.setup();
    const searchQueries: string[] = [];
    server.use(
      http.get('/api/v1/chat/conversations', ({ request }) => {
        const query = new URL(request.url).searchParams.get('q');
        if (query === null) {
          return HttpResponse.json({
            conversations: [
              {
                conversation_id: 'conv-latest',
                last_message: 'Thanks, bye',
                last_timestamp: '2025-01-01T10:00:00Z',
                message_count: 4,
              },
            ],
            count: 1,
          });
        }
        searchQueries.push(query);
        return HttpResponse.json({
          conversations: [
            {
              conversation_id: 'conv-passport',
              last_message: 'You are welcome',
              last_timestamp: '2025-01-01T09:00:00Z',
              message_count: 6,
              match_excerpt: 'renew the passport before the trip',
            },
          ],
          count: 1,
        });
      })
    );

    await renderChatApp({ waitForReady: true });
    await user.type(screen.getByPlaceholderText('Search...'), 'passport');

    expect(await screen.findByText('renew the passport before the trip')).toBeInTheDocument();
    expect(screen.getByTestId('conversation-item-conv-passport')).toBeInTheDocument();
    expect(screen.queryByTestId('conversation-item-conv-latest')).not.toBeInTheDocument();
    expect(searchQueries).toEqual(['passport']);
  });

  it('shows conversation previews', async () => {
    // Mock conversations with preview text
    server.use(
      http.get('/api/v1/chat/conversations', () => {
        return HttpResponse.json({
          conversations: [
            {
              conversation_id: 'conv-preview-test',
              last_message: 'This is a preview of the conversation content',
              last_timestamp: '2025-01-01T10:00:00Z',
              message_count: 1,
            },
          ],
          count: 1,
        });
      })
    );

    await renderChatApp({ waitForReady: true });

    const item = await screen.findByTestId('conversation-item-conv-preview-test');
    expect(
      within(item).getByText('This is a preview of the conversation content')
    ).toBeInTheDocument();
  });

  it('handles empty conversation list', async () => {
    let listServed = false;
    server.use(
      http.get('/api/v1/chat/conversations', () => {
        listServed = true;
        return HttpResponse.json({
          conversations: [],
          count: 0,
        });
      })
    );

    await renderChatApp({ waitForReady: true });

    await waitFor(() => {
      expect(listServed).toBe(true);
    });
    expect(await screen.findByText('No conversations yet')).toBeInTheDocument();
    expect(screen.getByText('Conversations')).toBeInTheDocument();
    expect(renderedConversationIds()).toEqual([]);
  });

  it('works on mobile viewport', async () => {
    // Set mobile viewport
    Object.defineProperty(window, 'innerWidth', {
      writable: true,
      configurable: true,
      value: 375,
    });

    window.dispatchEvent(new Event('resize'));

    await renderChatApp({ waitForReady: true });

    // On mobile, the app auto-creates a conversation on startup, so we see the chat detail view
    // with a back button instead of a sidebar toggle
    expect(screen.getByText('Chat')).toBeInTheDocument();
    expect(screen.getByPlaceholderText('Message Family Assistant...')).toBeInTheDocument();

    // The back button should be present to navigate to conversation list
    expect(screen.getByLabelText('Back to conversations')).toBeInTheDocument();
  });

  it('shows conversation list when navigating back on mobile', async () => {
    // Set mobile viewport
    Object.defineProperty(window, 'innerWidth', {
      writable: true,
      configurable: true,
      value: 375,
    });

    window.dispatchEvent(new Event('resize'));

    await renderChatApp({ waitForReady: true });

    const user = userEvent.setup();
    expect(screen.queryByText('Conversations')).not.toBeInTheDocument();

    // Tap back button to go to conversation list
    const backButton = screen.getByLabelText('Back to conversations');
    await user.click(backButton);

    expect(await screen.findByTestId('conversation-item-web_conv_test-1')).toBeInTheDocument();
    expect(screen.getByText('Conversations')).toBeInTheDocument();
    expect(screen.queryByLabelText('Back to conversations')).not.toBeInTheDocument();
  });
});
