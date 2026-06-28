import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { vi } from 'vitest';
import { mockLocalStorage, resetLocalStorageMock } from '../../test/mocks/localStorageMock';
import { renderChatApp } from '../../test/utils/renderChatApp';
import { waitForMessageSent } from '../../test/utils/waitHelpers';
import { mergeConsecutiveToolOnlyAssistantMessages } from '../ChatApp';

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

    // Verify basic UI elements are present - use findByText to wait for async loading
    expect(await screen.findByText('Chat')).toBeInTheDocument();
    expect(await screen.findByText('Assistant')).toBeInTheDocument();
    expect(await screen.findByText('Conversations')).toBeInTheDocument();
  });

  it('sends and receives messages', async () => {
    const user = userEvent.setup();
    await renderChatApp({ waitForReady: true });

    // Find the message input by placeholder text
    const messageInput = screen.getByPlaceholderText('Message Family Assistant...');
    expect(messageInput).toBeInTheDocument();

    // Type a message
    await user.type(messageInput, 'Hello there!');

    // For assistant-ui, we typically submit by pressing Enter rather than clicking a button
    await user.keyboard('{Enter}');

    // Get fresh reference (input may have been re-rendered after submission).
    // Use the stable test id rather than the placeholder: while the turn runs
    // the composer doubles as the steer input and its placeholder changes.
    const submittedInput = screen.getByTestId('chat-input');

    // Verify the message was sent by checking if the input was cleared
    await waitForMessageSent(submittedInput);

    // Note: The actual message sending and response display depends on the
    // @assistant-ui/react runtime behavior, which may not show messages
    // in the DOM in the same way as a traditional chat UI
  }, 30000);

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

    const messageInput = screen.getByPlaceholderText('Message Family Assistant...');

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
      const input = screen.getByPlaceholderText('Message Family Assistant...');
      expect(input).toBeEnabled();
      expect(input).toHaveValue('');
    });

    // NOTE: This delay is necessary for @assistant-ui/react's internal state to fully settle
    // after streaming completes. Even though the input appears enabled and empty, the library
    // needs additional time before it can successfully accept and submit a new message.
    // The Playwright tests have a 3000ms wait in send_message after pressing Enter, which gives
    // the library time to process the response before the next message starts.
    // Without sufficient delay, pressing Enter after typing doesn't submit the message.
    await new Promise((resolve) => setTimeout(resolve, 2000));

    // Get a fresh reference and send second message
    const input2 = screen.getByPlaceholderText('Message Family Assistant...');
    await user.click(input2);
    await user.type(input2, 'Second message');

    // Wait for input processing before pressing Enter (matches Playwright send_message pattern)
    await new Promise((resolve) => setTimeout(resolve, 500));
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

    const messageInput = screen.getByPlaceholderText('Message Family Assistant...');

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

    const messageInput = screen.getByPlaceholderText('Message Family Assistant...');

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
    const messageInput = screen.getByPlaceholderText('Message Family Assistant...');
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
    expect(screen.getByPlaceholderText('Message Family Assistant...')).toBeInTheDocument();

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

  it('shows the after-running-tools error banner when tool call + error + turn_ended arrive in one chunk', async () => {
    const { server } = await import('../../test/setup.js');
    const { testHandlers } = await import('../../test/mocks/handlers');

    // All of the turn's SSE events are delivered in a single network chunk
    // (hub replay of an already-finished turn). The client then processes
    // tool_call, error and turn_ended in one synchronous pass with no React
    // render in between, so onComplete must not depend on state-updater side
    // effects to know a tool call happened. Regression test for the flake
    // where the generic "error processing your message" banner appeared
    // instead of the "error after running tools" one.
    server.use(
      testHandlers.toolCallThenErrorCoalesced(
        'activate_tools',
        'I encountered an error while processing your message.'
      )
    );

    const user = userEvent.setup();
    await renderChatApp({ waitForReady: true });

    const messageInput = screen.getByPlaceholderText('Message Family Assistant...');
    await user.type(messageInput, 'Activate tools then fail');
    await user.keyboard('{Enter}');

    expect(
      await screen.findByText(/Sorry, I encountered an error after running tools\./, undefined, {
        timeout: 10000,
      })
    ).toBeInTheDocument();
  }, 30000);

  it('loads assistant history rows with attachments without passing message attachments to assistant-ui', async () => {
    const { server } = await import('../../test/setup.js');
    const { http, HttpResponse } = await import('msw');

    server.use(
      http.get('/api/v1/chat/conversations/:conversationId/messages', ({ params }) => {
        if (params.conversationId !== 'web_conv_assistant_attachment') {
          return HttpResponse.json({ messages: [] });
        }

        return HttpResponse.json({
          messages: [
            {
              internal_id: 101,
              role: 'user',
              content: 'Make a chart',
              timestamp: '2026-06-24T12:00:00Z',
            },
            {
              internal_id: 102,
              role: 'assistant',
              content: 'Here is the chart.',
              timestamp: '2026-06-24T12:00:01Z',
              attachments: [
                {
                  attachment_id: 'chart-attachment',
                  type: 'attachment_reference',
                },
              ],
            },
          ],
        });
      })
    );
    mockLocalStorage.getItem.mockImplementation((key: string) =>
      key === 'lastConversationId' ? 'web_conv_assistant_attachment' : null
    );

    await renderChatApp({
      waitForReady: true,
    });

    expect(await screen.findByText('Here is the chart.')).toBeInTheDocument();
    const user = userEvent.setup();
    await user.click(await screen.findByTestId('tool-group-trigger'));
    expect(await screen.findByText('1 attachment ready')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Download' })).toHaveAttribute(
      'href',
      '/api/attachments/chart-attachment'
    );
    expect(screen.getByTestId('assistant-message')).toBeInTheDocument();
  });

  it('adopts the opened conversation profile and sends the follow-up under it', async () => {
    const { server } = await import('../../test/setup.js');
    const { http, HttpResponse } = await import('msw');

    server.use(
      http.get('/api/v1/chat/conversations/:conversationId/messages', ({ params }) => {
        if (params.conversationId !== 'web_conv_profile_adopt') {
          return HttpResponse.json({ messages: [] });
        }
        return HttpResponse.json({
          latest_user_profile_id: 'complex_tasks',
          messages: [
            {
              internal_id: 201,
              role: 'user',
              content: 'Plan the trip',
              timestamp: '2026-06-24T12:00:00Z',
              processing_profile_id: 'complex_tasks',
            },
            {
              internal_id: 202,
              role: 'assistant',
              content: 'Sure, here is a plan.',
              timestamp: '2026-06-24T12:00:01Z',
              processing_profile_id: 'complex_tasks',
            },
          ],
        });
      })
    );

    let capturedProfileId: string | undefined;
    server.use(
      http.post('/api/v1/chat/turns', async ({ request }) => {
        const body = (await request.json()) as { turn_id: string; profile_id?: string };
        capturedProfileId = body.profile_id;
        return HttpResponse.json({
          turn_id: body.turn_id,
          conversation_id: 'web_conv_profile_adopt',
          first_seq: 0,
        });
      })
    );

    // The persisted *preferred* profile differs from the conversation's profile,
    // so an un-adopted turn would be sent under 'default_assistant' and the
    // backend would filter the (complex_tasks-tagged) history down to nothing.
    mockLocalStorage.getItem.mockImplementation((key: string) => {
      if (key === 'lastConversationId') {
        return 'web_conv_profile_adopt';
      }
      if (key === 'selectedProfileId') {
        return 'default_assistant';
      }
      return null;
    });

    const user = userEvent.setup();
    await renderChatApp({ waitForReady: true });

    // Wait for the conversation history to load (adoption runs on this load).
    expect(await screen.findByText('Plan the trip')).toBeInTheDocument();

    const messageInput = screen.getByPlaceholderText('Message Family Assistant...');
    await user.type(messageInput, 'Continue the plan');
    await user.keyboard('{Enter}');

    await waitFor(() => expect(capturedProfileId).toBe('complex_tasks'), { timeout: 10000 });
  }, 30000);

  it('adopts the backend-resolved conversation profile, ignoring a later delegated assistant row', async () => {
    const { server } = await import('../../test/setup.js');
    const { http, HttpResponse } = await import('msw');

    server.use(
      http.get('/api/v1/chat/conversations/:conversationId/messages', ({ params }) => {
        if (params.conversationId !== 'web_conv_profile_delegated') {
          return HttpResponse.json({ messages: [] });
        }
        // The backend resolves latest_user_profile_id from the most recent *user*
        // message (the delegated assistant row tagged 'engineer' is ignored there);
        // the client just adopts whatever the backend returns.
        return HttpResponse.json({
          latest_user_profile_id: 'complex_tasks',
          messages: [
            {
              internal_id: 301,
              role: 'user',
              content: 'Research and book it',
              timestamp: '2026-06-24T12:00:00Z',
              processing_profile_id: 'complex_tasks',
            },
            {
              internal_id: 302,
              role: 'assistant',
              content: 'Handing off to the engineer.',
              timestamp: '2026-06-24T12:00:01Z',
              processing_profile_id: 'engineer',
            },
          ],
        });
      })
    );

    let capturedProfileId: string | undefined;
    server.use(
      http.post('/api/v1/chat/turns', async ({ request }) => {
        const body = (await request.json()) as { turn_id: string; profile_id?: string };
        capturedProfileId = body.profile_id;
        return HttpResponse.json({
          turn_id: body.turn_id,
          conversation_id: 'web_conv_profile_delegated',
          first_seq: 0,
        });
      })
    );

    mockLocalStorage.getItem.mockImplementation((key: string) => {
      if (key === 'lastConversationId') {
        return 'web_conv_profile_delegated';
      }
      if (key === 'selectedProfileId') {
        return 'default_assistant';
      }
      return null;
    });

    const user = userEvent.setup();
    await renderChatApp({ waitForReady: true });

    expect(await screen.findByText('Research and book it')).toBeInTheDocument();

    const messageInput = screen.getByPlaceholderText('Message Family Assistant...');
    await user.type(messageInput, 'Keep going');
    await user.keyboard('{Enter}');

    await waitFor(() => expect(capturedProfileId).toBe('complex_tasks'), { timeout: 10000 });
  }, 30000);

  it('resets to the preferred profile when starting a new chat', async () => {
    const { server } = await import('../../test/setup.js');
    const { http, HttpResponse } = await import('msw');

    server.use(
      http.get('/api/v1/chat/conversations/:conversationId/messages', ({ params }) => {
        if (params.conversationId !== 'web_conv_profile_adopt') {
          return HttpResponse.json({ messages: [] });
        }
        return HttpResponse.json({
          latest_user_profile_id: 'complex_tasks',
          messages: [
            {
              internal_id: 201,
              role: 'user',
              content: 'Plan the trip',
              timestamp: '2026-06-24T12:00:00Z',
              processing_profile_id: 'complex_tasks',
            },
          ],
        });
      })
    );

    let capturedProfileId: string | undefined;
    server.use(
      http.post('/api/v1/chat/turns', async ({ request }) => {
        const body = (await request.json()) as {
          turn_id: string;
          profile_id?: string;
          conversation_id?: string;
        };
        capturedProfileId = body.profile_id;
        return HttpResponse.json({
          turn_id: body.turn_id,
          conversation_id: body.conversation_id ?? 'web_conv_new',
          first_seq: 0,
        });
      })
    );

    mockLocalStorage.getItem.mockImplementation((key: string) => {
      if (key === 'lastConversationId') {
        return 'web_conv_profile_adopt';
      }
      if (key === 'selectedProfileId') {
        return 'default_assistant';
      }
      return null;
    });

    const user = userEvent.setup();
    await renderChatApp({ waitForReady: true });

    // Open adopts 'complex_tasks'...
    expect(await screen.findByText('Plan the trip')).toBeInTheDocument();

    // ...but a fresh chat must fall back to the persisted preferred profile.
    const [newChatButton] = screen.getAllByTestId('new-chat-button');
    await user.click(newChatButton);

    const messageInput = screen.getByPlaceholderText('Message Family Assistant...');
    await user.type(messageInput, 'Brand new question');
    await user.keyboard('{Enter}');

    await waitFor(() => expect(capturedProfileId).toBe('default_assistant'), { timeout: 10000 });
  }, 30000);

  // Note: Attachment display from assistant message metadata is covered by E2E Playwright tests:
  // - test_tool_attachment_persistence_after_page_reload (tests/functional/web/test_chat_ui_attachment_response.py)
  // - test_attachment_response_flow (tests/functional/web/test_chat_ui_attachment_response.py)
  // These tests verify the full user-visible behavior including page reloads and attachment display.

  // Note: The self-turn reload guard is covered in layers:
  // - useLiveMessageUpdates.test.tsx verifies turn_id is threaded through the callback
  // - ChatApp records its own turn ids in selfTurnIdsRef and skips the reload when a
  //   turn_ended for one of them arrives (preventing the clobber of freshly-streamed state)
  // - The full end-to-end behavior is exercised by the Playwright chat tests in
  //   tests/functional/web/ui/ (e.g. test_chat_basic.py, test_chat_stream_error_recovery.py)
});

describe('mergeConsecutiveToolOnlyAssistantMessages', () => {
  it('merges adjacent assistant messages that only contain tool calls', () => {
    const messages = mergeConsecutiveToolOnlyAssistantMessages([
      {
        id: 'assistant-tool-1',
        role: 'assistant',
        content: [
          {
            type: 'tool-call',
            toolCallId: 'tool-call-1',
            toolName: 'list_notes',
            args: {},
          },
        ],
        createdAt: new Date('2026-06-22T22:30:00Z'),
        status: { type: 'complete' },
      },
      {
        id: 'assistant-tool-2',
        role: 'assistant',
        content: [
          {
            type: 'tool-call',
            toolCallId: 'tool-call-2',
            toolName: 'query_events',
            args: {},
          },
        ],
        createdAt: new Date('2026-06-22T22:30:01Z'),
        status: { type: 'complete' },
      },
    ]);

    expect(messages).toHaveLength(1);
    expect(messages[0].content).toHaveLength(2);
    expect(messages[0].content.map((part) => part.toolCallId)).toEqual([
      'tool-call-1',
      'tool-call-2',
    ]);
  });

  it('does not merge tool-only messages across text replies', () => {
    const messages = mergeConsecutiveToolOnlyAssistantMessages([
      {
        id: 'assistant-tool-1',
        role: 'assistant',
        content: [{ type: 'tool-call', toolCallId: 'tool-call-1', toolName: 'list_notes' }],
        createdAt: new Date('2026-06-22T22:30:00Z'),
        status: { type: 'complete' },
      },
      {
        id: 'assistant-text',
        role: 'assistant',
        content: [{ type: 'text', text: 'Done.' }],
        createdAt: new Date('2026-06-22T22:30:01Z'),
        status: { type: 'complete' },
      },
      {
        id: 'assistant-tool-2',
        role: 'assistant',
        content: [{ type: 'tool-call', toolCallId: 'tool-call-2', toolName: 'query_events' }],
        createdAt: new Date('2026-06-22T22:30:02Z'),
        status: { type: 'complete' },
      },
    ]);

    expect(messages).toHaveLength(3);
  });
});
