import React from 'react';
import { render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { describe, it, expect } from 'vitest';
import AutomationDetail from './AutomationDetail';
import { server } from '../../../test/mocks/server';

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
