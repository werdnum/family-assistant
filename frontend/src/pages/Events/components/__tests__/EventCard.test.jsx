import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { describe, expect, it } from 'vitest';
import EventCard from '../EventCard';

describe('EventCard', () => {
  it('labels Home Assistant events recorded under the canonical source id', () => {
    render(
      <MemoryRouter>
        <EventCard
          event={{
            event_id: 'home_assistant:1',
            source_id: 'home_assistant',
            timestamp: '2026-08-01T00:00:00Z',
            event_data: {
              event_type: 'state_changed',
              entity_id: 'light.kitchen',
              old_state: { state: 'off' },
              new_state: { state: 'on' },
            },
          }}
        />
      </MemoryRouter>
    );

    expect(screen.getByText('Home Assistant')).toBeInTheDocument();
    expect(screen.getByTitle('Source: Home Assistant')).toHaveTextContent('🏠');
    expect(screen.getByText('state_changed: light.kitchen')).toBeInTheDocument();
  });
});
