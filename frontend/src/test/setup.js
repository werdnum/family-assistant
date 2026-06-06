import '@testing-library/jest-dom';
import './mocks/localStorageMock';
import { afterEach, afterAll, beforeAll } from 'vitest';
import { setupServer } from 'msw/node';
import { handlers } from './mocks/handlers';

// Set up MSW server
export const server = setupServer(...handlers);

let originalFetch;

// Start server before all tests
beforeAll(() => {
  server.listen({
    onUnhandledRequest: 'warn', // Log unhandled requests during development
  });

  originalFetch = globalThis.fetch;
  globalThis.fetch = (input, init) => {
    if (init && 'signal' in init) {
      const sanitizedInit = { ...init };
      delete sanitizedInit.signal;
      return originalFetch(input, sanitizedInit);
    }

    return originalFetch(input, init);
  };
});

// Reset handlers after each test (RTL auto-cleanup handles component unmount)
afterEach(() => {
  server.resetHandlers();
});

// Stop server after all tests
afterAll(() => {
  globalThis.fetch = originalFetch;
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
