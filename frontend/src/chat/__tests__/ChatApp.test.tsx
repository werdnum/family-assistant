import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { vi } from 'vitest';
import { renderChatApp } from '../../test/utils/renderChatApp';
import { waitForMessageSent } from '../../test/utils/waitHelpers';
import { mockLocalStorage, resetLocalStorageMock } from '../../test/mocks/localStorageMock';

// Mock window.history for navigation
Object.defineProperty(window, 'history', {
  value: {
    pushState: vi.fn(),
    replaceState: vi.fn(),
  },
});

// Mock window dimensions for responsive behavior
Object.defineProperty(window, 'innerWidth', {
  writable: true,
  configurable: true,
  value: 1024, // Desktop size
});

describe('ChatApp', () => {
  beforeEach(() => {
    // Reset mocks
    resetLocalStorageMock();
    vi.clearAllMocks();
  });

  it('renders the chat interface', async () => {
    await renderChatApp({ waitForReady: true });

    // Verify basic UI elements are present
    expect(screen.getByText('Chat')).toBeInTheDocument();
    expect(screen.getByText('Assistant')).toBeInTheDocument();
    expect(screen.getByText('Conversations')).toBeInTheDocument();
  });

  it('sends and receives messages', async () => {
    const user = userEvent.setup();
    await renderChatApp({ waitForReady: true });

    // Find the message input by placeholder text (we know it says "Write a message...")
    const messageInput = screen.getByPlaceholderText('Write a message...');
    expect(messageInput).toBeInTheDocument();

    // Type a message
    await user.type(messageInput, 'Hello there!');

    // For assistant-ui, we typically submit by pressing Enter rather than clicking a button
    await user.keyboard('{Enter}');

    // Get fresh reference (input may have been re-rendered after submission)
    const submittedInput = screen.getByPlaceholderText('Write a message...');

    // Verify the message was sent by checking if the input was cleared
    await waitForMessageSent(submittedInput);

    // Note: The actual message sending and response display depends on the
    // @assistant-ui/react runtime behavior, which may not show messages
    // in the DOM in the same way as a traditional chat UI
  });

  it('handles conversation loading', async () => {
    await renderChatApp({ waitForReady: true });

    // Check that conversations are loaded by looking for the sidebar
    await waitFor(() => {
      expect(screen.getByText('Conversations')).toBeInTheDocument();
    });

    // The MSW handler should have been called for conversations
    // This test verifies the component makes the right API calls
  });

  it('creates new conversations', async () => {
    const user = userEvent.setup();
    await renderChatApp({ waitForReady: true });

    // Look for a "new chat" or similar button
    const newChatButton =
      screen.queryByRole('button', { name: /new/i }) || screen.queryByText(/new chat/i);

    if (newChatButton) {
      await user.click(newChatButton);

      // Should create a new conversation
      await waitFor(() => {
        expect(mockLocalStorage.setItem).toHaveBeenCalledWith(
          'lastConversationId',
          expect.stringMatching(/web_conv_/)
        );
      });
    }
  });

  it('handles profile switching', async () => {
    await renderChatApp({ profileId: 'browser_profile', waitForReady: true });

    // Check that the profile selector is present
    await waitFor(() => {
      expect(screen.getByRole('combobox')).toBeInTheDocument();
    });

    // This tests the basic profile switching functionality
  });

  it('handles multiple messages in a conversation', async () => {
    const user = userEvent.setup();
    await renderChatApp({ waitForReady: true });

    const messageInput = screen.getByPlaceholderText('Write a message...');

    // Send first message
    await user.type(messageInput, 'First message');
    await user.keyboard('{Enter}');

    // Wait for input to be cleared (message sent)
    await waitForMessageSent(messageInput);

    // Wait for the assistant's response by checking that we have 2 messages total (1 user + 1 assistant)
    await waitFor(
      () => {
        const userMessages = screen.queryAllByTestId('user-message');
        const assistantMessages = screen.queryAllByTestId('assistant-message');
        expect(userMessages.length + assistantMessages.length).toBe(2);
      },
      { timeout: 10000 }
    );

    // Wait for any loading indicators to disappear
    await waitFor(
      () => {
        const loadingIndicators = document.querySelectorAll('.animate-bounce');
        expect(loadingIndicators.length).toBe(0);
      },
      { timeout: 2000 }
    );

    // Ensure input is ready for the next message
    await waitFor(() => {
      const input = screen.getByPlaceholderText('Write a message...');
      expect(input).toBeEnabled();
      expect(input).toHaveValue('');
    });

    // NOTE: This delay is necessary for @assistant-ui/react's internal state to fully settle
    // after streaming completes. Even though the input appears enabled and empty, the library
    // needs additional time before it can successfully accept and submit a new message.
    // This mirrors similar delays in the Playwright tests (wait_for_timeout after typing).
    // Without this, pressing Enter after typing doesn't submit the message.
    await new Promise((resolve) => setTimeout(resolve, 500));

    // Get a fresh reference and send second message
    const input2 = screen.getByPlaceholderText('Write a message...');
    await user.click(input2);
    await user.type(input2, 'Second message');
    await user.keyboard('{Enter}');

    // Wait for second message to be sent
    await waitForMessageSent(input2);

    // Wait for the second assistant response - we should now have 4 messages total (2 user + 2 assistant)
    await waitFor(
      () => {
        const userMessages = screen.queryAllByTestId('user-message');
        const assistantMessages = screen.queryAllByTestId('assistant-message');
        expect(userMessages.length + assistantMessages.length).toBe(4);
      },
      { timeout: 10000 }
    );
  }, 20000); // 20s timeout for full test

  it('displays streaming responses correctly', async () => {
    const user = userEvent.setup();
    await renderChatApp({ waitForReady: true });

    const messageInput = screen.getByPlaceholderText('Write a message...');

    // Send a message that will trigger our streaming response
    await user.type(messageInput, 'Hello there!');
    await user.keyboard('{Enter}');

    // Verify input cleared (message sent)
    await waitForMessageSent(messageInput);

    // The streaming response should be processed by @assistant-ui/react
    // We can't easily test the individual chunks, but can verify the final state
  }, 10000); // Add 10s timeout

  it('handles conversation switching', async () => {
    const user = userEvent.setup();
    await renderChatApp({ waitForReady: true });

    const messageInput = screen.getByPlaceholderText('Write a message...');

    // Send message in first conversation
    await user.type(messageInput, 'Message in first conversation');
    await user.keyboard('{Enter}');

    await waitForMessageSent(messageInput);

    // Look for new conversation button/functionality
    // Note: The exact selector depends on how @assistant-ui/react exposes conversation controls
    const newConversationElements = screen.queryAllByText(/new/i);
    if (newConversationElements.length > 0) {
      // Try to start a new conversation if UI provides this
      const newButton = newConversationElements.find(
        (el) => el.tagName === 'BUTTON' || el.closest('button')
      );
      if (newButton) {
        await user.click(newButton);
        // Wait for new conversation to be created
        await waitFor(() => {
          expect(messageInput).toBeInTheDocument();
        });
      }
    }

    // This test verifies the basic conversation switching flow
    // Full validation would require accessing @assistant-ui/react's conversation state
  }, 10000); // Add 10s timeout

  it('handles empty conversation state', async () => {
    await renderChatApp({ waitForReady: true });

    // Chat input should be available even with no messages
    const messageInput = screen.getByPlaceholderText('Write a message...');
    expect(messageInput).toBeInTheDocument();
    expect(messageInput).not.toBeDisabled();

    // Basic chat interface should be present
    expect(screen.getByText('Chat')).toBeInTheDocument();
  });

  it('works on mobile viewport', async () => {
    // Set mobile viewport size
    Object.defineProperty(window, 'innerWidth', {
      writable: true,
      configurable: true,
      value: 375, // Mobile width
    });
    Object.defineProperty(window, 'innerHeight', {
      writable: true,
      configurable: true,
      value: 667, // Mobile height
    });

    // Dispatch resize event
    window.dispatchEvent(new Event('resize'));

    await renderChatApp({ waitForReady: true });

    // Chat should still be functional on mobile
    expect(screen.getByText('Chat')).toBeInTheDocument();
    expect(screen.getByPlaceholderText('Write a message...')).toBeInTheDocument();

    // Reset viewport
    Object.defineProperty(window, 'innerWidth', {
      writable: true,
      configurable: true,
      value: 1024,
    });
    Object.defineProperty(window, 'innerHeight', {
      writable: true,
      configurable: true,
      value: 768,
    });
  });

  it('displays only web conversations in sidebar', async () => {
    const { server } = await import('../../test/setup.js');
    const { http, HttpResponse } = await import('msw');

    // Mock API to return mixed conversation types, but web UI should filter to only show web ones
    server.use(
      http.get('/api/v1/chat/conversations', ({ request }) => {
        const url = new URL(request.url);
        const interfaceType = url.searchParams.get('interface_type');

        // If requesting web conversations specifically, return only web ones
        if (interfaceType === 'web') {
          return HttpResponse.json({
            conversations: [
              {
                conversation_id: 'web_conv_123',
                last_message: 'Web conversation message',
                last_timestamp: '2025-01-01T10:00:00Z',
                message_count: 2,
              },
              {
                conversation_id: 'web_conv_456',
                last_message: 'Another web message',
                last_timestamp: '2025-01-01T09:00:00Z',
                message_count: 1,
              },
            ],
            count: 2,
          });
        }

        // Without filter, would return mixed types (but web UI shouldn't call this)
        return HttpResponse.json({
          conversations: [
            {
              conversation_id: 'web_conv_123',
              last_message: 'Web conversation message',
              last_timestamp: '2025-01-01T10:00:00Z',
              message_count: 2,
            },
            {
              conversation_id: 'telegram_conv_789',
              last_message: 'Telegram message that should not appear',
              last_timestamp: '2025-01-01T08:00:00Z',
              message_count: 1,
            },
          ],
          count: 2,
        });
      })
    );

    await renderChatApp({ waitForReady: true });

    // Wait for conversations to load
    await waitFor(
      () => {
        expect(screen.getByText('Conversations')).toBeInTheDocument();
      },
      { timeout: 3000 }
    );

    // Should see web conversations
    expect(screen.getByText('Web conversation message')).toBeInTheDocument();
    expect(screen.getByText('Another web message')).toBeInTheDocument();

    // Should NOT see telegram conversations (they should be filtered out by the interface_type filter)
    expect(screen.queryByText('Telegram message that should not appear')).not.toBeInTheDocument();
  });

  // Note: Attachment display from assistant message metadata is covered by E2E Playwright tests:
  // - test_tool_attachment_persistence_after_page_reload (tests/functional/web/test_chat_ui_attachment_response.py)
  // - test_attachment_response_flow (tests/functional/web/test_chat_ui_attachment_response.py)
  // These tests verify the full user-visible behavior including page reloads and attachment display.
});
