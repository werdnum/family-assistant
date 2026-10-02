import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { HttpResponse, http } from 'msw';
import { server } from '../../../test/setup.js';
import type React from 'react';
import { describe, expect, it, vi } from 'vitest';
import { AttachmentPreview } from '../AttachmentPreview';

describe('AttachmentPreview', () => {
  it('renders loading state initially', () => {
    render(<AttachmentPreview attachmentId="test-attachment" />);
    expect(screen.getByText('Loading...')).toBeInTheDocument();
  });

  it('renders attachment image after loading', async () => {
    render(<AttachmentPreview attachmentId="test-attachment" />);

    await waitFor(() => {
      const img = screen.getByAltText('test-attachment-test-attachment.png');
      expect(img).toBeInTheDocument();
    });
  });

  it('removes the attachment by id without submitting the enclosing form', async () => {
    const user = userEvent.setup();
    const handleRemove = vi.fn();
    const handleSubmit = vi.fn((e: React.FormEvent) => e.preventDefault());
    render(
      <form onSubmit={handleSubmit}>
        <AttachmentPreview
          attachmentId="test-attachment"
          onRemove={handleRemove}
          canRemove={true}
        />
      </form>
    );

    await user.click(await screen.findByTitle('Remove attachment'));

    expect(handleRemove).toHaveBeenCalledTimes(1);
    expect(handleRemove).toHaveBeenCalledWith('test-attachment');
    expect(handleSubmit).not.toHaveBeenCalled();
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  });

  it('does not render remove button when canRemove is false', async () => {
    const handleRemove = vi.fn();
    render(
      <AttachmentPreview attachmentId="test-attachment" onRemove={handleRemove} canRemove={false} />
    );

    await waitFor(() => {
      const img = screen.getByAltText('test-attachment-test-attachment.png');
      expect(img).toBeInTheDocument();
    });

    expect(screen.queryByTitle('Remove attachment')).not.toBeInTheDocument();
  });
  it('allows an unavailable attachment to be removed', async () => {
    const user = userEvent.setup();
    const handleRemove = vi.fn();
    server.use(http.get('/api/attachments/missing', () => HttpResponse.json({}, { status: 404 })));
    render(<AttachmentPreview attachmentId="missing" onRemove={handleRemove} />);
    await user.click(await screen.findByRole('button', { name: 'Remove attachment' }));
    expect(handleRemove).toHaveBeenCalledWith('missing');
    expect(screen.getByText('Error loading')).toBeInTheDocument();
    expect(screen.queryByText('Loading...')).not.toBeInTheDocument();
  });

  it('opens image previews from the keyboard', async () => {
    const user = userEvent.setup();
    render(<AttachmentPreview attachmentId="test-attachment" />);
    const trigger = await screen.findByRole('button', { name: /Preview test-attachment/ });
    trigger.focus();
    await user.keyboard('{Enter}');
    expect(await screen.findByRole('dialog')).toBeInTheDocument();
  });
});
