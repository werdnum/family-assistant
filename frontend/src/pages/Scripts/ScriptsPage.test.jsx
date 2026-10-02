import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { MemoryRouter } from 'react-router-dom';
import { describe, expect, it } from 'vitest';
import { server } from '../../test/setup.js';
import ScriptsPage from './ScriptsPage';

const script = {
  kind: 'script',
  id: 1,
  name: 'greeting',
  content: {
    name: 'greeting',
    description: 'Original greeting',
    script_code: 'print(message)',
    parameters_schema: { type: 'object', properties: { message: { type: 'string' } } },
  },
  content_hash: 'sha256:original',
  trust_tier: 'unknown_external',
  disposition: null,
};

const setup = (scripts = []) => {
  server.use(
    http.get('/api/artifacts/', ({ request }) => {
      expect(new globalThis.URL(request.url).searchParams.get('kind')).toBe('script');
      return HttpResponse.json(scripts);
    })
  );
  render(
    <MemoryRouter>
      <ScriptsPage />
    </MemoryRouter>
  );
};

describe('Script editor', () => {
  it('loads existing code and schema and saves a complete edit', async () => {
    let submitted;
    server.use(
      http.post('/api/scripts/', async ({ request }) => {
        submitted = await request.json();
        return HttpResponse.json({});
      })
    );
    setup([script]);
    fireEvent.click(await screen.findByRole('button', { name: 'Edit script' }));
    expect(screen.getByLabelText('Name')).toBeDisabled();
    expect(screen.getByLabelText('Code')).toHaveValue('print(message)');
    expect(JSON.parse(screen.getByLabelText('Parameter schema (JSON, optional)').value)).toEqual(
      script.content.parameters_schema
    );
    fireEvent.change(screen.getByLabelText('Description'), {
      target: { value: 'Updated greeting' },
    });
    fireEvent.change(screen.getByLabelText('Code'), { target: { value: 'print("updated")' } });
    fireEvent.change(screen.getByLabelText('Parameter schema (JSON, optional)'), {
      target: { value: '' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Save script' }));
    await waitFor(() => expect(screen.queryByLabelText('Code')).not.toBeInTheDocument());
    expect(submitted).toEqual({
      name: 'greeting',
      description: 'Updated greeting',
      script_code: 'print("updated")',
      parameters_schema: null,
      expected_content_hash: script.content_hash,
    });
  });

  it('creates a new script', async () => {
    let submitted;
    server.use(
      http.post('/api/scripts/', async ({ request }) => {
        submitted = await request.json();
        return HttpResponse.json({});
      })
    );
    setup();
    fireEvent.click(screen.getByRole('button', { name: 'New script' }));
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'new-script' } });
    fireEvent.change(screen.getByLabelText('Code'), { target: { value: 'print("hello")' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save script' }));
    await waitFor(() => expect(submitted?.name).toBe('new-script'));
    expect(submitted.expected_content_hash).toBeNull();
  });

  it('retains edits and reports server validation errors', async () => {
    server.use(
      http.post('/api/scripts/', () =>
        HttpResponse.json({ detail: 'Script validation failed: bad syntax' }, { status: 400 })
      )
    );
    setup([script]);
    fireEvent.click(await screen.findByRole('button', { name: 'Edit script' }));
    fireEvent.change(screen.getByLabelText('Code'), { target: { value: 'def broken(' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save script' }));
    expect(await screen.findByText('Script validation failed: bad syntax')).toBeInTheDocument();
    expect(screen.getByLabelText('Code')).toHaveValue('def broken(');
  });

  it('rejects malformed parameter JSON without sending it', async () => {
    let saves = 0;
    server.use(
      http.post('/api/scripts/', () => {
        saves++;
        return HttpResponse.json({});
      })
    );
    setup([script]);
    fireEvent.click(await screen.findByRole('button', { name: 'Edit script' }));
    fireEvent.change(screen.getByLabelText('Parameter schema (JSON, optional)'), {
      target: { value: '[1]' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Save script' }));
    expect(await screen.findByText('Parameter schema must be a JSON object.')).toBeInTheDocument();
    expect(saves).toBe(0);
  });
});
