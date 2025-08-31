# Chat UI Testing Strategy - Revised Approach

## Executive Summary

After attempting to mock the `@assistant-ui/react` library, we've discovered it's not feasible due
to its tight internal coupling. Instead, we'll adopt a strategy similar to how we test React itself
\- use the real library with proper test harnesses and mock only at the network boundary.

## Core Principles

1. **Use Real Components**: Run actual `@assistant-ui/react` components with test runtimes
2. **Mock at Network Boundary**: Intercept API calls, not library internals
3. **Fast & Deterministic**: Tests run quickly with predictable results
4. **Realistic Environment**: Components behave exactly as in production
5. **Comprehensive Coverage**: Cover same scenarios as Playwright tests but faster

## Architecture

### Layer 1: Test Runtime Setup

Create a real assistant-ui runtime for testing that connects to mocked backend:

```typescript
// frontend/src/test/utils/testRuntime.tsx
import { useExternalStoreRuntime } from '@assistant-ui/react';
import { createMockAdapter } from './mockAdapter';

export function createTestRuntime(options = {}) {
  const mockAdapter = createMockAdapter(options);
  
  return useExternalStoreRuntime({
    adapters: {
      ...mockAdapter,
      speech: null, // Disable speech in tests
    },
    onError: (error) => {
      // Suppress known MessageRepository errors
      if (!error.message?.includes('MessageRepository')) {
        console.error(error);
      }
    },
  });
}
```

### Layer 2: Mock Backend Adapter

Create adapter that simulates backend responses:

```typescript
// frontend/src/test/utils/mockAdapter.tsx
export function createMockAdapter(options = {}) {
  const { initialMessages = [], responses = {} } = options;
  
  return {
    async *run({ messages, tools }) {
      // Simulate streaming response
      const userMessage = messages[messages.length - 1];
      const response = responses[userMessage.content] || 'Default test response';
      
      // Yield chunks to simulate streaming
      for (const chunk of response.split(' ')) {
        yield { content: chunk + ' ' };
        await new Promise(resolve => setTimeout(resolve, 10));
      }
    },
    
    // Mock tool execution
    async executeTool({ toolName, args }) {
      if (toolName === 'test_tool') {
        return { success: true, result: 'Tool executed' };
      }
      throw new Error(`Unknown tool: ${toolName}`);
    },
  };
}
```

### Layer 3: Network Mocking with MSW

Use Mock Service Worker for API mocking:

```typescript
// frontend/src/test/mocks/handlers.ts
import { http, HttpResponse } from 'msw';

export const handlers = [
  // Mock conversations endpoint
  http.get('/api/conversations', () => {
    return HttpResponse.json({
      conversations: [
        { id: '1', title: 'Test Conversation', created_at: new Date().toISOString() }
      ]
    });
  }),
  
  // Mock SSE streaming endpoint
  http.post('/api/send_message_stream', async ({ request }) => {
    const body = await request.json();
    
    // Return SSE stream
    const encoder = new TextEncoder();
    const stream = new ReadableStream({
      start(controller) {
        controller.enqueue(encoder.encode('data: {"content": "Test "}\n\n'));
        controller.enqueue(encoder.encode('data: {"content": "response"}\n\n'));
        controller.enqueue(encoder.encode('data: {"done": true}\n\n'));
        controller.close();
      }
    });
    
    return new Response(stream, {
      headers: {
        'Content-Type': 'text/event-stream',
        'Cache-Control': 'no-cache',
      },
    });
  }),
  
  // Mock profile endpoints
  http.get('/api/profiles', () => {
    return HttpResponse.json({
      profiles: ['default', 'browser', 'email_assistant']
    });
  }),
];
```

### Layer 4: Test Utilities

Render helper with all necessary providers:

```typescript
// frontend/src/test/utils/renderWithProviders.tsx
import { render } from '@testing-library/react';
import { AssistantRuntimeProvider } from '@assistant-ui/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

export function renderWithChat(component, options = {}) {
  const {
    runtime = createTestRuntime(),
    initialMessages = [],
    responses = {},
  } = options;
  
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  });
  
  return render(
    <QueryClientProvider client={queryClient}>
      <AssistantRuntimeProvider runtime={runtime}>
        {component}
      </AssistantRuntimeProvider>
    </QueryClientProvider>
  );
}
```

## Test Implementation Examples

### Example 1: Basic Message Send Test

```typescript
// frontend/src/chat/__tests__/ChatApp.test.tsx
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { renderWithChat } from '../../test/utils/renderWithProviders';
import { ChatApp } from '../ChatApp';

describe('ChatApp', () => {
  it('sends and receives messages', async () => {
    const user = userEvent.setup();
    
    renderWithChat(<ChatApp />, {
      responses: {
        'Hello': 'Hi there! How can I help you?'
      }
    });
    
    // Type message
    const input = await screen.findByRole('textbox', { name: /message/i });
    await user.type(input, 'Hello');
    
    // Send message
    const sendButton = screen.getByRole('button', { name: /send/i });
    await user.click(sendButton);
    
    // Verify user message appears
    expect(screen.getByText('Hello')).toBeInTheDocument();
    
    // Verify assistant response appears
    await waitFor(() => {
      expect(screen.getByText('Hi there! How can I help you?')).toBeInTheDocument();
    });
  });
});
```

### Example 2: Tool Confirmation Test

```typescript
describe('Tool Confirmation', () => {
  it('shows confirmation dialog for tools requiring confirmation', async () => {
    const user = userEvent.setup();
    
    renderWithChat(<ChatApp />, {
      mockTools: {
        'delete_note': { requiresConfirmation: true }
      }
    });
    
    // Trigger tool that needs confirmation
    const input = await screen.findByRole('textbox');
    await user.type(input, 'Delete my meeting notes');
    await user.click(screen.getByRole('button', { name: /send/i }));
    
    // Verify confirmation dialog appears
    await waitFor(() => {
      expect(screen.getByText(/confirm deletion/i)).toBeInTheDocument();
    });
    
    // Approve confirmation
    await user.click(screen.getByRole('button', { name: /approve/i }));
    
    // Verify tool executes
    await waitFor(() => {
      expect(screen.getByText(/note deleted/i)).toBeInTheDocument();
    });
  });
});
```

### Example 3: Conversation Switching Test

```typescript
describe('Conversation Management', () => {
  it('switches between conversations', async () => {
    const user = userEvent.setup();
    
    renderWithChat(<ChatApp />, {
      initialConversations: [
        { id: '1', title: 'Morning Chat', messages: ['Good morning!'] },
        { id: '2', title: 'Evening Chat', messages: ['Good evening!'] }
      ]
    });
    
    // Start with first conversation
    expect(screen.getByText('Good morning!')).toBeInTheDocument();
    
    // Open sidebar and switch conversation
    await user.click(screen.getByRole('button', { name: /menu/i }));
    await user.click(screen.getByText('Evening Chat'));
    
    // Verify switched to second conversation
    expect(screen.queryByText('Good morning!')).not.toBeInTheDocument();
    expect(screen.getByText('Good evening!')).toBeInTheDocument();
  });
});
```

## Migration Path

### Phase 1: Setup Infrastructure (Immediate)

1. Install MSW: `npm install --save-dev msw`
2. Create test utilities in `frontend/src/test/utils/`
3. Set up MSW handlers for all API endpoints
4. Create renderWithChat helper

### Phase 2: Write New Tests (Day 1-2)

1. Start with ChatApp component tests
2. Add tests for ToolWithConfirmation
3. Test ConversationSidebar interactions
4. Test DynamicToolUI rendering

### Phase 3: Verify Coverage (Day 2-3)

1. Ensure all Playwright scenarios are covered
2. Add edge cases not covered by e2e tests
3. Test error states and loading states
4. Test keyboard interactions and accessibility

### Phase 4: Optimize & Clean Up (Day 3)

1. Remove old broken test attempts
2. Consolidate shared test utilities
3. Document testing patterns for future developers
4. Ensure CI runs smoothly

## Benefits of This Approach

1. **Real Component Behavior**: Tests use actual @assistant-ui/react components
2. **Fast Execution**: No browser overhead, runs in jsdom
3. **Deterministic**: Mocked network responses ensure consistency
4. **Maintainable**: Tests follow production code patterns
5. **Comprehensive**: Can test all UI interactions and states
6. **Debuggable**: Standard React Testing Library debugging tools work

## Known Limitations

1. **MessageRepository Warnings**: Will still appear but can be suppressed in test setup
2. **WebSocket/SSE Complexity**: Requires careful mocking of streaming responses
3. **Initial Setup Cost**: More upfront work than simple mocks
4. **Learning Curve**: Developers need to understand the test runtime setup

## Success Criteria

- All ChatApp functionality tested at unit level
- Tests run in \<10 seconds
- No flaky tests
- 90%+ code coverage for UI components
- CI passes consistently
- Developers can easily add new tests

## Comparison with Current Approach

| Aspect        | Current (Broken)         | New Approach                    |
| ------------- | ------------------------ | ------------------------------- |
| Mock Strategy | Mock @assistant-ui/react | Mock network calls only         |
| Runtime       | Fake/undefined           | Real assistant-ui runtime       |
| Test Speed    | Would be fast if working | Fast (\<10s)                    |
| Reliability   | Broken - context errors  | Stable                          |
| Maintenance   | High - fighting library  | Low - using library as intended |
| Coverage      | 0% (tests don't run)     | 90%+ achievable                 |

## Implementation Priority

1. **Critical Path** (Must Have):

   - MSW setup for API mocking
   - Test runtime creation
   - Basic ChatApp tests

2. **Important** (Should Have):

   - Tool confirmation tests
   - Conversation management tests
   - Error handling tests

3. **Nice to Have** (Could Have):

   - Keyboard shortcut tests
   - Accessibility tests
   - Performance tests

## Next Steps

1. Delete current broken ChatApp.test.tsx
2. Install MSW and set up handlers
3. Create test utilities following this design
4. Write new ChatApp tests from scratch
5. Verify with `poe test`

This approach aligns with modern testing best practices - we treat @assistant-ui/react like any
other UI library and test WITH it, not against it. Just as we don't mock React itself, we shouldn't
mock assistant-ui's core functionality.
