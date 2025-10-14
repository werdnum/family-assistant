import '@testing-library/jest-dom';
import { setupServer } from 'msw/node';
import { handlers } from './mocks/handlers';
import { afterAll, afterEach, beforeAll } from 'vitest';

// Set up MSW server
export const server = setupServer(...handlers);

// Start server before all tests
beforeAll(() => {
  server.listen({
    onUnhandledRequest: 'warn', // Log unhandled requests during development
  });
});

// Reset handlers after each test
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
  value: (query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addListener: () => {},
    removeListener: () => {},
    addEventListener: () => {},
    removeEventListener: () => {},
    dispatchEvent: (_event: Event) => true,
  }),
});

// Mock ResizeObserver for @assistant-ui/react
global.ResizeObserver = class ResizeObserver {
  callback: ResizeObserverCallback;

  constructor(callback: ResizeObserverCallback) {
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
global.URL.createObjectURL = (blob: Blob) => {
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
console.error = (...args: any[]) => {
  const errorMessage = args.join(' ');

  // Suppress known third-party library cleanup issues
  if (
    errorMessage.includes('MessageRepository') ||
    errorMessage.includes('This is likely an internal bug') ||
    errorMessage.includes('Warning: An update to') ||
    errorMessage.includes('not wrapped in act(')
  ) {
    return; // Don't log these expected errors
  }

  originalError.apply(console, args);
};
