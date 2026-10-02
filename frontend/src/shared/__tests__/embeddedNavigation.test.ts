import { afterEach, describe, expect, it } from 'vitest';
import { isEmbeddedPage, pageHref } from '../embeddedNavigation';

const originalURL = window.location.href;
afterEach(() => window.history.replaceState(null, '', originalURL));

describe('embedded navigation', () => {
  it('keeps page links beneath the authenticated prefix', () => {
    window.history.replaceState(null, '', '/app/documents/');
    expect(isEmbeddedPage()).toBe(true);
    expect(pageHref('/history?conversation_id=123')).toBe('/app/history?conversation_id=123');
    expect(pageHref('/api/documents/1')).toBe('/api/documents/1');
    expect(pageHref('/app/tasks')).toBe('/app/tasks');
    expect(pageHref('https://example.com/')).toBe('https://example.com/');
    expect(pageHref('//example.com/')).toBe('//example.com/');
  });

  it('preserves ordinary website navigation', () => {
    window.history.replaceState(null, '', '/documents/');
    expect(isEmbeddedPage()).toBe(false);
    expect(pageHref('/tasks')).toBe('/tasks');
  });

  it('does not treat similarly named API routes as embedded pages', () => {
    window.history.replaceState(null, '', '/app-other');
    expect(isEmbeddedPage()).toBe(false);
  });
});
