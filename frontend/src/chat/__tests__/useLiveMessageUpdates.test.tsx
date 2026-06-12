import { renderHook, waitFor } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { server } from '../../test/setup.js';
import { useLiveMessageUpdates } from '../useLiveMessageUpdates';

/** Minimal EventSource stand-in: the real one is unavailable under jsdom. */
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

  close() {
    // No-op for tests.
  }

  emit(type: string, payload: Record<string, unknown>) {
    for (const listener of this.listeners.get(type) ?? []) {
      listener({ data: JSON.stringify(payload) });
    }
  }
}

describe('useLiveMessageUpdates', () => {
  let originalEventSource: typeof EventSource;

  beforeEach(() => {
    originalEventSource = globalThis.EventSource;
    globalThis.EventSource = MockEventSource as unknown as typeof EventSource;
    MockEventSource.instances = [];
    Object.defineProperty(document, 'readyState', {
      configurable: true,
      get: () => 'complete',
    });
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
    globalThis.EventSource = originalEventSource;
  });

  const flushConnect = async () => {
    // The hook defers connecting by ~1.5s (requestIdleCallback + setTimeout).
    await vi.advanceTimersByTimeAsync(2000);
  };

  it('subscribes as a follow stream filtered to message,turn_ended with ack_seq=-1', async () => {
    renderHook(() => useLiveMessageUpdates({ conversationId: 'web_conv_live', enabled: true }));

    await flushConnect();

    expect(MockEventSource.instances.length).toBeGreaterThan(0);
    const url = MockEventSource.instances[0].url;
    expect(url).toContain('follow=true');
    expect(url).toContain('from_seq=-1');
    expect(url).toContain('ack_seq=-1');
    expect(url).toContain('event_types=message%2Cturn_ended');
  });

  it('POSTs /ack with the turn_ended seq', async () => {
    const ackBodies: Array<{ conversation_id: string; ack_seq: number }> = [];
    server.use(
      http.post('/api/v1/chat/ack', async ({ request }) => {
        ackBodies.push((await request.json()) as { conversation_id: string; ack_seq: number });
        return HttpResponse.json({ ok: true });
      })
    );

    renderHook(() => useLiveMessageUpdates({ conversationId: 'web_conv_live', enabled: true }));
    await flushConnect();

    MockEventSource.instances[0].emit('turn_ended', { seq: 42, status: 'complete' });

    vi.useRealTimers();
    await waitFor(() => {
      expect(ackBodies.length).toBe(1);
    });
    expect(ackBodies[0]).toEqual({ conversation_id: 'web_conv_live', ack_seq: 42 });
  });

  it('includes turn_id in callback when turn_ended event carries it', async () => {
    const updates: Array<{ conversation_id?: string; turn_id?: string }> = [];
    renderHook(() =>
      useLiveMessageUpdates({
        conversationId: 'web_conv_live',
        enabled: true,
        onMessageReceived: (update) => {
          updates.push({ conversation_id: update.conversation_id, turn_id: update.turn_id });
        },
      })
    );
    await flushConnect();

    MockEventSource.instances[0].emit('turn_ended', {
      seq: 42,
      turn_id: 'turn_xyz_123',
      status: 'complete',
    });

    await vi.runOnlyPendingTimersAsync();

    expect(updates.length).toBeGreaterThan(0);
    const lastUpdate = updates[updates.length - 1];
    expect(lastUpdate.turn_id).toBe('turn_xyz_123');
  });

  it('does not include turn_id when turn_ended event omits it', async () => {
    const updates: Array<{ conversation_id?: string; turn_id?: string }> = [];
    renderHook(() =>
      useLiveMessageUpdates({
        conversationId: 'web_conv_live',
        enabled: true,
        onMessageReceived: (update) => {
          updates.push({ conversation_id: update.conversation_id, turn_id: update.turn_id });
        },
      })
    );
    await flushConnect();

    MockEventSource.instances[0].emit('turn_ended', { seq: 42, status: 'complete' });

    await vi.runOnlyPendingTimersAsync();

    expect(updates.length).toBeGreaterThan(0);
    const lastUpdate = updates[updates.length - 1];
    expect(lastUpdate.turn_id).toBeUndefined();
  });

  it('skips the reload on the initial open but reloads on a reconnect open', async () => {
    const updates: Array<{ new_messages?: boolean }> = [];
    renderHook(() =>
      useLiveMessageUpdates({
        conversationId: 'web_conv_live',
        enabled: true,
        onMessageReceived: (update) => {
          updates.push({ new_messages: update.new_messages });
        },
      })
    );
    await flushConnect();

    // Initial connect: history was already loaded on mount, so an open here
    // must NOT trigger a redundant (clobber-prone) reload.
    MockEventSource.instances[0].emit('open', {});
    await vi.runOnlyPendingTimersAsync();
    expect(updates.length).toBe(0);

    // A second open represents a reconnect, which may have missed events while
    // offline and therefore must trigger a catch-up reload.
    MockEventSource.instances[0].emit('open', {});
    await vi.runOnlyPendingTimersAsync();
    expect(updates.length).toBe(1);
    expect(updates[0].new_messages).toBe(true);
  });
});
