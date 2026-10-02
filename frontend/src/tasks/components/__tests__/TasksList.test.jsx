import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { HttpResponse, http } from 'msw';
import { MemoryRouter } from 'react-router-dom';
import { describe, expect, it } from 'vitest';
import { server } from '../../../test/setup.js';
import TasksList from '../TasksList';

describe('TasksList', () => {
  it('keeps the filter field and focus while results refresh', async () => {
    let release;
    const responseGate = new Promise((resolve) => {
      release = resolve;
    });
    server.use(
      http.get('/api/tasks/', async ({ request }) => {
        if (new globalThis.URL(request.url).searchParams.has('task_type')) {
          await responseGate;
        }
        return HttpResponse.json({ tasks: [] });
      })
    );
    render(
      <MemoryRouter>
        <TasksList />
      </MemoryRouter>
    );
    await screen.findByText('No tasks found.');
    const input = screen.getByLabelText('Task Type');
    try {
      await userEvent.setup().type(input, 'reminder');
      expect(screen.getByLabelText('Task Type')).toBe(input);
      expect(input).toHaveFocus();
      expect(input).toHaveValue('reminder');
    } finally {
      release();
    }
    await waitFor(() => expect(screen.queryByRole('status')).not.toBeInTheDocument());
    expect(screen.getByText('No tasks match the current filters.')).toBeInTheDocument();
  });
});
