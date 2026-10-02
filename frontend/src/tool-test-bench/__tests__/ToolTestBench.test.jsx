import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
import { ToolTestBench } from '../ToolTestBench';

describe('ToolTestBench', () => {
  it('finds sample states and explains searches with no matches', async () => {
    render(<ToolTestBench />);
    const user = userEvent.setup();
    const search = screen.getByLabelText('Search samples');
    await user.click(search);
    await user.paste('Get Note - Running');
    expect(screen.getByRole('heading', { name: 'Get Note - Running' })).toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: 'Note Tool - Running' })).not.toBeInTheDocument();
    await user.clear(search);
    await user.paste('nonexistent sample');
    expect(screen.getByText('No samples match your search.')).toBeInTheDocument();
  });
});
