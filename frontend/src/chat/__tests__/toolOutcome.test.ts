import { describe, expect, it } from 'vitest';
import { rendererStatusForOutcome, resolveToolOutcome } from '../toolOutcome';

describe('resolveToolOutcome', () => {
  it('is running while a live call has no result', () => {
    expect(resolveToolOutcome({ status: { type: 'running' } })).toBe('running');
  });

  it('is awaiting approval while a confirmation is pending, whatever the status', () => {
    expect(resolveToolOutcome({ status: { type: 'running' }, awaitingApproval: true })).toBe(
      'awaiting_approval'
    );
  });

  it('takes the backend outcome for a finished call', () => {
    expect(
      resolveToolOutcome({
        status: { type: 'complete' },
        result: 'Error: Database temporarily unavailable',
        outcome: 'failed',
      })
    ).toBe('failed');
    expect(
      resolveToolOutcome({
        status: { type: 'complete' },
        result: "OK. Action cancelled by user for tool 'delete_note'.",
        outcome: 'rejected',
      })
    ).toBe('rejected');
  });

  it('treats an explicit error flag as failure even with a result', () => {
    expect(resolveToolOutcome({ status: { type: 'complete' }, result: 'x', isError: true })).toBe(
      'failed'
    );
  });

  it('does not call a finished turn with no result running or succeeded', () => {
    expect(resolveToolOutcome({ status: { type: 'complete' } })).toBe('unknown');
    expect(resolveToolOutcome({ status: { type: 'incomplete', reason: 'cancelled' } })).toBe(
      'unknown'
    );
  });

  it('is running for a history row whose turn is still running', () => {
    expect(resolveToolOutcome({ status: { type: 'complete' }, awaitingResult: true })).toBe(
      'running'
    );
  });
});

describe('rendererStatusForOutcome', () => {
  it('gives renderers a complete status only for success', () => {
    expect(rendererStatusForOutcome('succeeded')).toEqual({ type: 'complete' });
    for (const outcome of ['failed', 'rejected', 'unknown', 'awaiting_approval'] as const) {
      expect(rendererStatusForOutcome(outcome).type).not.toBe('complete');
    }
    expect(rendererStatusForOutcome('failed')).toEqual({ type: 'incomplete', reason: 'error' });
  });
});
