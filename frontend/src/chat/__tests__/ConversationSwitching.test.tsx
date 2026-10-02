import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { HttpResponse, http } from 'msw';
import { vi } from 'vitest';
import { resetLocalStorageMock } from '../../test/mocks/localStorageMock';
import { server } from '../../test/setup.js';
import { renderChatApp } from '../../test/utils/renderChatApp';

const CONVERSATIONS = ['conv-a', 'conv-b', 'conv-c'];

// A promise the test resolves by hand, to hold a request open.
const gate = (): { promise: Promise<void>; open: () => void } => {
  let open = () => {};
  const promise = new Promise<void>((resolve) => {
    open = resolve;
  });
  return { promise, open };
};

const historyFor = (conversationId: string) =>
  HttpResponse.json({
    messages: [
      {
        internal_id: `${conversationId}-msg-1`,
        role: 'user',
        content: `Hello from ${conversationId}`,
        timestamp: '2025-01-01T09:00:00Z',
      },
    ],
  });

// Serves the sidebar list and every conversation's history; a test overrides
// what one conversation's history request does by putting it in `respond`.
const serveConversations = (
  respond: Record<string, () => Promise<Response> | Response> = {}
): void => {
  server.use(
    http.get('/api/v1/chat/conversations', () =>
      HttpResponse.json({
        conversations: CONVERSATIONS.map((id) => ({
          conversation_id: id,
          last_message: `Preview of ${id}`,
          last_timestamp: '2025-01-01T10:00:00Z',
          message_count: 1,
        })),
        count: CONVERSATIONS.length,
      })
    ),
    http.get('/api/v1/chat/conversations/:conversationId/messages', ({ params }) => {
      const conversationId = String(params.conversationId);
      const override = respond[conversationId];
      return override ? override() : historyFor(conversationId);
    })
  );
};

const openConversation = async (
  user: ReturnType<typeof userEvent.setup>,
  conversationId: string
): Promise<void> => {
  await user.click(await screen.findByTestId(`conversation-item-${conversationId}`));
};

const composerAttachmentNames = (): string[] =>
  within(screen.getByTestId('composer-container'))
    .queryAllByText(/\.txt$/)
    .map((element) => element.textContent ?? '');

describe('Switching conversations', () => {
  beforeEach(() => {
    resetLocalStorageMock();
    vi.clearAllMocks();
    window.history.replaceState({}, '', '/');
    document.documentElement.removeAttribute('data-app-ready');
  });

  it("shows a failed conversation's error, never the previous transcript, and recovers on Retry", async () => {
    const user = userEvent.setup();
    let failB = true;
    serveConversations({
      'conv-b': () =>
        failB
          ? HttpResponse.json({ detail: 'unavailable' }, { status: 503 })
          : historyFor('conv-b'),
    });
    let turnsStarted = 0;
    server.use(
      http.post('/api/v1/chat/turns', () => {
        turnsStarted += 1;
        return undefined;
      })
    );
    await renderChatApp({ waitForReady: true });

    await openConversation(user, 'conv-a');
    expect(await screen.findByText('Hello from conv-a')).toBeInTheDocument();

    await openConversation(user, 'conv-b');

    expect(await screen.findByTestId('conversation-load-error')).toBeInTheDocument();
    expect(screen.queryByText('Hello from conv-a')).not.toBeInTheDocument();
    expect(window.location.search).toBe('?conversation_id=conv-b');

    // Text can still be drafted, but it cannot be sent into a conversation
    // whose history the user has not seen.
    await user.type(screen.getByTestId('chat-input'), 'follow-up{Enter}');
    expect(screen.getByTestId('send-button')).toBeDisabled();
    expect(screen.getByTestId('chat-input')).toHaveValue('follow-up');
    expect(turnsStarted).toBe(0);

    failB = false;
    await user.click(screen.getByRole('button', { name: 'Retry' }));

    expect(await screen.findByText('Hello from conv-b')).toBeInTheDocument();
    expect(screen.queryByTestId('conversation-load-error')).not.toBeInTheDocument();
    await waitFor(() => expect(screen.getByTestId('send-button')).toBeEnabled());
  });

  it('shows the destination loading, not the previous transcript, while its history is in flight', async () => {
    const user = userEvent.setup();
    const releaseB = gate();
    serveConversations({
      'conv-b': async () => {
        await releaseB.promise;
        return historyFor('conv-b');
      },
    });
    await renderChatApp({ waitForReady: true });

    await openConversation(user, 'conv-a');
    expect(await screen.findByText('Hello from conv-a')).toBeInTheDocument();

    await openConversation(user, 'conv-b');

    expect(await screen.findByTestId('conversation-loading')).toBeInTheDocument();
    expect(screen.queryByText('Hello from conv-a')).not.toBeInTheDocument();
    await user.type(screen.getByTestId('chat-input'), 'too early');
    expect(screen.getByTestId('send-button')).toBeDisabled();

    releaseB.open();
    expect(await screen.findByText('Hello from conv-b')).toBeInTheDocument();
    await waitFor(() => expect(screen.getByTestId('send-button')).toBeEnabled());
  });

  it('shows only the last conversation chosen when switching faster than history loads', async () => {
    const user = userEvent.setup();
    const releaseB = gate();
    serveConversations({
      'conv-b': async () => {
        await releaseB.promise;
        return historyFor('conv-b');
      },
    });
    await renderChatApp({ waitForReady: true });

    await openConversation(user, 'conv-b');
    await screen.findByTestId('conversation-loading');
    await openConversation(user, 'conv-c');
    expect(await screen.findByText('Hello from conv-c')).toBeInTheDocument();

    // B's response arriving late must not replace C's transcript.
    releaseB.open();
    await waitFor(() => expect(screen.getByText('Hello from conv-c')).toBeInTheDocument());
    expect(screen.queryByText('Hello from conv-b')).not.toBeInTheDocument();
    expect(screen.queryByTestId('conversation-loading')).not.toBeInTheDocument();
    expect(window.location.search).toBe('?conversation_id=conv-c');
  });

  it("keeps each conversation's text and attachments together and restores them on return", async () => {
    const user = userEvent.setup();
    serveConversations();
    await renderChatApp({ waitForReady: true });

    await openConversation(user, 'conv-a');
    await screen.findByText('Hello from conv-a');
    await user.type(screen.getByTestId('chat-input'), 'draft for A');
    await user.upload(
      screen.getByTestId('file-input'),
      new File(['a'], 'first-conversation.txt', { type: 'text/plain' })
    );
    await waitFor(() => expect(composerAttachmentNames()).toEqual(['first-conversation.txt']));

    await openConversation(user, 'conv-b');
    await screen.findByText('Hello from conv-b');

    expect(screen.getByTestId('chat-input')).toHaveValue('');
    expect(composerAttachmentNames()).toEqual([]);

    await user.type(screen.getByTestId('chat-input'), 'draft for B');

    await openConversation(user, 'conv-a');
    await screen.findByText('Hello from conv-a');

    expect(screen.getByTestId('chat-input')).toHaveValue('draft for A');
    await waitFor(() => expect(composerAttachmentNames()).toEqual(['first-conversation.txt']));

    await openConversation(user, 'conv-b');
    await screen.findByText('Hello from conv-b');
    expect(screen.getByTestId('chat-input')).toHaveValue('draft for B');
    expect(composerAttachmentNames()).toEqual([]);
  });

  it('does not leave a conversation while its message is still uploading an attachment', async () => {
    const user = userEvent.setup();
    serveConversations();
    const releaseUpload = gate();
    const turnConversations: (string | undefined)[] = [];
    server.use(
      http.post('/api/attachments/upload', async () => {
        await releaseUpload.promise;
        return HttpResponse.json({
          attachment_id: 'uploaded-1',
          filename: 'first-conversation.txt',
          content_type: 'text/plain',
          size: 1,
          url: '/api/attachments/uploaded-1',
        });
      }),
      http.post('/api/v1/chat/turns', async ({ request }) => {
        const body = (await request.clone().json()) as { conversation_id?: string };
        turnConversations.push(body.conversation_id);
        // Fall through to the default handler, which runs the turn.
        return undefined;
      })
    );
    await renderChatApp({ waitForReady: true });

    await openConversation(user, 'conv-a');
    await screen.findByText('Hello from conv-a');
    await user.type(screen.getByTestId('chat-input'), 'message for A');
    await user.upload(
      screen.getByTestId('file-input'),
      new File(['a'], 'first-conversation.txt', { type: 'text/plain' })
    );
    await waitFor(() => expect(composerAttachmentNames()).toEqual(['first-conversation.txt']));
    await user.click(screen.getByTestId('send-button'));
    await waitFor(() => expect(screen.getByTestId('chat-input')).toHaveValue(''));

    await openConversation(user, 'conv-b');
    expect(window.location.search).toBe('?conversation_id=conv-a');

    releaseUpload.open();
    await waitFor(() => expect(turnConversations).toEqual(['conv-a']));
  });

  it('does not change profile while its message is still uploading an attachment', async () => {
    // Radix Select needs pointer-capture and scrolling APIs jsdom lacks.
    const proto = window.HTMLElement.prototype as unknown as Record<string, unknown>;
    const stubbed = ['hasPointerCapture', 'releasePointerCapture', 'scrollIntoView'].filter(
      (method) => !(method in proto)
    );
    for (const method of stubbed) {
      proto[method] = vi.fn();
    }
    try {
      const user = userEvent.setup({ pointerEventsCheck: 0 });
      serveConversations();
      const releaseUpload = gate();
      const turns: { conversation_id?: string; profile_id?: string }[] = [];
      server.use(
        http.post('/api/attachments/upload', async () => {
          await releaseUpload.promise;
          return HttpResponse.json({
            attachment_id: 'uploaded-1',
            filename: 'first-conversation.txt',
            content_type: 'text/plain',
            size: 1,
            url: '/api/attachments/uploaded-1',
          });
        }),
        http.post('/api/v1/chat/turns', async ({ request }) => {
          turns.push((await request.clone().json()) as (typeof turns)[number]);
          return undefined;
        })
      );
      await renderChatApp({ waitForReady: true });

      await openConversation(user, 'conv-a');
      await screen.findByText('Hello from conv-a');
      await user.type(screen.getByTestId('chat-input'), 'message for A');
      await user.upload(
        screen.getByTestId('file-input'),
        new File(['a'], 'first-conversation.txt', { type: 'text/plain' })
      );
      await waitFor(() => expect(composerAttachmentNames()).toEqual(['first-conversation.txt']));
      await user.click(screen.getByTestId('send-button'));
      await waitFor(() => expect(screen.getByTestId('chat-input')).toHaveValue(''));

      await user.click(screen.getByRole('combobox', { name: 'Processing profile' }));
      await user.click(await screen.findByRole('option', { name: /research/i }));

      releaseUpload.open();
      await waitFor(() => expect(turns).toHaveLength(1));
      expect(turns[0]).toMatchObject({
        conversation_id: 'conv-a',
        profile_id: 'default_assistant',
      });
      expect(window.location.search).toBe('?conversation_id=conv-a');
    } finally {
      for (const method of stubbed) {
        delete proto[method];
      }
    }
  });
});

class MockEventSource {
  static instances: MockEventSource[] = [];
  readonly url: string;
  private listeners = new Map<string, Array<(event: { data: string }) => void>>();
  onerror: ((event: unknown) => void) | null = null;

  constructor(url: string) {
    this.url = url;
    MockEventSource.instances.push(this);
  }

  addEventListener(type: string, listener: (event: { data: string }) => void) {
    const listeners = this.listeners.get(type) ?? [];
    listeners.push(listener);
    this.listeners.set(type, listeners);
  }

  removeEventListener() {}

  close() {}

  emit(type: string, payload: Record<string, unknown>) {
    for (const listener of this.listeners.get(type) ?? []) {
      listener({ data: JSON.stringify(payload) });
    }
  }
}

describe('A live update while a conversation is opening', () => {
  let originalEventSource: typeof EventSource;

  beforeEach(() => {
    resetLocalStorageMock();
    window.history.replaceState({}, '', '/');
    document.documentElement.removeAttribute('data-app-ready');
    originalEventSource = globalThis.EventSource;
    globalThis.EventSource = MockEventSource as unknown as typeof EventSource;
    MockEventSource.instances = [];
  });

  afterEach(() => {
    globalThis.EventSource = originalEventSource;
  });

  const followStreamFor = async (conversationId: string): Promise<MockEventSource> => {
    let stream: MockEventSource | undefined;
    await waitFor(
      () => {
        // The follow stream connects a moment after the page goes idle.
        const streams = MockEventSource.instances.filter((es) =>
          es.url.includes(`/conversations/${conversationId}/`)
        );
        stream = streams[streams.length - 1];
        expect(stream).toBeDefined();
      },
      { timeout: 3000 }
    );
    return stream as MockEventSource;
  };

  it('still ends the open in an error with Retry when the reload that took it over fails', async () => {
    const user = userEvent.setup();
    let requestsForB = 0;
    serveConversations({
      'conv-b': () => {
        requestsForB += 1;
        // The open itself never answers; the live update's reload aborts it.
        return requestsForB === 1
          ? new Promise<Response>(() => {})
          : HttpResponse.json({ detail: 'unavailable' }, { status: 503 });
      },
    });
    await renderChatApp({ waitForReady: true });

    await openConversation(user, 'conv-b');
    await screen.findByTestId('conversation-loading');
    (await followStreamFor('conv-b')).emit('turn_ended', { seq: 1, status: 'complete' });

    expect(await screen.findByTestId('conversation-load-error')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Retry' })).toBeEnabled();
  });

  it('still ends the open with the history when the reload that took it over succeeds', async () => {
    const user = userEvent.setup();
    let requestsForB = 0;
    serveConversations({
      'conv-b': () => {
        requestsForB += 1;
        return requestsForB === 1 ? new Promise<Response>(() => {}) : historyFor('conv-b');
      },
    });
    await renderChatApp({ waitForReady: true });

    await openConversation(user, 'conv-b');
    await screen.findByTestId('conversation-loading');
    (await followStreamFor('conv-b')).emit('turn_ended', { seq: 1, status: 'complete' });

    expect(await screen.findByText('Hello from conv-b')).toBeInTheDocument();
    expect(screen.queryByTestId('conversation-loading')).not.toBeInTheDocument();
    await waitFor(() =>
      expect(screen.getByRole('combobox', { name: 'Processing profile' })).toBeEnabled()
    );
  });
});
