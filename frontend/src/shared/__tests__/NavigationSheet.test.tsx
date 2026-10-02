import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { describe, expect, it } from 'vitest';
import NavigationSheet from '../NavigationSheet';

describe('NavigationSheet', () => {
  it.each(['', '/api/app/pages'])(
    'closes after navigation while preserving the %s router base',
    async (basename) => {
      const user = userEvent.setup();
      render(
        <MemoryRouter basename={basename} initialEntries={[`${basename}/context`]}>
          <NavigationSheet>
            <button type="button">Open navigation</button>
          </NavigationSheet>
          <Routes>
            <Route path="/context" element={<h1>Context</h1>} />
            <Route path="/notes" element={<h1>Notes</h1>} />
          </Routes>
        </MemoryRouter>
      );
      await user.click(screen.getByRole('button', { name: 'Open navigation' }));
      const notesLink = within(screen.getByRole('dialog')).getByRole('link', { name: 'Notes' });
      expect(notesLink).toHaveAttribute('href', `${basename}/notes`);
      await user.click(notesLink);
      expect(await screen.findByRole('heading', { name: 'Notes' })).toBeInTheDocument();
      await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    }
  );
});
