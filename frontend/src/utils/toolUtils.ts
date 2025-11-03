/**
 * Utility functions for handling tool calls and tool arguments
 */

/**
 * Parse tool arguments from string or return as-is if already an object.
 * Handles both JSON strings and raw objects.
 *
 * @param args - The tool arguments to parse (can be string or object)
 * @returns The parsed arguments object, or the raw string if parsing fails
 */
export const parseToolArguments = (args: unknown): unknown => {
  if (typeof args === 'string') {
    try {
      return JSON.parse(args);
    } catch (e) {
      console.error('Failed to parse tool arguments:', e, 'Raw args:', args);
      // Return the raw string for display - the viewer will handle it
      return args;
    }
  }
  return args;
};
