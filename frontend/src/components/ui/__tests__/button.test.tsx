import React from 'react';
import { render, screen } from '@testing-library/react';
import { Button } from '../button';

describe('Button', () => {
  it('renders with default styling', () => {
    render(<Button>Test Button</Button>);

    const button = screen.getByRole('button', { name: 'Test Button' });
    expect(button).toBeInTheDocument();
    expect(button).toHaveClass('inline-flex', 'items-center', 'justify-center');
  });

  it('applies variant classes correctly', () => {
    render(<Button variant="secondary">Secondary Button</Button>);

    const button = screen.getByRole('button', { name: 'Secondary Button' });
    expect(button).toHaveClass('bg-secondary', 'text-secondary-foreground');
  });

  it('applies size classes correctly', () => {
    render(<Button size="lg">Large Button</Button>);

    const button = screen.getByRole('button', { name: 'Large Button' });
    expect(button).toHaveClass('h-11', 'rounded-md', 'px-8');
  });

  it('merges custom className with variant classes', () => {
    render(<Button className="custom-class">Button with custom class</Button>);

    const button = screen.getByRole('button', { name: 'Button with custom class' });
    expect(button).toHaveClass('custom-class');
    expect(button).toHaveClass('inline-flex'); // Should still have base classes
  });

  it('forwards props correctly', () => {
    render(
      <Button disabled data-testid="disabled-button">
        Disabled Button
      </Button>
    );

    const button = screen.getByTestId('disabled-button');
    expect(button).toBeDisabled();
  });
});
