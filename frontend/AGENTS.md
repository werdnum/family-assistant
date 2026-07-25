# Frontend Development Guide

Guidance for the frontend codebase: a React single-page app built with Vite that talks to the
FastAPI backend over REST.

Stack: React 19 with TypeScript, Vite, Tailwind CSS, shadcn/ui, `@assistant-ui/react` for the chat
interface, Vitest for testing, MSW (Mock Service Worker) for API mocking in tests.

## Commands

```bash
poe frontend-lint     # or npm run lint --prefix frontend      (eslint)
poe frontend-format   # or npm run format --prefix frontend    (biome format --write)
poe frontend-check    # or npm run check --prefix frontend     (biome + eslint, no fixes)

npm test --prefix frontend        # vitest
npm run build --prefix frontend
```

## Testing

Tests run under Vitest with jsdom. `vitest.config.js` registers `src/test/setup.js` as its
`setupFiles`, so every test gets the shared MSW server started, reset after each test, and closed at
the end.

Do not hand-mock `fetch`. Add handlers to `src/test/mocks/handlers.ts` and override them per test
with `server.use(...)`, importing the shared instance rather than constructing a new `setupServer()`
(otherwise the automatic per-test reset does not apply):

```typescript
import { server } from '../../test/setup.js';
```

If a static import causes build issues, import dynamically inside the test instead:

```typescript
const { server } = await import('../../test/setup.js');
const { http, HttpResponse } = await import('msw');
```

Chat tests render via `renderChatApp()` from `src/test/utils/renderChatApp.tsx`. Prefer `waitFor()`
over fixed timeouts when waiting for post-request DOM updates.

If a request is not being intercepted, check that the handler URL and HTTP method match the call
exactly.

## Architecture Notes

### Chat System

The chat system is built on `@assistant-ui/react`, which supplies message threading and state
management, streaming response handling, tool call visualization, and attachment support. Key
components: `ChatApp.tsx`, `ConversationSidebar.tsx`, and `useStreamingResponse.js` for the
streaming API integration.

### Routing

Client-side routing with separate entry points: `chat.html` for the chat interface, `router.html`
for the main application router, plus feature-specific pages in `src/pages/`.

### Error Reporting

`src/api/errorClient.ts` POSTs to `POST /api/errors/`. The optional `severity` field selects which
lane the report lands in server-side: omitting it (what the web frontend does) persists a genuine
error, while `"info"`/`"warning"`/`"debug"` route to an in-memory telemetry ring buffer for
high-frequency breadcrumbs. See
[src/family_assistant/web/CLAUDE.md](../src/family_assistant/web/CLAUDE.md) before changing what a
client sends. The errors viewer UI is `src/errors/`.

### Push Notifications

Web Push support, so notifications arrive even when the app is closed:

- `src/sw.js` - custom service worker handling push events and displaying notifications; Vite is
  configured with the `injectManifest` strategy for it
- `src/api/pushClient.ts` - push API client
- `src/chat/PushNotificationButton.tsx` - subscribe/unsubscribe UI, rendered only when a VAPID
  public key is available

Endpoints: `GET /api/client_config` (returns the VAPID public key), `POST /api/push/subscribe`, and
`POST /api/push/unsubscribe`. Tests live in `src/chat/__tests__/PushNotificationButton.test.tsx` and
`src/api/__tests__/pushClient.test.ts`.
