import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import ChatApp from '../../chat/ChatApp';

interface RenderChatAppOptions {
  profileId?: string;
  waitForReady?: boolean;
  initialEntries?: string[];
}

/**
 * Renders ChatApp with all necessary providers for testing.
 *
 * This helper:
 * - Uses real @assistant-ui/react components (no mocking)
 * - Relies on MSW to intercept network calls
 * - Provides a clean, minimal setup for testing
 * - Optionally waits for the chat interface to be ready
 * - Wraps ChatApp in MemoryRouter for React Router hooks
 */
export async function renderChatApp(options: RenderChatAppOptions = {}) {
  const {
    profileId = 'default_assistant',
    waitForReady = false,
    initialEntries = ['/chat'],
  } = options;

  const result = render(
    <MemoryRouter initialEntries={initialEntries}>
      <ChatApp profileId={profileId} />
    </MemoryRouter>
  );

  if (waitForReady) {
    // Wait for the chat interface to be interactive
    await screen.findByPlaceholderText('Write a message...', {}, { timeout: 5000 });
  }

  return result;
}
