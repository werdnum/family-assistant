import { fireEvent, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http } from 'msw';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { resetLocalStorageMock } from '../../../test/mocks/localStorageMock';
import { server } from '../../../test/setup.js';
import { renderChatApp } from '../../../test/utils/renderChatApp';

describe('ComposerAddAttachment', () => {
  beforeEach(() => {
    resetLocalStorageMock();
    vi.clearAllMocks();
  });

  // The attach button sits inside the composer form, so if it acted as a submit
  // button, clicking it would send the half-written message. Sending empties the
  // composer synchronously, so the draft still being there shows no send began.
  it('leaves the message being composed unsent when clicked', async () => {
    await renderChatApp({ waitForReady: true });

    const chatInput = screen.getByTestId('chat-input');
    fireEvent.change(chatInput, { target: { value: 'hello' } });

    fireEvent.click(screen.getByTestId('add-attachment-button'));

    expect(chatInput).toHaveValue('hello');
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

  // A browser fires no change event when the picker returns the selection the
  // input already holds, and user.upload models that. The composer therefore
  // has to clear the input after taking a file, or picking the same file again
  // would silently do nothing.
  it('attaches the same file again when it is picked a second time', async () => {
    const user = userEvent.setup();
    await renderChatApp({ waitForReady: true });

    const fileInput = await screen.findByTestId('file-input');
    const testFile = new File(['test content'], 'test.png', { type: 'image/png' });

    await user.upload(fileInput, testFile);
    await waitFor(
      () => {
        expect(screen.getAllByTestId('remove-attachment-button')).toHaveLength(1);
      },
      { timeout: 15000 }
    );
    expect(screen.getAllByText('test.png').length).toBeGreaterThan(0);

    await user.upload(fileInput, testFile);
    await waitFor(
      () => {
        expect(screen.getAllByTestId('remove-attachment-button')).toHaveLength(2);
      },
      { timeout: 15000 }
    );
  }, 35000);

  // A file waits in the composer until the message is sent, and is uploaded as
  // part of that send, so the turn must carry the uploaded file's URL rather
  // than the local file or nothing at all.
  it('uploads an attached file and sends it with the message', async () => {
    const turnRequests: unknown[] = [];
    server.use(
      http.post('/api/v1/chat/turns', async ({ request }) => {
        turnRequests.push(await request.clone().json());
      })
    );
    await renderChatApp({ waitForReady: true });

    fireEvent.change(screen.getByTestId('chat-input'), { target: { value: 'look at this' } });
    fireEvent.change(await screen.findByTestId('file-input'), {
      target: { files: [new File(['test content'], 'test.png', { type: 'image/png' })] },
    });
    await screen.findByTestId('remove-attachment-button', {}, { timeout: 15000 });

    const sendButton = screen.getByTestId('send-button');
    expect(sendButton).toBeEnabled();
    fireEvent.click(sendButton);

    await waitFor(
      () => {
        expect(turnRequests).toHaveLength(1);
      },
      { timeout: 15000 }
    );
    expect(turnRequests[0]).toMatchObject({
      prompt: 'look at this',
      attachments: [{ name: 'test.png', content: '/api/attachments/server-uuid-456' }],
    });
  }, 35000);
});
