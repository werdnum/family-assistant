/**
 * Utility for generating diagnostics export URLs.
 *
 * These URLs link to the diagnostics export API endpoint which provides
 * debugging information including error logs, LLM requests, and message history.
 */

interface DiagnosticsUrlOptions {
  /** Time window in minutes (default: 5) */
  minutes?: number;
  /** Conversation ID to filter by (optional) */
  conversationId?: string;
  /** Output format (default: 'markdown' for easy reading) */
  format?: 'json' | 'markdown';
}

/**
 * Generates a URL to the diagnostics export endpoint.
 *
 * The resulting URL can be used in error messages to provide users with
 * quick access to debugging information when errors occur.
 *
 * @param options - Configuration options for the diagnostics URL
 * @returns A URL string to the diagnostics export endpoint
 *
 * @example
 * // Basic usage - 5 minute window, markdown format
 * const url = getDiagnosticsUrl();
 * // => "/api/diagnostics/export?minutes=5&format=markdown"
 *
 * @example
 * // With conversation ID for filtering
 * const url = getDiagnosticsUrl({ conversationId: 'conv_123' });
 * // => "/api/diagnostics/export?minutes=5&conversation_id=conv_123&format=markdown"
 */
export function getDiagnosticsUrl(options: DiagnosticsUrlOptions = {}): string {
  const { minutes = 5, conversationId, format = 'markdown' } = options;

  const params = new URLSearchParams();
  params.set('minutes', String(minutes));

  if (conversationId) {
    params.set('conversation_id', conversationId);
  }

  params.set('format', format);

  return `/api/diagnostics/export?${params.toString()}`;
}
