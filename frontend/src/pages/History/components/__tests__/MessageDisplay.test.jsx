import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
import MessageDisplay from '../MessageDisplay';

/**
 * The backend records reasoning summaries under `reasoning_info.thought_summaries`
 * as a list of `{ summary }` entries.
 */

const baseMessage = {
  internal_id: 1,
  role: 'assistant',
  content: 'The answer is 714.',
  timestamp: '2026-08-01T00:00:00Z',
};

async function openDetails(user) {
  await user.click(screen.getByRole('button', { name: /Show Details/ }));
}

describe('MessageDisplay reasoning details', () => {
  it('renders thought summaries recorded by the backend', async () => {
    const user = userEvent.setup();
    render(
      <MessageDisplay
        message={{
          ...baseMessage,
          reasoning_info: {
            thought_summaries: [
              { part_index: 0, summary: 'First I multiply 42 by 17.' },
              { part_index: 1, summary: 'That gives 714.' },
            ],
          },
        }}
      />
    );

    await openDetails(user);

    expect(screen.getByText(/Thinking Summary/)).toBeInTheDocument();
    expect(screen.getByText(/First I multiply 42 by 17\./)).toBeInTheDocument();
    expect(screen.getByText(/That gives 714\./)).toBeInTheDocument();
  });

  it('omits the thinking panel when no summaries were recorded', async () => {
    const user = userEvent.setup();
    render(
      <MessageDisplay
        message={{
          ...baseMessage,
          reasoning_info: { prompt_tokens: 10, completion_tokens: 4 },
        }}
      />
    );

    await openDetails(user);

    expect(screen.queryByText(/Thinking Summary/)).not.toBeInTheDocument();
  });

  it('omits the thinking panel when the summary list is empty', async () => {
    const user = userEvent.setup();
    render(
      <MessageDisplay
        message={{
          ...baseMessage,
          reasoning_info: { thought_summaries: [] },
        }}
      />
    );

    await openDetails(user);

    expect(screen.queryByText(/Thinking Summary/)).not.toBeInTheDocument();
  });
});
