import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import React from 'react';
import { ToolWithConfirmation } from '../ToolWithConfirmation';
import { ToolConfirmationContext } from '../ToolConfirmationContext';

const DummyTool = ({ toolName }: { toolName: string }) => <div>{toolName}</div>;

describe('ToolWithConfirmation', () => {
  it('renders confirmation UI and handles approval and rejection', () => {
    const handleConfirmation = vi.fn();
    const pendingConfirmations = new Map([
      [
        'tc1',
        {
          request_id: 'req1',
          confirmation_prompt: 'Confirm?',
        },
      ],
    ]);

    render(
      <ToolConfirmationContext.Provider value={{ pendingConfirmations, handleConfirmation }}>
        <ToolWithConfirmation
          toolName="add_or_update_note"
          toolCallId="tc1"
          args={{ title: 'Test' }}
          ToolComponent={DummyTool}
        />
      </ToolConfirmationContext.Provider>
    );

    expect(screen.getByText('Confirm?')).toBeInTheDocument();
    const approve = screen.getByText('Approve');
    const reject = screen.getByText('Reject');

    fireEvent.click(approve);
    expect(handleConfirmation).toHaveBeenCalledWith('tc1', 'req1', true);

    fireEvent.click(reject);
    expect(handleConfirmation).toHaveBeenCalledWith('tc1', 'req1', false);
  });
});
