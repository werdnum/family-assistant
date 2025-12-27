import { render, screen } from '@testing-library/react';
import React from 'react';
import { describe, expect, it } from 'vitest';
import { toolTestCases } from '../../test/toolTestData';
import { getAttachmentKey } from '../../types/attachments';
import { ToolFallback, toolUIsByName } from '../ToolUI';

// Helper component that mimics how tools are rendered in the app
const ToolUI = ({ toolCall, toolResponse }) => {
  const toolName = toolCall?.function?.name;

  let args = {};
  try {
    args = toolCall?.function?.arguments ? JSON.parse(toolCall.function.arguments) : {};
  } catch (_e) {
    // Handle malformed JSON
    args = { _raw: toolCall?.function?.arguments };
  }

  const status = toolResponse ? { type: 'complete' } : { type: 'running' };
  const result = toolResponse?.content || null;

  const ToolComponent = toolUIsByName[toolName] || ToolFallback;

  return <ToolComponent toolName={toolName} args={args} result={result} status={status} />;
};

describe('ToolUI Component', () => {
  describe('Component Rendering', () => {
    it('renders ToolFallback for unknown tool types', () => {
      const unknownTool = {
        id: 'test-1',
        type: 'function',
        function: {
          name: 'unknown_tool',
          arguments: '{}',
        },
      };

      render(<ToolUI toolCall={unknownTool} />);

      expect(screen.getByText(/unknown_tool/)).toBeInTheDocument();
      expect(screen.getByText(/Executing tool.../)).toBeInTheDocument();
    });

    it('renders specific tool UI components', () => {
      const noteToolCall = {
        id: 'test-2',
        type: 'function',
        function: {
          name: 'add_or_update_note',
          arguments: '{"title": "Test Note"}',
        },
      };

      render(<ToolUI toolCall={noteToolCall} />);

      // Check for tool-specific content - Notes show "📝 Note" as header
      expect(screen.getByText(/📝 Note/)).toBeInTheDocument();
      expect(screen.getByText('Test Note')).toBeInTheDocument();
    });

    it('passes tool response to component when available', () => {
      const toolCall = {
        id: 'test-3',
        type: 'function',
        function: {
          name: 'search_calendar_events',
          arguments: '{"start_date": "2025-01-01"}',
        },
      };

      const toolResponse = {
        tool_call_id: 'test-3',
        content: 'Found 5 events',
      };

      render(<ToolUI toolCall={toolCall} toolResponse={toolResponse} />);

      expect(screen.getByText(/Found 5 events/)).toBeInTheDocument();
    });
  });

  describe('Real Production Data Tests', () => {
    Object.entries(toolTestCases).forEach(([toolName, testCases]) => {
      describe(`${toolName} tool`, () => {
        testCases.forEach((testCase, index) => {
          it(`renders with real data case ${index + 1}`, () => {
            const { tool_call, tool_response } = testCase;

            render(<ToolUI toolCall={tool_call} toolResponse={tool_response} />);

            // Verify component renders without crashing by checking for container
            expect(document.querySelector('.tool-call-container')).toBeInTheDocument();

            // For tools with responses, verify at least some content is shown
            if (tool_response && tool_response.content && tool_response.content.length > 0) {
              // Tools might transform content, so just check the container has content
              const container = document.querySelector('.tool-call-container');
              expect(container.textContent.length).toBeGreaterThan(0);
            }
          });
        });
      });
    });
  });

  describe('Edge Cases', () => {
    it('handles malformed arguments gracefully', () => {
      const malformedTool = {
        id: 'test-malformed',
        type: 'function',
        function: {
          name: 'add_or_update_note',
          arguments: 'not valid json',
        },
      };

      render(<ToolUI toolCall={malformedTool} />);
      // Should render the note UI container
      expect(document.querySelector('.tool-call-container.tool-note')).toBeInTheDocument();
    });

    it('handles missing function property', () => {
      const incompleteTool = {
        id: 'test-incomplete',
        type: 'function',
        // function property missing
      };

      render(<ToolUI toolCall={incompleteTool} />);
      // Should render fallback with undefined tool name
      expect(document.querySelector('.tool-call-container')).toBeInTheDocument();
    });

    it('handles empty tool response', () => {
      const toolCall = {
        id: 'test-empty-response',
        type: 'function',
        function: {
          name: 'get_current_time',
          arguments: '{}',
        },
      };

      const emptyResponse = {
        tool_call_id: 'test-empty-response',
        content: '',
      };

      render(<ToolUI toolCall={toolCall} toolResponse={emptyResponse} />);

      // Should still render without errors
      expect(document.querySelector('.tool-call-container')).toBeInTheDocument();
    });
  });

  describe('Multi-tool Scenarios', () => {
    it('renders multiple tool calls independently', () => {
      const toolCalls = [
        {
          id: 'multi-1',
          type: 'function',
          function: {
            name: 'add_or_update_note',
            arguments: '{"title": "Note 1"}',
          },
        },
        {
          id: 'multi-2',
          type: 'function',
          function: {
            name: 'search_calendar_events',
            arguments: '{"start_date": "2025-01-01"}',
          },
        },
      ];

      const { container } = render(
        <div>
          {toolCalls.map((toolCall) => (
            <ToolUI key={toolCall.id} toolCall={toolCall} />
          ))}
        </div>
      );

      // Check both tools rendered
      expect(container.querySelectorAll('.tool-call-container')).toHaveLength(2);
      // Note tool shows title
      expect(screen.getByText('Note 1')).toBeInTheDocument();
      // Calendar tool shows its header
      expect(screen.getByText(/🔍📅 Search Calendar Events/)).toBeInTheDocument();
    });
  });

  describe('Tool Display Names', () => {
    it('shows appropriate display names for each tool type', () => {
      // Tools with custom UIs have friendly display names
      const toolsWithCustomUI = {
        add_or_update_note: '📝 Note',
        search_calendar_events: '🔍📅 Search Calendar Events',
        send_message_to_user: '💬 Send Message',
        search_documents: '🔍 Search Documents',
      };

      // Tools without custom UIs show their tool name
      const toolsWithFallback = ['browser_navigate', 'browser_install'];

      Object.entries(toolsWithCustomUI).forEach(([toolName, displayName]) => {
        const toolCall = {
          id: `test-${toolName}`,
          type: 'function',
          function: {
            name: toolName,
            arguments: '{}',
          },
        };

        const { unmount } = render(<ToolUI toolCall={toolCall} />);

        // Check the expected display name appears
        expect(screen.getByText(new RegExp(displayName))).toBeInTheDocument();

        unmount();
      });

      // Test fallback tools show their raw name
      toolsWithFallback.forEach((toolName) => {
        const toolCall = {
          id: `test-${toolName}`,
          type: 'function',
          function: {
            name: toolName,
            arguments: '{}',
          },
        };

        const { unmount } = render(<ToolUI toolCall={toolCall} />);

        // Fallback tools show their raw name
        expect(screen.getByText(toolName)).toBeInTheDocument();

        unmount();
      });
    });
  });

  describe('Camera Tool UIs', () => {
    it('renders list_cameras with cameras array', () => {
      const toolCall = {
        id: 'test-list-cameras',
        type: 'function',
        function: {
          name: 'list_cameras',
          arguments: '{}',
        },
      };

      const toolResponse = {
        tool_call_id: 'test-list-cameras',
        content: JSON.stringify({
          cameras: [
            { id: 'cam1', name: 'Front Door', status: 'online', backend: 'reolink' },
            { id: 'cam2', name: 'Backyard', status: 'offline', backend: 'reolink' },
          ],
          count: 2,
        }),
      };

      render(<ToolUI toolCall={toolCall} toolResponse={toolResponse} />);

      expect(screen.getByText(/📹 List Cameras/)).toBeInTheDocument();
      expect(screen.getByText('Front Door')).toBeInTheDocument();
      expect(screen.getByText('Backyard')).toBeInTheDocument();
      expect(screen.getByText('online')).toBeInTheDocument();
      expect(screen.getByText('offline')).toBeInTheDocument();
    });

    it('renders list_cameras with no cameras', () => {
      const toolCall = {
        id: 'test-list-cameras-empty',
        type: 'function',
        function: {
          name: 'list_cameras',
          arguments: '{}',
        },
      };

      const toolResponse = {
        tool_call_id: 'test-list-cameras-empty',
        content: JSON.stringify({
          cameras: [],
          count: 0,
        }),
      };

      render(<ToolUI toolCall={toolCall} toolResponse={toolResponse} />);

      expect(screen.getByText(/📹 List Cameras/)).toBeInTheDocument();
      expect(screen.getByText(/No cameras configured/)).toBeInTheDocument();
    });

    it('renders search_camera_events with events', () => {
      const toolCall = {
        id: 'test-search-events',
        type: 'function',
        function: {
          name: 'search_camera_events',
          arguments: JSON.stringify({
            camera_id: 'chickens',
            start_time: '2025-12-26T06:00:00',
            end_time: '2025-12-26T06:10:00',
          }),
        },
      };

      const toolResponse = {
        tool_call_id: 'test-search-events',
        content: JSON.stringify({
          events: [
            {
              camera_id: 'chickens',
              start_time: '2025-12-26T06:01:00+11:00',
              end_time: '2025-12-26T06:02:00+11:00',
              event_type: 'pet',
              confidence: 0.95,
            },
            {
              camera_id: 'chickens',
              start_time: '2025-12-26T06:05:00+11:00',
              end_time: null,
              event_type: 'motion',
              confidence: 0.8,
            },
          ],
          count: 2,
          warning: null,
        }),
      };

      render(<ToolUI toolCall={toolCall} toolResponse={toolResponse} />);

      expect(screen.getByText(/🔍 Search Camera Events/)).toBeInTheDocument();
      expect(screen.getByText(/Camera:/)).toBeInTheDocument();
      expect(screen.getByText('chickens')).toBeInTheDocument();
      expect(screen.getByText(/Found 2 events/)).toBeInTheDocument();
      expect(screen.getByText(/pet/)).toBeInTheDocument();
      expect(screen.getByText(/motion/)).toBeInTheDocument();
      expect(screen.getByText(/95% confidence/)).toBeInTheDocument();
    });

    it('renders search_camera_events with no events', () => {
      const toolCall = {
        id: 'test-search-events-empty',
        type: 'function',
        function: {
          name: 'search_camera_events',
          arguments: JSON.stringify({
            camera_id: 'cam1',
            start_time: '2025-12-26T00:00:00',
            end_time: '2025-12-26T01:00:00',
          }),
        },
      };

      const toolResponse = {
        tool_call_id: 'test-search-events-empty',
        content: JSON.stringify({
          events: [],
          count: 0,
          warning: null,
        }),
      };

      render(<ToolUI toolCall={toolCall} toolResponse={toolResponse} />);

      expect(screen.getByText(/🔍 Search Camera Events/)).toBeInTheDocument();
      expect(screen.getByText(/No events found/)).toBeInTheDocument();
    });

    it('renders search_camera_events with warning about old dates', () => {
      const toolCall = {
        id: 'test-search-events-warning',
        type: 'function',
        function: {
          name: 'search_camera_events',
          arguments: JSON.stringify({
            camera_id: 'cam1',
            start_time: '2024-01-01T00:00:00',
            end_time: '2024-01-01T01:00:00',
          }),
        },
      };

      const toolResponse = {
        tool_call_id: 'test-search-events-warning',
        content: JSON.stringify({
          events: [],
          count: 0,
          warning: 'These dates are more than 30 days in the past.',
        }),
      };

      render(<ToolUI toolCall={toolCall} toolResponse={toolResponse} />);

      expect(screen.getByText(/⚠️/)).toBeInTheDocument();
      expect(screen.getByText(/30 days in the past/)).toBeInTheDocument();
    });

    it('renders get_camera_recordings with recordings', () => {
      const toolCall = {
        id: 'test-recordings',
        type: 'function',
        function: {
          name: 'get_camera_recordings',
          arguments: JSON.stringify({
            camera_id: 'cam1',
            start_time: '2025-12-26T00:00:00',
            end_time: '2025-12-26T06:00:00',
          }),
        },
      };

      const toolResponse = {
        tool_call_id: 'test-recordings',
        content: JSON.stringify({
          recordings: [
            {
              camera_id: 'cam1',
              start_time: '2025-12-26T00:00:00+11:00',
              end_time: '2025-12-26T02:00:00+11:00',
              filename: 'recording1.mp4',
              size_bytes: 104857600,
              duration_seconds: 7200,
            },
          ],
          count: 1,
          total_duration_hours: 2.0,
        }),
      };

      render(<ToolUI toolCall={toolCall} toolResponse={toolResponse} />);

      expect(screen.getByText(/🎬 Camera Recordings/)).toBeInTheDocument();
      expect(screen.getByText(/Found 1 recording/)).toBeInTheDocument();
      expect(screen.getByText(/2 hours total/)).toBeInTheDocument();
    });

    it('renders get_camera_frame while running', () => {
      const toolCall = {
        id: 'test-frame',
        type: 'function',
        function: {
          name: 'get_camera_frame',
          arguments: JSON.stringify({
            camera_id: 'cam1',
            timestamp: '2025-12-26T06:05:00',
          }),
        },
      };

      render(<ToolUI toolCall={toolCall} />);

      expect(screen.getByText(/📸 Camera Frame/)).toBeInTheDocument();
      expect(screen.getByText(/cam1/)).toBeInTheDocument();
      expect(screen.getByText(/Extracting frame/)).toBeInTheDocument();
    });

    it('renders get_camera_frames_batch with timestamps', () => {
      const toolCall = {
        id: 'test-batch',
        type: 'function',
        function: {
          name: 'get_camera_frames_batch',
          arguments: JSON.stringify({
            camera_id: 'cam1',
            start_time: '2025-12-26T00:00:00',
            end_time: '2025-12-26T01:00:00',
            interval_minutes: 15,
          }),
        },
      };

      const toolResponse = {
        tool_call_id: 'test-batch',
        content: JSON.stringify({
          camera_id: 'cam1',
          timestamps: [
            '2025-12-26T00:00:00+11:00',
            '2025-12-26T00:15:00+11:00',
            '2025-12-26T00:30:00+11:00',
            '2025-12-26T00:45:00+11:00',
          ],
          count: 4,
        }),
      };

      render(<ToolUI toolCall={toolCall} toolResponse={toolResponse} />);

      expect(screen.getByText(/🎞️ Camera Frames Batch/)).toBeInTheDocument();
      expect(screen.getByText(/4 frames/)).toBeInTheDocument();
    });

    it('renders camera tool with error', () => {
      const toolCall = {
        id: 'test-error',
        type: 'function',
        function: {
          name: 'list_cameras',
          arguments: '{}',
        },
      };

      const toolResponse = {
        tool_call_id: 'test-error',
        content: JSON.stringify({
          error: 'Camera backend not configured',
        }),
      };

      render(<ToolUI toolCall={toolCall} toolResponse={toolResponse} />);

      expect(screen.getByText(/📹 List Cameras/)).toBeInTheDocument();
      expect(screen.getByText(/Camera backend not configured/)).toBeInTheDocument();
    });
  });

  describe('Attachment Key Generation', () => {
    it('generates stable keys for attachments using getAttachmentKey', () => {
      // Test with valid attachment
      const validAttachment = {
        attachment_id: 'test-attachment-123',
        type: 'image',
        mime_type: 'image/png',
        content_url: 'https://example.com/image.png',
      };

      const key1 = getAttachmentKey(validAttachment, 0);
      const key2 = getAttachmentKey(validAttachment, 0);

      // Should be stable and use attachment_id
      expect(key1).toBe('test-attachment-123');
      expect(key2).toBe('test-attachment-123');
    });

    it('handles attachments without attachment_id gracefully', () => {
      const attachmentWithoutId = {
        type: 'user',
        mime_type: 'text/plain',
        filename: 'test.txt',
        size: 100,
      };

      const key = getAttachmentKey(attachmentWithoutId, 5);

      // Should generate a fallback key but not use the index
      expect(key).toMatch(/^attachment-\d+$/);
      expect(key).not.toContain('index');
    });

    it('falls back to index-based key for completely invalid attachments', () => {
      const invalidAttachment = { not_an_attachment: true };

      const key = getAttachmentKey(invalidAttachment, 3);

      expect(key).toBe('attachment-index-3');
    });

    it('renders tool with attachments using stable keys', () => {
      const toolCall = {
        id: 'test-with-attachments',
        type: 'function',
        function: {
          name: 'add_or_update_note',
          arguments: '{"title": "Note with attachments"}',
        },
      };

      const toolResponse = {
        tool_call_id: 'test-with-attachments',
        content: 'Note created successfully',
      };

      // Mock attachments data
      const mockAttachments = [
        {
          attachment_id: 'attachment-1',
          type: 'image',
          mime_type: 'image/png',
          content_url: 'https://example.com/image1.png',
        },
        {
          attachment_id: 'attachment-2',
          type: 'user',
          mime_type: 'text/plain',
          filename: 'document.txt',
          size: 1024,
        },
      ];

      // Create a modified ToolUI component that accepts attachments
      const ToolUIWithAttachments = ({ toolCall, toolResponse, attachments }) => {
        const toolName = toolCall?.function?.name;
        let args = {};
        try {
          args = toolCall?.function?.arguments ? JSON.parse(toolCall.function.arguments) : {};
        } catch (_e) {
          args = { _raw: toolCall?.function?.arguments };
        }

        const status = toolResponse ? { type: 'complete' } : { type: 'running' };
        const result = toolResponse?.content || null;
        const ToolComponent = toolUIsByName[toolName] || ToolFallback;

        return (
          <ToolComponent
            toolName={toolName}
            args={args}
            result={result}
            status={status}
            attachments={attachments}
          />
        );
      };

      render(
        <ToolUIWithAttachments
          toolCall={toolCall}
          toolResponse={toolResponse}
          attachments={mockAttachments}
        />
      );

      // Should render without errors
      expect(document.querySelector('.tool-call-container')).toBeInTheDocument();
    });
  });
});
