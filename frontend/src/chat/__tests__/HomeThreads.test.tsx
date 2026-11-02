import { act, fireEvent, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { HttpResponse, http } from 'msw';
import { vi } from 'vitest';
import { mockLocalStorage, resetLocalStorageMock } from '../../test/mocks/localStorageMock';
import { server } from '../../test/setup.js';
import { renderChatApp } from '../../test/utils/renderChatApp';
import type { ThreadSummary } from '../types';
import { MARK_READ_DELAY_MS } from '../useMarkRead';

const HOME = 'web_conv_home';

function thread(overrides: Partial<ThreadSummary> & { conversation_id: string }): ThreadSummary {
  return {
    home_conversation_id: HOME,
    title: 'Hot water system quotes',
    profile_id: 'research',
    state: 'waiting',
    source_tool_call_id: null,
    seed_request: 'Find three plumbers and get quotes.',
    preview: 'Gas or heat pump?',
    created_at: '2026-10-18T09:00:00Z',
    updated_at: '2026-10-18T09:05:00Z',
    ...overrides,
  };
}

const QUOTES = thread({ conversation_id: 'web_conv_quotes', source_tool_call_id: 'call_quotes' });

interface ThreadsServer {
  threads: ThreadSummary[];
  conversationListUrls: string[];
  /** Each read marker the client moved, in order. */
  reads: { conversationId: string; throughMessageId: number | null }[];
  /** Messages appended to Home after its start_thread turn. */
  homeExtra: Record<string, unknown>[];
}

/** Threads on, with Home holding a start_thread call whose thread is QUOTES. */
function serveThreads(threads: ThreadSummary[] = [QUOTES]): ThreadsServer {
  const state: ThreadsServer = { threads, conversationListUrls: [], reads: [], homeExtra: [] };
  server.use(
    http.get('/api/v1/chat/home', () =>
      HttpResponse.json({ enabled: true, conversation_id: HOME })
    ),
    http.post('/api/v1/chat/conversations/:conversationId/read', async ({ params, request }) => {
      const conversationId = String(params.conversationId);
      const body = (await request.json()) as { through_message_id?: number | null };
      state.reads.push({ conversationId, throughMessageId: body.through_message_id ?? null });
      state.threads = state.threads.map((entry) =>
        entry.conversation_id === conversationId ? { ...entry, unread_count: 0 } : entry
      );
      return HttpResponse.json({ conversation_id: conversationId, unread_count: 0 });
    }),
    http.post('/api/v1/chat/home/threads/:conversationId/done', ({ params }) => {
      const conversationId = String(params.conversationId);
      state.threads = state.threads.map((entry) =>
        entry.conversation_id === conversationId
          ? { ...entry, state: 'done', closed_by: 'user', closed_at: '2026-10-18T10:00:00Z' }
          : entry
      );
      const closed = state.threads.find((entry) => entry.conversation_id === conversationId);
      return closed ? HttpResponse.json(closed) : new HttpResponse(null, { status: 404 });
    }),
    http.get('/api/v1/chat/home/threads', () =>
      HttpResponse.json({ home_conversation_id: HOME, threads: state.threads })
    ),
    http.get('/api/v1/chat/conversations', ({ request }) => {
      state.conversationListUrls.push(request.url);
      return HttpResponse.json({
        conversations: [
          {
            conversation_id: HOME,
            last_message: 'Home preview',
            last_timestamp: '2026-10-18T09:10:00Z',
            message_count: 4,
            kind: 'home',
          },
          {
            conversation_id: 'web_conv_old',
            last_message: 'Kitchen reno budget',
            last_timestamp: '2026-10-01T09:00:00Z',
            message_count: 2,
            kind: 'conversation',
          },
        ],
        count: 2,
      });
    }),
    http.get('/api/v1/chat/conversations/:conversationId/messages', ({ params }) => {
      const conversationId = String(params.conversationId);
      if (conversationId === HOME) {
        return HttpResponse.json({
          conversation_kind: 'home',
          latest_user_profile_id: 'default_assistant',
          messages: [
            {
              internal_id: 1,
              role: 'user',
              content: 'Can you find a plumber and get a couple of quotes?',
              timestamp: '2026-10-18T09:00:00Z',
            },
            {
              internal_id: 2,
              role: 'assistant',
              content: '',
              timestamp: '2026-10-18T09:00:01Z',
              tool_calls: [
                {
                  id: 'call_lookup',
                  type: 'function',
                  function: { name: 'search_documents', arguments: '{"query":"plumber"}' },
                },
                {
                  id: 'call_quotes',
                  type: 'function',
                  function: {
                    name: 'start_thread',
                    arguments: JSON.stringify({
                      target_service_id: 'research',
                      title: 'Hot water system quotes',
                      request: 'Find three plumbers and get quotes.',
                    }),
                  },
                },
                {
                  id: 'call_refused',
                  type: 'function',
                  function: {
                    name: 'start_thread',
                    arguments: JSON.stringify({
                      target_service_id: 'coder',
                      title: 'Refused job',
                      request: 'Do something.',
                    }),
                  },
                },
              ],
            },
            {
              internal_id: 3,
              role: 'tool',
              tool_call_id: 'call_lookup',
              content: 'Two plumbers found.',
              timestamp: '2026-10-18T09:00:02Z',
            },
            {
              internal_id: 4,
              role: 'tool',
              tool_call_id: 'call_quotes',
              content: 'Started the thread.',
              timestamp: '2026-10-18T09:00:02Z',
            },
            {
              internal_id: 5,
              role: 'tool',
              tool_call_id: 'call_refused',
              content: 'Error: not allowed',
              timestamp: '2026-10-18T09:00:02Z',
            },
            {
              internal_id: 6,
              role: 'assistant',
              content: "On it. I've started a thread for it.",
              timestamp: '2026-10-18T09:00:03Z',
            },
            ...state.homeExtra,
          ],
        });
      }
      const opened = state.threads.find((entry) => entry.conversation_id === conversationId);
      if (opened) {
        return HttpResponse.json({
          conversation_kind: 'thread',
          thread: opened,
          latest_user_profile_id: opened.profile_id,
          messages: [
            {
              internal_id: 10,
              role: 'assistant',
              content: 'Gas or heat pump?',
              timestamp: '2026-10-18T09:05:00Z',
            },
          ],
        });
      }
      return HttpResponse.json({
        conversation_kind: 'conversation',
        messages: [
          {
            internal_id: 20,
            role: 'user',
            content: `Hello from ${conversationId}`,
            timestamp: '2026-10-01T09:00:00Z',
          },
        ],
      });
    })
  );
  return state;
}

describe('Home and threads', () => {
  beforeEach(() => {
    resetLocalStorageMock();
    vi.clearAllMocks();
    window.history.replaceState({}, '', '/');
    document.documentElement.removeAttribute('data-app-ready');
  });

  it('lands on Home instead of the last conversation, on the default profile', async () => {
    serveThreads();
    mockLocalStorage.getItem.mockImplementation((key: string) => {
      if (key === 'lastConversationId') {
        return 'web_conv_old';
      }
      return key === 'selectedProfileId' ? 'research' : null;
    });

    await renderChatApp({ waitForReady: true });

    expect(await screen.findByText("On it. I've started a thread for it.")).toBeInTheDocument();
    expect(screen.queryByText('Hello from web_conv_old')).not.toBeInTheDocument();
    expect(screen.getByTestId('conversation-title')).toHaveTextContent('Home');
    expect(window.location.search).toBe(`?conversation_id=${HOME}`);
    const picker = await screen.findByRole('combobox', { name: 'Processing profile' });
    await waitFor(() => expect(picker).toHaveTextContent('Assistant'));
    expect(picker).toBeDisabled();
  });

  it('shows a start_thread call as a card that stays out of the collapsed tool group', async () => {
    serveThreads();

    await renderChatApp({ waitForReady: true });

    const card = await screen.findByTestId('thread-card');
    expect(card).toHaveAttribute('data-thread-state', 'waiting');
    expect(within(card).getByTestId('thread-card-title')).toHaveTextContent(
      'Hot water system quotes'
    );
    expect(within(card).getByTestId('thread-card-profile')).toHaveTextContent('Research');
    expect(within(card).getByTestId('thread-card-preview')).toHaveTextContent('Gas or heat pump?');
    expect(card.closest('[data-testid="tool-group-content"]')).toBeNull();
    // The other calls still fold into their groups.
    expect(screen.getAllByTestId('tool-group-content').length).toBeGreaterThan(0);
  });

  it('renders a start_thread call with no thread as an ordinary tool call', async () => {
    serveThreads();

    await renderChatApp({ waitForReady: true });

    await screen.findByTestId('thread-card');
    const standalone = screen.getAllByTestId('standalone-tool-call');
    expect(standalone).toHaveLength(2);
    expect(within(standalone[1]).queryByTestId('thread-card')).toBeNull();
    expect(within(standalone[1]).getByTestId('tool-call')).toBeInTheDocument();
  });

  it('opens a thread from its card and returns to Home from the breadcrumb', async () => {
    const user = userEvent.setup();
    serveThreads();

    await renderChatApp({ waitForReady: true });
    await user.click(await screen.findByTestId('thread-card'));

    const banner = await screen.findByTestId('thread-banner');
    expect(window.location.search).toBe(`?conversation_id=${HOME}&thread=web_conv_quotes`);
    expect(within(banner).getByTestId('thread-origin')).toHaveTextContent(
      'Started from Home · Assistant asked: “Find three plumbers and get quotes.”'
    );
    expect(screen.getByTestId('conversation-title')).toHaveTextContent('Hot water system quotes');
    const picker = screen.getByRole('combobox', { name: 'Processing profile' });
    await waitFor(() => expect(picker).toHaveTextContent('Research'));
    expect(picker).toBeDisabled();

    await user.click(within(banner).getByTestId('thread-breadcrumb-home'));

    expect(await screen.findByText("On it. I've started a thread for it.")).toBeInTheDocument();
    expect(screen.queryByTestId('thread-banner')).not.toBeInTheDocument();
    expect(window.location.search).toBe(`?conversation_id=${HOME}`);
  });

  it('shows a working thread as working', async () => {
    serveThreads([{ ...QUOTES, state: 'working' }]);
    window.history.replaceState({}, '', `/chat?conversation_id=${HOME}&thread=web_conv_quotes`);

    await renderChatApp({ waitForReady: true });

    expect(await screen.findByTestId('thread-working-indicator')).toBeInTheDocument();
    expect(screen.getByTestId('thread-banner')).toHaveAttribute('data-thread-state', 'working');
  });

  it('groups the sidebar into Home, threads by state, and earlier conversations', async () => {
    const threads = serveThreads([
      QUOTES,
      thread({ conversation_id: 'web_conv_flights', title: 'Flights', state: 'working' }),
      thread({ conversation_id: 'web_conv_rego', title: 'Car rego', state: 'failed' }),
      thread({ conversation_id: 'web_conv_tyres', title: 'New tyres', state: 'done' }),
    ]);

    await renderChatApp({ waitForReady: true });

    expect(
      within(await screen.findByTestId('thread-group-done')).getByTestId(
        'thread-item-web_conv_tyres'
      )
    ).toBeInTheDocument();
    const homeItem = await screen.findByTestId('home-item');
    expect(homeItem).toHaveAttribute('data-conversation-id', HOME);
    expect(
      within(await screen.findByTestId('thread-group-waiting')).getByTestId(
        'thread-item-web_conv_quotes'
      )
    ).toHaveTextContent('Gas or heat pump?');
    expect(
      within(screen.getByTestId('thread-group-working')).getByTestId('thread-item-web_conv_flights')
    ).toBeInTheDocument();
    expect(
      within(screen.getByTestId('thread-group-failed')).getByTestId('thread-item-web_conv_rego')
    ).toBeInTheDocument();
    expect(screen.getByText('Earlier')).toBeInTheDocument();
    expect(
      screen
        .getAllByTestId(/^conversation-item-/)
        .map((item) => item.getAttribute('data-conversation-id'))
    ).toEqual(['web_conv_old']);
    await waitFor(() =>
      expect(threads.conversationListUrls.some((url) => url.includes('kind=ordinary'))).toBe(true)
    );
  });

  it('says when Home could not be loaded, and lands on it once a retry succeeds', async () => {
    const user = userEvent.setup();
    serveThreads();
    let homeAvailable = false;
    server.use(
      http.get('/api/v1/chat/home', () =>
        homeAvailable
          ? HttpResponse.json({ enabled: true, conversation_id: HOME })
          : new HttpResponse(null, { status: 500 })
      )
    );
    const consoleError = vi.spyOn(console, 'error').mockImplementation(() => {});

    await renderChatApp({ waitForReady: true });

    const alert = await screen.findByTestId('home-lookup-failed');
    expect(screen.queryByTestId('home-item')).not.toBeInTheDocument();

    homeAvailable = true;
    await user.click(within(alert).getByRole('button', { name: 'Retry' }));

    expect(await screen.findByText("On it. I've started a thread for it.")).toBeInTheDocument();
    expect(screen.queryByTestId('home-lookup-failed')).not.toBeInTheDocument();
    expect(screen.getByTestId('home-item')).toBeInTheDocument();
    consoleError.mockRestore();
  });

  it('says when the thread list could not be loaded, and shows it once a retry succeeds', async () => {
    const user = userEvent.setup();
    const threads = serveThreads();
    let threadsAvailable = false;
    server.use(
      http.get('/api/v1/chat/home/threads', () =>
        threadsAvailable
          ? HttpResponse.json({ home_conversation_id: HOME, threads: threads.threads })
          : new HttpResponse(null, { status: 500 })
      )
    );
    const consoleError = vi.spyOn(console, 'error').mockImplementation(() => {});

    await renderChatApp({ waitForReady: true });

    const alert = await screen.findByTestId('threads-load-failed');
    threadsAvailable = true;
    await user.click(within(alert).getByRole('button', { name: 'Retry' }));

    expect(await screen.findByTestId('thread-item-web_conv_quotes')).toBeInTheDocument();
    expect(screen.queryByTestId('threads-load-failed')).not.toBeInTheDocument();
    consoleError.mockRestore();
  });

  it('keeps a draft typed before Home was known', async () => {
    const user = userEvent.setup();
    serveThreads();
    let releaseHome: () => void = () => {};
    const homeGate = new Promise<void>((resolve) => {
      releaseHome = resolve;
    });
    server.use(
      http.get('/api/v1/chat/home', async () => {
        await homeGate;
        return HttpResponse.json({ enabled: true, conversation_id: HOME });
      })
    );

    await renderChatApp({ waitForReady: true });
    await user.type(screen.getByRole('textbox'), 'Is the bin out?');
    releaseHome();

    expect(await screen.findByText("On it. I've started a thread for it.")).toBeInTheDocument();
    expect(screen.getByRole('textbox')).toHaveValue('Is the bin out?');
  });

  it('stays in a conversation sent before Home was known, and links to it', async () => {
    const user = userEvent.setup();
    serveThreads();
    let releaseHome: () => void = () => {};
    const homeGate = new Promise<void>((resolve) => {
      releaseHome = resolve;
    });
    server.use(
      http.get('/api/v1/chat/home', async () => {
        await homeGate;
        return HttpResponse.json({ enabled: true, conversation_id: HOME });
      })
    );

    await renderChatApp({ waitForReady: true });
    await user.type(screen.getByRole('textbox'), 'Is the bin out?');
    await user.keyboard('{Enter}');
    await waitFor(() => expect(window.location.search).toMatch(/^\?conversation_id=web_conv_/));
    const sentTo = window.location.search;
    releaseHome();

    expect(await screen.findByTestId('home-item')).toBeInTheDocument();
    expect(window.location.search).toBe(sentTo);
    expect(screen.getByTestId('user-message')).toHaveTextContent('Is the bin out?');
    expect(screen.queryByText("On it. I've started a thread for it.")).not.toBeInTheDocument();
  });

  it('moves a thread between groups when a turn ends and the list is refetched', async () => {
    const user = userEvent.setup();
    const threads = serveThreads();

    await renderChatApp({ waitForReady: true });
    expect(await screen.findByTestId('thread-group-waiting')).toBeInTheDocument();

    threads.threads = [{ ...QUOTES, state: 'working' }];
    await user.click(screen.getByTestId('thread-item-web_conv_quotes'));
    await screen.findByTestId('thread-banner');
    await user.type(screen.getByRole('textbox'), 'Either is fine');
    await user.keyboard('{Enter}');

    expect(await screen.findByTestId('thread-group-working')).toBeInTheDocument();
    expect(screen.queryByTestId('thread-group-waiting')).not.toBeInTheDocument();
  });
});

describe('Thread state, unread and echoes', () => {
  beforeEach(() => {
    resetLocalStorageMock();
    vi.clearAllMocks();
    window.history.replaceState({}, '', '/');
    document.documentElement.removeAttribute('data-app-ready');
  });

  it.each([
    { name: 'working', update: { state: 'working' }, status: 'Working' },
    { name: 'waiting', update: { state: 'waiting' }, status: 'Waiting on you' },
    {
      name: 'done by the assistant',
      update: { state: 'done', closed_by: 'assistant', preview: 'Booked Acme for Tuesday.' },
      status: 'Done',
    },
    {
      name: 'done by the user',
      update: { state: 'done', closed_by: 'user' },
      status: 'You marked this done',
    },
    { name: 'failed', update: { state: 'failed' }, status: 'Failed' },
  ] as { name: string; update: Partial<ThreadSummary>; status: string }[])(
    'shows a $name thread on its card and in the sidebar',
    async ({ update, status }) => {
      const quotes = { ...QUOTES, ...update };
      serveThreads([quotes]);

      await renderChatApp({ waitForReady: true });

      const card = await screen.findByTestId('thread-card');
      expect(card).toHaveAttribute('data-thread-state', quotes.state);
      expect(within(card).getByTestId('thread-card-state')).toHaveTextContent(status);
      expect(within(card).getByTestId('thread-card-preview')).toHaveTextContent(
        quotes.preview ?? ''
      );
      const row = within(screen.getByTestId(`thread-group-${quotes.state}`)).getByTestId(
        'thread-item-web_conv_quotes'
      );
      if (update.closed_by === 'user') {
        expect(within(row).getByTestId('thread-item-status')).toHaveTextContent(
          'You marked this done'
        );
      } else {
        expect(within(row).queryByTestId('thread-item-status')).toBeNull();
      }
    }
  );

  it('shows unread counts on cards and sidebar rows, and how many threads wait on you', async () => {
    serveThreads([
      { ...QUOTES, unread_count: 3 },
      thread({ conversation_id: 'web_conv_flights', title: 'Flights', state: 'working' }),
      thread({ conversation_id: 'web_conv_rego', title: 'Car rego', unread_count: 1 }),
    ]);

    await renderChatApp({ waitForReady: true });

    const card = await screen.findByTestId('thread-card');
    expect(within(card).getByTestId('thread-unread-badge')).toHaveTextContent('3');
    expect(
      within(screen.getByTestId('thread-item-web_conv_quotes')).getByTestId('thread-unread-badge')
    ).toHaveTextContent('3');
    expect(
      within(screen.getByTestId('thread-item-web_conv_flights')).queryByTestId(
        'thread-unread-badge'
      )
    ).toBeNull();
    expect(screen.getByTestId('home-waiting-count')).toHaveTextContent('2 waiting');
  });

  it('lists working threads first and a quiet thread apart from those waiting', async () => {
    serveThreads([
      QUOTES,
      thread({ conversation_id: 'web_conv_old', title: 'Old', state: 'waiting', quiet: true }),
      thread({ conversation_id: 'web_conv_flights', title: 'Flights', state: 'working' }),
    ]);

    await renderChatApp({ waitForReady: true });

    expect(
      within(await screen.findByTestId('thread-group-quiet')).getByTestId(
        'thread-item-web_conv_old'
      )
    ).toBeInTheDocument();
    expect(
      screen.getAllByTestId(/^thread-group-/).map((group) => group.getAttribute('data-testid'))
    ).toEqual(['thread-group-working', 'thread-group-waiting', 'thread-group-quiet']);
    expect(screen.getByTestId('home-waiting-count')).toHaveTextContent('1 waiting');
  });

  it('marks Home read through its newest message once it is on screen', async () => {
    const threads = serveThreads();

    await renderChatApp({ waitForReady: true });

    await waitFor(
      () => expect(threads.reads).toEqual([{ conversationId: HOME, throughMessageId: 6 }]),
      {
        timeout: 3000,
      }
    );
  });

  it('opening a thread marks it read and clears its badge', async () => {
    const user = userEvent.setup();
    const threads = serveThreads([{ ...QUOTES, unread_count: 2 }]);

    await renderChatApp({ waitForReady: true });
    const card = await screen.findByTestId('thread-card');
    expect(within(card).getByTestId('thread-unread-badge')).toHaveTextContent('2');

    await user.click(card);

    const row = screen.getByTestId('thread-item-web_conv_quotes');
    await waitFor(() => expect(within(row).queryByTestId('thread-unread-badge')).toBeNull(), {
      timeout: 3000,
    });
    expect(threads.reads).toContainEqual({
      conversationId: 'web_conv_quotes',
      throughMessageId: 10,
    });
  });

  it('holds off marking a thread read while the reader is scrolled up', async () => {
    // Real time still passes, so loading works as usual; the debounce is
    // jumped over explicitly below.
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      const fetchSpy = vi.spyOn(globalThis, 'fetch');
      // The read request goes out synchronously when the debounce fires.
      const readRequests = () =>
        fetchSpy.mock.calls.filter(([input]) =>
          String(input instanceof Request ? input.url : input).endsWith(
            '/conversations/web_conv_quotes/read'
          )
        );
      const threads = serveThreads();
      window.history.replaceState({}, '', `/chat?conversation_id=${HOME}&thread=web_conv_quotes`);

      await renderChatApp({ waitForReady: true });
      await screen.findByText('Gas or heat pump?');
      const viewport = screen.getByTestId('thread-viewport');
      const scrollTo = (scrollTop: number) => {
        Object.defineProperty(viewport, 'scrollHeight', { configurable: true, value: 2000 });
        Object.defineProperty(viewport, 'clientHeight', { configurable: true, value: 500 });
        viewport.scrollTop = scrollTop;
        fireEvent.scroll(viewport);
      };
      // Opened at the newest message, then scrolled up to older ones.
      scrollTo(1500);
      scrollTo(0);

      await act(async () => {
        await vi.advanceTimersByTimeAsync(MARK_READ_DELAY_MS * 2);
      });
      expect(readRequests()).toHaveLength(0);

      scrollTo(1500);
      await act(async () => {
        await vi.advanceTimersByTimeAsync(MARK_READ_DELAY_MS);
      });

      expect(readRequests()).toHaveLength(1);
      await waitFor(() =>
        expect(threads.reads).toEqual([{ conversationId: 'web_conv_quotes', throughMessageId: 10 }])
      );
    } finally {
      vi.useRealTimers();
    }
  });

  it.each([
    ['waiting', true],
    ['failed', true],
    ['working', false],
  ] as const)('offers Mark done on a %s thread: %s', async (state, offered) => {
    serveThreads([{ ...QUOTES, state }]);
    window.history.replaceState({}, '', `/chat?conversation_id=${HOME}&thread=web_conv_quotes`);

    await renderChatApp({ waitForReady: true });
    const banner = await screen.findByTestId('thread-banner');

    expect(within(banner).queryByTestId('thread-mark-done') !== null).toBe(offered);
  });

  it('marks a thread read once a reply streamed into it ends', async () => {
    const user = userEvent.setup();
    const threads = serveThreads();
    window.history.replaceState({}, '', `/chat?conversation_id=${HOME}&thread=web_conv_quotes`);

    await renderChatApp({ waitForReady: true });
    await screen.findByTestId('thread-banner');
    await waitFor(
      () =>
        expect(threads.reads).toEqual([
          { conversationId: 'web_conv_quotes', throughMessageId: 10 },
        ]),
      { timeout: 3000 }
    );

    await user.type(screen.getByRole('textbox'), 'Heat pump, please');
    await user.keyboard('{Enter}');

    await waitFor(
      () =>
        expect(threads.reads).toContainEqual({
          conversationId: 'web_conv_quotes',
          throughMessageId: null,
        }),
      { timeout: 5000 }
    );
  });

  it('shows an echoed outcome attributed to its thread, opening it on click', async () => {
    const user = userEvent.setup();
    const threads = serveThreads([{ ...QUOTES, state: 'done', closed_by: 'assistant' }]);
    threads.homeExtra = [
      {
        internal_id: 7,
        role: 'assistant',
        content: 'Three quotes are in; Acme is cheapest at $2,400.',
        timestamp: '2026-10-18T10:00:00Z',
        processing_profile_id: 'default_assistant',
        echoed_from_thread: 'web_conv_quotes',
      },
    ];

    await renderChatApp({ waitForReady: true });

    const outcome = await screen.findByText('Three quotes are in; Acme is cheapest at $2,400.');
    const message = outcome.closest('[data-testid="assistant-message"]') as HTMLElement;
    expect(message).toHaveAttribute('data-echoed-from-thread', 'web_conv_quotes');
    const header = within(message).getByTestId('thread-echo-header');
    expect(header).toHaveTextContent('From thread: Hot water system quotes');
    const ordinary = screen
      .getByText("On it. I've started a thread for it.")
      .closest('[data-testid="assistant-message"]') as HTMLElement;
    expect(within(ordinary).queryByTestId('thread-echo-header')).toBeNull();

    await user.click(header);

    expect(await screen.findByTestId('thread-banner')).toBeInTheDocument();
    expect(window.location.search).toBe(`?conversation_id=${HOME}&thread=web_conv_quotes`);
  });

  it('marks an open thread done from its banner', async () => {
    const user = userEvent.setup();
    serveThreads();
    window.history.replaceState({}, '', `/chat?conversation_id=${HOME}&thread=web_conv_quotes`);

    await renderChatApp({ waitForReady: true });
    const banner = await screen.findByTestId('thread-banner');
    await user.click(within(banner).getByTestId('thread-mark-done'));

    await waitFor(() => expect(banner).toHaveAttribute('data-thread-state', 'done'));
    expect(within(banner).getByTestId('thread-banner-state')).toHaveTextContent(
      'You marked this done'
    );
    expect(within(banner).queryByTestId('thread-mark-done')).toBeNull();
    expect(
      within(screen.getByTestId('thread-group-done')).getByTestId('thread-item-web_conv_quotes')
    ).toBeInTheDocument();
  });

  it('says so when marking a thread done fails, and leaves it as it was', async () => {
    const user = userEvent.setup();
    serveThreads();
    server.use(
      http.post(
        '/api/v1/chat/home/threads/:conversationId/done',
        () => new HttpResponse(null, { status: 500 })
      )
    );
    const consoleError = vi.spyOn(console, 'error').mockImplementation(() => {});
    window.history.replaceState({}, '', `/chat?conversation_id=${HOME}&thread=web_conv_quotes`);

    await renderChatApp({ waitForReady: true });
    const banner = await screen.findByTestId('thread-banner');
    await user.click(within(banner).getByTestId('thread-mark-done'));

    expect(await within(banner).findByTestId('thread-mark-done-failed')).toHaveTextContent(
      "Couldn't mark this thread done"
    );
    expect(banner).toHaveAttribute('data-thread-state', 'waiting');
    expect(within(banner).getByTestId('thread-mark-done')).toBeEnabled();
    consoleError.mockRestore();
  });
});

const WIDE = 1600;
const NARROW = 1024;

function setWindowWidth(width: number): void {
  Object.defineProperty(window, 'innerWidth', { writable: true, configurable: true, value: width });
  act(() => {
    window.dispatchEvent(new Event('resize'));
  });
}

/** The pane showing a conversation, or null when none is on screen. */
function paneFor(conversationId: string): HTMLElement | null {
  return (
    screen
      .queryAllByTestId('conversation-pane')
      .find((pane) => pane.getAttribute('data-pane-conversation-id') === conversationId) ?? null
  );
}

async function findPane(conversationId: string): Promise<HTMLElement> {
  return waitFor(() => {
    const pane = paneFor(conversationId);
    expect(pane).not.toBeNull();
    return pane as HTMLElement;
  });
}

/** Each turn replies naming the conversation it ran in; sends are recorded. */
function serveTurnsNamingTheirConversation(): { conversationId: string; prompt: string }[] {
  const sends: { conversationId: string; prompt: string }[] = [];
  const turnIds = new Map<string, string>();
  server.use(
    http.post('/api/v1/chat/turns', async ({ request }) => {
      const body = (await request.json()) as {
        turn_id: string;
        prompt: string;
        conversation_id: string;
      };
      sends.push({ conversationId: body.conversation_id, prompt: body.prompt });
      turnIds.set(body.conversation_id, body.turn_id);
      return HttpResponse.json({
        turn_id: body.turn_id,
        conversation_id: body.conversation_id,
        first_seq: 0,
      });
    }),
    http.get('/api/v1/chat/conversations/:conversationId/stream', ({ params }) => {
      const conversationId = String(params.conversationId);
      const turnId = turnIds.get(conversationId) ?? 'mock-turn';
      const encoder = new TextEncoder();
      const events = [
        `event: turn_started\ndata: ${JSON.stringify({ turn_id: turnId, seq: 0 })}\n\n`,
        `event: text\ndata: ${JSON.stringify({ content: `Reply for ${conversationId}` })}\n\n`,
        `event: turn_ended\ndata: ${JSON.stringify({ turn_id: turnId, status: 'complete' })}\n\n`,
      ];
      const stream = new ReadableStream({
        start(controller) {
          for (const event of events) {
            controller.enqueue(encoder.encode(event));
          }
          controller.close();
        },
      });
      return new HttpResponse(stream, { headers: { 'Content-Type': 'text/event-stream' } });
    })
  );
  return sends;
}

describe('The thread panel', () => {
  let originalWidth: number;

  beforeEach(() => {
    resetLocalStorageMock();
    vi.clearAllMocks();
    window.history.replaceState({}, '', '/');
    document.documentElement.removeAttribute('data-app-ready');
    originalWidth = window.innerWidth;
    setWindowWidth(WIDE);
  });

  afterEach(() => {
    setWindowWidth(originalWidth);
  });

  it('opens a thread from its card beside Home, which keeps its draft', async () => {
    const user = userEvent.setup();
    serveThreads();

    await renderChatApp({ waitForReady: true });
    await screen.findByText("On it. I've started a thread for it.");
    await user.type(screen.getByRole('textbox'), 'Half-written');
    await user.click(screen.getByTestId('thread-card'));

    const panel = await findPane('web_conv_quotes');
    const home = paneFor(HOME) as HTMLElement;
    expect(window.location.search).toBe(`?conversation_id=${HOME}&thread=web_conv_quotes`);
    expect(panel).toHaveAttribute('data-presentation', 'side');
    expect(home).toHaveAttribute('data-presentation', 'main');
    expect(within(home).getByText("On it. I've started a thread for it.")).toBeInTheDocument();
    expect(within(home).getByRole('textbox')).toHaveValue('Half-written');
    expect(await within(panel).findByText('Gas or heat pump?')).toBeInTheDocument();
    expect(within(panel).getByTestId('conversation-title')).toHaveTextContent(
      'Hot water system quotes'
    );
    // Home is beside it, so the panel offers no way back to it.
    const banner = within(panel).getByTestId('thread-banner');
    expect(within(banner).queryByTestId('thread-breadcrumb-home')).toBeNull();
    expect(within(banner).getByTestId('thread-origin')).toHaveTextContent('Assistant asked');
    const panelPicker = within(panel).getByRole('combobox', { name: 'Processing profile' });
    await waitFor(() => expect(panelPicker).toHaveTextContent('Research'));
    expect(within(home).getByRole('combobox', { name: 'Processing profile' })).toHaveTextContent(
      'Assistant'
    );
  });

  it('sends from the panel into its thread, and shows the reply there alone', async () => {
    const user = userEvent.setup();
    serveThreads();
    const sends = serveTurnsNamingTheirConversation();

    await renderChatApp({ waitForReady: true });
    await screen.findByText("On it. I've started a thread for it.");
    await user.click(screen.getByTestId('thread-card'));
    const panel = await findPane('web_conv_quotes');
    const home = paneFor(HOME) as HTMLElement;
    await within(panel).findByText('Gas or heat pump?');

    await user.type(within(panel).getByRole('textbox'), 'Heat pump, please{Enter}');
    expect(await within(panel).findByText('Reply for web_conv_quotes')).toBeInTheDocument();
    expect(within(home).queryByText('Reply for web_conv_quotes')).toBeNull();
    expect(within(home).queryByText('Heat pump, please')).toBeNull();

    expect(sends).toEqual([{ conversationId: 'web_conv_quotes', prompt: 'Heat pump, please' }]);
  });

  it('expands the thread into the centre, collapses it back, and closes it', async () => {
    const user = userEvent.setup();
    serveThreads();

    await renderChatApp({ waitForReady: true });
    await screen.findByText("On it. I've started a thread for it.");
    await user.type(screen.getByRole('textbox'), 'Draft for Home');
    await user.click(screen.getByTestId('thread-card'));
    await findPane('web_conv_quotes');

    await user.click(screen.getByRole('button', { name: 'Expand thread' }));

    await waitFor(() => expect(paneFor(HOME)).toBeNull());
    const expanded = paneFor('web_conv_quotes') as HTMLElement;
    expect(expanded).toHaveAttribute('data-presentation', 'main');
    expect(within(expanded).getByTestId('thread-breadcrumb-home')).toBeInTheDocument();
    expect(window.location.search).toBe(
      `?conversation_id=${HOME}&thread=web_conv_quotes&expanded=1`
    );

    await user.click(screen.getByRole('button', { name: 'Collapse thread to the side panel' }));

    const home = await findPane(HOME);
    expect(within(home).getByRole('textbox')).toHaveValue('Draft for Home');
    expect(paneFor('web_conv_quotes')).toHaveAttribute('data-presentation', 'side');
    expect(window.location.search).toBe(`?conversation_id=${HOME}&thread=web_conv_quotes`);

    await user.click(screen.getByRole('button', { name: 'Close thread' }));

    await waitFor(() => expect(paneFor('web_conv_quotes')).toBeNull());
    expect(window.location.search).toBe(`?conversation_id=${HOME}`);
    expect(within(paneFor(HOME) as HTMLElement).getByRole('textbox')).toHaveValue('Draft for Home');
  });

  it('keeps a draft typed in the panel when the panel closes and reopens', async () => {
    const user = userEvent.setup();
    serveThreads();

    await renderChatApp({ waitForReady: true });
    await user.click(await screen.findByTestId('thread-card'));
    const panel = await findPane('web_conv_quotes');
    await user.type(within(panel).getByRole('textbox'), 'Gas, I think');
    await user.click(screen.getByRole('button', { name: 'Close thread' }));
    await waitFor(() => expect(paneFor('web_conv_quotes')).toBeNull());

    await user.click(screen.getByTestId('thread-card'));

    const reopened = await findPane('web_conv_quotes');
    await waitFor(() => expect(within(reopened).getByRole('textbox')).toHaveValue('Gas, I think'));
  });

  it('restores the panel from the address, and follows back and forward', async () => {
    serveThreads();
    window.history.replaceState(
      {},
      '',
      `/chat?conversation_id=${HOME}&thread=web_conv_quotes&expanded=1`
    );

    await renderChatApp({ waitForReady: true });

    const expanded = await findPane('web_conv_quotes');
    expect(expanded).toHaveAttribute('data-presentation', 'main');
    expect(paneFor(HOME)).toBeNull();

    act(() => {
      window.history.replaceState({}, '', `/chat?conversation_id=${HOME}`);
      window.dispatchEvent(new PopStateEvent('popstate'));
    });
    const home = await findPane(HOME);
    expect(
      await within(home).findByText("On it. I've started a thread for it.")
    ).toBeInTheDocument();
    expect(paneFor('web_conv_quotes')).toBeNull();

    act(() => {
      window.history.replaceState({}, '', `/chat?conversation_id=${HOME}&thread=web_conv_quotes`);
      window.dispatchEvent(new PopStateEvent('popstate'));
    });
    expect(await findPane('web_conv_quotes')).toHaveAttribute('data-presentation', 'side');
    expect(paneFor(HOME)).not.toBeNull();
  });

  it('lands on a new chat without leaving the bare address behind it', async () => {
    window.history.replaceState({}, '', '/chat');
    const entriesBefore = window.history.length;

    await renderChatApp({ waitForReady: true });

    await waitFor(() => expect(window.location.search).toMatch(/^\?conversation_id=web_conv_/));
    expect(window.history.length).toBe(entriesBefore);
  });

  it('moves a thread linked to directly into the panel, with Home in the centre', async () => {
    serveThreads();
    window.history.replaceState({}, '', '/chat?conversation_id=web_conv_quotes');

    await renderChatApp({ waitForReady: true });

    await waitFor(() =>
      expect(window.location.search).toBe(`?conversation_id=${HOME}&thread=web_conv_quotes`)
    );
    const home = await findPane(HOME);
    expect(
      await within(home).findByText("On it. I've started a thread for it.")
    ).toBeInTheDocument();
    expect(await findPane('web_conv_quotes')).toHaveAttribute('data-presentation', 'side');
  });

  it('opens an earlier conversation in the centre, closing the panel', async () => {
    const user = userEvent.setup();
    serveThreads();

    await renderChatApp({ waitForReady: true });
    await user.click(await screen.findByTestId('thread-card'));
    await findPane('web_conv_quotes');

    await user.click(await screen.findByTestId('conversation-item-web_conv_old'));

    const old = await findPane('web_conv_old');
    expect(old).toHaveAttribute('data-presentation', 'main');
    expect(await within(old).findByText('Hello from web_conv_old')).toBeInTheDocument();
    expect(paneFor('web_conv_quotes')).toBeNull();
    expect(window.location.search).toBe('?conversation_id=web_conv_old');
  });

  it('opens a thread in place of Home on a narrow window, and keeps it open across the breakpoint', async () => {
    const user = userEvent.setup();
    serveThreads();
    setWindowWidth(NARROW);

    await renderChatApp({ waitForReady: true });
    await user.click(await screen.findByTestId('thread-card'));

    const thread = await findPane('web_conv_quotes');
    expect(thread).toHaveAttribute('data-presentation', 'main');
    expect(paneFor(HOME)).toBeNull();
    expect(within(thread).getByTestId('thread-breadcrumb-home')).toBeInTheDocument();

    setWindowWidth(WIDE);
    await waitFor(() =>
      expect(paneFor('web_conv_quotes')).toHaveAttribute('data-presentation', 'side')
    );
    expect(paneFor(HOME)).not.toBeNull();

    setWindowWidth(NARROW);
    await waitFor(() =>
      expect(paneFor('web_conv_quotes')).toHaveAttribute('data-presentation', 'main')
    );
    expect(paneFor(HOME)).toBeNull();

    await user.click(screen.getByTestId('thread-breadcrumb-home'));
    const home = await findPane(HOME);
    expect(
      await within(home).findByText("On it. I've started a thread for it.")
    ).toBeInTheDocument();
    expect(paneFor('web_conv_quotes')).toBeNull();
    expect(window.location.search).toBe(`?conversation_id=${HOME}`);
  });

  it('marks Home and the thread beside it read, each through its own newest message', async () => {
    const user = userEvent.setup();
    const threads = serveThreads([{ ...QUOTES, unread_count: 2 }]);

    await renderChatApp({ waitForReady: true });
    await user.click(await screen.findByTestId('thread-card'));

    await waitFor(
      () => {
        expect(threads.reads).toContainEqual({ conversationId: HOME, throughMessageId: 6 });
        expect(threads.reads).toContainEqual({
          conversationId: 'web_conv_quotes',
          throughMessageId: 10,
        });
      },
      { timeout: 3000 }
    );
    await waitFor(() =>
      expect(
        within(screen.getByTestId('thread-item-web_conv_quotes')).queryByTestId(
          'thread-unread-badge'
        )
      ).toBeNull()
    );
  });

  it('does not mark Home read while a thread covers it', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      const threads = serveThreads();
      setWindowWidth(NARROW);
      window.history.replaceState({}, '', `/chat?conversation_id=${HOME}&thread=web_conv_quotes`);

      await renderChatApp({ waitForReady: true });
      await screen.findByText('Gas or heat pump?');
      await act(async () => {
        await vi.advanceTimersByTimeAsync(MARK_READ_DELAY_MS * 3);
      });

      await waitFor(() =>
        expect(threads.reads).toContainEqual({
          conversationId: 'web_conv_quotes',
          throughMessageId: 10,
        })
      );
      expect(threads.reads.map((read) => read.conversationId)).not.toContain(HOME);
    } finally {
      vi.useRealTimers();
    }
  });
});
