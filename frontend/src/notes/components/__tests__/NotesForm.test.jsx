import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { HttpResponse, http } from 'msw';
import React from 'react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { describe, expect, it, vi } from 'vitest';
import { server } from '../../../test/setup.js';
import NotesForm from '../NotesForm';

const renderEdit = () =>
  render(
    <MemoryRouter initialEntries={['/notes/edit/Household']}>
      <Routes>
        <Route
          path="/notes/edit/:title"
          element={<NotesForm isEdit onSuccess={vi.fn()} onCancel={vi.fn()} />}
        />
      </Routes>
    </MemoryRouter>
  );

describe('NotesForm load recovery', () => {
  it('withholds the editor after a failed load', async () => {
    server.use(
      http.get('/api/notes/Household', () =>
        HttpResponse.json({ detail: 'Unavailable' }, { status: 503 })
      )
    );
    renderEdit();

    await screen.findByRole('button', { name: 'Try again' });
    expect(screen.queryByRole('button', { name: 'Save' })).not.toBeInTheDocument();
    expect(screen.queryByLabelText('Title *')).not.toBeInTheDocument();
  });

  it('restores the existing note on retry', async () => {
    const user = userEvent.setup();
    server.use(http.get('/api/notes/Household', () => HttpResponse.json({}, { status: 503 })));
    renderEdit();
    const retry = await screen.findByRole('button', { name: 'Try again' });

    server.use(
      http.get('/api/notes/Household', () =>
        HttpResponse.json({
          title: 'Household',
          content: 'Existing note content',
          include_in_prompt: true,
          attachment_ids: [],
        })
      )
    );
    await user.click(retry);

    expect(await screen.findByDisplayValue('Existing note content')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Save' })).toBeEnabled();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });
});
