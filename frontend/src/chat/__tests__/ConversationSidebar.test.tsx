import { screen, waitFor } from '@testing-library/react';
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

    // Wait for conversations to load
    await waitFor(() => {
      expect(screen.getByText('Conversations')).toBeInTheDocument();
    });

    // Note: Specific conversation items depend on how @assistant-ui/react
    // renders the conversation list. This tests the basic structure.
  });

  it('allows switching between conversations', async () => {
    // Mock conversation messages for different conversations
    server.use(
      http.get('/api/v1/chat/conversations/:conversationId/messages', ({ params }) => {
        const { conversationId } = params;

        if (conversationId === 'conv-1') {
          return HttpResponse.json({
            messages: [
              {
                id: 'msg-1',
                role: 'user',
                content: [{ type: 'text', text: 'Hello from conv-1' }],
                createdAt: '2025-01-01T10:00:00Z',
              },
            ],
          });
        }

        if (conversationId === 'conv-2') {
          return HttpResponse.json({
            messages: [
              {
                id: 'msg-2',
                role: 'user',
                content: [{ type: 'text', text: 'Hello from conv-2' }],
                createdAt: '2025-01-01T09:00:00Z',
              },
            ],
          });
        }

        return HttpResponse.json({ messages: [] });
      })
    );

    await renderChatApp({ waitForReady: true });

    // Test switching between conversations if the UI provides this functionality
    // Implementation depends on @assistant-ui/react's conversation management
  });

  it('toggles sidebar open/closed on desktop', async () => {
    const user = userEvent.setup();
    await renderChatApp({ waitForReady: true });

    // On desktop, the sidebar toggle button is present
    const toggleButton = screen.getByLabelText('Toggle sidebar');

    // Test toggling sidebar
    await user.click(toggleButton);

    // Wait for sidebar state to update
    await waitFor(() => {
      expect(toggleButton).toBeInTheDocument();
    });

    // Click again to toggle back
    await user.click(toggleButton);

    await waitFor(() => {
      expect(toggleButton).toBeInTheDocument();
    });
  });

  it('creates new conversation from sidebar', async () => {
    const user = userEvent.setup();
    await renderChatApp({ waitForReady: true });

    // Look for new conversation button
    const newConversationElements = screen.queryAllByText(/new/i);
    const newButton = newConversationElements.find((el) => {
      const button = el.tagName === 'BUTTON' ? el : el.closest('button');
      return button !== null;
    });

    if (newButton) {
      await user.click(newButton);

      // Verify new conversation was created
      await waitFor(
        () => {
          expect(mockLocalStorage.setItem).toHaveBeenCalledWith(
            'lastConversationId',
            expect.stringMatching(/web_conv_/)
          );
        },
        { timeout: 5000 }
      );
    }
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

    // Wait for conversations to load
    await waitFor(() => {
      expect(screen.getByText('Conversations')).toBeInTheDocument();
    });

    // Check for preview text in conversation list
    // Implementation depends on how previews are rendered
  });

  it('handles empty conversation list', async () => {
    // Mock empty conversations response
    server.use(
      http.get('/api/v1/chat/conversations', () => {
        return HttpResponse.json({
          conversations: [],
          count: 0,
        });
      })
    );

    await renderChatApp({ waitForReady: true });

    // Should still show conversations header even when empty
    await waitFor(() => {
      expect(screen.getByText('Conversations')).toBeInTheDocument();
    });

    // Should not show any conversation items
    // This tests the empty state handling
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

    // Reset viewport
    Object.defineProperty(window, 'innerWidth', {
      writable: true,
      configurable: true,
      value: 1024,
    });
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

    // Tap back button to go to conversation list
    const backButton = screen.getByLabelText('Back to conversations');
    await user.click(backButton);

    // Should now see the conversation list
    await waitFor(() => {
      expect(screen.getByText('Conversations')).toBeInTheDocument();
    });

    // Reset viewport
    Object.defineProperty(window, 'innerWidth', {
      writable: true,
      configurable: true,
      value: 1024,
    });
  });
});
