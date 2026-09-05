import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { vi } from 'vitest';
import IntelligenceSelector from '../IntelligenceSelector';
import type { ModelTier } from '../profilesContext';

const TIERS: ModelTier[] = [
  { id: 'standard', label: 'Standard', description: 'Good value for everyday requests.' },
  { id: 'deep', label: 'Deep', description: 'Stronger reasoning for harder requests.' },
  { id: 'frontier', label: 'Max', description: null },
];

// Radix Select needs pointer-capture and scroll APIs jsdom lacks. Stub them for
// the tests that open the menu and restore afterwards, so a later test doesn't
// silently get a defined no-op where jsdom has nothing.
const stubs: Array<() => void> = [];
const setupUser = () => {
  const proto = window.HTMLElement.prototype as unknown as Record<string, unknown>;
  for (const method of ['hasPointerCapture', 'releasePointerCapture', 'scrollIntoView']) {
    const hadOwn = Object.prototype.hasOwnProperty.call(proto, method);
    const original = proto[method];
    proto[method] = vi.fn();
    stubs.push(() => {
      if (hadOwn) {
        proto[method] = original;
      } else {
        delete proto[method];
      }
    });
  }
  return userEvent.setup({ pointerEventsCheck: 0 });
};

afterEach(() => {
  while (stubs.length > 0) {
    stubs.pop()?.();
  }
});

describe('IntelligenceSelector', () => {
  it('shows the profile default when nothing is chosen', () => {
    render(
      <IntelligenceSelector
        tiers={TIERS}
        defaultTierId="standard"
        selectedTierId={null}
        pinned={false}
        onChange={vi.fn()}
      />
    );

    expect(screen.getByRole('combobox', { name: 'Intelligence' })).toHaveTextContent('Standard');
    // Nothing to pin while the profile default is in effect.
    expect(screen.queryByTestId('intelligence-pin')).not.toBeInTheDocument();
  });

  it('lists every tier with its description and marks the default', async () => {
    const user = setupUser();
    render(
      <IntelligenceSelector
        tiers={TIERS}
        defaultTierId="standard"
        selectedTierId={null}
        pinned={false}
        onChange={vi.fn()}
      />
    );

    await user.click(screen.getByRole('combobox', { name: 'Intelligence' }));

    const standard = await screen.findByRole('option', { name: /Standard/ });
    expect(standard).toHaveTextContent('Default');
    expect(standard).toHaveTextContent('Good value for everyday requests.');
    const deep = screen.getByRole('option', { name: /Deep/ });
    expect(deep).not.toHaveTextContent('Default');
    expect(screen.getByRole('option', { name: /Max/ })).toBeInTheDocument();
  });

  it('reports a non-default choice, keeping the current pin state', async () => {
    const user = setupUser();
    const onChange = vi.fn();
    render(
      <IntelligenceSelector
        tiers={TIERS}
        defaultTierId="standard"
        selectedTierId="frontier"
        pinned={true}
        onChange={onChange}
      />
    );

    await user.click(screen.getByRole('combobox', { name: 'Intelligence' }));
    await user.click(await screen.findByRole('option', { name: /Deep/ }));

    expect(onChange).toHaveBeenCalledWith('deep', true);
  });

  it('reports choosing the default as no choice at all', async () => {
    const user = setupUser();
    const onChange = vi.fn();
    render(
      <IntelligenceSelector
        tiers={TIERS}
        defaultTierId="standard"
        selectedTierId="deep"
        pinned={true}
        onChange={onChange}
      />
    );

    await user.click(screen.getByRole('combobox', { name: 'Intelligence' }));
    await user.click(await screen.findByRole('option', { name: /Standard/ }));

    expect(onChange).toHaveBeenCalledWith(null, false);
  });

  it('toggles the pin for the selected tier', async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(
      <IntelligenceSelector
        tiers={TIERS}
        defaultTierId="standard"
        selectedTierId="deep"
        pinned={false}
        onChange={onChange}
      />
    );

    const pin = screen.getByTestId('intelligence-pin');
    expect(pin).toHaveAttribute('aria-pressed', 'false');
    await user.click(pin);

    expect(onChange).toHaveBeenCalledWith('deep', true);
  });

  it('looks pinned once the choice holds for the conversation', () => {
    render(
      <IntelligenceSelector
        tiers={TIERS}
        defaultTierId="standard"
        selectedTierId="deep"
        pinned={true}
        onChange={vi.fn()}
      />
    );

    const pin = screen.getByTestId('intelligence-pin');
    expect(pin).toHaveAttribute('aria-pressed', 'true');
    expect(pin).toHaveAttribute('title', expect.stringContaining('stays selected'));
  });

  it('renders nothing for a profile with no tier to choose', () => {
    const { container } = render(
      <IntelligenceSelector
        tiers={[]}
        defaultTierId={null}
        selectedTierId={null}
        pinned={false}
        onChange={vi.fn()}
      />
    );

    expect(container).toBeEmptyDOMElement();
  });

  it('renders nothing when the profile offers only the tier it already runs', () => {
    const { container } = render(
      <IntelligenceSelector
        tiers={[TIERS[0]]}
        defaultTierId="standard"
        selectedTierId={null}
        pinned={false}
        onChange={vi.fn()}
      />
    );

    expect(container).toBeEmptyDOMElement();
  });
});
