import { render, screen, waitFor } from '@testing-library/react';
import { HttpResponse, http } from 'msw';
import { describe, expect, it } from 'vitest';
import { server } from '../../test/setup.js';
import ContextPage from '../ContextPage';

describe('ContextPage', () => {
  it('ends loading and explains when there are no profiles', async () => {
    server.use(http.get('/api/v1/context/profiles', () => HttpResponse.json([])));
    render(<ContextPage />);
    expect(await screen.findByText('No processing profiles are available.')).toBeInTheDocument();
    expect(screen.queryByText('Loading context data...')).not.toBeInTheDocument();
    expect(screen.getByLabelText('Processing Profile:')).toBeDisabled();
  });

  it('ends loading when profile retrieval fails', async () => {
    server.use(
      http.get('/api/v1/context/profiles', () =>
        HttpResponse.json({ detail: 'Unavailable' }, { status: 503 })
      )
    );
    render(<ContextPage />);
    expect(await screen.findByRole('alert')).toHaveTextContent('Failed to load profiles: 503');
    await waitFor(() =>
      expect(screen.queryByText('Loading context data...')).not.toBeInTheDocument()
    );
  });
});
