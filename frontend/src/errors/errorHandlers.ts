/**
 * Global error handlers for capturing uncaught JavaScript errors
 * and unhandled promise rejections.
 */

import {
  reportErrorFromException,
  reportError,
  type FrontendErrorReport,
} from '../api/errorClient';

let initialized = false;

/**
 * Initialize global error handlers.
 *
 * This function sets up:
 * - window.onerror for uncaught exceptions
 * - window.onunhandledrejection for unhandled promise rejections
 *
 * Call this once during application startup.
 */
export function initializeErrorHandlers(): void {
  if (initialized) {
    return;
  }
  initialized = true;

  // Handle uncaught exceptions
  window.onerror = (
    message: string | Event,
    source?: string,
    lineno?: number,
    colno?: number,
    error?: Error
  ): boolean => {
    // If we have an Error object, use it for better stack traces
    if (error instanceof Error) {
      reportErrorFromException(error, 'uncaught', undefined, {
        source,
        lineno,
        colno,
      });
    } else {
      // Fallback for cases where we only get a message
      const report: FrontendErrorReport = {
        message: typeof message === 'string' ? message : 'Unknown error',
        stack: source ? `at ${source}:${lineno}:${colno}` : null,
        url: window.location.href,
        user_agent: navigator.userAgent,
        error_type: 'uncaught',
        extra_data: {
          source,
          lineno,
          colno,
        },
      };
      reportError(report);
    }

    // Return false to allow the error to propagate to the console
    return false;
  };

  // Handle unhandled promise rejections
  window.onunhandledrejection = (event: PromiseRejectionEvent): void => {
    const reason = event.reason;

    if (reason instanceof Error) {
      reportErrorFromException(reason, 'promise_rejection');
    } else {
      // Handle non-Error rejections (strings, objects, etc.)
      const report: FrontendErrorReport = {
        message: typeof reason === 'string' ? reason : 'Unhandled promise rejection',
        stack: null,
        url: window.location.href,
        user_agent: navigator.userAgent,
        error_type: 'promise_rejection',
        extra_data: {
          reason: typeof reason === 'object' ? JSON.stringify(reason) : String(reason),
        },
      };
      reportError(report);
    }
  };
}

/**
 * Check if error handlers have been initialized.
 * Primarily for testing purposes.
 */
export function isInitialized(): boolean {
  return initialized;
}

/**
 * Reset initialization state (for testing purposes only)
 */
export function _resetForTesting(): void {
  initialized = false;
  window.onerror = null;
  window.onunhandledrejection = null;
}
