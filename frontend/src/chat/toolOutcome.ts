/**
 * What a tool call shows the user: one outcome, derived the same way for a
 * live stream and for history.
 *
 * Only `succeeded` earns a success mark. A finished call's outcome comes from
 * the backend (`outcome` on the stream's tool_result event, `tool_outcome` on a
 * history row), which classifies the result once for every client; the client
 * adds the states only it can see: still running, waiting for approval, and a
 * call that has no result although its turn is over.
 */

/** A finished call's outcome, as the backend reports it. */
export type TerminalToolOutcome = 'succeeded' | 'failed' | 'rejected';

export type ToolOutcome = 'running' | 'awaiting_approval' | TerminalToolOutcome | 'unknown';

export interface ToolPartStatus {
  type: string;
  reason?: string;
}

export interface ToolOutcomeInput {
  /** The runtime's part status. */
  status?: ToolPartStatus;
  result?: unknown;
  outcome?: TerminalToolOutcome;
  isError?: boolean;
  /** A history row from a turn the server still reports as running. */
  awaitingResult?: boolean;
  awaitingApproval?: boolean;
}

export function isTerminalToolOutcome(value: unknown): value is TerminalToolOutcome {
  return value === 'succeeded' || value === 'failed' || value === 'rejected';
}

export function resolveToolOutcome({
  status,
  result,
  outcome,
  isError,
  awaitingResult,
  awaitingApproval,
}: ToolOutcomeInput): ToolOutcome {
  if (awaitingApproval) {
    return 'awaiting_approval';
  }
  if (isError || (status?.type === 'incomplete' && status.reason === 'error')) {
    return 'failed';
  }
  if (result !== undefined) {
    return outcome ?? 'succeeded';
  }
  if (status?.type === 'running' || awaitingResult) {
    return 'running';
  }
  // The turn is over and nothing answered this call: it was interrupted, or
  // its result was never recorded. Neither is success, and neither is running.
  return 'unknown';
}

/**
 * The part status a tool renderer is given. Renderers draw a success mark for
 * `complete`, so only a succeeded call is complete.
 */
export function rendererStatusForOutcome(outcome: ToolOutcome): ToolPartStatus {
  switch (outcome) {
    case 'running':
      return { type: 'running' };
    case 'awaiting_approval':
      return { type: 'requires-action', reason: 'tool-calls' };
    case 'succeeded':
      return { type: 'complete' };
    case 'failed':
      return { type: 'incomplete', reason: 'error' };
    case 'rejected':
      return { type: 'incomplete', reason: 'cancelled' };
    case 'unknown':
      return { type: 'incomplete', reason: 'other' };
  }
}

export const TOOL_OUTCOME_LABELS: Record<ToolOutcome, string> = {
  running: 'Running',
  awaiting_approval: 'Waiting for approval',
  succeeded: 'Succeeded',
  failed: 'Failed',
  rejected: 'Not run',
  unknown: 'No result recorded',
};
