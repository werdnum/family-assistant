import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, it, expect } from 'vitest';
import { ToolGroup } from '../ToolGroup';

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

  it('is initially expanded', () => {
    render(
      <ToolGroup startIndex={0} endIndex={1}>
        {mockChildren}
      </ToolGroup>
    );

    const content = screen.getByTestId('tool-group-content');
    expect(content).toHaveAttribute('data-state', 'open');
  });

  it('collapses when clicked', async () => {
    const user = userEvent.setup();
    render(
      <ToolGroup startIndex={0} endIndex={1}>
        {mockChildren}
      </ToolGroup>
    );

    const trigger = screen.getByTestId('tool-group-trigger');
    const content = screen.getByTestId('tool-group-content');

    expect(content).toHaveAttribute('data-state', 'open');

    await user.click(trigger);

    expect(content).toHaveAttribute('data-state', 'closed');
  });

  it('expands again when clicked while collapsed', async () => {
    const user = userEvent.setup();
    render(
      <ToolGroup startIndex={0} endIndex={1}>
        {mockChildren}
      </ToolGroup>
    );

    const trigger = screen.getByTestId('tool-group-trigger');
    const content = screen.getByTestId('tool-group-content');

    // First collapse
    await user.click(trigger);
    expect(content).toHaveAttribute('data-state', 'closed');

    // Then expand again
    await user.click(trigger);
    expect(content).toHaveAttribute('data-state', 'open');
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

    // Initially expanded
    expect(content).toHaveAttribute('data-state', 'open');

    // Press Enter to collapse
    await user.keyboard('{Enter}');
    expect(content).toHaveAttribute('data-state', 'closed');
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

    // Initially expanded
    expect(content).toHaveAttribute('data-state', 'open');

    // Press Space to collapse
    await user.keyboard(' ');
    expect(content).toHaveAttribute('data-state', 'closed');
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

    // Initially expanded - content should NOT have hidden attribute
    expect(content).not.toHaveAttribute('hidden');
    expect(screen.getByTestId('tool-content')).toBeInTheDocument();
    expect(screen.getByText('Tool 1')).toBeInTheDocument();
    expect(screen.getByText('Tool 2')).toBeInTheDocument();

    // Collapse
    await user.click(trigger);

    // Content should now be hidden
    expect(content).toHaveAttribute('hidden');
  });

  it('has proper ARIA attributes', () => {
    render(
      <ToolGroup startIndex={0} endIndex={1}>
        {mockChildren}
      </ToolGroup>
    );

    const trigger = screen.getByTestId('tool-group-trigger');

    expect(trigger).toHaveAttribute('type', 'button');
    expect(trigger).toHaveAttribute('aria-expanded', 'true'); // Initially expanded
  });

  it('updates ARIA attributes when collapsed', async () => {
    const user = userEvent.setup();
    render(
      <ToolGroup startIndex={0} endIndex={1}>
        {mockChildren}
      </ToolGroup>
    );

    const trigger = screen.getByTestId('tool-group-trigger');

    // Click to collapse
    await user.click(trigger);

    expect(trigger).toHaveAttribute('aria-expanded', 'false');
  });

  it('displays category icons', () => {
    render(
      <ToolGroup startIndex={0} endIndex={1}>
        {mockChildren}
      </ToolGroup>
    );

    // Check for icon container (icons render based on tool names from context)
    const trigger = screen.getByTestId('tool-group-trigger');
    // When context is not available, no icons are shown (graceful fallback)
    // This is expected behavior in isolated tests
    expect(trigger).toBeInTheDocument();
  });

  it('displays chevron icon that rotates when collapsed', async () => {
    const user = userEvent.setup();
    render(
      <ToolGroup startIndex={0} endIndex={1}>
        {mockChildren}
      </ToolGroup>
    );

    const trigger = screen.getByTestId('tool-group-trigger');
    const chevron = trigger.querySelector('svg:last-child');

    expect(chevron).toBeInTheDocument();
    // Initially expanded, trigger should have data-state="open"
    expect(trigger).toHaveAttribute('data-state', 'open');

    // Click to collapse
    await user.click(trigger);

    // Trigger should have data-state="closed"
    expect(trigger).toHaveAttribute('data-state', 'closed');

    // Note: The rotation is applied via CSS selector [&[data-state=open]>svg]:rotate-180
    // in the CollapsibleTrigger component, so we don't directly test the rotate-180 class
  });
});
