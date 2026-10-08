import { render, screen, waitFor } from '@testing-library/react';
import { HttpResponse, http } from 'msw';
import React from 'react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { describe, expect, it } from 'vitest';
import { server } from '../../../test/setup.js';
import AutomationDetail from './AutomationDetail';

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

  it('says when each run starts a new conversation', async () => {
    server.use(
      http.get('/api/automations/:type/:id', () =>
        HttpResponse.json({
          id: '789',
          type: 'schedule',
          name: 'Morning digest',
          conversation_id: 'web',
          enabled: true,
          action_type: 'wake_llm',
          action_config: { context: 'Summarise the news', conversation: 'new' },
        })
      )
    );

    render(
      <MemoryRouter initialEntries={['/automations/schedule/789']}>
        <Routes>
          <Route path="/automations/:type/:id" element={<AutomationDetail />} />
        </Routes>
      </MemoryRouter>
    );

    await waitFor(() => {
      expect(
        screen.getByText('Each run starts a new conversation with the prompt below.')
      ).toBeInTheDocument();
    });
  });
});
