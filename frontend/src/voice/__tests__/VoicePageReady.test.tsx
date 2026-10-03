import { render } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { describe, expect, it } from 'vitest';
import VoicePage from '../VoicePage';

describe('VoicePage readiness signal', () => {
  it('marks the app ready while mounted and clears it on unmount', () => {
    const { unmount } = render(
      <MemoryRouter>
        <VoicePage />
      </MemoryRouter>
    );

    expect(document.documentElement.getAttribute('data-app-ready')).toBe('true');

    unmount();

    expect(document.documentElement.hasAttribute('data-app-ready')).toBe(false);
  });
});
