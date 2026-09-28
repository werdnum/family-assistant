import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { afterEach, describe, expect, it } from 'vitest';
import { server } from '../../../test/setup';
import { AttachmentUpload } from '../AttachmentUpload';

describe('AttachmentUpload', () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('opens file picker when button is clicked', async () => {
    const user = userEvent.setup();
    render(<AttachmentUpload onUploadComplete={vi.fn()} />);
    const fileInput = document.querySelector('input[type="file"]');
    const clickSpy = vi.spyOn(HTMLInputElement.prototype, 'click');

    await user.click(screen.getByRole('button', { name: 'Add Attachments' }));

    expect(clickSpy).toHaveBeenCalledTimes(1);
    expect(clickSpy.mock.contexts[0]).toBe(fileInput);
  });

  it('uploads file and calls onUploadComplete', async () => {
    const user = userEvent.setup();
    const onUploadComplete = vi.fn();

    render(<AttachmentUpload onUploadComplete={onUploadComplete} />);

    const button = screen.getByText('Add Attachments');
    await user.click(button);

    const fileInput = document.querySelector('input[type="file"]') as HTMLInputElement;
    const file = new File(['test content'], 'test.png', { type: 'image/png' });

    await user.upload(fileInput, file);

    await waitFor(() => {
      expect(onUploadComplete).toHaveBeenCalledWith('server-uuid-456');
    });
  });

  it('shows the server error detail when upload fails', async () => {
    const user = userEvent.setup();
    const onUploadComplete = vi.fn();

    server.use(
      http.post('/api/attachments/upload', () => {
        return HttpResponse.json({ detail: 'Attachment storage is full' }, { status: 500 });
      })
    );

    render(<AttachmentUpload onUploadComplete={onUploadComplete} />);

    const fileInput = document.querySelector('input[type="file"]') as HTMLInputElement;
    const file = new File(['test content'], 'test.png', { type: 'image/png' });

    await user.upload(fileInput, file);

    expect(await screen.findByText('Attachment storage is full')).toBeInTheDocument();
    const button = screen.getByRole('button', { name: 'Add Attachments' });
    expect(button).toBeEnabled();
    expect(onUploadComplete).not.toHaveBeenCalled();
  });

  it('disables button when disabled prop is true', () => {
    render(<AttachmentUpload onUploadComplete={vi.fn()} disabled={true} />);
    const button = screen.getByText('Add Attachments');
    expect(button).toBeDisabled();
  });
});
