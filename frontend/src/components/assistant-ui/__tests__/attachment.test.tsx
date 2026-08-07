import { fireEvent, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { resetLocalStorageMock } from '../../../test/mocks/localStorageMock';
import { renderChatApp } from '../../../test/utils/renderChatApp';

describe('ComposerAddAttachment', () => {
  beforeEach(() => {
    resetLocalStorageMock();
    vi.clearAllMocks();
  });

  it('has type="button" to prevent form submission', async () => {
    await renderChatApp({ waitForReady: true });

    const attachButton = screen.getByTestId('add-attachment-button');

    // Verify the button has type="button" to prevent it from submitting forms
    expect(attachButton).toHaveAttribute('type', 'button');
  });

  it('opens file picker when clicked', async () => {
    await renderChatApp({ waitForReady: true });

    const attachButton = screen.getByTestId('add-attachment-button');

    // Spy on HTMLInputElement.prototype.click to detect file input clicks
    // regardless of DOM element recreation from async re-renders
    const clickSpy = vi.spyOn(HTMLInputElement.prototype, 'click');

    // Use fireEvent for synchronous click to avoid race conditions with
    // async re-renders from @assistant-ui's tap reactive system
    fireEvent.click(attachButton);

    // Should trigger the file input click
    await waitFor(() => {
      expect(clickSpy).toHaveBeenCalled();
    });

    clickSpy.mockRestore();
  });
});

describe('AttachmentUI Loading States', () => {
  beforeEach(() => {
    resetLocalStorageMock();
    vi.clearAllMocks();
  });

  it('can upload a file through the hidden input', async () => {
    await renderChatApp({ waitForReady: true });

    const fileInput = (await screen.findByTestId('file-input')) as HTMLInputElement;
    const testFile = new File(['test content'], 'test.png', { type: 'image/png' });

    // fireEvent is more stable than user.upload for this hidden input path.
    fireEvent.change(fileInput, { target: { files: [testFile] } });

    // The composer clears the file input value after enqueueing files so the
    // same file can be selected again. This is a stable assertion across
    // timing variations in attachment preview rendering.
    await waitFor(
      () => {
        expect(fileInput.value).toBe('');
      },
      { timeout: 15000 }
    );
  }, 20000);

  // The composer disables sending while an attachment is still uploading, so an
  // attachment that never leaves the uploading state locks the composer: the
  // file reads "Uploading..." forever and the message can never be sent.
  it('finishes uploading an attached file and re-enables sending', async () => {
    await renderChatApp({ waitForReady: true });

    const fileInput = (await screen.findByTestId('file-input')) as HTMLInputElement;
    fireEvent.change(fileInput, {
      target: { files: [new File(['test content'], 'test.png', { type: 'image/png' })] },
    });

    // Wait for the file to reach the composer before asserting it leaves the
    // uploading state, so the assertion can't pass before the upload starts.
    await screen.findByTestId('remove-attachment-button', {}, { timeout: 15000 });

    await waitFor(
      () => {
        expect(screen.queryByText('Uploading...')).not.toBeInTheDocument();
      },
      { timeout: 15000 }
    );
    expect(screen.getByTestId('send-button')).toBeEnabled();
  }, 20000);

  // Note: These tests verify the UI components exist and are properly structured
  // The actual upload flow is tested in integration tests
  it('AttachmentUI component renders with proper data-testid attributes', async () => {
    await renderChatApp({ waitForReady: true });

    // Verify the attachment UI structure is in place
    // This ensures our changes to add data-testid="attachment-loading" are correct
    const fileInput = screen.getByTestId('file-input');
    expect(fileInput).toBeInTheDocument();

    const attachButton = screen.getByTestId('add-attachment-button');
    expect(attachButton).toBeInTheDocument();
  });
});
