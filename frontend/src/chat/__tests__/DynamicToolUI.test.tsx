import { render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { getAttachmentKey } from '../../types/attachments';
import { DynamicToolUI } from '../DynamicToolUI';

// Mock the ToolWithConfirmation component since we're testing DynamicToolUI logic
vi.mock('../ToolWithConfirmation', () => ({
  ToolWithConfirmation: ({
    toolName,
    attachments,
  }: {
    toolName: string;
    attachments: unknown[];
  }) => (
    <div data-testid="tool-with-confirmation">
      <span data-testid="tool-name">{toolName}</span>
      <span data-testid="attachments-count">{attachments.length}</span>
      {attachments.map((attachment, index) => (
        <div key={getAttachmentKey(attachment, index)} data-testid={`attachment-${index}`}>
          {JSON.stringify(attachment)}
        </div>
      ))}
    </div>
  ),
}));

describe('DynamicToolUI', () => {
  const mockProps = {
    type: 'tool-call' as const,
    toolCallId: 'test-call-id',
    toolName: 'test_tool',
    args: {},
    argsText: '{}',
    status: { type: 'complete' },
  };

  describe('Attachment Extraction', () => {
    it('extracts valid attachments from artifact', () => {
      const validAttachments = [
        {
          attachment_id: 'attachment-1',
          type: 'image',
          mime_type: 'image/png',
          content_url: 'https://example.com/image.png',
        },
        {
          attachment_id: 'attachment-2',
          type: 'user',
          mime_type: 'text/plain',
          filename: 'document.txt',
          size: 1024,
        },
      ];

      render(<DynamicToolUI {...mockProps} artifact={{ attachments: validAttachments }} />);

      expect(screen.getByTestId('attachments-count')).toHaveTextContent('2');
      expect(screen.getByTestId('attachment-0')).toHaveTextContent('attachment-1');
      expect(screen.getByTestId('attachment-1')).toHaveTextContent('attachment-2');
    });

    it('filters out invalid attachments and logs warnings', () => {
      const consoleWarnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {});

      const mixedAttachments = [
        {
          attachment_id: 'valid-attachment',
          type: 'image',
          mime_type: 'image/png',
          content_url: 'https://example.com/image.png',
        },
        {
          // Invalid: user attachments require filename and size
          attachment_id: 'user-missing-filename',
          type: 'user',
          mime_type: 'text/plain',
        },
        {
          // Invalid: wrong type structure
          not_an_attachment: true,
        },
      ];

      render(<DynamicToolUI {...mockProps} artifact={{ attachments: mixedAttachments }} />);

      expect(screen.getByTestId('attachments-count')).toHaveTextContent('1');
      expect(screen.getByTestId('attachment-0')).toHaveTextContent('valid-attachment');

      expect(consoleWarnSpy).toHaveBeenCalled();

      consoleWarnSpy.mockRestore();
    });

    it('handles non-array attachments gracefully', () => {
      render(<DynamicToolUI {...mockProps} artifact={{ attachments: 'not-an-array' }} />);

      expect(screen.getByTestId('attachments-count')).toHaveTextContent('0');
    });

    it('handles null/undefined artifact', () => {
      render(<DynamicToolUI {...mockProps} artifact={undefined} />);

      expect(screen.getByTestId('attachments-count')).toHaveTextContent('0');
    });

    it('prefers artifact attachments over direct attachments prop', () => {
      const artifactAttachments = [
        {
          attachment_id: 'artifact-attachment',
          type: 'image',
          mime_type: 'image/png',
          content_url: 'https://example.com/artifact.png',
        },
      ];

      const directAttachments = [
        {
          attachment_id: 'direct-attachment',
          type: 'image',
          mime_type: 'image/png',
          content_url: 'https://example.com/direct.png',
        },
      ];

      render(
        <DynamicToolUI
          {...mockProps}
          artifact={{ attachments: artifactAttachments }}
          attachments={directAttachments}
        />
      );

      // Should use artifact attachments, not direct ones
      expect(screen.getByTestId('attachments-count')).toHaveTextContent('1');
      expect(screen.getByTestId('attachment-0')).toHaveTextContent('artifact-attachment');
    });

    it('falls back to direct attachments when artifact has none', () => {
      const directAttachments = [
        {
          attachment_id: 'direct-attachment',
          type: 'image',
          mime_type: 'image/png',
          content_url: 'https://example.com/direct.png',
        },
      ];

      render(<DynamicToolUI {...mockProps} artifact={{}} attachments={directAttachments} />);

      expect(screen.getByTestId('attachments-count')).toHaveTextContent('1');
      expect(screen.getByTestId('attachment-0')).toHaveTextContent('direct-attachment');
    });
  });
});
