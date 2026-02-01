import { HttpResponse, http } from 'msw';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { server } from '../../test/setup.js';
import {
  reportError,
  reportErrorFromException,
  forceFlush,
  _resetForTesting,
  _getQueueLength,
  type FrontendErrorReport,
} from '../errorClient';

describe('errorClient', () => {
  beforeEach(() => {
    _resetForTesting();
    vi.useFakeTimers();
  });

  afterEach(() => {
    _resetForTesting();
    vi.useRealTimers();
  });

  describe('reportError', () => {
    it('should queue an error for reporting', () => {
      const report: FrontendErrorReport = {
        message: 'Test error',
        url: 'http://localhost:3000/',
        error_type: 'manual',
      };

      reportError(report);

      expect(_getQueueLength()).toBe(1);
    });

    it('should deduplicate identical errors within the window', () => {
      const report: FrontendErrorReport = {
        message: 'Duplicate error',
        url: 'http://localhost:3000/',
        error_type: 'uncaught',
      };

      reportError(report);
      reportError(report);
      reportError(report);

      expect(_getQueueLength()).toBe(1);
    });

    it('should not deduplicate different errors', () => {
      reportError({
        message: 'Error 1',
        url: 'http://localhost:3000/',
        error_type: 'uncaught',
      });

      reportError({
        message: 'Error 2',
        url: 'http://localhost:3000/',
        error_type: 'uncaught',
      });

      expect(_getQueueLength()).toBe(2);
    });

    it('should not deduplicate same message with different URLs', () => {
      reportError({
        message: 'Same error',
        url: 'http://localhost:3000/page1',
        error_type: 'uncaught',
      });

      reportError({
        message: 'Same error',
        url: 'http://localhost:3000/page2',
        error_type: 'uncaught',
      });

      expect(_getQueueLength()).toBe(2);
    });
  });

  describe('reportErrorFromException', () => {
    it('should create a report from an Error object', () => {
      const error = new Error('Test exception');

      reportErrorFromException(error, 'component_error', 'TestComponent');

      expect(_getQueueLength()).toBe(1);
    });

    it('should include extra data when provided', () => {
      const error = new Error('Error with extra data');

      reportErrorFromException(error, 'manual', undefined, {
        customField: 'customValue',
      });

      expect(_getQueueLength()).toBe(1);
    });

    it('should default to manual error type', () => {
      const error = new Error('Default type error');

      reportErrorFromException(error);

      expect(_getQueueLength()).toBe(1);
    });
  });

  describe('forceFlush', () => {
    it('should flush queued errors to the backend', async () => {
      vi.useRealTimers();

      let requestCount = 0;
      server.use(
        http.post('/api/errors/', () => {
          requestCount++;
          return HttpResponse.json({ status: 'reported' });
        })
      );

      reportError({
        message: 'Error to flush',
        url: 'http://localhost:3000/',
        error_type: 'manual',
      });

      await forceFlush();

      expect(requestCount).toBe(1);
      expect(_getQueueLength()).toBe(0);
    });

    it('should handle network errors silently', async () => {
      vi.useRealTimers();

      server.use(
        http.post('/api/errors/', () => {
          return HttpResponse.error();
        })
      );

      reportError({
        message: 'Error that will fail',
        url: 'http://localhost:3000/',
        error_type: 'manual',
      });

      // Should not throw
      await expect(forceFlush()).resolves.toBeUndefined();
    });

    it('should flush multiple errors', async () => {
      vi.useRealTimers();

      const receivedMessages: string[] = [];
      server.use(
        http.post('/api/errors/', async ({ request }) => {
          const body = (await request.json()) as { message: string };
          receivedMessages.push(body.message);
          return HttpResponse.json({ status: 'reported' });
        })
      );

      reportError({
        message: 'Error 1',
        url: 'http://localhost:3000/',
        error_type: 'manual',
      });
      reportError({
        message: 'Error 2',
        url: 'http://localhost:3000/',
        error_type: 'manual',
      });

      await forceFlush();

      expect(receivedMessages).toContain('Error 1');
      expect(receivedMessages).toContain('Error 2');
    });
  });

  describe('automatic flush', () => {
    it('should schedule a flush after adding an error', () => {
      reportError({
        message: 'Scheduled error',
        url: 'http://localhost:3000/',
        error_type: 'manual',
      });

      // The flush should be scheduled but not yet executed
      expect(_getQueueLength()).toBe(1);
    });
  });

  describe('error report structure', () => {
    it('should send correctly structured error report', async () => {
      vi.useRealTimers();

      let receivedBody: FrontendErrorReport | null = null;
      server.use(
        http.post('/api/errors/', async ({ request }) => {
          receivedBody = (await request.json()) as FrontendErrorReport;
          return HttpResponse.json({ status: 'reported' });
        })
      );

      const report: FrontendErrorReport = {
        message: 'Structured error',
        stack: 'Error: Structured error\n    at test.ts:1:1',
        url: 'http://localhost:3000/test',
        user_agent: 'Test Agent',
        component_name: 'TestComponent',
        error_type: 'component_error',
        extra_data: { testKey: 'testValue' },
      };

      reportError(report);
      await forceFlush();

      expect(receivedBody).toEqual(report);
    });
  });
});
