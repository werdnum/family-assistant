import { act, render, screen, waitFor } from '@testing-library/react';
import { HttpResponse, http } from 'msw';
import { describe, expect, it, vi } from 'vitest';
import { server } from '../../../test/setup.js';
import { NewRunOption } from './NewRunOption';

/** Answers the Home lookup; `answered` says whether it has been asked. */
function serveHome(enabled: boolean): { answered: () => boolean } {
  let answered = false;
  server.use(
    http.get('/api/v1/chat/home', () => {
      answered = true;
      return HttpResponse.json({ enabled, conversation_id: enabled ? 'web_conv_home' : null });
    })
  );
  return { answered: () => answered };
}

describe('NewRunOption', () => {
  it('offers a new thread each time while threads are on', async () => {
    serveHome(true);
    render(<NewRunOption checked={false} onCheckedChange={vi.fn()} />);

    expect(
      await screen.findByRole('checkbox', { name: 'Start a new thread each time' })
    ).toBeInTheDocument();
    expect(screen.getByText(/opens its own thread off Home/)).toBeInTheDocument();
  });

  it('offers a new conversation each time while threads are off', async () => {
    const home = serveHome(false);
    render(<NewRunOption checked={false} onCheckedChange={vi.fn()} />);

    await waitFor(() => expect(home.answered()).toBe(true));
    // Let the answer land before reading the label it settles on.
    await act(async () => {});
    expect(
      screen.getByRole('checkbox', { name: 'Start a new conversation each time' })
    ).toBeInTheDocument();
    expect(screen.queryByText(/thread/)).not.toBeInTheDocument();
  });
});
