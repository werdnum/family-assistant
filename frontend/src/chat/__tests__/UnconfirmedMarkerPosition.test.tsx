import { screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { HttpResponse, http } from 'msw';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { resetLocalStorageMock } from '../../test/mocks/localStorageMock';
import { server } from '../../test/setup.js';
import { renderChatApp } from '../../test/utils/renderChatApp';
import { streamResumeTuning } from '../useStreamingResponse';

function sse(frames: string[]) {
  const encoder = new TextEncoder();
  const stream = new ReadableStream({
    start(controller) {
      for (const frame of frames) {
        controller.enqueue(encoder.encode(frame));
      }
      controller.close();
    },
  });
  return new HttpResponse(stream, { headers: { 'Content-Type': 'text/event-stream' } });
}

describe('Unconfirmed reply marker position', () => {
  const originalTuning = { ...streamResumeTuning };

  beforeEach(() => {
    resetLocalStorageMock();
    vi.clearAllMocks();
    window.history.replaceState({}, '', '/chat');
    streamResumeTuning.initialDelayMs = 1;
    streamResumeTuning.maxDelayMs = 2;
  });

  afterEach(() => {
    Object.assign(streamResumeTuning, originalTuning);
  });

  it('stays under its own turn when later messages follow it', { timeout: 30000 }, async () => {
    // History reloads on every new message. The marker belongs to the turn
    // that gave up, so a reload that brings in later rows must not move it
    // down under them.
    let ourTurnId = '';
    server.use(
      http.post('/api/v1/chat/turns', async ({ request }) => {
        const body = (await request.json()) as { turn_id: string; conversation_id?: string };
        ourTurnId = body.turn_id;
        return HttpResponse.json({
          turn_id: body.turn_id,
          conversation_id: body.conversation_id || 'web_conv_marker_position',
          first_seq: 0,
        });
      }),
      http.get('/api/v1/chat/conversations/:conversationId/messages', () =>
        HttpResponse.json({
          messages: [
            {
              internal_id: 'persisted-user',
              role: 'user',
              content: 'Delegate the thing',
              timestamp: '2026-06-09T10:00:00Z',
              turn_id: ourTurnId,
            },
            {
              internal_id: 'later-assistant',
              role: 'assistant',
              content: 'Approved action completed.',
              timestamp: '2026-06-09T10:20:00Z',
            },
          ],
        })
      ),
      http.get('/api/v1/chat/conversations/:conversationId/stream', () =>
        sse([`event: stream_dropped\ndata: ${JSON.stringify({ reason: 'server_shutdown' })}\n\n`])
      )
    );

    const user = userEvent.setup();
    await renderChatApp({ waitForReady: true });

    const input = screen.getByPlaceholderText('Message Family Assistant...');
    await user.type(input, 'Delegate the thing');
    await user.keyboard('{Enter}');

    const marker = await screen.findByText(/couldn't confirm the reply/i, {}, { timeout: 10000 });
    const later = screen.getByText('Approved action completed.');
    expect(marker.compareDocumentPosition(later) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });
});
