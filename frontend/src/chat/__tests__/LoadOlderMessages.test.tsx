import { fireEvent, screen, waitFor } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { mockLocalStorage, resetLocalStorageMock } from '../../test/mocks/localStorageMock';
import { server } from '../../test/setup.js';
import { renderChatApp } from '../../test/utils/renderChatApp';

/**
 * A long conversation opens on its most recent page of history; "Load earlier
 * messages" widens the window, and later reloads keep the widened window.
 */

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

  close() {}

  emit(type: string, payload: Record<string, unknown>) {
    for (const listener of this.listeners.get(type) ?? []) {
      listener({ data: JSON.stringify(payload) });
    }
  }
}

const CONVERSATION_ID = 'web_conv_long';
const INITIAL_ROWS = 120;

function historyRow(index: number) {
  return {
    internal_id: `row_${index}`,
    role: index % 2 === 0 ? 'user' : 'assistant',
    content: `Message ${index}`,
    timestamp: new Date(Date.UTC(2026, 0, 1, 0, index)).toISOString(),
  };
}

describe('Loading earlier messages', () => {
  let originalEventSource: typeof EventSource;
  let requestedLimits: number[];
  let rows: Array<ReturnType<typeof historyRow>>;
  // When set, loads wider than one page wait on it before responding.
  let widenedLoadGate: Promise<void> | null;
  let widenedLoadServed: boolean;

  beforeEach(() => {
    resetLocalStorageMock();
    vi.clearAllMocks();
    window.history.replaceState({}, '', '/chat');
    originalEventSource = globalThis.EventSource;
    globalThis.EventSource = MockEventSource as unknown as typeof EventSource;
    MockEventSource.instances = [];

    mockLocalStorage.getItem.mockImplementation((key: string) =>
      key === 'lastConversationId' ? CONVERSATION_ID : null
    );

    requestedLimits = [];
    rows = Array.from({ length: INITIAL_ROWS }, (_, index) => historyRow(index));
    widenedLoadGate = null;
    widenedLoadServed = false;
    server.use(
      http.get('/api/v1/chat/conversations/:conversationId/messages', async ({ request }) => {
        const limit = Number(new URL(request.url).searchParams.get('limit'));
        // limit=1 is the active-turn poll, not a history load.
        if (limit !== 1) {
          requestedLimits.push(limit);
        }
        if (widenedLoadGate && limit > 50) {
          await widenedLoadGate;
          widenedLoadServed = true;
        }
        const page = rows.slice(Math.max(0, rows.length - limit));
        return HttpResponse.json({
          messages: page,
          has_more_before: page.length < rows.length,
        });
      })
    );
  });

  afterEach(() => {
    globalThis.EventSource = originalEventSource;
  });

  const openConversation = async () => {
    await renderChatApp({ waitForReady: true });
    await screen.findByText(`Message ${INITIAL_ROWS - 1}`, {}, { timeout: 5000 });
  };

  const clickLoadEarlier = () => {
    fireEvent.click(screen.getByRole('button', { name: /load earlier messages/i }));
  };

  it('opens on the latest page and widens the window a page at a time', async () => {
    await openConversation();
    expect(screen.getByText('Message 70')).toBeInTheDocument();
    expect(screen.queryByText('Message 69')).not.toBeInTheDocument();
    expect(requestedLimits).toEqual([50]);

    clickLoadEarlier();
    await screen.findByText('Message 20', {}, { timeout: 5000 });
    expect(screen.queryByText('Message 19')).not.toBeInTheDocument();
    expect(requestedLimits[requestedLimits.length - 1]).toBe(100);

    clickLoadEarlier();
    await screen.findByText('Message 0', {}, { timeout: 5000 });
    expect(requestedLimits[requestedLimits.length - 1]).toBe(150);
    await waitFor(() => {
      expect(
        screen.queryByRole('button', { name: /load earlier messages/i })
      ).not.toBeInTheDocument();
    });
  });

  it('keeps the oldest loaded message when a reload brings new messages', async () => {
    await openConversation();
    clickLoadEarlier();
    await screen.findByText('Message 20', {}, { timeout: 5000 });

    // The follow stream connects once the browser is idle, not at mount.
    const followStream = await waitFor(
      () => {
        const streams = MockEventSource.instances.filter((es) =>
          es.url.includes(`/conversations/${CONVERSATION_ID}/`)
        );
        expect(streams.length).toBeGreaterThan(0);
        return streams[streams.length - 1];
      },
      { timeout: 3000 }
    );
    rows.push(historyRow(120), historyRow(121), historyRow(122));
    followStream.emit('turn_ended', { seq: 1, status: 'complete' });

    await screen.findByText('Message 122', {}, { timeout: 5000 });
    expect(requestedLimits[requestedLimits.length - 1]).toBe(150);
    expect(screen.getByText('Message 20')).toBeInTheDocument();
    expect(screen.queryByText('Message 19')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: /load earlier messages/i })).toBeInTheDocument();
  });

  it('drops an earlier-history load that finishes after starting a new chat', async () => {
    await openConversation();
    let releaseWidenedLoad: (() => void) | undefined;
    widenedLoadGate = new Promise<void>((resolve) => {
      releaseWidenedLoad = resolve;
    });

    clickLoadEarlier();
    await waitFor(() => {
      expect(requestedLimits).toContain(100);
    });
    fireEvent.click(screen.getAllByTestId('new-chat-button')[0]);
    await screen.findByText('How can I help you?', {}, { timeout: 5000 });

    releaseWidenedLoad?.();
    await waitFor(() => {
      expect(widenedLoadServed).toBe(true);
    });
    await screen.findByText('How can I help you?');
    expect(screen.queryByText(`Message ${INITIAL_ROWS - 1}`)).not.toBeInTheDocument();
  });
});
