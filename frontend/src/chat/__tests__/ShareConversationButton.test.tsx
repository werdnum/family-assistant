import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { vi } from 'vitest';
import { server } from '../../test/setup.js';
import { ShareConversationButton } from '../ShareConversationButton';

const endpoint = '/api/v1/chat/conversations/conversation-1/share';

function setup(active = false) {
  const user = userEvent.setup();
  const writeText = vi.fn().mockResolvedValue(undefined);
  Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText } });
  let generation = 0;
  const create = vi.fn(() => {
    generation += 1;
    return HttpResponse.json({ share_url: `/shared/conversations/token-${generation}` });
  });
  const revoke = vi.fn(() => new HttpResponse(null, { status: 204 }));
  server.use(
    http.get(endpoint, () => HttpResponse.json({ active })),
    http.post(endpoint, create),
    http.delete(endpoint, revoke)
  );
  const view = render(
    <ShareConversationButton conversationId="conversation-1" hasPersistedMessages={true} />
  );
  return { user, writeText, create, revoke, ...view };
}

async function openDialog(user: ReturnType<typeof userEvent.setup>) {
  await user.click(screen.getByRole('button', { name: 'Share conversation' }));
  await screen.findByRole('dialog', { name: 'Share conversation' });
}

describe('ShareConversationButton', () => {
  afterEach(() => vi.restoreAllMocks());

  it('opens without creating a link and explains read-only authorized access', async () => {
    const { user, create } = setup();
    await openDialog(user);
    await screen.findByRole('button', { name: 'Create and copy link' });
    expect(create).not.toHaveBeenCalled();
    expect(screen.getByRole('dialog')).toHaveAccessibleDescription(
      /signed in as an authorized Family Assistant user.*cannot reply or approve tool calls/
    );
  });

  it('copies the created read-only destination and reuses it after reopening', async () => {
    const { user, writeText, create } = setup();
    await openDialog(user);
    await user.click(await screen.findByRole('button', { name: 'Create and copy link' }));
    expect(await screen.findByText('Link copied')).toBeInTheDocument();
    const url = `${window.location.origin}/shared/conversations/token-1`;
    expect(writeText).toHaveBeenCalledWith(url);
    expect(screen.getByRole('textbox', { name: 'Share link' })).toHaveValue(url);
    expect(screen.getByRole('link', { name: 'Open read-only view' })).toHaveAttribute('href', url);
    await user.keyboard('{Escape}');
    expect(screen.getByRole('button', { name: 'Share conversation' })).toHaveFocus();
    expect(screen.getAllByRole('button')).toHaveLength(1);
    await openDialog(user);
    await user.click(screen.getByRole('button', { name: 'Copy link' }));
    expect(writeText).toHaveBeenCalledTimes(2);
    expect(create).toHaveBeenCalledOnce();
  });

  it('selects the URL after clipboard denial and retries copy without rotating', async () => {
    vi.spyOn(console, 'error').mockImplementation(() => undefined);
    const { user, writeText, create } = setup();
    writeText.mockRejectedValueOnce(new DOMException('Denied', 'NotAllowedError'));
    await openDialog(user);
    await user.click(await screen.findByRole('button', { name: 'Create and copy link' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Could not copy the link');
    const input = screen.getByRole('textbox', { name: 'Share link' }) as HTMLInputElement;
    expect(input).toHaveFocus();
    expect(input.selectionStart).toBe(0);
    expect(input.selectionEnd).toBe(input.value.length);
    expect(input).toHaveAttribute('readonly');
    await user.click(screen.getByRole('button', { name: 'Retry copy' }));
    expect(await screen.findByText('Link copied')).toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    expect(writeText.mock.calls).toEqual([[input.value], [input.value]]);
    expect(create).toHaveBeenCalledOnce();
  });

  it('explicitly replaces an existing link on every replacement action', async () => {
    const { user, create, writeText } = setup(true);
    await openDialog(user);
    expect(await screen.findByText(/existing URL cannot be retrieved/)).toBeInTheDocument();
    expect(screen.getByText(/even if copying fails/)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Copy link' })).not.toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Replace and copy link' }));
    await screen.findByText('Link copied');
    await user.click(screen.getByRole('button', { name: 'Replace and copy link' }));
    await waitFor(() => expect(writeText).toHaveBeenCalledTimes(2));
    expect(create).toHaveBeenCalledTimes(2);
    expect(writeText.mock.calls).toEqual([
      [`${window.location.origin}/shared/conversations/token-1`],
      [`${window.location.origin}/shared/conversations/token-2`],
    ]);
  });

  it('reports creation failures visibly and allows an explicit retry', async () => {
    vi.spyOn(console, 'error').mockImplementation(() => undefined);
    const { user, writeText } = setup();
    server.use(http.post(endpoint, () => new HttpResponse(null, { status: 500 })));
    await openDialog(user);
    await user.click(await screen.findByRole('button', { name: 'Create and copy link' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Could not create the link');
    expect(screen.getByRole('button', { name: 'Create and copy link' })).toBeEnabled();
    expect(screen.queryByRole('textbox')).not.toBeInTheDocument();
    expect(writeText).not.toHaveBeenCalled();
  });

  it('revokes the link, clears the URL and restores focus to creation', async () => {
    const { user, revoke } = setup();
    await openDialog(user);
    await user.click(await screen.findByRole('button', { name: 'Create and copy link' }));
    await screen.findByText('Link copied');
    await user.click(screen.getByRole('button', { name: 'Stop sharing' }));
    expect(
      await screen.findByText('Sharing stopped. The link no longer works.')
    ).toBeInTheDocument();
    expect(revoke).toHaveBeenCalledOnce();
    expect(screen.queryByRole('textbox')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Create and copy link' })).toHaveFocus();
  });

  it('retains the selectable URL and revoke action if revocation fails', async () => {
    vi.spyOn(console, 'error').mockImplementation(() => undefined);
    const { user } = setup();
    server.use(http.delete(endpoint, () => new HttpResponse(null, { status: 500 })));
    await openDialog(user);
    await user.click(await screen.findByRole('button', { name: 'Create and copy link' }));
    await screen.findByText('Link copied');
    await user.click(screen.getByRole('button', { name: 'Stop sharing' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Could not stop sharing');
    expect(screen.getByRole('textbox')).toHaveValue(
      `${window.location.origin}/shared/conversations/token-1`
    );
    expect(screen.getByRole('button', { name: 'Stop sharing' })).toBeEnabled();
  });

  it('blocks mutations and offers retry when status cannot load', async () => {
    vi.spyOn(console, 'error').mockImplementation(() => undefined);
    const user = userEvent.setup();
    const create = vi.fn();
    let attempts = 0;
    server.use(
      http.get(endpoint, () => {
        attempts += 1;
        return attempts === 1
          ? new HttpResponse(null, { status: 503 })
          : HttpResponse.json({ active: true });
      })
    );
    server.use(http.post(endpoint, create));
    render(<ShareConversationButton conversationId="conversation-1" hasPersistedMessages={true} />);
    await openDialog(user);
    await screen.findByRole('alert');
    expect(screen.queryByRole('button', { name: /and copy link/ })).not.toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Retry loading sharing status' }));
    expect(await screen.findByRole('button', { name: 'Stop sharing' })).toBeInTheDocument();
    expect(create).not.toHaveBeenCalled();
  });

  it('does not request status before messages persist', async () => {
    const statusRequest = vi.fn(() => HttpResponse.json({ active: false }));
    server.use(http.get(endpoint, statusRequest));
    const { rerender } = render(
      <ShareConversationButton conversationId="conversation-1" hasPersistedMessages={false} />
    );
    expect(screen.queryByRole('button')).not.toBeInTheDocument();
    expect(statusRequest).not.toHaveBeenCalled();
    rerender(
      <ShareConversationButton conversationId="conversation-1" hasPersistedMessages={true} />
    );
    await waitFor(() => expect(statusRequest).toHaveBeenCalledOnce());
  });

  it('does not retain a previous conversation URL when switching conversations', async () => {
    const { user, rerender } = setup();
    await openDialog(user);
    await user.click(await screen.findByRole('button', { name: 'Create and copy link' }));
    await screen.findByText('Link copied');
    server.use(
      http.get('/api/v1/chat/conversations/conversation-2/share', () =>
        HttpResponse.json({ active: true })
      )
    );
    rerender(
      <ShareConversationButton conversationId="conversation-2" hasPersistedMessages={true} />
    );
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    await openDialog(user);
    await screen.findByText(/existing URL cannot be retrieved/);
    expect(screen.queryByRole('textbox')).not.toBeInTheDocument();
  });
});
