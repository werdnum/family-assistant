import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { HttpResponse, http } from 'msw';
import { vi } from 'vitest';
import { server } from '../../test/setup.js';
import ProfileSelector from '../ProfileSelector';
import { ProfilesProvider, type ProfilesResponse } from '../profilesContext';

const PROFILES: ProfilesResponse = {
  default_profile_id: 'default_assistant',
  profiles: [
    {
      id: 'default_assistant',
      description: 'Main assistant',
      available_tools: [],
      enabled_mcp_servers: [],
      user_selectable: true,
    },
    {
      id: 'research',
      description: 'Deep research',
      available_tools: [],
      enabled_mcp_servers: [],
      user_selectable: true,
    },
    {
      id: 'council_member',
      description: 'Internal research seat',
      available_tools: [],
      enabled_mcp_servers: [],
      user_selectable: false,
    },
  ],
};

// Radix Select needs pointer-capture and scroll APIs jsdom lacks.
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

const renderSelector = (selectedProfileId: string) => {
  server.use(http.get('/api/v1/profiles', () => HttpResponse.json(PROFILES)));
  render(
    <ProfilesProvider>
      <ProfileSelector selectedProfileId={selectedProfileId} onProfileChange={vi.fn()} />
    </ProfilesProvider>
  );
};

describe('ProfileSelector', () => {
  it('offers only user-selectable profiles', async () => {
    const user = setupUser();
    renderSelector('default_assistant');

    await user.click(await screen.findByRole('combobox', { name: 'Processing profile' }));

    expect(await screen.findByRole('option', { name: /Research/ })).toBeInTheDocument();
    expect(screen.getByRole('option', { name: /Assistant/ })).toBeInTheDocument();
    expect(screen.queryByRole('option', { name: /Council_member/ })).not.toBeInTheDocument();
  });

  it('keeps an internal profile listed while it is the selection', async () => {
    const user = setupUser();
    renderSelector('council_member');

    const picker = await screen.findByRole('combobox', { name: 'Processing profile' });
    expect(picker).toHaveTextContent('Council_member');
    await user.click(picker);

    expect(await screen.findByRole('option', { name: /Council_member/ })).toBeInTheDocument();
  });
});
