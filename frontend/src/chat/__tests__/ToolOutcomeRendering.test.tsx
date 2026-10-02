import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { DynamicToolUI } from '../DynamicToolUI';

const NOTE_PROPS = {
  type: 'tool-call' as const,
  toolCallId: 'call_note',
  toolName: 'add_or_update_note',
  args: { title: 'Groceries', content: 'Milk' },
  argsText: '{}',
};

function renderedOutcome(): string | null {
  return screen.getByTestId('tool-call').getAttribute('data-tool-outcome');
}

describe('tool outcome rendering', () => {
  it('never shows a success mark for a failed note call, and keeps the error', () => {
    const { container } = render(
      <DynamicToolUI
        {...NOTE_PROPS}
        status={{ type: 'complete' }}
        result="Error: Database temporarily unavailable"
        outcome="failed"
      />
    );

    expect(renderedOutcome()).toBe('failed');
    expect(container.querySelector('.tool-success')).toBeNull();
    expect(container.querySelector('.tool-error')).not.toBeNull();
    expect(screen.getByText('Error: Database temporarily unavailable')).toBeInTheDocument();
    expect(screen.getByTestId('tool-outcome-note')).toHaveTextContent('Failed');
  });

  it('forwards isError to the generic renderer', () => {
    const { container } = render(
      <DynamicToolUI
        {...NOTE_PROPS}
        toolName="some_unknown_tool"
        status={{ type: 'complete' }}
        result="it broke"
        isError
      />
    );

    expect(renderedOutcome()).toBe('failed');
    expect(container.querySelector('.tool-complete')).toBeNull();
    expect(container.querySelector('.tool-error')).not.toBeNull();
  });

  it('shows a success mark for a succeeded call', () => {
    const { container } = render(
      <DynamicToolUI
        {...NOTE_PROPS}
        status={{ type: 'complete' }}
        result="Note 'Groceries' saved."
        outcome="succeeded"
      />
    );

    expect(renderedOutcome()).toBe('succeeded');
    expect(container.querySelector('.tool-success')).not.toBeNull();
    expect(screen.queryByTestId('tool-outcome-note')).toBeNull();
  });

  it('says a declined call did not run', () => {
    const { container } = render(
      <DynamicToolUI
        {...NOTE_PROPS}
        status={{ type: 'complete' }}
        result="OK. Action cancelled by user for tool 'add_or_update_note'."
        outcome="rejected"
      />
    );

    expect(renderedOutcome()).toBe('rejected');
    expect(container.querySelector('.tool-success')).toBeNull();
    expect(screen.getByTestId('tool-outcome-note')).toHaveTextContent('Not run');
  });

  it('neither spins nor succeeds for a finished call with no result', () => {
    const { container } = render(<DynamicToolUI {...NOTE_PROPS} status={{ type: 'complete' }} />);

    expect(renderedOutcome()).toBe('unknown');
    expect(container.querySelector('.tool-success')).toBeNull();
    expect(container.querySelector('.animate-spin')).toBeNull();
    expect(screen.getByTestId('tool-outcome-note')).toHaveTextContent('No result recorded');
  });

  it('spins while the call is running', () => {
    const { container } = render(<DynamicToolUI {...NOTE_PROPS} status={{ type: 'running' }} />);

    expect(renderedOutcome()).toBe('running');
    expect(container.querySelector('.animate-spin')).not.toBeNull();
  });
});
