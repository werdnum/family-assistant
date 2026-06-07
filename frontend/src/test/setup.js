import '@testing-library/jest-dom';
import { setupServer } from 'msw/node';
import { afterAll, afterEach, beforeAll } from 'vitest';
import { handlers } from './mocks/handlers';

// Set up MSW server
export const server = setupServer(...handlers);

// Start server before all tests
beforeAll(() => {
  server.listen({
    onUnhandledRequest: 'warn', // Log unhandled requests during development
  });
});

// Reset handlers after each test (RTL auto-cleanup handles component unmount)
afterEach(() => {
  server.resetHandlers();
});

// Stop server after all tests
afterAll(() => {
  server.close();
});

// Mock window.matchMedia for tests
Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: (query) => ({
    matches: false,
    media: query,
    onchange: null,
    addListener: () => {},
    removeListener: () => {},
    addEventListener: () => {},
    removeEventListener: () => {},
    dispatchEvent: () => {},
  }),
});

// Mock ResizeObserver for @assistant-ui/react
global.ResizeObserver = class ResizeObserver {
  constructor(callback) {
    this.callback = callback;
  }

  observe() {
    // Mock implementation - do nothing
  }

  unobserve() {
    // Mock implementation - do nothing
  }

  disconnect() {
    // Mock implementation - do nothing
  }
};

// Mock URL.createObjectURL and URL.revokeObjectURL for file attachments
global.URL.createObjectURL = (blob) => {
  return `blob:${blob.type}/${Math.random().toString(36).substring(7)}`;
};

global.URL.revokeObjectURL = () => {
  // Mock implementation - do nothing
};

// Mock scrollTo for @assistant-ui/react's scroll behavior
if (typeof globalThis.Element !== 'undefined') {
  globalThis.Element.prototype.scrollTo =
    globalThis.Element.prototype.scrollTo ||
    function () {
      // Mock implementation - do nothing
    };
}

// Suppress known MessageRepository errors from @assistant-ui/react
const originalError = console.error;
console.error = (...args) => {
  const errorMessage = args.join(' ');

  // Suppress known third-party library cleanup issues
  if (
    errorMessage.includes('MessageRepository') ||
    errorMessage.includes('This is likely an internal bug') ||
    errorMessage.includes('Warning: An update to') ||
    errorMessage.includes('not wrapped in act(') ||
    errorMessage.includes('tapClientLookup')
  ) {
    return; // Don't log these expected errors
  }

  originalError.apply(console, args);
};

// Suppress known @assistant-ui/store tapClientLookup race condition errors.
// These occur during test cleanup when React's useSyncExternalStore tries to
// read message state while the tap reactive system has already cleared its
// resources during unmount. This is a known library issue (assistant-ui#3395).
window.addEventListener('error', (event) => {
  if (event.error?.message?.includes('tapClientLookup')) {
    event.preventDefault();
  }
});

// Swallow post-teardown jsdom races. When an async callback (a pending timer,
// fetch continuation, or store effect) touches `window`/`document` after vitest
// has torn down the test file's jsdom environment, Node throws a bare
// `ReferenceError: window is not defined` as an unhandled rejection. While a
// test is running these globals always exist (this file even defines
// `window.matchMedia`), so this error can only originate after teardown and is
// never a real assertion failure. Under the heavy CPU contention of the full
// `poe test` run these stray callbacks are far more likely to land after
// teardown, which intermittently fails the otherwise-passing suite.
//
// vitest's worker treats an unhandled rejection as user-handled when a second
// listener is present (it returns early in that case), so we re-surface genuine
// rejections as uncaught exceptions to keep them failing the run. The guard
// symbol ensures we register exactly one listener per worker process even
// though this setup module is re-evaluated for every test file.
const TEARDOWN_RACE_PATTERN =
  /\b(?:window|document|navigator|self|localStorage|sessionStorage|location)\b is not defined/;
const UNHANDLED_GUARD = Symbol.for('familyAssistant.test.unhandledRejectionGuard');
const nodeProcess = globalThis.process;
if (nodeProcess && !nodeProcess[UNHANDLED_GUARD]) {
  nodeProcess[UNHANDLED_GUARD] = true;
  nodeProcess.on('unhandledRejection', (reason) => {
    if (reason instanceof ReferenceError && TEARDOWN_RACE_PATTERN.test(reason.message)) {
      return;
    }
    // Not a teardown race: re-surface so the test run still fails.
    throw reason;
  });
}
