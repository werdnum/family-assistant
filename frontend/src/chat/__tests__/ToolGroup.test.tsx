import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
import { ToolGroup } from '../ToolGroup';
import { ToolGroupShell } from '../ToolGroupShell';

describe('ToolGroup', () => {
  const mockChildren = (
    <div data-testid="tool-content">
      <div>Tool 1</div>
      <div>Tool 2</div>
    </div>
  );

  it('displays tool count correctly for single tool', () => {
    render(
      <ToolGroup startIndex={0} endIndex={0}>
        {mockChildren}
      </ToolGroup>
    );

    expect(screen.getByText('1 tool call')).toBeInTheDocument();
  });

  it('displays tool count correctly for multiple tools', () => {
    render(
      <ToolGroup startIndex={0} endIndex={2}>
        {mockChildren}
      </ToolGroup>
    );

    expect(screen.getByText('3 tool calls')).toBeInTheDocument();
  });

  it('expands when clicked', async () => {
    const user = userEvent.setup();
    render(
      <ToolGroup startIndex={0} endIndex={1}>
        {mockChildren}
      </ToolGroup>
    );

    const trigger = screen.getByTestId('tool-group-trigger');
    const content = screen.getByTestId('tool-group-content');

    expect(content).toHaveAttribute('data-state', 'closed');
    expect(trigger).toHaveAttribute('data-state', 'closed');
    expect(trigger).toHaveAttribute('aria-expanded', 'false');

    await user.click(trigger);

    expect(content).toHaveAttribute('data-state', 'open');
    expect(trigger).toHaveAttribute('data-state', 'open');
    expect(trigger).toHaveAttribute('aria-expanded', 'true');
  });

  it('collapses again when clicked while expanded', async () => {
    const user = userEvent.setup();
    render(
      <ToolGroup startIndex={0} endIndex={1}>
        {mockChildren}
      </ToolGroup>
    );

    const trigger = screen.getByTestId('tool-group-trigger');
    const content = screen.getByTestId('tool-group-content');

    // First expand
    await user.click(trigger);
    expect(content).toHaveAttribute('data-state', 'open');

    // Then collapse again
    await user.click(trigger);
    expect(content).toHaveAttribute('data-state', 'closed');
  });

  it('supports keyboard navigation with Enter key', async () => {
    const user = userEvent.setup();
    render(
      <ToolGroup startIndex={0} endIndex={1}>
        {mockChildren}
      </ToolGroup>
    );

    const trigger = screen.getByTestId('tool-group-trigger');
    const content = screen.getByTestId('tool-group-content');

    // Focus the trigger
    await user.tab();
    expect(trigger).toHaveFocus();

    // Initially collapsed
    expect(content).toHaveAttribute('data-state', 'closed');

    // Press Enter to expand
    await user.keyboard('{Enter}');
    expect(content).toHaveAttribute('data-state', 'open');
  });

  it('supports keyboard navigation with Space key', async () => {
    const user = userEvent.setup();
    render(
      <ToolGroup startIndex={0} endIndex={1}>
        {mockChildren}
      </ToolGroup>
    );

    const trigger = screen.getByTestId('tool-group-trigger');
    const content = screen.getByTestId('tool-group-content');

    // Focus the trigger
    await user.tab();
    expect(trigger).toHaveFocus();

    // Initially collapsed
    expect(content).toHaveAttribute('data-state', 'closed');

    // Press Space to expand
    await user.keyboard(' ');
    expect(content).toHaveAttribute('data-state', 'open');
  });

  it('renders children content when expanded', async () => {
    const user = userEvent.setup();
    render(
      <ToolGroup startIndex={0} endIndex={1}>
        {mockChildren}
      </ToolGroup>
    );

    const trigger = screen.getByTestId('tool-group-trigger');
    const content = screen.getByTestId('tool-group-content');

    // Initially collapsed - content should be unmounted
    expect(content).toHaveAttribute('hidden');
    expect(screen.queryByTestId('tool-content')).not.toBeInTheDocument();

    // Expand
    await user.click(trigger);

    // Content should now be visible
    expect(content).not.toHaveAttribute('hidden');
    expect(screen.getByTestId('tool-content')).toBeInTheDocument();
    expect(screen.getByText('Tool 1')).toBeInTheDocument();
    expect(screen.getByText('Tool 2')).toBeInTheDocument();
  });

  it('has proper ARIA attributes', () => {
    render(
      <ToolGroup startIndex={0} endIndex={1}>
        {mockChildren}
      </ToolGroup>
    );

    const trigger = screen.getByTestId('tool-group-trigger');

    expect(trigger).toHaveAttribute('type', 'button');
    expect(trigger).toHaveAttribute('aria-expanded', 'false');
  });

  describe('category summary and icons', () => {
    function renderShell(toolNames: string[], toolCount: number = toolNames.length): HTMLElement {
      render(
        <ToolGroupShell
          toolNames={toolNames}
          toolCount={toolCount}
          isExpanded={false}
          onOpenChange={() => {}}
        >
          {mockChildren}
        </ToolGroupShell>
      );
      return screen.getByTestId('tool-group-trigger');
    }

    function categoryIconCount(trigger: HTMLElement): number {
      return trigger.querySelectorAll('svg:not(.lucide-chevron-down)').length;
    }

    it('summarises by category and shows one icon per distinct category', () => {
      const trigger = renderShell(['add_or_update_note', 'get_note', 'search_documents']);

      expect(trigger).toHaveTextContent('2 notes and 1 document');
      expect(categoryIconCount(trigger)).toBe(2);
      expect(trigger.querySelector('.lucide-chevron-down')).toBeInTheDocument();
    });

    it('caps category icons at four when tools span more categories', () => {
      const trigger = renderShell([
        'add_or_update_note',
        'add_calendar_event',
        'search_documents',
        'schedule_reminder',
        'execute_script',
      ]);

      expect(trigger).toHaveTextContent('5 tools from 5 categories');
      expect(categoryIconCount(trigger)).toBe(4);
    });

    it('falls back to a plain count without icons when tool names are unavailable', () => {
      const trigger = renderShell([], 2);

      expect(trigger).toHaveTextContent('2 tool calls');
      expect(categoryIconCount(trigger)).toBe(0);
    });
  });

  it('counts calls that did not succeed in the collapsed header', () => {
    render(
      <ToolGroupShell
        toolNames={[]}
        toolCount={3}
        unsuccessfulCount={1}
        isExpanded={false}
        onOpenChange={() => {}}
      >
        {mockChildren}
      </ToolGroupShell>
    );

    expect(screen.getByTestId('tool-group-trigger')).toHaveTextContent(
      "3 tool calls · 1 didn't finish"
    );
  });
});
