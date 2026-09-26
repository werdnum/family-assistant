import { HttpResponse, http } from 'msw';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { server } from '../../test/setup.js';
import {
  reportError,
  reportErrorFromException,
  forceFlush,
  _resetForTesting,
  type FrontendErrorReport,
} from '../errorClient';

function recordDeliveredReports(): FrontendErrorReport[] {
  const delivered: FrontendErrorReport[] = [];
  server.use(
    http.post('/api/errors/', async ({ request }) => {
      delivered.push((await request.json()) as FrontendErrorReport);
      return HttpResponse.json({ status: 'reported' });
    })
  );
  return delivered;
}

describe('errorClient', () => {
  beforeEach(() => {
    _resetForTesting();
  });

  afterEach(() => {
    _resetForTesting();
    vi.useRealTimers();
  });

  describe('reportError', () => {
    it('should deduplicate identical errors within the window', async () => {
      const delivered = recordDeliveredReports();
      const report: FrontendErrorReport = {
        message: 'Duplicate error',
        url: 'http://localhost:3000/',
        error_type: 'uncaught',
      };

      reportError(report);
      reportError(report);
      reportError(report);
      await forceFlush();

      expect(delivered.map((r) => r.message)).toEqual(['Duplicate error']);
    });

    it('should not deduplicate different errors', async () => {
      const delivered = recordDeliveredReports();

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
      await forceFlush();

      expect(delivered.map((r) => r.message).sort()).toEqual(['Error 1', 'Error 2']);
    });

    it('should not deduplicate same message with different URLs', async () => {
      const delivered = recordDeliveredReports();

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
      await forceFlush();

      expect(delivered.map((r) => r.url).sort()).toEqual([
        'http://localhost:3000/page1',
        'http://localhost:3000/page2',
      ]);
    });
  });

  describe('reportErrorFromException', () => {
    it('should create a report from an Error object', async () => {
      const delivered = recordDeliveredReports();
      const error = new Error('Test exception');

      reportErrorFromException(error, 'component_error', 'TestComponent');
      await forceFlush();

      expect(delivered).toHaveLength(1);
      expect(delivered[0]).toMatchObject({
        message: 'Test exception',
        stack: error.stack,
        url: window.location.href,
        user_agent: navigator.userAgent,
        component_name: 'TestComponent',
        error_type: 'component_error',
      });
      expect(delivered[0].stack).toContain('Test exception');
    });

    it('should include extra data when provided', async () => {
      const delivered = recordDeliveredReports();
      const error = new Error('Error with extra data');

      reportErrorFromException(error, 'manual', undefined, {
        customField: 'customValue',
      });
      await forceFlush();

      expect(delivered).toHaveLength(1);
      expect(delivered[0]).toMatchObject({
        message: 'Error with extra data',
        error_type: 'manual',
        extra_data: { customField: 'customValue' },
      });
    });

    it('should default to manual error type', async () => {
      const delivered = recordDeliveredReports();
      const error = new Error('Default type error');

      reportErrorFromException(error);
      await forceFlush();

      expect(delivered).toHaveLength(1);
      expect(delivered[0]).toMatchObject({
        message: 'Default type error',
        error_type: 'manual',
      });
    });
  });

  describe('forceFlush', () => {
    it('should flush queued errors to the backend', async () => {
      const delivered = recordDeliveredReports();

      reportError({
        message: 'Error to flush',
        url: 'http://localhost:3000/',
        error_type: 'manual',
      });

      await forceFlush();
      expect(delivered.map((r) => r.message)).toEqual(['Error to flush']);

      await forceFlush();
      expect(delivered).toHaveLength(1);
    });

    it('should handle network errors silently', async () => {
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

      await expect(forceFlush()).resolves.toBeUndefined();
    });

    it('should flush multiple errors', async () => {
      const delivered = recordDeliveredReports();

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

      expect(delivered.map((r) => r.message).sort()).toEqual(['Error 1', 'Error 2']);
    });
  });

  describe('automatic flush', () => {
    it('should deliver a reported error once the flush interval elapses', async () => {
      vi.useFakeTimers();
      const delivered = recordDeliveredReports();

      reportError({
        message: 'Scheduled error',
        url: 'http://localhost:3000/',
        error_type: 'manual',
      });

      await vi.advanceTimersByTimeAsync(4999);
      expect(delivered).toHaveLength(0);

      await vi.advanceTimersByTimeAsync(1);
      await vi.waitFor(() => expect(delivered).toHaveLength(1));
      expect(delivered[0]).toMatchObject({ message: 'Scheduled error' });

      await forceFlush();
      expect(delivered).toHaveLength(1);
    });
  });

  describe('error report structure', () => {
    it('should send correctly structured error report', async () => {
      const delivered = recordDeliveredReports();

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

      expect(delivered).toEqual([report]);
    });
  });
});
