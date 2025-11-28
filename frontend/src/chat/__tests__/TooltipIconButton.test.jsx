import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
import { TooltipIconButton } from '../TooltipIconButton';

describe('TooltipIconButton', () => {
  it('renders the button without tooltip initially', () => {
    render(
      <TooltipIconButton tooltip="Test tooltip" variant="default">
        Click me
      </TooltipIconButton>
    );

    const button = screen.getByRole('button');
    expect(button).toBeInTheDocument();
    expect(button).toHaveTextContent('Click me');

    // Tooltip should not be visible initially
    expect(screen.queryByRole('tooltip')).not.toBeInTheDocument();
  });

  it('shows tooltip on hover', async () => {
    const user = userEvent.setup();

    render(
      <TooltipIconButton tooltip="Test tooltip" variant="default">
        Hover me
      </TooltipIconButton>
    );

    const button = screen.getByRole('button');

    // Hover over the button
    await user.hover(button);

    // Tooltip should appear
    await waitFor(() => {
      expect(screen.getByRole('tooltip')).toBeInTheDocument();
      expect(screen.getByRole('tooltip')).toHaveTextContent('Test tooltip');
    });
  });

  it('renders tooltip via portal to document.body', async () => {
    const user = userEvent.setup();

    render(
      <div data-testid="container">
        <TooltipIconButton tooltip="Portaled tooltip" variant="default">
          Button
        </TooltipIconButton>
      </div>
    );

    const button = screen.getByRole('button');

    // Hover to show tooltip
    await user.hover(button);

    await waitFor(() => {
      const tooltip = screen.getByRole('tooltip');
      expect(tooltip).toBeInTheDocument();

      // Verify tooltip is NOT inside the container div (it's portaled to body)
      const containerElement = screen.getByTestId('container');
      expect(containerElement).not.toContainElement(tooltip);

      // Verify tooltip is in document.body
      expect(document.body).toContainElement(tooltip);
    });
  });

  it('hides tooltip on mouse leave', async () => {
    const user = userEvent.setup();

    render(
      <TooltipIconButton tooltip="Hide me" variant="default">
        Test
      </TooltipIconButton>
    );

    const button = screen.getByRole('button');

    // Hover to show tooltip
    await user.hover(button);
    await waitFor(() => {
      expect(screen.getByRole('tooltip')).toBeInTheDocument();
    });

    // Unhover to hide tooltip
    await user.unhover(button);

    // Tooltip should be gone
    await waitFor(() => {
      expect(screen.queryByRole('tooltip')).not.toBeInTheDocument();
    });
  });

  it('has pointer-events: none on tooltip to prevent interference', async () => {
    const user = userEvent.setup();

    render(
      <TooltipIconButton tooltip="No interference" variant="default">
        Test
      </TooltipIconButton>
    );

    const button = screen.getByRole('button');

    // Hover to show tooltip
    await user.hover(button);

    await waitFor(() => {
      const tooltip = screen.getByRole('tooltip');
      expect(tooltip).toBeInTheDocument();

      // Verify tooltip has pointer-events: none
      const computedStyle = window.getComputedStyle(tooltip);
      expect(computedStyle.pointerEvents).toBe('none');
    });
  });

  it('positions tooltip above button', async () => {
    const user = userEvent.setup();

    render(
      <TooltipIconButton tooltip="Above me" variant="default" side="top">
        Test
      </TooltipIconButton>
    );

    const button = screen.getByRole('button');
    const buttonRect = button.getBoundingClientRect();

    // Hover to show tooltip
    await user.hover(button);

    await waitFor(() => {
      const tooltip = screen.getByRole('tooltip');
      expect(tooltip).toBeInTheDocument();

      // Get tooltip position
      const tooltipStyle = tooltip.style;

      // Tooltip should be positioned absolutely
      expect(tooltipStyle.position).toBe('absolute');

      // Parse the top value (it's in pixels, e.g., "100px")
      const tooltipTop = parseFloat(tooltipStyle.top);
      const buttonTop = buttonRect.top + window.scrollY;

      // Tooltip should be above the button (smaller top value)
      expect(tooltipTop).toBeLessThan(buttonTop);
    });
  });
});
