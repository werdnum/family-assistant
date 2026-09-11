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

const TOTAL_ROWS = 120;

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
  // Each test opens its own conversation, so a follow stream opened late by a
  // previous test's app can't be mistaken for this test's.
  let testIndex = 0;
  let conversationId: string;

  beforeEach(() => {
    resetLocalStorageMock();
    vi.clearAllMocks();
    window.history.replaceState({}, '', '/chat');
    originalEventSource = globalThis.EventSource;
    globalThis.EventSource = MockEventSource as unknown as typeof EventSource;
    MockEventSource.instances = [];

    testIndex += 1;
    conversationId = `web_conv_long_${testIndex}`;
    mockLocalStorage.getItem.mockImplementation((key: string) =>
      key === 'lastConversationId' ? conversationId : null
    );

    requestedLimits = [];
    const rows = Array.from({ length: TOTAL_ROWS }, (_, index) => historyRow(index));
    server.use(
      http.get('/api/v1/chat/conversations/:conversationId/messages', ({ params, request }) => {
        const limit = Number(new URL(request.url).searchParams.get('limit'));
        // limit=1 is the active-turn poll, not a history load.
        if (params.conversationId === conversationId && limit !== 1) {
          requestedLimits.push(limit);
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

  it('opens on the latest page and widens the window a page at a time', async () => {
    await renderChatApp({ waitForReady: true });

    await screen.findByText(`Message ${TOTAL_ROWS - 1}`, {}, { timeout: 5000 });
    expect(screen.getByText('Message 70')).toBeInTheDocument();
    expect(screen.queryByText('Message 69')).not.toBeInTheDocument();
    expect(requestedLimits).toEqual([50]);

    fireEvent.click(screen.getByRole('button', { name: /load earlier messages/i }));
    await screen.findByText('Message 20', {}, { timeout: 5000 });
    expect(screen.queryByText('Message 19')).not.toBeInTheDocument();
    expect(requestedLimits[requestedLimits.length - 1]).toBe(100);

    fireEvent.click(screen.getByRole('button', { name: /load earlier messages/i }));
    await screen.findByText('Message 0', {}, { timeout: 5000 });
    expect(requestedLimits[requestedLimits.length - 1]).toBe(150);
    await waitFor(() => {
      expect(
        screen.queryByRole('button', { name: /load earlier messages/i })
      ).not.toBeInTheDocument();
    });
  });

  it('keeps the loaded older messages across a background reload', async () => {
    await renderChatApp({ waitForReady: true });

    await screen.findByText(`Message ${TOTAL_ROWS - 1}`, {}, { timeout: 5000 });
    fireEvent.click(screen.getByRole('button', { name: /load earlier messages/i }));
    await screen.findByText('Message 20', {}, { timeout: 5000 });

    // The follow stream connects once the browser is idle, not at mount.
    const followStream = await waitFor(
      () => {
        const streams = MockEventSource.instances.filter((es) =>
          es.url.includes(`/conversations/${conversationId}/`)
        );
        expect(streams.length).toBeGreaterThan(0);
        return streams[streams.length - 1];
      },
      { timeout: 3000 }
    );
    const requestsBeforeReload = requestedLimits.length;
    followStream.emit('turn_ended', { seq: 1, status: 'complete' });

    await waitFor(() => {
      expect(requestedLimits.length).toBeGreaterThan(requestsBeforeReload);
    });
    expect(requestedLimits[requestedLimits.length - 1]).toBe(100);
    expect(screen.getByText('Message 20')).toBeInTheDocument();
  });
});
