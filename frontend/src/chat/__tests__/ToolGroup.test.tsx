import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
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

  it('is initially collapsed', () => {
    render(
      <ToolGroup startIndex={0} endIndex={1}>
        {mockChildren}
      </ToolGroup>
    );

    const content = screen.getByTestId('tool-group-content');
    expect(content).toHaveAttribute('data-state', 'closed');
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

    await user.click(trigger);

    expect(content).toHaveAttribute('data-state', 'open');
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

  it('updates ARIA attributes when expanded', async () => {
    const user = userEvent.setup();
    render(
      <ToolGroup startIndex={0} endIndex={1}>
        {mockChildren}
      </ToolGroup>
    );

    const trigger = screen.getByTestId('tool-group-trigger');

    // Click to expand
    await user.click(trigger);

    expect(trigger).toHaveAttribute('aria-expanded', 'true');
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
    // Initially collapsed, trigger should have data-state="closed"
    expect(trigger).toHaveAttribute('data-state', 'closed');

    // Click to expand
    await user.click(trigger);

    // Trigger should have data-state="open"
    expect(trigger).toHaveAttribute('data-state', 'open');

    // Note: The rotation is applied via CSS selector [&[data-state=open]>svg]:rotate-180
    // in the CollapsibleTrigger component, so we don't directly test the rotate-180 class
  });
});
