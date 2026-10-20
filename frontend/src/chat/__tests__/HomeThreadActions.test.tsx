import { act, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { HttpResponse, http } from 'msw';
import { vi } from 'vitest';
import { resetLocalStorageMock } from '../../test/mocks/localStorageMock';
import { server } from '../../test/setup.js';
import { renderChatApp } from '../../test/utils/renderChatApp';
import type { ThreadSummary } from '../types';

const HOME = 'web_conv_home';
const NEW_THREAD = 'web_conv_new';
const FIRST_SEQ = 7;
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;

function thread(overrides: Partial<ThreadSummary> & { conversation_id: string }): ThreadSummary {
  return {
    home_conversation_id: HOME,
    title: 'Hot water system quotes',
    profile_id: 'research',
    state: 'waiting',
    source_tool_call_id: null,
    seed_request: null,
    preview: 'Gas or heat pump?',
    created_at: '2026-10-18T09:00:00Z',
    updated_at: '2026-10-18T09:05:00Z',
    ...overrides,
  };
}

const QUOTES = thread({ conversation_id: 'web_conv_quotes' });
const BROWSING = thread({
  conversation_id: 'web_conv_browse',
  title: 'Book the campsite',
  profile_id: 'browser_profile',
});
const FLIGHTS = thread({
  conversation_id: 'web_conv_flights',
  title: 'Flights to Hobart',
  profile_id: 'default_assistant',
  state: 'working',
});

const GUTTER_REPLY = 'Probably, for a roof under gum trees.';
const ECHO = 'Acme is cheapest at $2,400.';
const REMINDER = 'Bins go out tonight.';

const HOME_ROWS = [
  {
    internal_id: 1,
    turn_id: 'turn-gutter',
    role: 'user',
    content: 'Is gutter guard worth it?',
    timestamp: '2026-10-18T09:00:00Z',
  },
  {
    internal_id: 2,
    turn_id: 'turn-gutter',
    role: 'assistant',
    content: GUTTER_REPLY,
    timestamp: '2026-10-18T09:00:01Z',
  },
  {
    internal_id: 3,
    turn_id: 'turn-echo',
    role: 'assistant',
    content: ECHO,
    timestamp: '2026-10-18T09:10:00Z',
    echoed_from_thread: QUOTES.conversation_id,
  },
  {
    internal_id: 4,
    turn_id: 'turn-reminder',
    role: 'assistant',
    content: REMINDER,
    timestamp: '2026-10-18T09:20:00Z',
  },
];

interface ActionsServer {
  threads: ThreadSummary[];
  homeRows: Record<string, unknown>[];
  starts: Record<string, unknown>[];
  moves: { threadId: string; body: Record<string, unknown> }[];
  streamUrls: string[];
  /** Answers the next start instead of starting a thread, then is cleared. */
  refuseNextStart: (() => Response) | null;
  refuseNextMove: (() => Response) | null;
  /** Answer starts as a retry whose turn finished and left the live stream. */
  startsAlreadyComplete: boolean;
}

/** Threads on, Home holding a user's exchange, an echo and a reminder. */
function serveHome(threads: ThreadSummary[] = [QUOTES, BROWSING, FLIGHTS]): ActionsServer {
  const state: ActionsServer = {
    threads,
    homeRows: [...HOME_ROWS],
    starts: [],
    moves: [],
    streamUrls: [],
    refuseNextStart: null,
    refuseNextMove: null,
    startsAlreadyComplete: false,
  };
  const threadRows = new Map<string, Record<string, unknown>[]>();
  let lastTurnId = 'mock-turn';
  server.use(
    // A Home turn is stored as it streams: the user's message, then the reply.
    http.post('/api/v1/chat/turns', async ({ request }) => {
      const body = (await request.json()) as { turn_id: string; prompt: string };
      lastTurnId = body.turn_id;
      state.homeRows = [
        ...state.homeRows,
        {
          internal_id: 60,
          turn_id: body.turn_id,
          role: 'user',
          content: body.prompt,
          timestamp: '2026-10-18T11:00:00Z',
        },
        {
          internal_id: 61,
          turn_id: body.turn_id,
          role: 'assistant',
          content: `Reply in ${HOME}`,
          timestamp: '2026-10-18T11:00:01Z',
        },
      ];
      return HttpResponse.json({
        turn_id: body.turn_id,
        conversation_id: HOME,
        first_seq: FIRST_SEQ,
      });
    }),
    http.get('/api/v1/chat/home', () =>
      HttpResponse.json({ enabled: true, conversation_id: HOME })
    ),
    http.get('/api/v1/chat/home/threads', () =>
      HttpResponse.json({ home_conversation_id: HOME, threads: state.threads })
    ),
    http.post('/api/v1/chat/conversations/:conversationId/read', ({ params }) =>
      HttpResponse.json({ conversation_id: String(params.conversationId), unread_count: 0 })
    ),
    http.get('/api/v1/chat/conversations', () =>
      HttpResponse.json({ conversations: [], count: 0 })
    ),
    http.post('/api/v1/chat/home/threads', async ({ request }) => {
      const body = (await request.json()) as Record<string, unknown>;
      state.starts.push(body);
      lastTurnId = String(body.turn_id);
      const refusal = state.refuseNextStart;
      if (refusal) {
        state.refuseNextStart = null;
        return refusal();
      }
      const started = thread({
        conversation_id: NEW_THREAD,
        title: typeof body.prompt === 'string' ? body.prompt : 'Continued from Home',
        profile_id: String(body.profile_id),
        state: body.prompt ? 'working' : 'waiting',
        preview: null,
        anchor_message_id: (body.anchor_message_id as number | undefined) ?? null,
      });
      state.threads = [started, ...state.threads];
      const rows: Record<string, unknown>[] = [];
      if (body.include_context) {
        rows.push({ ...HOME_ROWS[0], internal_id: 101 }, { ...HOME_ROWS[1], internal_id: 102 });
      }
      if (typeof body.prompt === 'string') {
        rows.push({
          internal_id: 103,
          turn_id: body.turn_id,
          role: 'user',
          content: body.prompt,
          timestamp: '2026-10-18T10:00:00Z',
        });
      }
      if (state.startsAlreadyComplete && typeof body.prompt === 'string') {
        rows.push({
          internal_id: 104,
          turn_id: body.turn_id,
          role: 'assistant',
          content: 'Answered before the retry.',
          timestamp: '2026-10-18T10:00:01Z',
        });
      }
      threadRows.set(NEW_THREAD, rows);
      return HttpResponse.json({
        thread: started,
        turn: body.prompt
          ? {
              turn_id: body.turn_id,
              conversation_id: NEW_THREAD,
              first_seq: FIRST_SEQ,
              ...(state.startsAlreadyComplete ? { already_complete: true, incomplete: false } : {}),
            }
          : null,
        copied_message_ids: body.include_context ? [101, 102] : [],
      });
    }),
    http.post('/api/v1/chat/home/threads/:threadId/moved-turns', async ({ params, request }) => {
      const threadId = String(params.threadId);
      const body = (await request.json()) as Record<string, unknown>;
      state.moves.push({ threadId, body });
      const refusal = state.refuseNextMove;
      if (refusal) {
        state.refuseNextMove = null;
        return refusal();
      }
      const moved = state.homeRows.filter((row) => row.turn_id === 'turn-gutter');
      state.homeRows = [
        {
          internal_id: 50,
          turn_id: 'turn-gutter',
          role: 'assistant',
          content: 'An exchange was moved to a thread.',
          timestamp: '2026-10-18T09:00:01Z',
          moved_to_thread: threadId,
        },
        ...state.homeRows.filter((row) => row.turn_id !== 'turn-gutter'),
      ];
      threadRows.set(threadId, moved);
      const destination = state.threads.find((entry) => entry.conversation_id === threadId);
      return HttpResponse.json({
        thread: destination,
        marker_message_id: 50,
        moved_message_ids: [201, 202],
      });
    }),
    http.get('/api/v1/chat/conversations/:conversationId/messages', ({ params }) => {
      const conversationId = String(params.conversationId);
      if (conversationId === HOME) {
        return HttpResponse.json({
          conversation_kind: 'home',
          latest_user_profile_id: 'default_assistant',
          messages: state.homeRows,
        });
      }
      const opened = state.threads.find((entry) => entry.conversation_id === conversationId);
      return HttpResponse.json({
        conversation_kind: 'thread',
        thread: opened,
        latest_user_profile_id: opened?.profile_id,
        messages: threadRows.get(conversationId) ?? [
          {
            internal_id: 10,
            role: 'assistant',
            content: `Earlier in ${conversationId}`,
            timestamp: '2026-10-18T09:05:00Z',
          },
        ],
      });
    }),
    http.get('/api/v1/chat/conversations/:conversationId/stream', ({ params, request }) => {
      const conversationId = String(params.conversationId);
      state.streamUrls.push(request.url);
      const turnId = lastTurnId;
      const encoder = new TextEncoder();
      const events = [
        `event: turn_started\ndata: ${JSON.stringify({ turn_id: turnId, seq: FIRST_SEQ })}\n\n`,
        `event: text\ndata: ${JSON.stringify({ content: `Reply in ${conversationId}`, seq: FIRST_SEQ + 1 })}\n\n`,
        `event: turn_ended\ndata: ${JSON.stringify({ turn_id: turnId, status: 'complete', seq: FIRST_SEQ + 2 })}\n\n`,
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
  return state;
}

function setWindowWidth(width: number): void {
  Object.defineProperty(window, 'innerWidth', { writable: true, configurable: true, value: width });
  act(() => {
    window.dispatchEvent(new Event('resize'));
  });
}

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

/** The Home reply holding `text`, once it has rendered. */
async function homeReply(text: string): Promise<HTMLElement> {
  const home = await findPane(HOME);
  const content = await within(home).findByText(text);
  const message = content.closest('[data-testid="assistant-message"]');
  expect(message).not.toBeNull();
  return message as HTMLElement;
}

type User = ReturnType<typeof userEvent.setup>;

async function openContinue(user: User, text: string): Promise<HTMLElement> {
  await user.click(within(await homeReply(text)).getByTestId('continue-in-thread'));
  return screen.findByTestId('start-thread-dialog');
}

async function openMove(user: User, text: string): Promise<HTMLElement> {
  await user.click(within(await homeReply(text)).getByTestId('move-to-thread'));
  return screen.findByTestId('move-to-thread-dialog');
}

// Radix menus and selects ask the DOM for pointer capture and scrolling, which
// jsdom lacks.
const POINTER_METHODS = ['hasPointerCapture', 'releasePointerCapture', 'scrollIntoView'];
const stubbedPointerMethods: string[] = [];

describe('Starting and moving threads from Home', () => {
  let originalWidth: number;

  beforeAll(() => {
    const proto = window.HTMLElement.prototype as unknown as Record<string, unknown>;
    for (const method of POINTER_METHODS) {
      if (!(method in proto)) {
        proto[method] = vi.fn();
        stubbedPointerMethods.push(method);
      }
    }
  });

  afterAll(() => {
    const proto = window.HTMLElement.prototype as unknown as Record<string, unknown>;
    for (const method of stubbedPointerMethods) {
      delete proto[method];
    }
  });

  beforeEach(() => {
    resetLocalStorageMock();
    vi.clearAllMocks();
    window.history.replaceState({}, '', '/');
    document.documentElement.removeAttribute('data-app-ready');
    originalWidth = window.innerWidth;
    setWindowWidth(1600);
  });

  afterEach(() => {
    setWindowWidth(originalWidth);
  });

  it('starts a new thread from the sidebar and streams its first reply in the panel', async () => {
    const user = userEvent.setup({ pointerEventsCheck: 0 });
    const home = serveHome();
    await renderChatApp({ waitForReady: true });
    await homeReply(GUTTER_REPLY);

    await user.click(await screen.findByTestId('new-thread-button'));
    const dialog = await screen.findByTestId('start-thread-dialog');
    const submit = within(dialog).getByTestId('start-thread-submit');
    expect(submit).toBeDisabled();
    await user.selectOptions(within(dialog).getByTestId('start-thread-profile'), 'research');
    await user.type(within(dialog).getByTestId('start-thread-prompt'), 'Plan the veggie garden');
    await user.click(submit);

    const panel = await findPane(NEW_THREAD);
    expect(panel).toHaveAttribute('data-presentation', 'side');
    expect(await within(panel).findByText(`Reply in ${NEW_THREAD}`)).toBeInTheDocument();
    expect(within(panel).getByTestId('user-message')).toHaveTextContent('Plan the veggie garden');
    expect(home.starts).toEqual([
      {
        profile_id: 'research',
        turn_id: expect.stringMatching(UUID),
        prompt: 'Plan the veggie garden',
      },
    ]);
    const followed = home.streamUrls.map((url) => new URL(url));
    expect(
      followed.some(
        (url) =>
          url.pathname.endsWith(`/${NEW_THREAD}/stream`) &&
          url.searchParams.get('from_seq') === String(FIRST_SEQ)
      )
    ).toBe(true);
    expect(window.location.search).toBe(`?conversation_id=${HOME}&thread=${NEW_THREAD}`);
    expect(screen.queryByTestId('start-thread-dialog')).not.toBeInTheDocument();
    // The sidebar lists it at once, ahead of the refetch.
    expect(screen.getByTestId(`thread-item-${NEW_THREAD}`)).toBeInTheDocument();
  });

  it('retries a start that got no answer with the same turn_id, so it starts only once', async () => {
    const user = userEvent.setup({ pointerEventsCheck: 0 });
    const home = serveHome();
    home.refuseNextStart = () => HttpResponse.error();
    await renderChatApp({ waitForReady: true });

    await user.click(await screen.findByTestId('new-thread-button'));
    const dialog = await screen.findByTestId('start-thread-dialog');
    await user.type(within(dialog).getByTestId('start-thread-prompt'), 'Sort out the insurance');
    await user.click(within(dialog).getByTestId('start-thread-submit'));

    expect(await within(dialog).findByTestId('start-thread-error')).toHaveTextContent(
      "Couldn't start the thread: the server couldn't be reached. Try again."
    );
    await user.click(within(dialog).getByTestId('start-thread-submit'));

    await findPane(NEW_THREAD);
    expect(home.starts).toHaveLength(2);
    expect(home.starts[1].turn_id).toBe(home.starts[0].turn_id);
    expect(home.starts[0].profile_id).toBe('default_assistant');
  });

  it('starts afresh when a refused start is changed before it is sent again', async () => {
    const user = userEvent.setup({ pointerEventsCheck: 0 });
    const home = serveHome();
    home.refuseNextStart = () => HttpResponse.error();
    await renderChatApp({ waitForReady: true });

    await user.click(await screen.findByTestId('new-thread-button'));
    const dialog = await screen.findByTestId('start-thread-dialog');
    const prompt = within(dialog).getByTestId('start-thread-prompt');
    await user.type(prompt, 'Sort out the insurance');
    await user.click(within(dialog).getByTestId('start-thread-submit'));
    await within(dialog).findByTestId('start-thread-error');
    await user.type(prompt, ' renewal');
    await user.click(within(dialog).getByTestId('start-thread-submit'));

    await findPane(NEW_THREAD);
    expect(home.starts).toHaveLength(2);
    expect(home.starts[1].prompt).toBe('Sort out the insurance renewal');
    expect(home.starts[1].turn_id).not.toBe(home.starts[0].turn_id);
  });

  it('continues from a reply streamed here, anchored on that reply', async () => {
    const user = userEvent.setup({ pointerEventsCheck: 0 });
    const home = serveHome();
    await renderChatApp({ waitForReady: true });
    const homePane = await findPane(HOME);
    await homeReply(GUTTER_REPLY);

    await user.type(within(homePane).getByTestId('chat-input'), 'What about box gutters?');
    await user.keyboard('{Enter}');
    const dialog = await openContinue(user, `Reply in ${HOME}`);
    await user.click(within(dialog).getByTestId('start-thread-submit'));

    await findPane(NEW_THREAD);
    expect(home.starts[0].anchor_message_id).toBe(61);
  });

  it('reads a retried start whose turn already finished from the record, not the stream', async () => {
    const user = userEvent.setup({ pointerEventsCheck: 0 });
    const home = serveHome();
    home.startsAlreadyComplete = true;
    await renderChatApp({ waitForReady: true });

    await user.click(await screen.findByTestId('new-thread-button'));
    const dialog = await screen.findByTestId('start-thread-dialog');
    await user.type(within(dialog).getByTestId('start-thread-prompt'), 'Plan the veggie garden');
    await user.click(within(dialog).getByTestId('start-thread-submit'));

    const panel = await findPane(NEW_THREAD);
    expect(await within(panel).findByText('Answered before the retry.')).toBeInTheDocument();
    expect(home.streamUrls.filter((url) => url.includes(`/${NEW_THREAD}/stream`))).toEqual([]);
  });

  it('starts afresh when the tier of a refused send-as-thread is changed', async () => {
    const user = userEvent.setup({ pointerEventsCheck: 0 });
    const home = serveHome();
    home.refuseNextStart = () => HttpResponse.error();
    await renderChatApp({ waitForReady: true });
    const homePane = await findPane(HOME);

    const pickTier = async (tier: RegExp) => {
      await user.click(within(homePane).getByRole('combobox', { name: 'Intelligence' }));
      await user.click(await screen.findByRole('option', { name: tier }));
    };
    const sendAsThread = async () => {
      await user.click(within(homePane).getByTestId('send-as-thread-button'));
      await user.click(await screen.findByTestId('send-as-thread-default_assistant'));
    };
    await pickTier(/Deep/);
    await user.type(within(homePane).getByTestId('chat-input'), 'Compare solar quotes');
    await sendAsThread();
    await within(homePane).findByTestId('send-as-thread-error');
    await waitFor(() =>
      expect(within(homePane).getByTestId('chat-input')).toHaveValue('Compare solar quotes')
    );
    await pickTier(/Standard/);
    await sendAsThread();

    await findPane(NEW_THREAD);
    expect(home.starts).toHaveLength(2);
    expect(home.starts[0].model_tier).toBe('deep');
    // Back on the default, which a start names by leaving the tier out.
    expect(home.starts[1]).not.toHaveProperty('model_tier');
    expect(home.starts[1].turn_id).not.toBe(home.starts[0].turn_id);
  });

  it('sends the composed message as a thread, with its attachment and tier', async () => {
    const user = userEvent.setup({ pointerEventsCheck: 0 });
    const home = serveHome();
    await renderChatApp({ waitForReady: true });
    const homePane = await findPane(HOME);
    await homeReply(GUTTER_REPLY);

    const intelligence = within(homePane).getByRole('combobox', { name: 'Intelligence' });
    await user.click(intelligence);
    await user.click(await screen.findByRole('option', { name: /Deep/ }));
    await user.type(within(homePane).getByTestId('chat-input'), 'Compare solar quotes');
    await user.upload(
      within(homePane).getByTestId('file-input'),
      new File(['quote'], 'quote.pdf', { type: 'application/pdf' })
    );
    await user.click(within(homePane).getByTestId('send-as-thread-button'));
    await user.click(await screen.findByTestId('send-as-thread-default_assistant'));

    const panel = await findPane(NEW_THREAD);
    expect(await within(panel).findByText(`Reply in ${NEW_THREAD}`)).toBeInTheDocument();
    expect(home.starts).toHaveLength(1);
    const [start] = home.starts;
    expect(start).toMatchObject({
      profile_id: 'default_assistant',
      prompt: 'Compare solar quotes',
      model_tier: 'deep',
      turn_id: expect.stringMatching(UUID),
    });
    expect(start.attachments).toEqual([
      expect.objectContaining({ name: 'quote.pdf', content: expect.any(String) }),
    ]);
    // It went to the thread, not into Home.
    expect(within(homePane).queryByText('Compare solar quotes')).not.toBeInTheDocument();
    expect(within(homePane).getByTestId('chat-input')).toHaveValue('');
  });

  it('leaves out a tier the thread profile does not offer', async () => {
    const user = userEvent.setup({ pointerEventsCheck: 0 });
    const home = serveHome();
    await renderChatApp({ waitForReady: true });
    const homePane = await findPane(HOME);

    await user.click(within(homePane).getByRole('combobox', { name: 'Intelligence' }));
    await user.click(await screen.findByRole('option', { name: /Deep/ }));
    await user.type(within(homePane).getByTestId('chat-input'), 'Research heat pumps');
    await user.click(within(homePane).getByTestId('send-as-thread-button'));
    await user.click(await screen.findByTestId('send-as-thread-research'));

    await findPane(NEW_THREAD);
    expect(home.starts[0]).not.toHaveProperty('model_tier');
    expect(home.starts[0].profile_id).toBe('research');
  });

  it('says why a send-as-thread was refused and puts the message back', async () => {
    const user = userEvent.setup({ pointerEventsCheck: 0 });
    const home = serveHome();
    home.refuseNextStart = () =>
      HttpResponse.json(
        { detail: "Profile 'research' is not available for a thread." },
        { status: 400 }
      );
    await renderChatApp({ waitForReady: true });
    const homePane = await findPane(HOME);

    await user.type(within(homePane).getByTestId('chat-input'), 'Find a tiler');
    await user.click(within(homePane).getByTestId('send-as-thread-button'));
    await user.click(await screen.findByTestId('send-as-thread-research'));

    expect(await within(homePane).findByTestId('send-as-thread-error')).toHaveTextContent(
      "Profile 'research' is not available for a thread."
    );
    await waitFor(() =>
      expect(within(homePane).getByTestId('chat-input')).toHaveValue('Find a tiler')
    );
    expect(paneFor(NEW_THREAD)).toBeNull();
    expect(within(homePane).queryByTestId('user-message')).toHaveTextContent(
      'Is gutter guard worth it?'
    );
  });

  it('continues a Home reply in a thread with its context, quoting where it started', async () => {
    const user = userEvent.setup({ pointerEventsCheck: 0 });
    const home = serveHome();
    await renderChatApp({ waitForReady: true });

    const dialog = await openContinue(user, GUTTER_REPLY);
    expect(within(dialog).getByTestId('start-thread-anchor')).toHaveTextContent(GUTTER_REPLY);
    await user.selectOptions(within(dialog).getByTestId('start-thread-profile'), 'research');
    expect(within(dialog).getByTestId('start-thread-with-context')).toBeChecked();
    // With the conversation along, a message is optional.
    await user.click(within(dialog).getByTestId('start-thread-submit'));

    const panel = await findPane(NEW_THREAD);
    expect(home.starts).toEqual([
      {
        profile_id: 'research',
        turn_id: expect.stringMatching(UUID),
        anchor_message_id: 2,
        include_context: true,
      },
    ]);
    expect(within(panel).getByTestId('thread-anchor-quote')).toHaveTextContent(GUTTER_REPLY);
    expect(await within(panel).findByText('Is gutter guard worth it?')).toBeInTheDocument();
    // Nothing to follow: the thread waits for the user.
    expect(home.streamUrls).toEqual([]);
  });

  it('offers a profile without household context as a request-only start, and says so', async () => {
    const user = userEvent.setup({ pointerEventsCheck: 0 });
    const home = serveHome();
    await renderChatApp({ waitForReady: true });

    const dialog = await openContinue(user, GUTTER_REPLY);
    const picker = within(dialog).getByTestId('start-thread-profile');
    expect(
      within(picker).getByRole('option', { name: 'Browser_profile (your message only)' })
    ).toBeInTheDocument();
    expect(within(picker).getByRole('option', { name: 'Research' })).toBeInTheDocument();
    await user.selectOptions(picker, 'browser_profile');

    expect(within(dialog).queryByTestId('start-thread-with-context')).not.toBeInTheDocument();
    expect(within(dialog).getByTestId('start-thread-request-only')).toHaveTextContent(
      "Browser_profile doesn't receive household context"
    );
    const submit = within(dialog).getByTestId('start-thread-submit');
    expect(submit).toBeDisabled();
    await user.type(within(dialog).getByTestId('start-thread-prompt'), 'Book a site at Wilsons');
    await user.click(submit);

    await findPane(NEW_THREAD);
    expect(home.starts).toEqual([
      {
        profile_id: 'browser_profile',
        turn_id: expect.stringMatching(UUID),
        prompt: 'Book a site at Wilsons',
        anchor_message_id: 2,
        include_context: false,
      },
    ]);
  });

  it('shows why a continuation was refused', async () => {
    const user = userEvent.setup({ pointerEventsCheck: 0 });
    const home = serveHome();
    home.refuseNextStart = () =>
      HttpResponse.json({ detail: 'That reply is still in progress.' }, { status: 409 });
    await renderChatApp({ waitForReady: true });

    const dialog = await openContinue(user, GUTTER_REPLY);
    await user.click(within(dialog).getByTestId('start-thread-submit'));

    expect(await within(dialog).findByTestId('start-thread-error')).toHaveTextContent(
      'That reply is still in progress.'
    );
    expect(paneFor(NEW_THREAD)).toBeNull();
  });

  it('moves a Home turn into a thread that can take it, leaving a marker that opens it', async () => {
    const user = userEvent.setup({ pointerEventsCheck: 0 });
    const home = serveHome();
    await renderChatApp({ waitForReady: true });

    const dialog = await openMove(user, GUTTER_REPLY);
    const quotes = within(dialog).getByTestId(`move-option-${QUOTES.conversation_id}`);
    const browsing = within(dialog).getByTestId(`move-option-${BROWSING.conversation_id}`);
    const flights = within(dialog).getByTestId(`move-option-${FLIGHTS.conversation_id}`);
    expect(quotes).toBeEnabled();
    expect(browsing).toBeDisabled();
    expect(within(browsing).getByTestId('move-option-detail')).toHaveTextContent(
      "Browser_profile doesn't receive household context"
    );
    expect(flights).toBeDisabled();
    expect(within(flights).getByTestId('move-option-detail')).toHaveTextContent(
      'Working: wait for it to finish'
    );
    const submit = within(dialog).getByTestId('move-to-thread-submit');
    expect(submit).toBeDisabled();
    await user.click(quotes);
    await user.click(submit);

    const homePane = await findPane(HOME);
    const marker = await within(homePane).findByTestId('moved-turn-marker');
    expect(marker).toHaveTextContent('Moved to Hot water system quotes');
    expect(home.moves).toEqual([{ threadId: QUOTES.conversation_id, body: { message_id: 2 } }]);
    expect(within(homePane).queryByText(GUTTER_REPLY)).not.toBeInTheDocument();
    expect(within(homePane).queryByText('An exchange was moved to a thread.')).toBeNull();
    expect(screen.queryByTestId('move-to-thread-dialog')).not.toBeInTheDocument();

    await user.click(within(marker).getByTestId('moved-turn-link'));
    const panel = await findPane(QUOTES.conversation_id);
    expect(await within(panel).findByText(GUTTER_REPLY)).toBeInTheDocument();
  });

  it('shows why a move was refused', async () => {
    const user = userEvent.setup({ pointerEventsCheck: 0 });
    const home = serveHome();
    home.refuseNextMove = () =>
      HttpResponse.json(
        { detail: 'Wait for the reply in progress to finish, then move the turn.' },
        { status: 409 }
      );
    await renderChatApp({ waitForReady: true });

    const dialog = await openMove(user, GUTTER_REPLY);
    await user.click(within(dialog).getByTestId(`move-option-${QUOTES.conversation_id}`));
    await user.click(within(dialog).getByTestId('move-to-thread-submit'));

    expect(await within(dialog).findByTestId('move-to-thread-error')).toHaveTextContent(
      'Wait for the reply in progress to finish, then move the turn.'
    );
    expect(await homeReply(GUTTER_REPLY)).toBeInTheDocument();
  });

  it('says so when no thread can take the turn', async () => {
    const user = userEvent.setup({ pointerEventsCheck: 0 });
    serveHome([BROWSING]);
    await renderChatApp({ waitForReady: true });

    const dialog = await openMove(user, GUTTER_REPLY);

    expect(within(dialog).getByTestId('move-to-thread-unavailable')).toBeInTheDocument();
    expect(within(dialog).getByTestId('move-to-thread-submit')).toBeDisabled();
  });

  it("offers no move for an echo or a message the user didn't start", async () => {
    serveHome();
    await renderChatApp({ waitForReady: true });

    const echo = await homeReply(ECHO);
    const reminder = await homeReply(REMINDER);
    const reply = await homeReply(GUTTER_REPLY);

    expect(within(reply).getByTestId('move-to-thread')).toBeInTheDocument();
    expect(within(echo).queryByTestId('move-to-thread')).not.toBeInTheDocument();
    expect(within(reminder).queryByTestId('move-to-thread')).not.toBeInTheDocument();
    // Any of them can still be continued in a thread.
    expect(within(echo).getByTestId('continue-in-thread')).toBeInTheDocument();
    expect(within(reminder).getByTestId('continue-in-thread')).toBeInTheDocument();
  });

  it('offers no thread actions outside Home', async () => {
    const user = userEvent.setup({ pointerEventsCheck: 0 });
    serveHome();
    await renderChatApp({ waitForReady: true });

    await user.click(await screen.findByTestId(`thread-item-${QUOTES.conversation_id}`));
    const panel = await findPane(QUOTES.conversation_id);
    const reply = (await within(panel).findByText(`Earlier in ${QUOTES.conversation_id}`)).closest(
      '[data-testid="assistant-message"]'
    ) as HTMLElement;

    expect(within(reply).queryByTestId('continue-in-thread')).not.toBeInTheDocument();
    expect(within(panel).queryByTestId('send-as-thread-button')).not.toBeInTheDocument();
  });
});
