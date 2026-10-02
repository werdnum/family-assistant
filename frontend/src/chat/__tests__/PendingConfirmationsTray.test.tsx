import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { HttpResponse, http } from 'msw';
import { vi } from 'vitest';
import { confirmationMapsEqual } from '../ChatApp';
import { resetLocalStorageMock } from '../../test/mocks/localStorageMock';
import { server } from '../../test/setup.js';
import { renderChatApp } from '../../test/utils/renderChatApp';

describe('PendingConfirmationsTray', () => {
  beforeEach(() => {
    resetLocalStorageMock();
    vi.clearAllMocks();
  });

  it('loads durable pending confirmations and approves one through the confirmation API', async () => {
    const user = userEvent.setup();
    const postedBodies: Array<{ request_id: string; approved: boolean }> = [];
    let pending = true;

    server.use(
      http.get('/api/v1/chat/confirmations/pending', () => {
        return HttpResponse.json({
          confirmations: pending
            ? [
                {
                  request_id: 'confirm_abc123',
                  tool_name: 'add_or_update_note',
                  tool_call_id: 'tool-call-not-visible',
                  confirmation_prompt: 'Create a note for this itinerary?',
                  args: {
                    title: 'Trip',
                    content: 'Flight lands at 6pm',
                  },
                  created_at: '2099-05-04T10:00:00Z',
                  expires_at: '2099-05-04T10:30:00Z',
                  timeout_seconds: 1800,
                },
              ]
            : [],
        });
      }),
      http.post('/api/v1/chat/confirm_tool', async ({ request }) => {
        const body = (await request.json()) as { request_id: string; approved: boolean };
        postedBodies.push(body);
        pending = false;
        return HttpResponse.json({ success: true, message: 'Tool execution approved' });
      })
    );

    await renderChatApp({ waitForReady: true });

    const tray = await screen.findByTestId('pending-confirmations-tray');
    expect(within(tray).getByText('Create a note for this itinerary?')).toBeInTheDocument();
    expect(within(tray).getByText('Approve Add/Update Note?')).toBeInTheDocument();

    await user.click(within(tray).getByRole('button', { name: 'Approve' }));

    await waitFor(() => {
      expect(postedBodies).toHaveLength(1);
      expect(postedBodies[0]).toMatchObject({ request_id: 'confirm_abc123', approved: true });
      expect(screen.queryByTestId('pending-confirmations-tray')).not.toBeInTheDocument();
    });
  }, 30000);

  it('shows a tray error when durable pending confirmations cannot be loaded', async () => {
    server.use(
      http.get('/api/v1/chat/confirmations/pending', () => {
        return HttpResponse.json({ error: 'unavailable' }, { status: 503 });
      })
    );

    await renderChatApp({ waitForReady: true });

    expect(
      await screen.findByText("Couldn't check for pending approvals. Retrying…", undefined, {
        timeout: 10000,
      })
    ).toBeInTheDocument();
    expect(screen.getByTestId('pending-confirmations-tray')).toBeInTheDocument();
  }, 30000);

  it('shows a tray error when durable pending confirmations response is malformed', async () => {
    server.use(
      http.get('/api/v1/chat/confirmations/pending', () => {
        return HttpResponse.json({ status: 'ok' });
      })
    );

    await renderChatApp({ waitForReady: true });

    expect(
      await screen.findByText("Couldn't check for pending approvals. Retrying…", undefined, {
        timeout: 10000,
      })
    ).toBeInTheDocument();
    expect(screen.getByTestId('pending-confirmations-tray')).toBeInTheDocument();
  }, 30000);

  it('does not show the tray for a single failed poll that the retry recovers', async () => {
    let requests = 0;
    server.use(
      http.get('/api/v1/chat/confirmations/pending', () => {
        requests += 1;
        if (requests === 1) {
          return HttpResponse.json({ error: 'unavailable' }, { status: 503 });
        }
        return HttpResponse.json({ confirmations: [] });
      })
    );

    await renderChatApp({ waitForReady: true });

    await waitFor(() => expect(requests).toBeGreaterThanOrEqual(2), { timeout: 10000 });
    expect(screen.queryByTestId('pending-confirmations-tray')).not.toBeInTheDocument();
  }, 30000);

  it('renders the markdown prompt and says which conversation the request came from', async () => {
    const user = userEvent.setup();
    server.use(
      http.get('/api/v1/chat/confirmations/pending', () => {
        return HttpResponse.json({
          confirmations: [
            {
              request_id: 'confirm_markdown',
              tool_name: 'add_or_update_note',
              tool_call_id: 'tool-call-elsewhere',
              confirmation_prompt:
                'Please confirm you want to *save* this note:\n- Title:\n```\nTrip\n```',
              args: { title: 'Trip' },
              created_at: '2099-05-04T10:00:00Z',
              expires_at: '2099-05-04T10:30:00Z',
              timeout_seconds: 1800,
              origin_interface_type: 'web',
              conversation_id: 'web_conv_elsewhere',
            },
          ],
        });
      })
    );

    await renderChatApp({ waitForReady: true });

    const tray = await screen.findByTestId('pending-confirmations-tray');
    expect(within(tray).getByText('save').tagName).toBe('EM');
    expect(within(tray).getByText('Trip').closest('pre')).not.toBeNull();
    expect(tray).not.toHaveTextContent('```');
    // The prompt already shows the payload; the raw JSON is collapsed behind a disclosure.
    const rawArguments = within(tray).getByText('Raw arguments').closest('details');
    expect(rawArguments).not.toHaveAttribute('open');
    expect(rawArguments).toHaveTextContent('"title": "Trip"');
    expect(within(tray).getByText(/From another conversation/)).toBeInTheDocument();

    await user.click(within(tray).getByRole('button', { name: 'Open it' }));

    await waitFor(() => {
      expect(window.location.search).toContain('conversation_id=web_conv_elsewhere');
    });
    expect(await screen.findByText('From this conversation')).toBeInTheDocument();
  }, 30000);

  it('treats refreshed server-side countdown timing as a changed pending confirmation', () => {
    const confirmation = {
      request_id: 'confirm_refresh',
      tool_name: 'add_or_update_note',
      tool_call_id: 'tool-call-refresh',
      confirmation_prompt: 'Create a note for this itinerary?',
      args: {
        title: 'Trip',
      },
      created_at: '2099-05-04T10:00:00Z',
      expires_at: '2099-05-04T10:30:00Z',
      timeout_seconds: 1800,
      time_remaining_seconds: 120,
    };

    expect(
      confirmationMapsEqual(
        new Map([['tool-call-refresh', confirmation]]),
        new Map([
          [
            'tool-call-refresh',
            {
              ...confirmation,
              time_remaining_seconds: 30,
            },
          ],
        ])
      )
    ).toBe(false);
  });
});
