import { render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import TasksFilter from '../TasksFilter';

const filters = { status: '', task_type: '', date_from: '', date_to: '', sort: 'desc' };
const props = { taskTypes: [], onFilterChange: vi.fn(), onClearFilters: vi.fn() };

afterEach(() => vi.restoreAllMocks());

describe('TasksFilter', () => {
  it('keeps fields in sync when navigation or Clear Filters updates the URL', () => {
    const { rerender } = render(
      <TasksFilter
        {...props}
        filters={{ ...filters, status: 'failed', task_type: 'reminder' }}
        hasActiveFilters
      />
    );
    expect(screen.getByLabelText('Status')).toHaveValue('failed');
    rerender(<TasksFilter {...props} filters={filters} hasActiveFilters={false} />);
    expect(screen.getByLabelText('Status')).toHaveValue('');
    expect(screen.getByLabelText('Task Type')).toHaveValue('');
  });

  it('displays datetime filters in the local time zone', () => {
    vi.spyOn(Date.prototype, 'getTimezoneOffset').mockReturnValue(-600);
    render(
      <TasksFilter
        {...props}
        filters={{ ...filters, date_from: '2026-10-02T00:30:00Z' }}
        hasActiveFilters
      />
    );
    expect(screen.getByLabelText('From Date')).toHaveValue('2026-10-02T10:30');
  });
});
