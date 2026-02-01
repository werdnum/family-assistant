/**
 * Client for reporting frontend JavaScript errors to the backend.
 *
 * This module provides functions for capturing and reporting errors
 * with batching, deduplication, and rate limiting to prevent flooding.
 */

/**
 * Frontend error report structure matching the backend API
 */
export interface FrontendErrorReport {
  message: string;
  stack?: string | null;
  url: string;
  user_agent?: string | null;
  component_name?: string | null;
  error_type?: 'uncaught' | 'promise_rejection' | 'component_error' | 'manual' | null;
  extra_data?: Record<string, unknown> | null;
}

/**
 * Response from the error reporting endpoint
 */
export interface FrontendErrorReportResponse {
  status: string;
}

// Configuration constants
const FLUSH_INTERVAL_MS = 5000; // 5 seconds between flushes
const MAX_ERRORS_PER_BATCH = 10;
const DEDUP_WINDOW_MS = 60000; // 60 seconds deduplication window

// Internal state
let errorQueue: FrontendErrorReport[] = [];
let recentErrors: Map<string, number> = new Map(); // key -> timestamp
let flushTimeoutId: ReturnType<typeof setTimeout> | null = null;
let isInitialized = false;

/**
 * Generate a deduplication key for an error report
 */
function getDedupeKey(report: FrontendErrorReport): string {
  return `${report.message}|${report.url}|${report.error_type || 'unknown'}`;
}

/**
 * Check if an error should be deduplicated (already reported recently)
 */
function shouldDedupe(report: FrontendErrorReport): boolean {
  const key = getDedupeKey(report);
  const lastReported = recentErrors.get(key);

  if (lastReported && Date.now() - lastReported < DEDUP_WINDOW_MS) {
    return true;
  }

  // Clean up old entries while we're here
  const now = Date.now();
  for (const [k, timestamp] of recentErrors.entries()) {
    if (now - timestamp >= DEDUP_WINDOW_MS) {
      recentErrors.delete(k);
    }
  }

  return false;
}

/**
 * Mark an error as recently reported
 */
function markAsReported(report: FrontendErrorReport): void {
  const key = getDedupeKey(report);
  recentErrors.set(key, Date.now());
}

/**
 * Flush queued errors to the backend
 */
async function flushErrors(): Promise<void> {
  if (errorQueue.length === 0) {
    return;
  }

  // Take up to MAX_ERRORS_PER_BATCH errors from the queue
  const batch = errorQueue.splice(0, MAX_ERRORS_PER_BATCH);

  // Send each error individually (backend expects single error per request)
  for (const error of batch) {
    try {
      await fetch('/api/errors/', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify(error),
      });
    } catch {
      // Silent failure - we don't want error reporting to cause more errors
      // Errors in error reporting should not propagate or cause infinite loops
    }
  }
}

/**
 * Schedule a flush if not already scheduled
 */
function scheduleFlush(): void {
  if (flushTimeoutId === null) {
    flushTimeoutId = setTimeout(async () => {
      flushTimeoutId = null;
      await flushErrors();

      // If there are more errors queued, schedule another flush
      if (errorQueue.length > 0) {
        scheduleFlush();
      }
    }, FLUSH_INTERVAL_MS);
  }
}

/**
 * Report an error to the backend.
 *
 * Errors are queued and sent in batches to prevent flooding the backend.
 * Duplicate errors (same message, url, and type) within the deduplication
 * window are ignored.
 *
 * @param report - The error report to send
 */
export function reportError(report: FrontendErrorReport): void {
  // Initialize if needed
  if (!isInitialized) {
    isInitialized = true;
  }

  // Check for deduplication
  if (shouldDedupe(report)) {
    return;
  }

  // Mark as reported
  markAsReported(report);

  // Add to queue
  errorQueue.push(report);

  // Schedule flush
  scheduleFlush();
}

/**
 * Report an error from an Error object.
 *
 * This is a convenience helper that extracts relevant information
 * from an Error object and reports it.
 *
 * @param error - The Error object to report
 * @param errorType - The type of error (uncaught, promise_rejection, component_error, manual)
 * @param componentName - Optional component name where the error occurred
 * @param extraData - Optional additional data to include
 */
export function reportErrorFromException(
  error: Error,
  errorType: FrontendErrorReport['error_type'] = 'manual',
  componentName?: string,
  extraData?: Record<string, unknown>
): void {
  const report: FrontendErrorReport = {
    message: error.message || String(error),
    stack: error.stack || null,
    url: typeof window !== 'undefined' ? window.location.href : '',
    user_agent: typeof navigator !== 'undefined' ? navigator.userAgent : null,
    component_name: componentName || null,
    error_type: errorType,
    extra_data: extraData || null,
  };

  reportError(report);
}

/**
 * Force an immediate flush of all queued errors.
 *
 * This is primarily useful for testing or before page unload.
 */
export async function forceFlush(): Promise<void> {
  if (flushTimeoutId !== null) {
    clearTimeout(flushTimeoutId);
    flushTimeoutId = null;
  }
  await flushErrors();
}

/**
 * Clear all state (for testing purposes)
 */
export function _resetForTesting(): void {
  errorQueue = [];
  recentErrors = new Map();
  if (flushTimeoutId !== null) {
    clearTimeout(flushTimeoutId);
    flushTimeoutId = null;
  }
  isInitialized = false;
}

/**
 * Get the current queue length (for testing purposes)
 */
export function _getQueueLength(): number {
  return errorQueue.length;
}
