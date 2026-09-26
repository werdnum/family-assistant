import { render, screen } from '@testing-library/react';
import { Button, type ButtonProps } from '../button';

function classesOf(name: string): string[] {
  return screen.getByRole('button', { name }).className.split(/\s+/).filter(Boolean);
}

describe('Button', () => {
  it.each<{ prop: string; props: ButtonProps }>([
    { prop: 'variant', props: { variant: 'secondary' } },
    { prop: 'size', props: { size: 'lg' } },
  ])('styles the button according to its $prop prop', ({ props }) => {
    render(
      <>
        <Button>Default</Button>
        <Button {...props}>Styled</Button>
      </>
    );

    expect(classesOf('Styled')).not.toEqual(classesOf('Default'));
  });

  it('merges custom className with variant classes', () => {
    render(
      <>
        <Button>Default</Button>
        <Button className="custom-class">Custom</Button>
      </>
    );

    expect(screen.getByRole('button', { name: 'Custom' })).toHaveClass(
      'custom-class',
      ...classesOf('Default')
    );
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
