import { screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { renderChatApp } from '../../test/utils/renderChatApp';

describe('Thread Send Button', () => {
  it('renders send button in normal state', async () => {
    await renderChatApp({ waitForReady: true });

    const sendButton = screen.getByTestId('send-button');
    expect(sendButton).toBeInTheDocument();
    expect(sendButton).not.toBeDisabled();
  });

  // Note: Testing the send button's loading state during file upload requires
  // a complex test setup. The UI logic is implemented and will be tested in
  // integration tests. This test verifies the basic rendering works correctly.
});
