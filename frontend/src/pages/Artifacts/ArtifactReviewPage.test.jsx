import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { MemoryRouter } from 'react-router-dom';
import { describe, expect, it } from 'vitest';
import { server } from '../../test/setup.js';
import ArtifactReviewPage from './ArtifactReviewPage';

const makeArtifact = (kind) => ({
  kind,
  id: 42,
  name: `${kind} to review`,
  content: {
    name: `${kind} to review`,
    script_code: 'print("complete code")',
    action_config: { context: 'full instructions', parameters: { secret_name: 'example' } },
  },
  content_hash: 'sha256:displayed-content',
  trust_tier: 'unknown_external',
  disposition: null,
});

// Tests live one level deeper than the shared page tests.
const setup = (artifact) => {
  server.use(http.get('/api/artifacts/', () => HttpResponse.json([artifact])));
  render(
    <MemoryRouter>
      <ArtifactReviewPage />
    </MemoryRouter>
  );
};

describe('Artifact review', () => {
  it.each(['note', 'script', 'event', 'schedule'])(
    'confirms the full displayed %s snapshot',
    async (kind) => {
      const artifact = makeArtifact(kind);
      let submitted;
      server.use(
        http.post(`/api/artifacts/${kind}/42/confirm`, async ({ request }) => {
          submitted = await request.json();
          return HttpResponse.json({
            ...artifact,
            disposition: 'human_confirmed',
            trust_tier: 'machine_reviewed',
          });
        })
      );
      setup(artifact);
      const user = userEvent.setup();
      await user.click(await screen.findByRole('button', { name: 'Review and confirm' }));
      const dialog = screen.getByRole('dialog');
      expect(within(dialog).getByText('print("complete code")')).toBeInTheDocument();
      expect(within(dialog).getByText(/full instructions/)).toBeInTheDocument();
      await user.click(within(dialog).getByRole('button', { name: 'Confirm as user reviewed' }));
      await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
      expect(submitted).toEqual({ content_hash: artifact.content_hash });
      expect(screen.getByRole('button', { name: 'User confirmed' })).toBeDisabled();
    }
  );

  it('allows a new confirmation when a previous human decision did not cure taint', async () => {
    const artifact = { ...makeArtifact('script'), disposition: 'human_confirmed' };
    server.use(
      http.post('/api/artifacts/script/42/confirm', () =>
        HttpResponse.json({
          ...artifact,
          trust_tier: 'machine_reviewed',
        })
      )
    );
    setup(artifact);
    const user = userEvent.setup();
    await user.click(await screen.findByRole('button', { name: 'Review and confirm' }));
    expect(screen.getByText('Needs review')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Confirm as user reviewed' }));
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(screen.getByRole('button', { name: 'User confirmed' })).toBeDisabled();
  });

  it('keeps the review open when confirmation fails', async () => {
    setup(makeArtifact('script'));
    server.use(
      http.post('/api/artifacts/script/42/confirm', () =>
        HttpResponse.json(
          { detail: 'Artifact changed. Reload and review it again.' },
          { status: 409 }
        )
      )
    );
    fireEvent.click(await screen.findByRole('button', { name: 'Review and confirm' }));
    fireEvent.click(screen.getByRole('button', { name: 'Confirm as user reviewed' }));
    expect(
      await screen.findByText('Artifact changed. Reload and review it again.')
    ).toBeInTheDocument();
    expect(screen.getByRole('dialog')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Confirm as user reviewed' })).toBeEnabled();
  });

  it('cancels review without recording approval', async () => {
    let confirmations = 0;
    setup(makeArtifact('note'));
    server.use(
      http.post('/api/artifacts/note/42/confirm', () => {
        confirmations++;
        return HttpResponse.json({});
      })
    );
    const user = userEvent.setup();
    await user.click(await screen.findByRole('button', { name: 'Review and confirm' }));
    await user.click(screen.getByRole('button', { name: 'Cancel' }));
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(confirmations).toBe(0);
  });
});
