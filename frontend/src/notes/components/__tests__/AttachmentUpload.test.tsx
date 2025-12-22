import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { describe, expect, it } from 'vitest';
import { server } from '../../../test/setup';
import { AttachmentUpload } from '../AttachmentUpload';

describe('AttachmentUpload', () => {
  it('renders upload button', () => {
    render(<AttachmentUpload onUploadComplete={vi.fn()} />);
    expect(screen.getByText('Add Attachments')).toBeInTheDocument();
  });

  it('opens file picker when button is clicked', async () => {
    const user = userEvent.setup();
    render(<AttachmentUpload onUploadComplete={vi.fn()} />);

    const button = screen.getByText('Add Attachments');
    await user.click(button);

    // Check that the hidden file input exists
    const fileInput = document.querySelector('input[type="file"]');
    expect(fileInput).toBeInTheDocument();
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

  it('shows error when upload fails', async () => {
    const user = userEvent.setup();

    // Override handler to return error
    server.use(
      http.post('/api/attachments/upload', () => {
        return HttpResponse.json({ detail: 'Upload failed' }, { status: 500 });
      })
    );

    render(<AttachmentUpload onUploadComplete={vi.fn()} />);

    const button = screen.getByText('Add Attachments');
    await user.click(button);

    const fileInput = document.querySelector('input[type="file"]') as HTMLInputElement;
    const file = new File(['test content'], 'test.png', { type: 'image/png' });

    await user.upload(fileInput, file);

    await waitFor(() => {
      expect(screen.getByText(/Upload failed/)).toBeInTheDocument();
    });
  });

  it('disables button when disabled prop is true', () => {
    render(<AttachmentUpload onUploadComplete={vi.fn()} disabled={true} />);
    const button = screen.getByText('Add Attachments');
    expect(button).toBeDisabled();
  });
});
