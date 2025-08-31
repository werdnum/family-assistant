import React from 'react';
import { render } from '@testing-library/react';
import ChatApp from '../../chat/ChatApp';

interface RenderChatAppOptions {
  profileId?: string;
}

/**
 * Renders ChatApp with all necessary providers for testing.
 *
 * This helper:
 * - Uses real @assistant-ui/react components (no mocking)
 * - Relies on MSW to intercept network calls
 * - Provides a clean, minimal setup for testing
 */
export function renderChatApp(options: RenderChatAppOptions = {}) {
  const { profileId = 'default_assistant' } = options;

  return render(<ChatApp profileId={profileId} />);
}
