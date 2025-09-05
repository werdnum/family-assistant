# Frontend Development Guide

This file provides guidance for working with the frontend codebase, which is a modern React
application built with Vite.

## Architecture

The frontend is a React single-page application (SPA) that communicates with the FastAPI backend via
REST APIs. Key technologies:

- **React 18** with TypeScript
- **Vite** for build tooling and dev server
- **Tailwind CSS** for styling
- **shadcn/ui** for UI components
- **@assistant-ui/react** for chat interface
- **Vitest** for testing
- **MSW (Mock Service Worker)** for API mocking in tests

## Project Structure

```
frontend/
├── src/
│   ├── components/ui/        # Reusable UI components (shadcn/ui)
│   ├── shared/              # Shared components and utilities
│   ├── chat/                # Chat application components
│   ├── pages/               # Feature-specific page components
│   ├── test/                # Test utilities and mocks
│   │   ├── setup.js         # Test setup with MSW configuration
│   │   ├── mocks/           # MSW handlers and test data
│   │   └── utils/           # Test helper functions
│   └── styles/              # Global styles
├── public/                  # Static assets
└── dist/                    # Build output
```

## Development

### Setup

```bash
# Install dependencies
npm install --prefix frontend

# Start development server
poe dev  # Starts both backend and frontend with HMR
```

### Commands

```bash
# Linting and formatting
poe frontend-lint     # or npm run lint --prefix frontend
poe frontend-format   # or npm run format --prefix frontend
poe frontend-check    # or npm run check --prefix frontend

# Testing
npm test --prefix frontend

# Building
npm run build --prefix frontend
```

## Testing

The frontend uses **Vitest** for testing with **Mock Service Worker (MSW)** for API mocking.

### Test Setup

All tests automatically include MSW setup via `src/test/setup.js`:

```javascript
import { setupServer } from 'msw/node';
import { handlers } from './mocks/handlers';

// MSW server is automatically started before tests
export const server = setupServer(...handlers);
```

### API Mocking with MSW

**DO NOT** manually mock `fetch` calls. Instead, use MSW handlers defined in
`src/test/mocks/handlers.ts`.

#### Adding New API Handlers

```typescript
// src/test/mocks/handlers.ts
export const handlers = [
  http.get('/api/v1/my-endpoint', ({ request }) => {
    const url = new URL(request.url);
    const param = url.searchParams.get('param');
    
    return HttpResponse.json({
      data: `Response for ${param}`,
    });
  }),
];
```

#### Overriding Handlers in Tests

```typescript
import { server } from '../test/setup.js';
import { http, HttpResponse } from 'msw';

it('handles specific API behavior', async () => {
  // Override the default handler for this test
  server.use(
    http.get('/api/v1/my-endpoint', ({ request }) => {
      return HttpResponse.json({ error: 'Test error' }, { status: 500 });
    })
  );

  // Test your component behavior
  renderMyComponent();
  
  // Assertions...
});
```

#### Verifying API Calls

```typescript
it('makes correct API call', async () => {
  let requestUrl = '';
  
  server.use(
    http.get('/api/v1/chat/conversations', ({ request }) => {
      requestUrl = request.url;  // Capture the full URL
      return HttpResponse.json({ conversations: [] });
    })
  );

  renderChatApp();
  await waitFor(() => {
    expect(requestUrl).toContain('interface_type=web');
  });
});
```

### Testing Patterns

#### Component Testing

```typescript
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { renderMyComponent } from '../test/utils/renderMyComponent';

it('renders and handles user interaction', async () => {
  const user = userEvent.setup();
  renderMyComponent();

  // Test rendering
  expect(screen.getByText('My Component')).toBeInTheDocument();

  // Test interaction
  await user.click(screen.getByRole('button', { name: /submit/i }));
  
  await waitFor(() => {
    expect(screen.getByText('Success')).toBeInTheDocument();
  });
});
```

#### Chat App Testing

Use the provided `renderChatApp` utility:

```typescript
import { renderChatApp } from '../../test/utils/renderChatApp';

it('tests chat functionality', async () => {
  renderChatApp({ profileId: 'test-profile' });
  
  await new Promise(resolve => setTimeout(resolve, 2000)); // Allow initialization
  
  const messageInput = screen.getByPlaceholderText('Write a message...');
  // ... test logic
});
```

### Common Testing Utilities

#### Mock Data

Define reusable test data in `src/test/mocks/`:

```typescript
// src/test/mocks/testData.js
export const mockConversations = [
  {
    conversation_id: 'web_conv_test-1',
    last_message: 'Test message',
    last_timestamp: '2025-01-01T10:00:00Z',
    message_count: 1,
  },
];
```

#### Test Helpers

Common patterns are extracted into utilities in `src/test/utils/`:

- `renderChatApp()` - Renders the chat application with MSW setup
- Custom render functions for specific components
- Helper functions for common test scenarios

### MSW Best Practices

1. **Use the global server instance** - Don't create new MSW servers in tests
2. **Override handlers with `server.use()`** - This allows per-test customization
3. **Capture request data for verification** - Store URLs, headers, or body data in test variables
4. **Reset handlers after each test** - Automatically handled by
   `afterEach(() => server.resetHandlers())`
5. **Return realistic responses** - Match the actual API response structure

### Testing Guidelines

1. **Test user behavior, not implementation** - Focus on what users see and do
2. **Use realistic waiting strategies** - Prefer `waitFor()` over arbitrary timeouts
3. **Mock external dependencies only** - Don't mock your own components unnecessarily
4. **Test error states** - Override MSW handlers to return errors
5. **Keep tests independent** - Each test should work in isolation

## Common Issues

### MSW Not Working

If API calls aren't being intercepted:

1. Check that the handler URL exactly matches your API call
2. Verify the HTTP method matches (GET, POST, etc.)
3. Ensure `src/test/setup.js` is being loaded (check `vitest.config.js`)

### Import Errors

Use dynamic imports for MSW in tests to avoid build issues:

```typescript
const { server } = await import('../test/setup.js');
const { http, HttpResponse } = await import('msw');
```

### Async Test Issues

- Always wait for async operations to complete
- Use `waitFor()` for DOM updates after API calls
- Give sufficient time for component initialization (especially chat components)

## Architecture Notes

### Chat System

The chat system uses `@assistant-ui/react` which provides:

- Message threading and state management
- Streaming response handling
- Tool call visualization
- Attachment support

Key components:

- `ChatApp.tsx` - Main chat interface
- `ConversationSidebar.tsx` - Conversation history
- `useStreamingResponse.js` - Streaming API integration

### Routing

The app uses client-side routing with different entry points:

- `chat.html` - Chat interface
- `router.html` - Main application router
- Feature-specific pages in `src/pages/`

### API Integration

All API calls go through the backend at `/api/` endpoints. The frontend assumes:

- RESTful API design
- JSON request/response format
- Error responses with appropriate HTTP status codes
- Streaming support for chat responses

## Contributing

When adding new features:

1. **Add API mocks first** - Define MSW handlers for any new endpoints
2. **Write tests** - Cover both happy path and error scenarios
3. **Follow existing patterns** - Use established components and utilities
4. **Update documentation** - Add notes about new testing patterns or utilities

When modifying existing features:

1. **Update tests** - Ensure all tests still pass
2. **Update MSW handlers** - If API contracts change
3. **Check related components** - Frontend components are often interconnected
