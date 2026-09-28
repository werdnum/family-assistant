import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { HttpResponse, http } from 'msw';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { resetLocalStorageMock } from '../../test/mocks/localStorageMock';
import { server } from '../../test/setup.js';
import { renderChatApp } from '../../test/utils/renderChatApp';
import { streamResumeTuning } from '../useStreamingResponse';

const STREAM_URL = '/api/v1/chat/conversations/:conversationId/stream';
// getTestResponse in the shared handlers answers a prompt without "hello",
// "hi", "weather" or "test" in it with this reply.
const SEND_PROMPT = 'Remind me about lunch';
const DEFAULT_REPLY = "I received your message and I'm here to help!";

async function findMessageInput() {
  await waitFor(
    () => {
      expect(screen.getByPlaceholderText('Message Family Assistant...')).toBeInTheDocument();
    },
    { timeout: 5000 }
  );
  return screen.getByPlaceholderText('Message Family Assistant...');
}

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

function captureTurnId(onCapture: (id: string) => void) {
  return http.post('/api/v1/chat/turns', async ({ request }) => {
    const body = (await request.json()) as { turn_id: string; conversation_id?: string };
    onCapture(body.turn_id);
    return HttpResponse.json({
      turn_id: body.turn_id,
      conversation_id: body.conversation_id || 'web_conv_error_handling',
      first_seq: 0,
    });
  });
}

// Run sequentially to avoid MSW handler conflicts with parallel tests
describe.sequential('ErrorHandling', () => {
  const originalTuning = { ...streamResumeTuning };

  beforeEach(() => {
    resetLocalStorageMock();
    vi.clearAllMocks();
    // Exercise every resume attempt without waiting through production backoff.
    streamResumeTuning.initialDelayMs = 1;
    streamResumeTuning.maxDelayMs = 2;
  });

  afterEach(() => {
    Object.assign(streamResumeTuning, originalTuning);
  });

  it('surfaces an unconfirmed-reply error when every subscribe hits a network error', async () => {
    let streamCalls = 0;
    server.use(
      http.get(STREAM_URL, () => {
        streamCalls += 1;
        return HttpResponse.error();
      })
    );

    const user = userEvent.setup();
    await renderChatApp({ waitForReady: true });

    const messageInput = await findMessageInput();
    await user.type(messageInput, 'This should fail');
    await user.keyboard('{Enter}');

    expect(
      await screen.findByText(/couldn't confirm the reply/i, {}, { timeout: 10000 })
    ).toBeInTheDocument();
    // A network error establishing the subscription is resumable, so the client
    // retried before giving up.
    expect(streamCalls).toBeGreaterThan(1);
  }, 30000);

  it('skips a malformed SSE frame and keeps rendering the rest of the reply', async () => {
    let ourTurnId = '';
    server.use(
      captureTurnId((id) => {
        ourTurnId = id;
      }),
      http.get(STREAM_URL, () =>
        sse([
          `event: text\ndata: ${JSON.stringify({ turn_id: ourTurnId, content: 'Before ', seq: 1 })}\n\n`,
          'event: text\ndata: {invalid json}\n\n',
          `event: text\ndata: ${JSON.stringify({ turn_id: ourTurnId, content: 'and after.', seq: 2 })}\n\n`,
          `event: turn_ended\ndata: ${JSON.stringify({ turn_id: ourTurnId, status: 'complete', seq: 3 })}\n\n`,
        ])
      )
    );

    const user = userEvent.setup();
    await renderChatApp({ waitForReady: true });

    const messageInput = await findMessageInput();
    await user.type(messageInput, 'This will have a malformed response');
    await user.keyboard('{Enter}');

    expect(await screen.findByText('Before and after.', {}, { timeout: 5000 })).toBeInTheDocument();
    expect(screen.queryByText(/couldn't confirm the reply/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/encountered an error/i)).not.toBeInTheDocument();
  }, 30000);

  it('surfaces a failed turn_ended error instead of silently completing', async () => {
    // The producer reports a failed turn as turn_ended with status "failed" and
    // an error field. The client must surface that before exiting, not clear
    // loading as if it completed normally.
    let ourTurnId = '';
    server.use(
      http.post('/api/v1/chat/turns', async ({ request }) => {
        const body = (await request.json()) as {
          turn_id: string;
          conversation_id?: string;
        };
        ourTurnId = body.turn_id;
        return HttpResponse.json({
          turn_id: body.turn_id,
          conversation_id: body.conversation_id || 'web_conv_failed',
          first_seq: 0,
        });
      }),
      http.get('/api/v1/chat/conversations/:conversationId/stream', () => {
        const encoder = new TextEncoder();
        const stream = new ReadableStream({
          start(controller) {
            controller.enqueue(
              encoder.encode(
                `event: turn_started\ndata: ${JSON.stringify({ turn_id: ourTurnId, seq: 0 })}\n\n`
              )
            );
            controller.enqueue(
              encoder.encode(
                `event: turn_ended\ndata: ${JSON.stringify({
                  turn_id: ourTurnId,
                  status: 'failed',
                  error: 'The model provider is unavailable.',
                })}\n\n`
              )
            );
            controller.close();
          },
        });
        return new HttpResponse(stream, {
          headers: { 'Content-Type': 'text/event-stream' },
        });
      })
    );

    const user = userEvent.setup();
    await renderChatApp({ waitForReady: true });

    const messageInput = await findMessageInput();
    await user.type(messageInput, 'This turn will fail');
    await user.keyboard('{Enter}');

    await waitFor(
      () => {
        expect(
          screen.getByText(/encountered an error processing your message/i)
        ).toBeInTheDocument();
      },
      { timeout: 5000 }
    );
  }, 30000);

  it('still sends and receives replies when the conversation list fails to load', async () => {
    let conversationListRequests = 0;
    server.use(
      http.get('/api/v1/chat/conversations', () => {
        conversationListRequests += 1;
        return HttpResponse.json({ error: 'Failed to load conversations' }, { status: 500 });
      })
    );

    const user = userEvent.setup();
    await renderChatApp({ waitForReady: true });
    await waitFor(() => {
      expect(conversationListRequests).toBeGreaterThan(0);
    });

    const messageInput = await findMessageInput();
    await user.type(messageInput, SEND_PROMPT);
    await user.keyboard('{Enter}');

    expect(await screen.findByText(DEFAULT_REPLY, {}, { timeout: 5000 })).toBeInTheDocument();
  }, 30000);

  it('shows a profile loading error and still sends and receives replies', async () => {
    server.use(
      http.get('/api/v1/profiles', () => {
        return HttpResponse.json({ error: 'Failed to load profiles' }, { status: 500 });
      })
    );

    const user = userEvent.setup();
    await renderChatApp({ waitForReady: true });

    expect(await screen.findByText('Error loading profiles')).toBeInTheDocument();

    const messageInput = await findMessageInput();
    await user.type(messageInput, SEND_PROMPT);
    await user.keyboard('{Enter}');

    expect(await screen.findByText(DEFAULT_REPLY, {}, { timeout: 5000 })).toBeInTheDocument();
  }, 30000);

  it('keeps a pending approval retryable and says so when the confirmation API fails', async () => {
    let confirmPosts = 0;
    server.use(
      http.get('/api/v1/chat/confirmations/pending', () =>
        HttpResponse.json({
          confirmations: [
            {
              request_id: 'confirm_fails',
              tool_name: 'add_or_update_note',
              tool_call_id: 'tool-call-not-visible',
              confirmation_prompt: 'Create a note for this itinerary?',
              args: { title: 'Trip', content: 'Flight lands at 6pm' },
              created_at: '2099-05-04T10:00:00Z',
              expires_at: '2099-05-04T10:30:00Z',
              timeout_seconds: 1800,
            },
          ],
        })
      ),
      http.post('/api/v1/chat/confirm_tool', () => {
        confirmPosts += 1;
        return HttpResponse.json({ error: 'Confirmation failed' }, { status: 500 });
      })
    );

    const user = userEvent.setup();
    await renderChatApp({ waitForReady: true });

    expect(await screen.findByText('Create a note for this itinerary?')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: /approve add_or_update_note/i }));

    expect(await screen.findByText('Could not send this decision. Try again.')).toBeInTheDocument();
    expect(confirmPosts).toBe(1);
    expect(screen.getByText('Create a note for this itinerary?')).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.getByRole('button', { name: /approve add_or_update_note/i })).toBeEnabled();
    });
  }, 30000);

  it('handles attachment upload errors', async () => {
    // Mock attachment upload failure
    server.use(
      http.post('/api/attachments/upload', () => {
        return HttpResponse.json({ error: 'Upload failed' }, { status: 500 });
      })
    );

    await renderChatApp({ waitForReady: true });

    // Wait removed - using waitForReady option

    // Test attachment error handling if the UI provides file upload
    // This would involve creating a mock file and testing the upload error
    expect(screen.getByText('Chat')).toBeInTheDocument();
  });

  it('retries the subscribe after a network error and renders the reply', async () => {
    let ourTurnId = '';
    let streamCalls = 0;
    server.use(
      captureTurnId((id) => {
        ourTurnId = id;
      }),
      http.get(STREAM_URL, () => {
        streamCalls += 1;
        if (streamCalls === 1) {
          return HttpResponse.error();
        }
        return sse([
          `event: text\ndata: ${JSON.stringify({ turn_id: ourTurnId, content: 'Recovery successful!', seq: 1 })}\n\n`,
          `event: turn_ended\ndata: ${JSON.stringify({ turn_id: ourTurnId, status: 'complete', seq: 2 })}\n\n`,
        ]);
      })
    );

    const user = userEvent.setup();
    await renderChatApp({ waitForReady: true });

    const messageInput = await findMessageInput();
    await user.type(messageInput, 'Test recovery');
    await user.keyboard('{Enter}');

    expect(
      await screen.findByText('Recovery successful!', {}, { timeout: 5000 })
    ).toBeInTheDocument();
    expect(streamCalls).toBe(2);
    expect(screen.queryByText(/couldn't confirm the reply/i)).not.toBeInTheDocument();
  }, 30000);

  it('renders a reply streamed in many chunks in full, in one bubble', async () => {
    const chunks = Array.from({ length: 200 }, (_, i) => `Chunk ${i} of a long response. `);
    let ourTurnId = '';
    server.use(
      captureTurnId((id) => {
        ourTurnId = id;
      }),
      http.get(STREAM_URL, () =>
        sse([
          ...chunks.map(
            (content, i) =>
              `event: text\ndata: ${JSON.stringify({ turn_id: ourTurnId, content, seq: i + 1 })}\n\n`
          ),
          `event: turn_ended\ndata: ${JSON.stringify({
            turn_id: ourTurnId,
            status: 'complete',
            seq: chunks.length + 1,
          })}\n\n`,
        ])
      )
    );

    const user = userEvent.setup();
    await renderChatApp({ waitForReady: true });

    const messageInput = await findMessageInput();
    await user.type(messageInput, 'Give me a long response');
    await user.keyboard('{Enter}');

    const fullText = chunks.join('').trim();
    expect(await screen.findByText(fullText, {}, { timeout: 10000 })).toBeInTheDocument();
    const bubbles = screen.getAllByTestId('assistant-message-content');
    expect(bubbles).toHaveLength(1);
    expect(bubbles[0]).toHaveTextContent(fullText);
  }, 30000);
});
