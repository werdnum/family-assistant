import { HttpResponse, http } from 'msw';

// Captures the prompt supplied to POST /v1/chat/turns so the follow-up
// GET /conversations/:id/stream can derive a mock response from it.
const pendingTurnPrompts = new Map<string, string>();
// Captures the turn_id from POST /v1/chat/turns so the SSE stream can echo it
// on per-turn events. The send-and-watch client only renders events whose
// turn_id matches the one it sent, so the mock must use the real id (not a
// fixed placeholder) for the turn_ended terminator to be recognized.
const pendingTurnIds = new Map<string, string>();

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

  // Mock pending durable confirmations endpoint
  http.get('/api/v1/chat/confirmations/pending', () => {
    return HttpResponse.json({ confirmations: [] });
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
    return HttpResponse.json([
      {
        title: 'Test Note',
        content: 'This is a test note',
        include_in_prompt: true,
        attachment_ids: [],
      },
      {
        title: 'Note with Attachments',
        content: 'This note has attachments',
        include_in_prompt: false,
        attachment_ids: ['attachment-1', 'attachment-2'],
      },
    ]);
  }),

  // Mock individual note GET endpoint
  http.get('/api/notes/:title', ({ params }) => {
    const { title } = params;
    if (title === 'Test Note') {
      return HttpResponse.json({
        title: 'Test Note',
        content: 'This is a test note',
        include_in_prompt: true,
        attachment_ids: [],
      });
    }
    if (title === 'Note with Attachments') {
      return HttpResponse.json({
        title: 'Note with Attachments',
        content: 'This note has attachments',
        include_in_prompt: false,
        attachment_ids: ['attachment-1', 'attachment-2'],
      });
    }
    return HttpResponse.json({ detail: 'Note not found' }, { status: 404 });
  }),

  // Mock note POST endpoint (create/update)
  http.post('/api/notes/', async ({ request }) => {
    const body = (await request.json()) as {
      title: string;
      content: string;
      include_in_prompt: boolean;
      attachment_ids?: string[];
    };

    // Basic validation
    if (!body.title || !body.content) {
      return HttpResponse.json({ detail: 'Title and content are required' }, { status: 400 });
    }

    return HttpResponse.json({ message: 'Note saved' }, { status: 201 });
  }),

  // Mock note DELETE endpoint
  http.delete('/api/notes/:title', () => {
    return HttpResponse.json({ message: 'Note deleted' });
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
  http.get('/api/attachments/:attachmentId', ({ params, request }) => {
    const { attachmentId } = params;

    // If Accept header is application/json, return metadata
    const acceptHeader = request.headers.get('Accept');
    if (acceptHeader?.includes('application/json')) {
      // Return metadata for the attachment
      return HttpResponse.json({
        attachment_id: attachmentId as string,
        mime_type: 'image/png',
        filename: `test-attachment-${attachmentId}.png`,
        size: 1024,
        created_at: '2025-01-01T10:00:00Z',
      });
    }

    // Otherwise, return the binary file data
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

  // Resumable streaming: POST /turns kicks off a turn (returns identity),
  // GET /conversations/:id/stream delivers the SSE events. This mirrors the
  // production two-step flow. The prompt captured at POST time is stashed so
  // the GET stream can derive a response from it.
  http.post('/api/v1/chat/turns', async ({ request }) => {
    const body = (await request.json()) as {
      turn_id: string;
      prompt: string;
      conversation_id?: string;
      profile_id?: string;
    };
    const conversationId = body.conversation_id || `web_conv_${Date.now()}`;
    pendingTurnPrompts.set(conversationId, body.prompt);
    pendingTurnIds.set(conversationId, body.turn_id);
    return HttpResponse.json({
      turn_id: body.turn_id,
      conversation_id: conversationId,
      first_seq: 0,
    });
  }),

  http.post('/api/v1/chat/ack', () => {
    // Client acknowledges receipt of a turn to suppress the disconnect push.
    return HttpResponse.json({ ok: true });
  }),

  http.get('/api/v1/chat/conversations/:conversationId/stream', ({ params }) => {
    const conversationId = String(params.conversationId);
    const prompt = pendingTurnPrompts.get(conversationId) ?? '';
    const turnId = pendingTurnIds.get(conversationId) ?? 'mock-turn';
    const encoder = new TextEncoder();
    const stream = new ReadableStream({
      start(controller) {
        controller.enqueue(
          encoder.encode(
            `event: turn_started\ndata: ${JSON.stringify({ turn_id: turnId, seq: 0 })}\n\n`
          )
        );
        const response = getTestResponse(prompt);
        const words = response.split(' ');
        words.forEach((word, index) => {
          setTimeout(() => {
            controller.enqueue(
              encoder.encode(`event: text\ndata: ${JSON.stringify({ content: `${word} ` })}\n\n`)
            );
            if (index === words.length - 1) {
              setTimeout(() => {
                controller.enqueue(
                  encoder.encode(
                    `event: turn_ended\ndata: ${JSON.stringify({ turn_id: turnId, status: 'complete' })}\n\n`
                  )
                );
                controller.close();
              }, 50);
            }
          }, index * 100);
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

  // Mock automations endpoint
  http.get('/api/automations', ({ request }) => {
    const url = new URL(request.url);
    const conversationId = url.searchParams.get('conversation_id');

    const automations = [
      {
        id: '123',
        type: 'event',
        name: 'Test Automation Web',
        conversation_id: 'web',
        enabled: true,
        match_conditions: { entity_id: 'sensor.temperature' },
        condition_script: "return event.get('new_state', {}).get('state') == 'on'",
      },
      {
        id: '456',
        type: 'event',
        name: 'Test Automation Telegram',
        conversation_id: 'telegram',
        enabled: false,
        match_conditions: {},
        condition_script: null,
      },
    ];

    const filteredAutomations = conversationId
      ? automations.filter((a) => a.conversation_id === conversationId)
      : automations;

    return HttpResponse.json({
      automations: filteredAutomations,
    });
  }),

  // Mock automation detail endpoint
  http.get('/api/automations/:type/:id', ({ params }) => {
    const { type, id } = params;

    const automations = [
      {
        id: '123',
        type: 'event',
        name: 'Test Automation Web',
        conversation_id: 'web',
        enabled: true,
        match_conditions: { entity_id: 'sensor.temperature' },
        condition_script: "return event.get('new_state', {}).get('state') == 'on'",
      },
      {
        id: '456',
        type: 'event',
        name: 'Test Automation Telegram',
        conversation_id: 'telegram',
        enabled: false,
        match_conditions: {},
        condition_script: null,
      },
    ];

    const automation = automations.find((a) => a.id === id && a.type === type);

    if (automation) {
      return HttpResponse.json(automation);
    }

    return new HttpResponse(null, { status: 404 });
  }),

  // Mock automation patch endpoint
  http.patch('/api/automations/:type/:id', async ({ request, params }) => {
    const { type, id } = params;
    const body = await request.json();

    const automations = [
      {
        id: '123',
        type: 'event',
        name: 'Test Automation Web',
        conversation_id: 'web',
        enabled: true,
        match_conditions: { entity_id: 'sensor.temperature' },
        condition_script: "return event.get('new_state', {}).get('state') == 'on'",
      },
      {
        id: '456',
        type: 'event',
        name: 'Test Automation Telegram',
        conversation_id: 'telegram',
        enabled: false,
        match_conditions: {},
        condition_script: null,
      },
    ];

    const automation = automations.find((a) => a.id === id && a.type === type);

    if (automation) {
      const updatedAutomation = { ...automation, ...(body as Record<string, unknown>) };
      return HttpResponse.json(updatedAutomation);
    }

    return new HttpResponse(null, { status: 404 });
  }),

  // Mock automation delete endpoint
  http.delete('/api/automations/:type/:id', ({ params }) => {
    const { type, id } = params;

    const automations = [
      {
        id: '123',
        type: 'event',
        name: 'Test Automation Web',
        conversation_id: 'web',
        enabled: true,
        match_conditions: { entity_id: 'sensor.temperature' },
        condition_script: "return event.get('new_state', {}).get('state') == 'on'",
      },
      {
        id: '456',
        type: 'event',
        name: 'Test Automation Telegram',
        conversation_id: 'telegram',
        enabled: false,
        match_conditions: {},
        condition_script: null,
      },
    ];

    const automation = automations.find((a) => a.id === id && a.type === type);

    if (automation) {
      return new HttpResponse(null, { status: 204 });
    }

    return new HttpResponse(null, { status: 404 });
  }),

  // Mock frontend error reporting endpoint
  http.post('/api/errors/', async () => {
    return HttpResponse.json({
      status: 'reported',
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

// Helper function to create test responses for streaming. Emits the
// resumable-streaming wire format: turn_started, text events, turn_ended.
export function createStreamingResponse(
  chunks: string[],
  turnId: string = 'mock-turn'
): ReadableStream {
  const encoder = new TextEncoder();

  return new ReadableStream({
    start(controller) {
      controller.enqueue(
        encoder.encode(
          `event: turn_started\ndata: ${JSON.stringify({ turn_id: turnId, seq: 0 })}\n\n`
        )
      );
      chunks.forEach((chunk, index) => {
        // Add delay between chunks to simulate streaming
        setTimeout(() => {
          controller.enqueue(
            encoder.encode(`event: text\ndata: ${JSON.stringify({ content: chunk })}\n\n`)
          );

          // Close stream after last chunk
          if (index === chunks.length - 1) {
            controller.enqueue(
              encoder.encode(
                `event: turn_ended\ndata: ${JSON.stringify({ turn_id: turnId, status: 'complete' })}\n\n`
              )
            );
            controller.close();
          }
        }, index * 100);
      });
    },
  });
}

const STREAM_HEADERS = {
  'Content-Type': 'text/event-stream',
  'Cache-Control': 'no-cache',
  Connection: 'keep-alive',
};

// Export test-specific handlers that can be used in individual tests.
// With the resumable-streaming split, the SSE body is served by the GET
// stream endpoint, so overrides target that route (a default POST /turns
// handler in `handlers` supplies the kickoff identity).
export const testHandlers = {
  // Handler for streaming chat responses
  streamingChat: (responseChunks: string[]) =>
    http.get('/api/v1/chat/conversations/:conversationId/stream', ({ params }) => {
      const turnId = pendingTurnIds.get(String(params.conversationId)) ?? 'mock-turn';
      const stream = createStreamingResponse(responseChunks, turnId);
      return new HttpResponse(stream, { headers: STREAM_HEADERS });
    }),

  // Handler for tool calls in responses
  toolCallResponse: (toolName: string, args: Record<string, unknown>) =>
    http.get('/api/v1/chat/conversations/:conversationId/stream', ({ params }) => {
      const turnId = pendingTurnIds.get(String(params.conversationId)) ?? 'mock-turn';
      const encoder = new TextEncoder();
      const stream = new ReadableStream({
        start(controller) {
          controller.enqueue(
            encoder.encode(
              `event: turn_started\ndata: ${JSON.stringify({ turn_id: turnId, seq: 0 })}\n\n`
            )
          );
          // Send a single tool call, matching the turn producer's `tool_call`
          // event shape (payload.tool_call.function.name / .arguments).
          controller.enqueue(
            encoder.encode(
              `event: tool_call\ndata: ${JSON.stringify({
                tool_call: {
                  id: 'tool-call-1',
                  type: 'function',
                  function: {
                    name: toolName,
                    arguments: JSON.stringify(args),
                  },
                },
              })}\n\n`
            )
          );

          // Then end the turn
          setTimeout(() => {
            controller.enqueue(
              encoder.encode(
                `event: turn_ended\ndata: ${JSON.stringify({ turn_id: turnId, status: 'complete' })}\n\n`
              )
            );
            controller.close();
          }, 50);
        },
      });

      return new HttpResponse(stream, { headers: STREAM_HEADERS });
    }),
};
