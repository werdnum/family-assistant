import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { HttpResponse, http } from 'msw';
import { vi } from 'vitest';
import { resetLocalStorageMock } from '../../test/mocks/localStorageMock';
import { server } from '../../test/setup.js';
import { renderChatApp } from '../../test/utils/renderChatApp';

const CONVERSATION = 'conv-feedback';
const REPLY = 'Spaghetti bolognese tonight.';

interface FeedbackServer {
  writes: {
    method: string;
    turnId: string;
    body: Record<string, unknown> | null;
  }[];
  failNext: boolean;
  /** Holds the next write's response until it resolves. */
  holdNext: Promise<void> | null;
}

function serveConversation(
  feedback: 'up' | 'down' | null = null,
  reasoningInfo: Record<string, string> | null = null
): FeedbackServer {
  const state: FeedbackServer = { writes: [], failNext: false, holdNext: null };
  const rows = [
    {
      internal_id: 1,
      turn_id: 'turn-dinner',
      role: 'user',
      content: "What's for dinner?",
      timestamp: '2026-10-22T09:00:00Z',
      feedback,
    },
    {
      internal_id: 2,
      turn_id: 'turn-dinner',
      role: 'assistant',
      content: REPLY,
      timestamp: '2026-10-22T09:00:01Z',
      feedback,
      reasoning_info: reasoningInfo,
    },
  ];
  const record = async (method: string, turnId: string, request: Request) => {
    const body = method === 'PUT' ? ((await request.json()) as Record<string, unknown>) : null;
    state.writes.push({ method, turnId, body });
    const hold = state.holdNext;
    state.holdNext = null;
    if (hold) {
      await hold;
    }
    if (state.failNext) {
      state.failNext = false;
      return HttpResponse.json({ detail: 'nope' }, { status: 500 });
    }
    return method === 'PUT'
      ? HttpResponse.json({
          turn_id: turnId,
          ...body,
          updated_at: '2026-10-22T09:01:00Z',
        })
      : new HttpResponse(null, { status: 204 });
  };
  server.use(
    http.get('/api/v1/chat/conversations/:conversationId/messages', ({ params }) =>
      HttpResponse.json({
        conversation_id: params.conversationId,
        messages: params.conversationId === CONVERSATION ? rows : [],
        count: rows.length,
        total_messages: rows.length,
        has_more_before: false,
        has_more_after: false,
        active_turns: [],
      })
    ),
    http.put(
      '/api/v1/chat/conversations/:conversationId/turns/:turnId/feedback',
      ({ params, request }) => record('PUT', String(params.turnId), request)
    ),
    http.delete(
      '/api/v1/chat/conversations/:conversationId/turns/:turnId/feedback',
      ({ params, request }) => record('DELETE', String(params.turnId), request)
    )
  );
  return state;
}

async function openReply() {
  await renderChatApp({
    waitForReady: true,
    initialEntries: [`/chat?conversation_id=${CONVERSATION}`],
  });
  await screen.findByText(REPLY);
  const reply = screen
    .getAllByTestId('assistant-message')
    .find((message) => message.textContent?.includes(REPLY));
  if (!reply) {
    throw new Error('reply not rendered');
  }
  return within(reply);
}

describe('rating a reply', () => {
  beforeEach(() => {
    resetLocalStorageMock();
    vi.clearAllMocks();
    window.history.replaceState(null, '', `/chat?conversation_id=${CONVERSATION}`);
  });

  afterEach(() => {
    window.history.replaceState(null, '', '/');
  });

  it('records thumbs up, and a second press withdraws it', async () => {
    const state = serveConversation();
    const user = userEvent.setup();
    const reply = await openReply();

    const up = reply.getByRole('button', { name: 'Good response' });
    expect(up).toHaveAttribute('aria-pressed', 'false');
    await user.click(up);
    await waitFor(() => expect(up).toHaveAttribute('aria-pressed', 'true'));
    expect(state.writes).toEqual([
      {
        method: 'PUT',
        turnId: 'turn-dinner',
        body: { rating: 'up', comment: null },
      },
    ]);
    expect(reply.queryByTestId('feedback-comment-form')).not.toBeInTheDocument();

    await user.click(up);
    await waitFor(() => expect(up).toHaveAttribute('aria-pressed', 'false'));
    expect(state.writes[1]).toEqual({
      method: 'DELETE',
      turnId: 'turn-dinner',
      body: null,
    });
  });

  it('asks what was wrong after thumbs down and sends the answer', async () => {
    const state = serveConversation();
    const user = userEvent.setup();
    const reply = await openReply();

    await user.click(reply.getByRole('button', { name: 'Bad response' }));
    const input = await reply.findByRole('textbox', {
      name: 'What was wrong with this response',
    });
    await user.type(input, 'Forgot I am vegetarian');
    await user.click(reply.getByRole('button', { name: 'Send' }));

    await waitFor(() =>
      expect(reply.queryByTestId('feedback-comment-form')).not.toBeInTheDocument()
    );
    expect(state.writes.map((write) => write.body)).toEqual([
      { rating: 'down', comment: null },
      { rating: 'down', comment: 'Forgot I am vegetarian' },
    ]);
    expect(reply.getByRole('button', { name: 'Bad response' })).toHaveAttribute(
      'aria-pressed',
      'true'
    );
  });

  it('shows a rating given earlier', async () => {
    serveConversation('down');
    const reply = await openReply();
    expect(reply.getByRole('button', { name: 'Bad response' })).toHaveAttribute(
      'aria-pressed',
      'true'
    );
    expect(reply.getByRole('button', { name: 'Good response' })).toHaveAttribute(
      'aria-pressed',
      'false'
    );
  });

  it('restores the shown rating when saving fails', async () => {
    const state = serveConversation();
    state.failNext = true;
    const user = userEvent.setup();
    const reply = await openReply();

    const up = reply.getByRole('button', { name: 'Good response' });
    await user.click(up);
    expect(await reply.findByRole('alert')).toHaveTextContent("Couldn't save your feedback");
    expect(up).toHaveAttribute('aria-pressed', 'false');
  });

  it('sends rating changes one at a time, so the last press is the one kept', async () => {
    const state = serveConversation();
    let release = () => {};
    state.holdNext = new Promise((resolve) => {
      release = resolve;
    });
    const user = userEvent.setup();
    const reply = await openReply();

    await user.click(reply.getByRole('button', { name: 'Bad response' }));
    const up = reply.getByRole('button', { name: 'Good response' });
    await user.click(up);
    await waitFor(() => expect(up).toHaveAttribute('aria-pressed', 'true'));
    // The thumbs up waits for the held thumbs down to finish.
    expect(state.writes).toHaveLength(1);
    release();

    await waitFor(() => expect(state.writes).toHaveLength(2));
    expect(state.writes.map((write) => write.body?.rating)).toEqual(['down', 'up']);
    // The thumbs up is sent only after the thumbs down settles, and the thumbs
    // down's completion is queued on that same settlement first, so by the
    // time the second write reaches the server the stale completion has run.
    expect(reply.queryByTestId('feedback-comment-form')).not.toBeInTheDocument();
    expect(up).toHaveAttribute('aria-pressed', 'true');
  });

  it('offers rating on a reply with nothing to copy', async () => {
    serveConversation();
    server.use(
      http.get('/api/v1/chat/conversations/:conversationId/messages', () =>
        HttpResponse.json({
          conversation_id: CONVERSATION,
          messages: [
            {
              internal_id: 1,
              turn_id: 'turn-weather',
              role: 'user',
              content: 'Weather?',
              timestamp: '2026-10-22T09:00:00Z',
            },
            {
              internal_id: 2,
              turn_id: 'turn-weather',
              role: 'assistant',
              content: '',
              timestamp: '2026-10-22T09:00:01Z',
              tool_calls: [
                {
                  id: 'call_weather',
                  type: 'function',
                  function: { name: 'get_weather', arguments: '{}' },
                },
              ],
            },
            {
              internal_id: 3,
              turn_id: 'turn-weather',
              role: 'tool',
              tool_call_id: 'call_weather',
              content: 'Sunny',
              timestamp: '2026-10-22T09:00:02Z',
            },
          ],
          count: 3,
          total_messages: 3,
          has_more_before: false,
          has_more_after: false,
          active_turns: [],
        })
      )
    );
    await renderChatApp({
      waitForReady: true,
      initialEntries: [`/chat?conversation_id=${CONVERSATION}`],
    });

    const up = await screen.findByRole('button', { name: 'Good response' });
    expect(up).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Copy response' })).not.toBeInTheDocument();
  });
});

describe('a reply from a live model experiment', () => {
  beforeEach(() => {
    resetLocalStorageMock();
    vi.clearAllMocks();
    window.history.replaceState(null, '', `/chat?conversation_id=${CONVERSATION}`);
  });

  afterEach(() => {
    window.history.replaceState(null, '', '/');
  });

  it('hides which model answered so the rating is blind', async () => {
    serveConversation(null, {
      model: 'gemini-4-argon',
      model_tier: 'gemini_4_argon',
      model_tier_source: 'experiment',
      experiment_id: 'argon-vs-opus',
      experiment_arm: 'gemini_4_argon',
    });
    const reply = await openReply();

    const badge = reply.getByTestId('model-tier-badge');
    expect(badge).toHaveTextContent('Experiment');
    expect(badge.getAttribute('title')).not.toMatch(/argon/i);
    expect(reply.queryByText(/argon/i)).not.toBeInTheDocument();
    expect(reply.queryByRole('button', { name: 'Which model answered' })).not.toBeInTheDocument();
  });

  it('reveals the arm once the reply is rated', async () => {
    serveConversation(null, EXPERIMENT_REASONING);
    const user = userEvent.setup();
    const reply = await openReply();

    await user.click(reply.getByRole('button', { name: 'Good response' }));
    const reveal = await reply.findByRole('button', {
      name: 'Which model answered',
    });
    await user.click(reveal);

    const details = reply.getByTestId('served-by-details');
    expect(details).toHaveTextContent('argon-vs-opus');
    expect(details).toHaveTextContent('gemini-4-argon-001 via google');
    // The badge stays blind; only the opened details name the arm.
    expect(reply.getByTestId('model-tier-badge')).toHaveTextContent('Experiment');

    await user.click(reveal);
    expect(reply.queryByTestId('served-by-details')).not.toBeInTheDocument();
  });

  it('hides the arm again if the rating is withdrawn', async () => {
    serveConversation('up', EXPERIMENT_REASONING);
    const user = userEvent.setup();
    const reply = await openReply();

    await user.click(reply.getByRole('button', { name: 'Which model answered' }));
    expect(reply.getByTestId('served-by-details')).toBeInTheDocument();

    await user.click(reply.getByRole('button', { name: 'Good response' }));
    await waitFor(() => expect(reply.queryByTestId('served-by-details')).not.toBeInTheDocument());
    expect(reply.queryByRole('button', { name: 'Which model answered' })).not.toBeInTheDocument();
  });
});

const EXPERIMENT_REASONING = {
  model: 'gemini-4-argon',
  resolved_model: 'gemini-4-argon-001',
  provider: 'google',
  model_tier: 'gemini_4_argon',
  model_tier_source: 'experiment',
  experiment_id: 'argon-vs-opus',
  experiment_arm: 'gemini_4_argon',
};

describe('which model answered an ordinary reply', () => {
  beforeEach(() => {
    resetLocalStorageMock();
    vi.clearAllMocks();
    window.history.replaceState(null, '', `/chat?conversation_id=${CONVERSATION}`);
  });

  afterEach(() => {
    window.history.replaceState(null, '', '/');
  });

  it('can be shown without rating first', async () => {
    serveConversation(null, {
      model: 'claude-opus-5-5',
      model_tier: 'claude_opus_5_5',
      model_tier_source: 'user',
    });
    const user = userEvent.setup();
    const reply = await openReply();

    await user.click(reply.getByRole('button', { name: 'Which model answered' }));
    const details = reply.getByTestId('served-by-details');
    expect(details).toHaveTextContent('claude-opus-5-5');
    expect(details).toHaveTextContent('chosen by you');
  });
});
