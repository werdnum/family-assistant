import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { HttpResponse, http } from 'msw';
import { vi } from 'vitest';
import { mockLocalStorage, resetLocalStorageMock } from '../../test/mocks/localStorageMock';
import { capturedTurnModelTiers } from '../../test/mocks/handlers';
import { server } from '../../test/setup.js';
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

  it('renders the chat interface with an empty, enabled composer', async () => {
    await renderChatApp({ waitForReady: true });

    expect(screen.getByText('Chat')).toBeInTheDocument();
    expect(await screen.findByText('Conversations')).toBeInTheDocument();
    expect(await screen.findByRole('combobox', { name: 'Processing profile' })).toHaveTextContent(
      'Assistant'
    );

    const messageInput = screen.getByTestId('chat-input');
    expect(messageInput).toHaveValue('');
    expect(messageInput).toBeEnabled();
  });

  it('sends and receives messages', async () => {
    const user = userEvent.setup();
    await renderChatApp({ waitForReady: true });

    await user.type(screen.getByPlaceholderText('Message Family Assistant...'), 'Hello there!');
    await user.keyboard('{Enter}');

    // Use the stable test id rather than the placeholder: while the turn runs
    // the composer doubles as the steer input and its placeholder changes.
    await waitForMessageSent(screen.getByTestId('chat-input'));

    expect(await screen.findByTestId('user-message')).toHaveTextContent('Hello there!');
    // The default mock streams this reply word by word for a greeting.
    await waitFor(
      () => {
        expect(screen.getByTestId('assistant-message')).toHaveTextContent(
          'Hi there! How can I help you today?'
        );
      },
      { timeout: 10000 }
    );
  }, 30000);

  it('waits for a new conversation to persist before loading share status', async () => {
    const user = userEvent.setup();
    const statusRequest = vi.fn(() => HttpResponse.json({ active: false }));
    let conversationId = '';
    let turnId = '';
    let finishStream: (() => void) | undefined;
    server.use(
      http.get('/api/v1/chat/conversations/:conversationId/share', statusRequest),
      http.post('/api/v1/chat/turns', async ({ request }) => {
        const body = (await request.json()) as {
          conversation_id: string;
          turn_id: string;
        };
        conversationId = body.conversation_id;
        turnId = body.turn_id;
        return HttpResponse.json({
          conversation_id: conversationId,
          turn_id: turnId,
          first_seq: 0,
        });
      }),
      http.get('/api/v1/chat/conversations/:conversationId/stream', () => {
        const encoder = new TextEncoder();
        const stream = new ReadableStream({
          start(controller) {
            controller.enqueue(
              encoder.encode(
                `event: turn_started\ndata: ${JSON.stringify({ turn_id: turnId, seq: 0 })}\n\n`
              )
            );
            finishStream = () => {
              controller.enqueue(
                encoder.encode(
                  `event: turn_ended\ndata: ${JSON.stringify({ turn_id: turnId, seq: 1, status: 'complete' })}\n\n`
                )
              );
              controller.close();
            };
          },
        });
        return new HttpResponse(stream, { headers: { 'Content-Type': 'text/event-stream' } });
      })
    );
    await renderChatApp({ waitForReady: true });

    await user.type(screen.getByPlaceholderText('Message Family Assistant...'), 'Hello');
    await user.keyboard('{Enter}');
    await waitFor(() => expect(finishStream).toBeDefined());
    expect(statusRequest).not.toHaveBeenCalled();

    finishStream?.();
    await waitFor(() => expect(statusRequest).toHaveBeenCalledOnce());
  });

  // Radix Select relies on pointer-capture and scroll APIs jsdom lacks. Stub them
  // per-test and restore afterwards: leaving them installed would give every
  // later test a defined no-op where jsdom has nothing, silently changing the
  // branch taken by code that feature-detects them.
  const profilePickerStubs: Array<() => void> = [];
  const setupProfilePickerUser = () => {
    const proto = window.HTMLElement.prototype as unknown as Record<string, unknown>;
    for (const method of ['hasPointerCapture', 'releasePointerCapture', 'scrollIntoView']) {
      const hadOwn = Object.prototype.hasOwnProperty.call(proto, method);
      const original = proto[method];
      proto[method] = vi.fn();
      profilePickerStubs.push(() => {
        if (hadOwn) {
          proto[method] = original;
        } else {
          delete proto[method];
        }
      });
    }
    return userEvent.setup({ pointerEventsCheck: 0 });
  };

  afterEach(() => {
    while (profilePickerStubs.length > 0) {
      profilePickerStubs.pop()?.();
    }
  });

  // handleNewChat is the only path that writes a conversation id to localStorage
  // after startup, so a new write is the signal that a fresh conversation began.
  const conversationIdsWritten = (): string[] =>
    mockLocalStorage.setItem.mock.calls
      .filter(([key]) => key === 'lastConversationId')
      .map(([, id]) => id as string);
  const conversationIdWrites = () => conversationIdsWritten().length;

  const switchProfileToResearch = async (user: ReturnType<typeof userEvent.setup>) => {
    await user.click(screen.getByRole('combobox', { name: 'Processing profile' }));
    await user.click(await screen.findByRole('option', { name: /research/i }));
    await waitFor(() => {
      expect(mockLocalStorage.setItem).toHaveBeenCalledWith('selectedProfileId', 'research');
    });
  };

  it('switches profile in place on an unsent conversation, keeping the draft', async () => {
    const user = setupProfilePickerUser();
    await renderChatApp({ waitForReady: true });

    const messageInput = screen.getByPlaceholderText('Message Family Assistant...');
    await user.type(messageInput, 'Draft in progress');

    const writesBeforeSwitch = conversationIdWrites();
    await switchProfileToResearch(user);

    // Nothing has been sent, so there is no context to separate: the switch must
    // not mint a new conversation, and the draft is untouched.
    expect(conversationIdWrites()).toBe(writesBeforeSwitch);
    expect(screen.getByTestId('chat-input')).toHaveValue('Draft in progress');
  });

  it('starts a new conversation but preserves the draft when switching profile mid-thread', async () => {
    const user = setupProfilePickerUser();
    await renderChatApp({ waitForReady: true });

    // Send a message so the conversation holds a real turn, and let it finish:
    // while a turn runs the composer is the steer box, which is deliberately
    // NOT carried over (see the TurnControl steer-leak test).
    const messageInput = screen.getByPlaceholderText('Message Family Assistant...');
    await user.type(messageInput, 'First message');
    await user.keyboard('{Enter}');
    await waitForMessageSent(screen.getByTestId('chat-input'));
    await waitFor(
      () => {
        expect(screen.queryAllByTestId('assistant-message').length).toBeGreaterThan(0);
        expect(screen.getByTestId('chat-input')).toHaveAttribute(
          'placeholder',
          'Message Family Assistant...'
        );
      },
      { timeout: 10000 }
    );

    await user.type(screen.getByTestId('chat-input'), 'Draft in progress');

    const writesBeforeSwitch = conversationIdWrites();
    await switchProfileToResearch(user);

    // The thread has context now, so the switch starts a fresh conversation...
    await waitFor(() => {
      expect(conversationIdWrites()).toBeGreaterThan(writesBeforeSwitch);
    });
    // ...but must not discard the message the user was composing.
    expect(screen.getByTestId('chat-input')).toHaveValue('Draft in progress');
  }, 30000);

  it('starts an empty conversation with a new id from the new chat button', async () => {
    const user = userEvent.setup();
    await renderChatApp({ waitForReady: true });

    await user.type(screen.getByTestId('chat-input'), 'Hello there!');
    await user.keyboard('{Enter}');
    await waitFor(
      () => {
        expect(screen.getByTestId('user-message')).toHaveTextContent('Hello there!');
        expect(screen.getByTestId('assistant-message')).toHaveTextContent(
          'Hi there! How can I help you today?'
        );
        expect(screen.getByTestId('send-button')).toBeInTheDocument();
      },
      { timeout: 10000 }
    );

    const idsBeforeNewChat = conversationIdsWritten();
    const [newChatButton] = screen.getAllByTestId('new-chat-button');
    await user.click(newChatButton);

    await waitFor(() => {
      expect(conversationIdsWritten()).toHaveLength(idsBeforeNewChat.length + 1);
      expect(screen.queryByTestId('user-message')).not.toBeInTheDocument();
      expect(screen.queryByTestId('assistant-message')).not.toBeInTheDocument();
    });
    const newConversationId = conversationIdsWritten()[idsBeforeNewChat.length];
    expect(newConversationId).toMatch(/^web_conv_/);
    expect(idsBeforeNewChat).not.toContain(newConversationId);
  }, 30000);

  it('handles multiple messages in a conversation', async () => {
    const user = userEvent.setup();
    await renderChatApp({ waitForReady: true });

    const messageInput = screen.getByPlaceholderText('Message Family Assistant...');

    await user.type(messageInput, 'First message');
    await user.keyboard('{Enter}');
    await waitForMessageSent(messageInput);

    await waitFor(
      () => {
        const userMessages = screen.getAllByTestId('user-message');
        expect(userMessages).toHaveLength(1);
        expect(userMessages[0]).toHaveTextContent('First message');
        const assistantMessages = screen.getAllByTestId('assistant-message');
        expect(assistantMessages).toHaveLength(1);
        expect(assistantMessages[0]).toHaveTextContent(
          "I received your message and I'm here to help!"
        );
      },
      { timeout: 10000 }
    );

    // Ensure input is ready for the next message
    await waitFor(() => {
      const input = screen.getByPlaceholderText('Message Family Assistant...');
      expect(input).toBeEnabled();
      expect(input).toHaveValue('');
    });

    // The send action appears only after the stream has finished.
    await screen.findByTestId('send-button');

    // Get a fresh reference and send second message
    const input2 = screen.getByPlaceholderText('Message Family Assistant...');
    await user.click(input2);
    await user.type(input2, 'Second message');

    await waitFor(() => expect(screen.getByTestId('send-button')).toBeEnabled());
    await user.keyboard('{Enter}');

    // Wait for second message to be sent
    await waitForMessageSent(input2);

    await waitFor(
      () => {
        const userMessages = screen.getAllByTestId('user-message');
        expect(userMessages).toHaveLength(2);
        expect(userMessages[0]).toHaveTextContent('First message');
        expect(userMessages[1]).toHaveTextContent('Second message');
        const assistantMessages = screen.getAllByTestId('assistant-message');
        expect(assistantMessages).toHaveLength(2);
        expect(assistantMessages[1]).toHaveTextContent(
          "I received your message and I'm here to help!"
        );
      },
      { timeout: 10000 }
    );
  }, 20000);

  describe('on a mobile viewport', () => {
    let desktopWidth: number;
    let desktopHeight: number;

    const setViewport = (width: number, height: number) => {
      Object.defineProperty(window, 'innerWidth', {
        writable: true,
        configurable: true,
        value: width,
      });
      Object.defineProperty(window, 'innerHeight', {
        writable: true,
        configurable: true,
        value: height,
      });
    };

    beforeEach(() => {
      desktopWidth = window.innerWidth;
      desktopHeight = window.innerHeight;
      setViewport(375, 667);
    });

    afterEach(() => {
      setViewport(desktopWidth, desktopHeight);
    });

    it('opens on the chat view with a back button instead of the desktop sidebar', async () => {
      await renderChatApp({ waitForReady: true });

      expect(screen.getByText('Chat')).toBeInTheDocument();
      expect(screen.getByLabelText('Back to conversations')).toBeInTheDocument();
      expect(screen.queryByLabelText('Toggle sidebar')).not.toBeInTheDocument();
      expect(screen.queryByText('Conversations')).not.toBeInTheDocument();
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

    expect(await screen.findByText('Web conversation message')).toBeInTheDocument();
    expect(screen.getByText('Another web message')).toBeInTheDocument();

    // Should NOT see telegram conversations (they should be filtered out by the interface_type filter)
    expect(screen.queryByText('Telegram message that should not appear')).not.toBeInTheDocument();
  });

  it('keeps the conversation list on screen while it refreshes after a send', async () => {
    const user = userEvent.setup();
    let listRequests = 0;
    let releaseRefresh: (() => void) | undefined;
    server.use(
      http.get('/api/v1/chat/conversations', async ({ request }) => {
        const url = new URL(request.url);
        if (url.searchParams.has('q')) {
          return HttpResponse.json({ conversations: [], count: 0 });
        }
        listRequests += 1;
        if (listRequests > 1 && !releaseRefresh) {
          await new Promise<void>((resolve) => {
            releaseRefresh = resolve;
          });
        }
        return HttpResponse.json({
          conversations: [
            {
              conversation_id: 'web_conv_existing',
              last_message: 'An earlier conversation',
              last_timestamp: '2025-01-01T10:00:00Z',
              message_count: 2,
            },
          ],
          count: 1,
        });
      })
    );

    await renderChatApp({ waitForReady: true });
    expect(await screen.findByText('An earlier conversation')).toBeInTheDocument();

    await user.type(screen.getByPlaceholderText('Message Family Assistant...'), 'Hello there!');
    await user.keyboard('{Enter}');

    await waitFor(() => expect(releaseRefresh).toBeDefined(), { timeout: 10000 });
    expect(screen.getByText('An earlier conversation')).toBeInTheDocument();
    expect(document.querySelector('[data-loading-indicator="true"]')).toBeNull();

    releaseRefresh?.();
  }, 30000);

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

  // History rows carry no per-part status, so a tool call whose result was
  // never recorded is finished history, not a tool still running.
  it('keeps a historical tool call without a result collapsed', async () => {
    server.use(
      http.get('/api/v1/chat/conversations/:conversationId/messages', ({ params }) => {
        if (params.conversationId !== 'web_conv_orphan_tool_call') {
          return HttpResponse.json({ messages: [] });
        }

        return HttpResponse.json({
          messages: [
            {
              internal_id: 301,
              role: 'user',
              content: 'Check the weather',
              timestamp: '2026-06-24T12:00:00Z',
            },
            {
              internal_id: 302,
              role: 'assistant',
              content: 'Checking.',
              timestamp: '2026-06-24T12:00:01Z',
              tool_calls: [
                {
                  id: 'call_orphan',
                  type: 'function',
                  function: { name: 'get_weather', arguments: '{"city":"Sydney"}' },
                },
              ],
            },
          ],
        });
      })
    );
    mockLocalStorage.getItem.mockImplementation((key: string) =>
      key === 'lastConversationId' ? 'web_conv_orphan_tool_call' : null
    );

    await renderChatApp({ waitForReady: true });

    expect(await screen.findByText('Checking.')).toBeInTheDocument();
    expect(await screen.findByTestId('tool-group-content')).toHaveAttribute('data-state', 'closed');
  });

  it('shows image attachments from history inline in the assistant reply', async () => {
    const { server } = await import('../../test/setup.js');
    const { http, HttpResponse } = await import('msw');

    server.use(
      http.get('/api/v1/chat/conversations/:conversationId/messages', ({ params }) => {
        if (params.conversationId !== 'web_conv_assistant_image') {
          return HttpResponse.json({ messages: [] });
        }

        return HttpResponse.json({
          messages: [
            {
              internal_id: 201,
              role: 'user',
              content: 'Show me the chart again',
              timestamp: '2026-06-24T12:00:00Z',
            },
            {
              internal_id: 202,
              role: 'assistant',
              content: 'Here it is.',
              timestamp: '2026-06-24T12:00:01Z',
              attachments: [
                {
                  attachment_id: 'chart-image',
                  type: 'tool_result',
                  description: 'Revenue chart',
                  mime_type: 'image/png',
                  content_url: '/api/attachments/chart-image',
                },
              ],
            },
          ],
        });
      })
    );
    mockLocalStorage.getItem.mockImplementation((key: string) =>
      key === 'lastConversationId' ? 'web_conv_assistant_image' : null
    );

    await renderChatApp({ waitForReady: true });

    expect(await screen.findByText('Here it is.')).toBeInTheDocument();
    // Visible without expanding the attachments tool group.
    const image = await screen.findByTestId('response-image');
    expect(image).toHaveAttribute('src', '/api/attachments/chart-image');
    expect(image).toHaveAttribute('alt', 'Revenue chart');
    expect(screen.getByTestId('tool-group-content')).toHaveAttribute('data-state', 'closed');

    // Clicking the inline image opens it full-screen rather than navigating away.
    await userEvent.click(screen.getByTestId('response-image-trigger'));
    const lightboxImage = await screen.findByTestId('image-lightbox-image');
    expect(lightboxImage).toHaveAttribute('src', '/api/attachments/chart-image');
    expect(screen.getByTestId('image-lightbox-caption')).toHaveTextContent('Revenue chart');
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

describe('ChatApp intelligence tier selection', () => {
  beforeEach(() => {
    resetLocalStorageMock();
    vi.clearAllMocks();
    capturedTurnModelTiers.length = 0;
  });

  // Radix Select relies on pointer-capture and scroll APIs jsdom lacks. Stub
  // them per-test and restore afterwards, so a later test doesn't silently get
  // a defined no-op where jsdom has nothing.
  const selectStubs: Array<() => void> = [];
  const setupSelectUser = () => {
    const proto = window.HTMLElement.prototype as unknown as Record<string, unknown>;
    for (const method of ['hasPointerCapture', 'releasePointerCapture', 'scrollIntoView']) {
      const hadOwn = Object.prototype.hasOwnProperty.call(proto, method);
      const original = proto[method];
      proto[method] = vi.fn();
      selectStubs.push(() => {
        if (hadOwn) {
          proto[method] = original;
        } else {
          delete proto[method];
        }
      });
    }
    return userEvent.setup({ pointerEventsCheck: 0 });
  };

  afterEach(() => {
    while (selectStubs.length > 0) {
      selectStubs.pop()?.();
    }
  });

  const chooseTier = async (user: ReturnType<typeof userEvent.setup>, name: RegExp) => {
    // The control appears with the profile list, which loads after the composer.
    await user.click(await screen.findByRole('combobox', { name: 'Intelligence' }));
    await user.click(await screen.findByRole('option', { name }));
  };

  const sendMessage = async (user: ReturnType<typeof userEvent.setup>, text: string) => {
    const input = screen.getByTestId('chat-input');
    await user.click(input);
    await user.type(input, text);
    await user.keyboard('{Enter}');
    await waitForMessageSent(input);
  };

  // The turn is over, and the composer takes a new message instead of steering
  // it into the running one, exactly when the thread stops running: that is the
  // state Enter submits in, and the state that puts the send button back and
  // returns the placeholder to its idle text.
  const waitForTurnToSettle = async (expectedTurns: number) => {
    await waitFor(
      () => {
        expect(screen.queryAllByTestId('assistant-message').length).toBe(expectedTurns);
        expect(screen.getByTestId('send-button')).toBeInTheDocument();
        expect(screen.getByTestId('chat-input')).toHaveAttribute(
          'placeholder',
          'Message Family Assistant...'
        );
      },
      { timeout: 10000 }
    );
  };

  it('sends no model_tier while the profile default is in effect', async () => {
    const user = userEvent.setup();
    await renderChatApp({ waitForReady: true });

    await sendMessage(user, 'Hello there!');

    await waitFor(() => expect(capturedTurnModelTiers).toEqual([undefined]), { timeout: 10000 });
  }, 30000);

  it('spends a chosen tier on the next message and then returns to the default', async () => {
    const user = setupSelectUser();
    await renderChatApp({ waitForReady: true });

    await chooseTier(user, /Deep/);
    await sendMessage(user, 'A hard question');
    await waitFor(() => expect(capturedTurnModelTiers).toEqual(['deep']), { timeout: 10000 });

    // The control is back on the profile default, with nothing left to pin.
    expect(screen.getByRole('combobox', { name: 'Intelligence' })).toHaveTextContent('Standard');
    expect(screen.queryByTestId('intelligence-pin')).not.toBeInTheDocument();

    await waitForTurnToSettle(1);
    await sendMessage(user, 'An easy follow-up');
    await waitFor(() => expect(capturedTurnModelTiers).toEqual(['deep', undefined]), {
      timeout: 10000,
    });
  }, 60000);

  it('keeps a pinned tier across messages until a new chat clears it', async () => {
    const user = setupSelectUser();
    await renderChatApp({ waitForReady: true });

    await chooseTier(user, /Deep/);
    await user.click(screen.getByTestId('intelligence-pin'));
    expect(screen.getByTestId('intelligence-pin')).toHaveAttribute('aria-pressed', 'true');

    await sendMessage(user, 'A hard question');
    await waitFor(() => expect(capturedTurnModelTiers).toEqual(['deep']), { timeout: 10000 });
    expect(screen.getByRole('combobox', { name: 'Intelligence' })).toHaveTextContent('Deep');

    await waitForTurnToSettle(1);
    await sendMessage(user, 'Another hard question');
    await waitFor(() => expect(capturedTurnModelTiers).toEqual(['deep', 'deep']), {
      timeout: 10000,
    });

    await waitForTurnToSettle(2);
    const [newChatButton] = screen.getAllByTestId('new-chat-button');
    await user.click(newChatButton);

    await waitFor(() => {
      expect(screen.getByRole('combobox', { name: 'Intelligence' })).toHaveTextContent('Standard');
    });
    expect(screen.queryByTestId('intelligence-pin')).not.toBeInTheDocument();

    await sendMessage(user, 'A brand new question');
    await waitFor(() => expect(capturedTurnModelTiers).toEqual(['deep', 'deep', undefined]), {
      timeout: 10000,
    });
  }, 90000);

  it('clears a pinned tier when the profile changes', async () => {
    const user = setupSelectUser();
    await renderChatApp({ waitForReady: true });

    await chooseTier(user, /Deep/);
    await user.click(screen.getByTestId('intelligence-pin'));

    await user.click(screen.getByRole('combobox', { name: 'Processing profile' }));
    await user.click(await screen.findByRole('option', { name: /research/i }));

    // The research profile pins its model, so there is no control to show at
    // all — and the pinned choice made under the assistant is gone with it.
    await waitFor(() => {
      expect(screen.queryByRole('combobox', { name: 'Intelligence' })).not.toBeInTheDocument();
    });

    await sendMessage(user, 'Research this');
    await waitFor(() => expect(capturedTurnModelTiers).toEqual([undefined]), { timeout: 10000 });
  }, 30000);

  it('does not start a new conversation when the tier changes', async () => {
    const user = setupSelectUser();
    await renderChatApp({ waitForReady: true });

    // Give the conversation a real turn, so a *profile* change here would mint
    // a new conversation: the tier change must not.
    await sendMessage(user, 'First message');
    await waitForTurnToSettle(1);

    const writesBeforeTierChange = mockLocalStorage.setItem.mock.calls.filter(
      ([key]) => key === 'lastConversationId'
    ).length;
    await chooseTier(user, /Deep/);

    expect(
      mockLocalStorage.setItem.mock.calls.filter(([key]) => key === 'lastConversationId').length
    ).toBe(writesBeforeTierChange);
    expect(screen.getByTestId('chat-input')).toHaveValue('');
    expect(mockLocalStorage.setItem).not.toHaveBeenCalledWith(
      expect.stringContaining('Tier'),
      expect.anything()
    );
  }, 60000);

  it('names the tier that served the reply on the assistant message', async () => {
    const user = setupSelectUser();
    await renderChatApp({ waitForReady: true });

    await chooseTier(user, /Deep/);
    await sendMessage(user, 'A hard question');

    const badge = await screen.findByTestId('model-tier-badge', undefined, { timeout: 10000 });
    expect(badge).toHaveTextContent('Deep');
    // The exact model stays available without dominating the bubble.
    expect(badge).toHaveAttribute('title', 'Deep · mock-model-1');
    // ...and an explicit choice is distinguishable from a routed one.
    expect(badge).toHaveTextContent('chosen');
  }, 30000);
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
