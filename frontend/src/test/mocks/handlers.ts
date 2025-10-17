import { http, HttpResponse } from 'msw';

export const handlers = [
  // Mock profiles endpoint
  http.get('/api/v1/profiles', () => {
    return HttpResponse.json({
      profiles: [
        {
          id: 'browser_profile',
          description:
            'Assistant profile with web browsing capabilities for complex browser interactions like filling forms or navigating JavaScript-heavy sites.',
          llm_model: null,
          available_tools: [],
          enabled_mcp_servers: [],
        },
        {
          id: 'default_assistant',
          description:
            'Main assistant using default settings, without browser tools. Suitable for general tasks, note-taking, calendar management, and information retrieval.',
          llm_model: null,
          available_tools: [],
          enabled_mcp_servers: [],
        },
        {
          id: 'research',
          description:
            'Assistant profile for deep research, utilizing the Perplexity Sonar model. Ideal for comprehensive information gathering and analysis.',
          llm_model: null,
          available_tools: [],
          enabled_mcp_servers: [],
        },
      ],
      default_profile_id: 'default_assistant',
    });
  }),

  // Mock conversations endpoint - checks for interface_type filter
  http.get('/api/v1/chat/conversations', ({ request }) => {
    const url = new URL(request.url);
    const interfaceType = url.searchParams.get('interface_type');

    // If interface_type=web is requested, return only web conversations
    if (interfaceType === 'web') {
      return HttpResponse.json({
        conversations: [
          {
            conversation_id: 'web_conv_test-1',
            last_message: 'Hi there! How can I help you today?',
            last_timestamp: '2025-01-01T10:00:00Z',
            message_count: 3,
          },
          {
            conversation_id: 'web_conv_test-2',
            last_message: 'Good evening!',
            last_timestamp: '2025-01-01T09:00:00Z',
            message_count: 2,
          },
        ],
        count: 2,
      });
    }

    // If no filter, return mixed conversations (including non-web)
    return HttpResponse.json({
      conversations: [
        {
          conversation_id: 'web_conv_test-1',
          last_message: 'Hi there! How can I help you today?',
          last_timestamp: '2025-01-01T10:00:00Z',
          message_count: 3,
        },
        {
          conversation_id: 'telegram_conv_test-1',
          last_message: 'Telegram message',
          last_timestamp: '2025-01-01T08:00:00Z',
          message_count: 1,
        },
        {
          conversation_id: 'web_conv_test-2',
          last_message: 'Good evening!',
          last_timestamp: '2025-01-01T09:00:00Z',
          message_count: 2,
        },
      ],
      count: 3,
    });
  }),

  // Mock conversation messages endpoint
  http.get('/api/v1/chat/conversations/:conversationId/messages', ({ params }) => {
    const { conversationId } = params;

    if (conversationId === 'web_conv_test-1') {
      return HttpResponse.json({
        messages: [
          {
            id: 'msg-1',
            role: 'user',
            content: [{ type: 'text', text: 'Hello!' }],
            createdAt: '2025-01-01T10:00:00Z',
          },
          {
            id: 'msg-2',
            role: 'assistant',
            content: [{ type: 'text', text: 'Hi there! How can I help you?' }],
            createdAt: '2025-01-01T10:00:05Z',
          },
        ],
      });
    }

    if (conversationId === 'web_conv_test-2') {
      return HttpResponse.json({
        messages: [
          {
            id: 'msg-3',
            role: 'user',
            content: [{ type: 'text', text: 'Good evening!' }],
            createdAt: '2025-01-01T09:00:00Z',
          },
        ],
      });
    }

    return HttpResponse.json({ messages: [] });
  }),

  // Mock tool confirmation endpoint
  http.post('/api/v1/chat/confirm_tool', async ({ request }) => {
    const body = (await request.json()) as { toolCallId: string; approved: boolean };

    return HttpResponse.json({
      success: true,
      toolCallId: body.toolCallId,
      approved: body.approved,
      result: body.approved ? 'Tool executed successfully' : 'Tool execution cancelled',
    });
  }),

  // Mock context profiles endpoint
  http.get('/api/v1/context/profiles', () => {
    return HttpResponse.json({
      profiles: [
        {
          id: 'browser_profile',
          description:
            'Assistant profile with web browsing capabilities for complex browser interactions like filling forms or navigating JavaScript-heavy sites.',
          llm_model: null,
          available_tools: [],
          enabled_mcp_servers: [],
        },
        {
          id: 'default_assistant',
          description:
            'Main assistant using default settings, without browser tools. Suitable for general tasks, note-taking, calendar management, and information retrieval.',
          llm_model: null,
          available_tools: [],
          enabled_mcp_servers: [],
        },
        {
          id: 'research',
          description:
            'Assistant profile for deep research, utilizing the Perplexity Sonar model. Ideal for comprehensive information gathering and analysis.',
          llm_model: null,
          available_tools: [],
          enabled_mcp_servers: [],
        },
      ],
      default_profile_id: 'default_assistant',
    });
  }),

  // Mock context endpoint
  http.get('/api/v1/context', ({ request }) => {
    const url = new URL(request.url);
    const profileId = url.searchParams.get('profile_id');

    return HttpResponse.json({
      profile_id: profileId || 'default_assistant',
      context: 'Test context information',
    });
  }),

  // Mock notes endpoint
  http.get('/api/notes/', () => {
    return HttpResponse.json({
      notes: [{ title: 'Test Note', content: 'This is a test note' }],
    });
  }),

  // Mock documents endpoint
  http.get('/api/documents/', () => {
    return HttpResponse.json({
      documents: [{ id: 'doc-1', title: 'Test Document', type: 'pdf' }],
    });
  }),

  // Mock attachments endpoints
  http.post('/api/attachments/upload', () => {
    return HttpResponse.json({
      attachment_id: 'server-uuid-456',
      filename: 'test-file.png',
      content_type: 'image/png',
      size: 1024,
      url: '/api/attachments/server-uuid-456',
    });
  }),

  // Mock attachment GET endpoint - returns attachment data with proper headers
  http.get('/api/attachments/:attachmentId', ({ params }) => {
    const { attachmentId } = params;

    // Create a simple test image binary data (small PNG)
    const pngData = new Uint8Array([
      137, 80, 78, 71, 13, 10, 26, 10, 0, 0, 0, 13, 73, 72, 68, 82, 0, 0, 0, 100, 0, 0, 0, 100, 8,
      2, 0, 0, 0, 255, 128, 2, 3, 0, 0, 0, 185, 73, 68, 65, 84, 120, 156, 237, 193, 1, 13, 0, 0, 0,
      130, 32, 251, 79, 109, 14, 55, 160, 0, 0, 0, 0, 0, 0, 0, 0,
    ]);

    return new HttpResponse(pngData, {
      status: 200,
      headers: {
        'Content-Type': 'image/png',
        'Content-Length': pngData.length.toString(),
        'Content-Disposition': `inline; filename="test-attachment-${attachmentId}.png"`,
      },
    });
  }),

  http.delete('/api/attachments/:attachmentId', () => {
    return HttpResponse.json({
      message: 'Attachment deleted successfully',
    });
  }),

  // Mock client config endpoint for push notifications
  http.get('/api/client_config', () => {
    return HttpResponse.json({
      vapidPublicKey:
        'BF1GkKm7nXeKQPK8hMw-CwGlKgXUKW3lYVc5rIDKBhBgxXeVKRZKzRJ8YbGlDlm8ZH9YVGvF5JaK',
    });
  }),

  // Mock push subscribe endpoint
  http.post('/api/push/subscribe', async ({ request }) => {
    const body = (await request.json()) as { subscription: Record<string, unknown> };

    if (!body.subscription || !body.subscription.endpoint) {
      return HttpResponse.json(
        { status: 'error', message: 'Invalid subscription' },
        { status: 400 }
      );
    }

    return HttpResponse.json({
      status: 'success',
      id: `sub_${Date.now()}`,
    });
  }),

  // Mock push unsubscribe endpoint
  http.post('/api/push/unsubscribe', async ({ request }) => {
    const body = (await request.json()) as { endpoint: string };

    if (!body.endpoint) {
      return HttpResponse.json({ status: 'error', message: 'Invalid endpoint' }, { status: 400 });
    }

    return HttpResponse.json({
      status: 'success',
    });
  }),

  // Mock streaming chat endpoint - this is the key one for ChatApp
  http.post('/api/v1/chat/send_message_stream', async ({ request }) => {
    const body = (await request.json()) as {
      prompt: string;
      conversation_id: string;
      profile_id?: string;
    };

    // Create a simple streaming response
    const encoder = new TextEncoder();
    const stream = new ReadableStream({
      start(controller) {
        // Simulate streaming response based on the prompt
        const response = getTestResponse(body.prompt);
        const words = response.split(' ');

        words.forEach((word, index) => {
          setTimeout(() => {
            controller.enqueue(encoder.encode(`data: {"content": "${word} "}\n\n`));

            // Send done signal after last word
            if (index === words.length - 1) {
              setTimeout(() => {
                controller.enqueue(encoder.encode('data: {"done": true}\n\n'));
                controller.close();
              }, 50);
            }
          }, index * 100); // 100ms delay between words
        });
      },
    });

    return new HttpResponse(stream, {
      headers: {
        'Content-Type': 'text/event-stream',
        'Cache-Control': 'no-cache',
        Connection: 'keep-alive',
      },
    });
  }),
];

// Helper function to generate test responses based on input
function getTestResponse(prompt: string): string {
  const lowerPrompt = prompt.toLowerCase();

  if (lowerPrompt.includes('hello') || lowerPrompt.includes('hi')) {
    return 'Hi there! How can I help you today?';
  }

  if (lowerPrompt.includes('weather')) {
    return 'I would need your location to check the weather for you.';
  }

  if (lowerPrompt.includes('test')) {
    return 'This is a test response from the mocked backend.';
  }

  // Default response
  return "I received your message and I'm here to help!";
}

// Helper function to create test responses for streaming
export function createStreamingResponse(chunks: string[]): ReadableStream {
  const encoder = new TextEncoder();

  return new ReadableStream({
    start(controller) {
      chunks.forEach((chunk, index) => {
        // Add delay between chunks to simulate streaming
        setTimeout(() => {
          controller.enqueue(encoder.encode(`data: ${JSON.stringify({ content: chunk })}\n\n`));

          // Close stream after last chunk
          if (index === chunks.length - 1) {
            controller.enqueue(encoder.encode('data: {"done": true}\n\n'));
            controller.close();
          }
        }, index * 100);
      });
    },
  });
}

// Export test-specific handlers that can be used in individual tests
export const testHandlers = {
  // Handler for streaming chat responses
  streamingChat: (responseChunks: string[]) =>
    http.post('/api/v1/chat/send_message_stream', () => {
      const stream = createStreamingResponse(responseChunks);
      return new HttpResponse(stream, {
        headers: {
          'Content-Type': 'text/event-stream',
          'Cache-Control': 'no-cache',
          Connection: 'keep-alive',
        },
      });
    }),

  // Handler for tool calls in responses
  toolCallResponse: (toolName: string, args: Record<string, unknown>) =>
    http.post('/api/v1/chat/send_message_stream', () => {
      const encoder = new TextEncoder();
      const stream = new ReadableStream({
        start(controller) {
          // Send tool call first
          controller.enqueue(
            encoder.encode(
              `data: ${JSON.stringify({
                tool_calls: [
                  {
                    id: 'tool-call-1',
                    name: toolName,
                    arguments: JSON.stringify(args),
                  },
                ],
              })}\n\n`
            )
          );

          // Then send done
          setTimeout(() => {
            controller.enqueue(encoder.encode('data: {"done": true}\n\n'));
            controller.close();
          }, 50);
        },
      });

      return new HttpResponse(stream, {
        headers: {
          'Content-Type': 'text/event-stream',
          'Cache-Control': 'no-cache',
        },
      });
    }),
};
