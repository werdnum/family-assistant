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
      expect(screen.getByTestId('attachment-0')).toBeInTheDocument();
      expect(screen.getByTestId('attachment-1')).toBeInTheDocument();
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
          // Invalid: missing required fields
          type: 'user',
          mime_type: 'text/plain',
          // missing filename and size
        },
        {
          // Invalid: wrong type structure
          not_an_attachment: true,
        },
      ];

      render(<DynamicToolUI {...mockProps} artifact={{ attachments: mixedAttachments }} />);

      // Should only have 1 valid attachment
      expect(screen.getByTestId('attachments-count')).toHaveTextContent('1');
      expect(screen.getByTestId('attachment-0')).toBeInTheDocument();

      // Should have logged warnings for invalid attachments
      expect(consoleWarnSpy).toHaveBeenCalledTimes(2);
      expect(consoleWarnSpy).toHaveBeenCalledWith(
        'Invalid attachment structure:',
        expect.any(Object)
      );

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

  describe('Performance with Large Attachment Arrays', () => {
    it('efficiently processes large arrays of attachments', () => {
      const consoleWarnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {});

      // Create a large array with mixed valid/invalid attachments
      const largeAttachmentArray = Array.from({ length: 1000 }, (_, index) => {
        if (index % 3 === 0) {
          // Valid attachment
          return {
            attachment_id: `attachment-${index}`,
            type: 'image',
            mime_type: 'image/png',
            content_url: `https://example.com/image-${index}.png`,
          };
        } else {
          // Invalid attachment
          return {
            invalid: true,
            index,
          };
        }
      });

      const startTime = performance.now();

      render(<DynamicToolUI {...mockProps} artifact={{ attachments: largeAttachmentArray }} />);

      const endTime = performance.now();
      const processingTime = endTime - startTime;

      // Should have processed 334 valid attachments (every 3rd one)
      expect(screen.getByTestId('attachments-count')).toHaveTextContent('334');

      // Should have logged warnings for 666 invalid attachments
      expect(consoleWarnSpy).toHaveBeenCalledTimes(666);

      // Processing should be reasonably fast (less than 500ms for 1000 items)
      expect(processingTime).toBeLessThan(500);

      consoleWarnSpy.mockRestore();
    });
  });
});
