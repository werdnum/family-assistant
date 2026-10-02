import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { HttpResponse, http } from 'msw';
import { describe, expect, it } from 'vitest';
import { server } from '../../test/setup.js';
import ToolsApp from '../ToolsApp';

describe('Tool Explorer', () => {
  it('filters by tool name or description and explains unmatched searches', async () => {
    server.use(
      http.get('/api/tools/definitions', () =>
        HttpResponse.json({
          tools: [
            { function: { name: 'get_calendar', description: 'Look up household appointments' } },
            { function: { name: 'get_weather', description: 'Current forecast' } },
          ],
        })
      )
    );
    render(<ToolsApp />);
    await screen.findByRole('button', { name: /get_calendar/ });
    const search = screen.getByLabelText('Search tools');
    const user = userEvent.setup();
    await user.type(search, 'appointments');
    expect(screen.getByRole('button', { name: /get_calendar/ })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /get_weather/ })).not.toBeInTheDocument();
    await user.clear(search);
    await user.type(search, 'nonexistent');
    expect(screen.getByText('No tools match your search.')).toBeInTheDocument();
  });
});
