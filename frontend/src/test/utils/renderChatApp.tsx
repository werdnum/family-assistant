import { render, screen } from '@testing-library/react';
import ChatApp from '../../chat/ChatApp';

interface RenderChatAppOptions {
  profileId?: string;
  waitForReady?: boolean;
}

/**
 * Renders ChatApp with all necessary providers for testing.
 *
 * This helper:
 * - Uses real @assistant-ui/react components (no mocking)
 * - Relies on MSW to intercept network calls
 * - Provides a clean, minimal setup for testing
 * - Optionally waits for the chat interface to be ready
 */
export async function renderChatApp(options: RenderChatAppOptions = {}) {
  const { profileId = 'default_assistant', waitForReady = false } = options;

  const result = render(<ChatApp profileId={profileId} />);

  if (waitForReady) {
    // Wait for the chat interface to be interactive
    await screen.findByPlaceholderText('Write a message...', {}, { timeout: 5000 });
  }

  return result;
}
