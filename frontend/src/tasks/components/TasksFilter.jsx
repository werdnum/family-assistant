import React, { useState } from 'react';
import { Button } from '@/components/ui/button';

const TasksFilter = ({ filters, taskTypes, onFilterChange, hasActiveFilters, onClearFilters }) => {
  const [localFilters, setLocalFilters] = useState(filters);

  // Handle input changes
  const handleInputChange = (field, value) => {
    const newFilters = { ...localFilters, [field]: value };
    setLocalFilters(newFilters);
    onFilterChange(newFilters);
  };

  // Format date for input (convert from ISO to YYYY-MM-DDTHH:MM)
  const formatDateForInput = (isoString) => {
    if (!isoString) {
      return '';
    }
    try {
      const date = new Date(isoString.replace('Z', '+00:00'));
      return date.toISOString().slice(0, 16); // YYYY-MM-DDTHH:MM
    } catch {
      return '';
    }
  };

  // Format date for API (convert from local to ISO)
  const formatDateForAPI = (inputValue) => {
    if (!inputValue) {
      return '';
    }
    try {
      const date = new Date(inputValue);
      return date.toISOString();
    } catch {
      return '';
    }
  };

  const handleDateChange = (field, value) => {
    const isoDate = formatDateForAPI(value);
    handleInputChange(field, isoDate);
  };

  return (
    <div
      style={{
        marginBottom: '2rem',
        padding: '1rem',
        border: '1px solid #ddd',
        borderRadius: '5px',
        backgroundColor: '#f9f9f9',
      }}
    >
      <h3 style={{ marginTop: 0, marginBottom: '1rem' }}>Filters</h3>

      <div
        style={{
          display: 'grid',
          gridTemplateColumns: 'repeat(auto-fit, minmax(200px, 1fr))',
          gap: '1rem',
          alignItems: 'end',
        }}
      >
        {/* Status Filter */}
        <div>
          <label
            htmlFor="status-filter"
            style={{ display: 'block', marginBottom: '0.25rem', fontWeight: 'bold' }}
          >
            Status:
          </label>
          <select
            id="status-filter"
            value={localFilters.status}
            onChange={(e) => handleInputChange('status', e.target.value)}
            style={{ width: '100%', padding: '0.5rem' }}
          >
            <option value="">All</option>
            <option value="pending">Pending</option>
            <option value="processing">Processing</option>
            <option value="done">Done</option>
            <option value="failed">Failed</option>
          </select>
        </div>

        {/* Task Type Filter with autocomplete */}
        <div>
          <label
            htmlFor="task-type-filter"
            style={{ display: 'block', marginBottom: '0.25rem', fontWeight: 'bold' }}
          >
            Task Type:
          </label>
          <input
            type="text"
            id="task-type-filter"
            list="task-types"
            value={localFilters.task_type}
            onChange={(e) => handleInputChange('task_type', e.target.value)}
            placeholder="Filter by task type..."
            style={{ width: '100%', padding: '0.5rem' }}
          />
          <datalist id="task-types">
            {taskTypes.map((type) => (
              <option key={type} value={type} />
            ))}
          </datalist>
        </div>

        {/* Date From Filter */}
        <div>
          <label
            htmlFor="date-from-filter"
            style={{ display: 'block', marginBottom: '0.25rem', fontWeight: 'bold' }}
          >
            From Date:
          </label>
          <input
            type="datetime-local"
            id="date-from-filter"
            value={formatDateForInput(localFilters.date_from)}
            onChange={(e) => handleDateChange('date_from', e.target.value)}
            style={{ width: '100%', padding: '0.5rem' }}
          />
        </div>

        {/* Date To Filter */}
        <div>
          <label
            htmlFor="date-to-filter"
            style={{ display: 'block', marginBottom: '0.25rem', fontWeight: 'bold' }}
          >
            To Date:
          </label>
          <input
            type="datetime-local"
            id="date-to-filter"
            value={formatDateForInput(localFilters.date_to)}
            onChange={(e) => handleDateChange('date_to', e.target.value)}
            style={{ width: '100%', padding: '0.5rem' }}
          />
        </div>

        {/* Sort Order Filter */}
        <div>
          <label
            htmlFor="sort-filter"
            style={{ display: 'block', marginBottom: '0.25rem', fontWeight: 'bold' }}
          >
            Sort Order:
          </label>
          <select
            id="sort-filter"
            value={localFilters.sort}
            onChange={(e) => handleInputChange('sort', e.target.value)}
            style={{ width: '100%', padding: '0.5rem' }}
          >
            <option value="desc">Newest First</option>
            <option value="asc">Oldest First</option>
          </select>
        </div>

        {/* Clear Filters Button */}
        {hasActiveFilters && (
          <div style={{ display: 'flex', alignItems: 'end' }}>
            <Button onClick={onClearFilters} variant="destructive" size="sm">
              Clear All Filters
            </Button>
          </div>
        )}
      </div>

      {hasActiveFilters && (
        <div style={{ marginTop: '1rem', fontSize: '0.9rem', color: '#666' }}>
          <strong>Active filters:</strong>
          {localFilters.status && (
            <span style={{ marginLeft: '0.5rem' }}>Status: {localFilters.status}</span>
          )}
          {localFilters.task_type && (
            <span style={{ marginLeft: '0.5rem' }}>Type: {localFilters.task_type}</span>
          )}
          {localFilters.date_from && (
            <span style={{ marginLeft: '0.5rem' }}>
              From: {new Date(localFilters.date_from).toLocaleString()}
            </span>
          )}
          {localFilters.date_to && (
            <span style={{ marginLeft: '0.5rem' }}>
              To: {new Date(localFilters.date_to).toLocaleString()}
            </span>
          )}
          {localFilters.sort !== 'desc' && (
            <span style={{ marginLeft: '0.5rem' }}>
              Sort: {localFilters.sort === 'asc' ? 'Oldest First' : 'Newest First'}
            </span>
          )}
        </div>
      )}
    </div>
  );
};

export default TasksFilter;
