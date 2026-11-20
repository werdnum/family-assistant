import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { HttpResponse, http } from 'msw';
import { describe, expect, it, vi } from 'vitest';
import { server } from '../../test/setup.js';
import { LiveBrowserView } from '../LiveBrowserView';
import { ProfilesProvider } from '../profilesContext';
import { SubconversationPeekSheet } from '../SubconversationPeekSheet';
import type { BrowserViewFrame } from '../types';
import type { PendingDelegation } from '../usePendingDelegations';

const makeFrame = (overrides: Partial<BrowserViewFrame> = {}): BrowserViewFrame => ({
  frame_id: '1',
  url: 'https://example.com/search?q=flights',
  captured_at: '2026-11-20T11:30:00Z',
  tool_name: 'browser_open',
  backend_kind: 'remote',
  width: 1280,
  height: 720,
  image_url: '/api/v1/chat/conversations/conv-1/browser-view/frame?v=1',
  ...overrides,
});

describe('LiveBrowserView', () => {
  it('renders nothing when no frame exists and not running on a browser profile', () => {
    const { container } = render(
      <LiveBrowserView frame={null} isRunning={false} isBrowserProfile={false} />
    );
    expect(container.firstChild).toBeNull();
  });

  it('shows a starting indicator while a browser profile turn is running before first frame', () => {
    render(<LiveBrowserView frame={null} isRunning isBrowserProfile />);
    expect(screen.getByTestId('live-browser-waiting')).toBeInTheDocument();
    expect(screen.getByText(/Starting session/i)).toBeInTheDocument();
  });

  it('renders live frame metadata, collapses/expands, and opens live fullscreen dialog', () => {
    const frame1 = makeFrame();
    const { rerender } = render(<LiveBrowserView frame={frame1} isRunning isBrowserProfile />);

    expect(screen.getByTestId('live-browser-view')).toBeInTheDocument();
    expect(screen.getByTestId('live-browser-status')).toHaveTextContent('Live');
    expect(screen.getByTestId('live-browser-backend-badge')).toHaveTextContent('remote');
    expect(screen.getByTestId('live-browser-url')).toHaveTextContent(
      'example.com/search?q=flights'
    );
    expect(screen.getByTestId('live-browser-tool')).toHaveTextContent('open');
    expect(screen.getByTestId('live-browser-image')).toHaveAttribute('src', frame1.image_url);

    // Collapse hides the viewport while keeping the header chrome bar.
    fireEvent.click(screen.getByTestId('live-browser-toggle'));
    expect(screen.queryByTestId('live-browser-viewport')).not.toBeInTheDocument();

    // Expand restores the viewport.
    fireEvent.click(screen.getByTestId('live-browser-toggle'));
    expect(screen.getByTestId('live-browser-viewport')).toBeInTheDocument();

    // Open fullscreen dialog and verify live updates propagate while open.
    fireEvent.click(screen.getByTestId('live-browser-expand'));
    expect(screen.getByTestId('live-browser-fullscreen-dialog')).toBeInTheDocument();
    expect(screen.getByTestId('live-browser-fullscreen-image')).toHaveAttribute(
      'src',
      frame1.image_url
    );

    const frame2 = makeFrame({
      frame_id: '2',
      url: 'https://example.com/checkout',
      tool_name: 'browser_click',
      backend_kind: 'local',
      image_url: '/api/v1/chat/conversations/conv-1/browser-view/frame?v=2',
    });
    rerender(<LiveBrowserView frame={frame2} isRunning={false} isBrowserProfile />);

    expect(screen.getByTestId('live-browser-status')).toHaveTextContent('Browser');
    expect(screen.getByTestId('live-browser-backend-badge')).toHaveTextContent('local');
    expect(screen.getByTestId('live-browser-fullscreen-image')).toHaveAttribute(
      'src',
      frame2.image_url
    );
  });
});

describe('SubconversationPeekSheet live browser integration', () => {
  it('renders initial browser_view snapshot and updates live on browser_frame SSE events', async () => {
    const delegation: PendingDelegation = {
      delegation_id: 'del-browser-1',
      target_profile_id: 'browser_profile',
      status: 'running',
      request_preview: 'Check store hours',
      created_at: '2026-11-20T11:30:00Z',
      started_at: '2026-11-20T11:30:01Z',
      completed_at: null,
      cancel_requested: false,
      children_total: 0,
      children_finished: 0,
      subconversation_id: 'sub-browser-1',
    };

    const initialFrame = makeFrame({
      frame_id: '10',
      url: 'https://shop.example/hours',
      image_url:
        '/api/v1/chat/conversations/conv-browser-1/subconversations/sub-browser-1/browser-view/frame?v=10',
    });

    const nextFrame = makeFrame({
      frame_id: '11',
      url: 'https://shop.example/locations',
      tool_name: 'browser_click',
      backend_kind: 'remote',
      image_url:
        '/api/v1/chat/conversations/conv-browser-1/subconversations/sub-browser-1/browser-view/frame?v=11',
    });

    const encoder = new TextEncoder();
    const streamHandles: {
      emitChunk: ((chunk: string) => void) | null;
      closeStream: (() => void) | null;
    } = {
      emitChunk: null,
      closeStream: null,
    };

    server.use(
      http.get('/api/v1/profiles', () =>
        HttpResponse.json({
          profiles: [
            {
              id: 'browser_profile',
              description: 'Browser profile',
              available_tools: ['browser_open', 'browser_click'],
              enabled_mcp_servers: [],
              has_browser_view: true,
            },
          ],
          default_profile_id: 'default_assistant',
        })
      ),
      http.get(
        '/api/v1/chat/conversations/conv-browser-1/subconversations/sub-browser-1/messages',
        () =>
          HttpResponse.json({
            conversation_id: 'conv-browser-1',
            subconversation_id: 'sub-browser-1',
            delegation,
            delegation_error: null,
            request_text: 'Check store hours',
            child_delegations: [],
            messages: [],
            count: 0,
            total_messages: 0,
            has_more_before: false,
            has_more_after: false,
            active_turns: [
              {
                turn_id: 'turn-sub-1',
                status: 'running',
                first_seq: 0,
                latest_seq: 0,
              },
            ],
            browser_view: initialFrame,
          })
      ),
      http.get(
        '/api/v1/chat/conversations/conv-browser-1/subconversations/sub-browser-1/stream',
        () => {
          const stream = new ReadableStream({
            start(controller) {
              streamHandles.emitChunk = (chunk: string) =>
                controller.enqueue(encoder.encode(chunk));
              streamHandles.closeStream = () => controller.close();
            },
          });
          return new HttpResponse(stream, {
            status: 200,
            headers: { 'Content-Type': 'text/event-stream' },
          });
        }
      )
    );

    render(
      <ProfilesProvider>
        <SubconversationPeekSheet
          conversationId="conv-browser-1"
          delegationStack={[delegation]}
          onPushDelegation={vi.fn()}
          onPopDelegation={vi.fn()}
          onClose={vi.fn()}
        />
      </ProfilesProvider>
    );

    await waitFor(() => {
      expect(screen.getByTestId('live-browser-image')).toHaveAttribute(
        'src',
        initialFrame.image_url
      );
    });
    expect(screen.getByTestId('live-browser-url')).toHaveTextContent('shop.example/hours');

    await waitFor(() => {
      expect(streamHandles.emitChunk).not.toBeNull();
    });
    streamHandles.emitChunk?.(
      `event: browser_frame\ndata: ${JSON.stringify({ type: 'browser_frame', seq: 1, ...nextFrame })}\n\n`
    );

    await waitFor(() => {
      expect(screen.getByTestId('live-browser-image')).toHaveAttribute('src', nextFrame.image_url);
    });
    expect(screen.getByTestId('live-browser-url')).toHaveTextContent('shop.example/locations');

    streamHandles.closeStream?.();
  });
});
