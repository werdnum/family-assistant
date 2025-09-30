import { screen, waitFor } from '@testing-library/react';

/**
 * Wait for the chat interface to be ready for interaction.
 * This is a more robust alternative to arbitrary setTimeout delays.
 */
export async function waitForChatReady(): Promise<void> {
  await screen.findByPlaceholderText('Write a message...', {}, { timeout: 5000 });
}

/**
 * Wait for a message to be sent by checking if the input has been cleared.
 * @param input The message input element
 */
export async function waitForMessageSent(input: HTMLElement): Promise<void> {
  await waitFor(
    () => {
      expect(input).toHaveValue('');
    },
    { timeout: 3000 }
  );
}

/**
 * Wait for conversations to be loaded in the sidebar.
 */
export async function waitForConversationsLoaded(): Promise<void> {
  await waitFor(
    () => {
      expect(screen.getByText('Conversations')).toBeInTheDocument();
    },
    { timeout: 5000 }
  );
}

/**
 * Wait for a specific text to appear in the document.
 * @param text The text to wait for
 * @param timeout Optional timeout in milliseconds (default: 5000)
 */
export async function waitForText(text: string, timeout = 5000): Promise<void> {
  await screen.findByText(text, {}, { timeout });
}
