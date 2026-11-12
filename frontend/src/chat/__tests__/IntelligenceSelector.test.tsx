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

const PRESETS: ModelTier[] = [
  { id: 'gemini_4_argon', label: 'Gemini 4 Argon', description: "Google's strongest model." },
  { id: 'kimi_k3', label: 'Kimi K3', description: null },
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
        onChange={vi.fn()}
      />
    );

    expect(screen.getByRole('button', { name: 'Intelligence' })).toHaveTextContent('Standard');
  });

  it('lists every tier with its description and marks the default', async () => {
    const user = setupUser();
    render(
      <IntelligenceSelector
        tiers={TIERS}
        defaultTierId="standard"
        selectedTierId={null}
        onChange={vi.fn()}
      />
    );

    await user.click(screen.getByRole('button', { name: 'Intelligence' }));

    const standard = await screen.findByRole('menuitemradio', { name: /Standard/ });
    expect(standard).toHaveTextContent('Default');
    expect(standard).toHaveTextContent('Good value for everyday requests.');
    const deep = screen.getByRole('menuitemradio', { name: /Deep/ });
    expect(deep).not.toHaveTextContent('Default');
    expect(screen.getByRole('menuitemradio', { name: /Max/ })).toBeInTheDocument();
  });

  it('reports a non-default choice', async () => {
    const user = setupUser();
    const onChange = vi.fn();
    render(
      <IntelligenceSelector
        tiers={TIERS}
        defaultTierId="standard"
        selectedTierId="frontier"
        onChange={onChange}
      />
    );

    await user.click(screen.getByRole('button', { name: 'Intelligence' }));
    await user.click(await screen.findByRole('menuitemradio', { name: /Deep/ }));

    expect(onChange).toHaveBeenCalledWith('deep');
  });

  it('reports choosing the default as no choice at all', async () => {
    const user = setupUser();
    const onChange = vi.fn();
    render(
      <IntelligenceSelector
        tiers={TIERS}
        defaultTierId="standard"
        selectedTierId="deep"
        onChange={onChange}
      />
    );

    await user.click(screen.getByRole('button', { name: 'Intelligence' }));
    await user.click(await screen.findByRole('menuitemradio', { name: /Standard/ }));

    expect(onChange).toHaveBeenCalledWith(null);
  });

  it('says a chosen tier holds for the conversation', () => {
    render(
      <IntelligenceSelector
        tiers={TIERS}
        defaultTierId="standard"
        selectedTierId="deep"
        onChange={vi.fn()}
      />
    );

    const trigger = screen.getByRole('button', { name: 'Intelligence' });
    expect(trigger).toHaveTextContent('Deep');
    expect(trigger).toHaveAttribute('title', expect.stringContaining('stays selected'));
  });

  it('renders nothing for a profile with no tier to choose', () => {
    const { container } = render(
      <IntelligenceSelector
        tiers={[]}
        defaultTierId={null}
        selectedTierId={null}
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
        onChange={vi.fn()}
      />
    );

    expect(container).toBeEmptyDOMElement();
  });

  it('keeps specific models out of the tier list, behind a submenu', async () => {
    const user = setupUser();
    render(
      <IntelligenceSelector
        tiers={TIERS}
        presets={PRESETS}
        defaultTierId="standard"
        selectedTierId={null}
        onChange={vi.fn()}
      />
    );

    await user.click(screen.getByRole('button', { name: 'Intelligence' }));

    expect(await screen.findAllByRole('menuitemradio')).toHaveLength(TIERS.length);
    expect(screen.getByRole('menuitem', { name: /Specific model/ })).toBeInTheDocument();
  });

  it('reports choosing a specific model from the submenu', async () => {
    const user = setupUser();
    const onChange = vi.fn();
    render(
      <IntelligenceSelector
        tiers={TIERS}
        presets={PRESETS}
        defaultTierId="standard"
        selectedTierId={null}
        onChange={onChange}
      />
    );

    await user.click(screen.getByRole('button', { name: 'Intelligence' }));
    // By keyboard: jsdom has no layout, so Radix's pointer grace area, which
    // keeps a submenu open while the pointer travels into it, cannot work here.
    (await screen.findByRole('menuitem', { name: /Specific model/ })).focus();
    await user.keyboard('{ArrowRight}');
    (await screen.findByRole('menuitemradio', { name: /Kimi K3/ })).focus();
    await user.keyboard('{Enter}');

    expect(onChange).toHaveBeenCalledWith('kimi_k3');
  });

  it('names a chosen specific model on the control', () => {
    render(
      <IntelligenceSelector
        tiers={TIERS}
        presets={PRESETS}
        defaultTierId="standard"
        selectedTierId="gemini_4_argon"
        onChange={vi.fn()}
      />
    );

    expect(screen.getByRole('button', { name: 'Intelligence' })).toHaveTextContent(
      'Gemini 4 Argon'
    );
  });

  it('offers a choice when the only tier is joined by a specific model', () => {
    render(
      <IntelligenceSelector
        tiers={[TIERS[0]]}
        presets={[PRESETS[0]]}
        defaultTierId="standard"
        selectedTierId={null}
        onChange={vi.fn()}
      />
    );

    expect(screen.getByRole('button', { name: 'Intelligence' })).toBeInTheDocument();
  });
});
