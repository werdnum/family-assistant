import { screen, waitFor } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { mockLocalStorage, resetLocalStorageMock } from '../../test/mocks/localStorageMock';
import { server } from '../../test/setup.js';
import { renderChatApp } from '../../test/utils/renderChatApp';

/**
 * Restores the in-app notification for out-of-band assistant replies. The hub's
 * live message/turn_ended events are content-free, so the notification is now
 * derived from the background history reload that the follow stream triggers.
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

// ChatApp opens two EventSources: the per-conversation follow stream
// (`/conversations/{id}/stream`) and the account-global activity stream
// (`/chat/activity/stream`). turn_ended lives on the follow stream, so select it
// by URL rather than assuming a particular instantiation order.
function latestFollowStream(): MockEventSource {
  const followStreams = MockEventSource.instances.filter((es) =>
    es.url.includes('/conversations/')
  );
  const stream = followStreams[followStreams.length - 1];
  if (!stream) {
    throw new Error('No conversation follow stream EventSource was opened');
  }
  return stream;
}

describe('In-app notification restore', () => {
  let originalEventSource: typeof EventSource;
  let originalBroadcastChannel: typeof BroadcastChannel;
  let notificationCtor: ReturnType<typeof vi.fn>;
  let assistantTurns: number;

  beforeEach(() => {
    resetLocalStorageMock();
    vi.clearAllMocks();
    window.history.replaceState({}, '', '/chat');

    originalEventSource = globalThis.EventSource;
    originalBroadcastChannel = globalThis.BroadcastChannel;
    globalThis.EventSource = MockEventSource as unknown as typeof EventSource;
    MockEventSource.instances = [];
    // Force single-tab leadership in the notification coordinator.
    globalThis.BroadcastChannel = undefined as unknown as typeof BroadcastChannel;

    // Notifications enabled + page hidden + permission granted so the only
    // missing piece is the (now restored) content-bearing message.
    mockLocalStorage.getItem.mockImplementation((key: string) => {
      if (key === 'notificationsEnabled') {
        return 'true';
      }
      if (key === 'lastConversationId') {
        return 'web_conv_notify';
      }
      return null;
    });
    Object.defineProperty(document, 'visibilityState', {
      configurable: true,
      get: () => 'hidden',
    });

    notificationCtor = vi.fn();
    class FakeNotification {
      onclick: (() => void) | null = null;
      static permission = 'granted';
      static requestPermission = vi.fn().mockResolvedValue('granted');
      constructor(title: string, options?: unknown) {
        notificationCtor(title, options);
      }
      close() {}
    }
    globalThis.Notification = FakeNotification as unknown as typeof Notification;

    // History grows by one assistant message on the second fetch, simulating
    // an out-of-band reply arriving while the tab is backgrounded.
    assistantTurns = 1;
    server.use(
      http.get('/api/v1/chat/conversations/:conversationId/messages', ({ params }) => {
        expect(params.conversationId).toBe('web_conv_notify');
        const messages = [
          {
            internal_id: 'a1',
            role: 'assistant',
            content: 'First reply',
            timestamp: '2026-06-09T10:00:00Z',
          },
        ];
        if (assistantTurns >= 2) {
          messages.push({
            internal_id: 'a2',
            role: 'assistant',
            content: 'Out-of-band scheduled reply',
            timestamp: '2026-06-09T10:05:00Z',
          });
        }
        return HttpResponse.json({ messages });
      })
    );
  });

  afterEach(() => {
    globalThis.EventSource = originalEventSource;
    globalThis.BroadcastChannel = originalBroadcastChannel;
  });

  it('fires an in-app notification when a background reload surfaces a new assistant message', async () => {
    await renderChatApp({ waitForReady: true });

    // Initial load establishes the baseline (First reply) without notifying.
    await waitFor(
      () => {
        expect(screen.getByText('First reply')).toBeInTheDocument();
      },
      { timeout: 5000 }
    );
    expect(notificationCtor).not.toHaveBeenCalled();

    await waitFor(
      () => {
        expect(MockEventSource.instances.some((es) => es.url.includes('/conversations/'))).toBe(
          true
        );
      },
      { timeout: 3000 }
    );

    // An out-of-band assistant reply arrives: the next history reload includes
    // it, and the follow stream's content-free turn_ended drives a background
    // reload that should fire the notification.
    assistantTurns = 2;
    latestFollowStream().emit('turn_ended', {
      seq: 1,
      status: 'complete',
    });

    await waitFor(
      () => {
        expect(notificationCtor).toHaveBeenCalledTimes(1);
      },
      { timeout: 5000 }
    );
    const [, options] = notificationCtor.mock.calls[0];
    expect((options as { body?: string }).body).toBe('Out-of-band scheduled reply');
  });
});
