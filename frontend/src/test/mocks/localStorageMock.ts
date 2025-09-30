import { vi } from 'vitest';

/**
 * Shared localStorage mock to ensure test isolation.
 *
 * All test files should import and use this single mock instance
 * instead of creating their own. This prevents test isolation issues
 * where different test files overwrite window.localStorage with
 * different mock objects.
 */
export const mockLocalStorage = {
  getItem: vi.fn(),
  setItem: vi.fn(),
  removeItem: vi.fn(),
  clear: vi.fn(),
  key: vi.fn(),
  length: 0,
};

// Set up the mock on window.localStorage once
Object.defineProperty(window, 'localStorage', {
  value: mockLocalStorage,
  writable: true,
  configurable: true,
});

/**
 * Helper to reset the localStorage mock.
 * Call this in beforeEach() to ensure clean state between tests.
 */
export function resetLocalStorageMock() {
  mockLocalStorage.getItem.mockReturnValue(null);
  mockLocalStorage.setItem.mockClear();
  mockLocalStorage.removeItem.mockClear();
  mockLocalStorage.clear.mockClear();
  mockLocalStorage.key.mockClear();
  mockLocalStorage.length = 0;
}
