import { render, screen, waitFor } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
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

  it('renders remove button when canRemove is true', async () => {
    const handleRemove = vi.fn();
    render(
      <AttachmentPreview attachmentId="test-attachment" onRemove={handleRemove} canRemove={true} />
    );

    await waitFor(() => {
      expect(screen.getByTitle('Remove attachment')).toBeInTheDocument();
    });
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
});
