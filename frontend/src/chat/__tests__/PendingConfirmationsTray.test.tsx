import { screen, waitFor } from '@testing-library/react';
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

    expect(await screen.findByTestId('pending-confirmations-tray')).toBeInTheDocument();
    expect(screen.getByText('Create a note for this itinerary?')).toBeInTheDocument();
    expect(screen.getByText(/"title": "Trip"/)).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: /approve add_or_update_note/i }));

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

    expect(await screen.findByTestId('pending-confirmations-tray')).toBeInTheDocument();
    expect(
      screen.getByText('Could not load pending approvals. Refresh or try again.')
    ).toBeInTheDocument();
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
