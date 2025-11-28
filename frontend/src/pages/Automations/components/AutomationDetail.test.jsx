import { render, screen, waitFor } from '@testing-library/react';
import React from 'react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { describe, expect, it } from 'vitest';
import { server } from '../../../test/mocks/server';
import AutomationDetail from './AutomationDetail';

beforeAll(() => server.listen());
afterEach(() => server.resetHandlers());
afterAll(() => server.close());

describe('AutomationDetail', () => {
  it('should fetch automation details', async () => {
    render(
      <MemoryRouter initialEntries={['/automations/event/456']}>
        <Routes>
          <Route path="/automations/:type/:id" element={<AutomationDetail />} />
        </Routes>
      </MemoryRouter>
    );

    await waitFor(() => {
      expect(screen.getByText(/Test Automation Telegram/)).toBeInTheDocument();
    });
  });

  it('should show a not found message if the automation is not found', async () => {
    render(
      <MemoryRouter initialEntries={['/automations/event/999']}>
        <Routes>
          <Route path="/automations/:type/:id" element={<AutomationDetail />} />
        </Routes>
      </MemoryRouter>
    );

    await waitFor(() => {
      expect(screen.getByText('Automation not found')).toBeInTheDocument();
    });
  });
});
