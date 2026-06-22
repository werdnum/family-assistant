import { screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { renderChatApp } from '../../test/utils/renderChatApp';
import { LOADING_MARKER } from '../constants';
import { hasCopyableAssistantContent } from '../Thread';

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

describe('hasCopyableAssistantContent', () => {
  it('returns true for non-empty text content', () => {
    expect(hasCopyableAssistantContent([{ type: 'text', text: 'Done.' }])).toBe(true);
    expect(hasCopyableAssistantContent('Done.')).toBe(true);
  });

  it('returns false for tool-only content', () => {
    expect(
      hasCopyableAssistantContent([
        {
          type: 'tool-call',
          toolCallId: 'tool-call-1',
          toolName: 'list_notes',
          args: {},
          result: { count: 1 },
        },
      ])
    ).toBe(false);
  });

  it('returns false for empty or loading text content', () => {
    expect(hasCopyableAssistantContent([{ type: 'text', text: '   ' }])).toBe(false);
    expect(hasCopyableAssistantContent([{ type: 'text', text: LOADING_MARKER }])).toBe(false);
  });
});
