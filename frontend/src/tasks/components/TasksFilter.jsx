import React from 'react';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { NativeSelect } from '@/components/ui/native-select';
import styles from './TasksFilter.module.css';

const TasksFilter = ({ filters, taskTypes, onFilterChange, hasActiveFilters, onClearFilters }) => {
  const localFilters = filters;

  // Handle input changes
  const handleInputChange = (field, value) => {
    const newFilters = { ...localFilters, [field]: value };
    onFilterChange(newFilters);
  };

  // Format date for input (convert from ISO to YYYY-MM-DDTHH:MM)
  const formatDateForInput = (isoString) => {
    if (!isoString) {
      return '';
    }
    try {
      const date = new Date(isoString.replace('Z', '+00:00'));
      if (Number.isNaN(date.getTime())) {
        return '';
      }
      const localDate = new Date(date.getTime() - date.getTimezoneOffset() * 60_000);
      return localDate.toISOString().slice(0, 16);
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
    <div className={styles.tasksFilter}>
      <h3 className={styles.filterTitle}>Filters</h3>

      <div className={styles.filterGrid}>
        {/* Status Filter */}
        <div className={styles.filterField}>
          <label htmlFor="status-filter" className={styles.filterLabel}>
            Status
          </label>
          <NativeSelect
            id="status-filter"
            value={localFilters.status}
            onChange={(e) => handleInputChange('status', e.target.value)}
          >
            <option value="">All</option>
            <option value="pending">Pending</option>
            <option value="processing">Processing</option>
            <option value="done">Done</option>
            <option value="failed">Failed</option>
            <option value="cancelled">Cancelled</option>
          </NativeSelect>
        </div>

        {/* Task Type Filter with autocomplete */}
        <div className={styles.filterField}>
          <label htmlFor="task-type-filter" className={styles.filterLabel}>
            Task Type
          </label>
          <Input
            type="text"
            id="task-type-filter"
            list="task-types"
            value={localFilters.task_type}
            onChange={(e) => handleInputChange('task_type', e.target.value)}
            placeholder="Filter by task type..."
          />
          <datalist id="task-types">
            {taskTypes.map((type) => (
              <option key={type} value={type} />
            ))}
          </datalist>
        </div>

        {/* Date From Filter */}
        <div className={styles.filterField}>
          <label htmlFor="date-from-filter" className={styles.filterLabel}>
            From Date
          </label>
          <Input
            type="datetime-local"
            id="date-from-filter"
            value={formatDateForInput(localFilters.date_from)}
            onChange={(e) => handleDateChange('date_from', e.target.value)}
          />
        </div>

        {/* Date To Filter */}
        <div className={styles.filterField}>
          <label htmlFor="date-to-filter" className={styles.filterLabel}>
            To Date
          </label>
          <Input
            type="datetime-local"
            id="date-to-filter"
            value={formatDateForInput(localFilters.date_to)}
            onChange={(e) => handleDateChange('date_to', e.target.value)}
          />
        </div>

        {/* Sort Order Filter */}
        <div className={styles.filterField}>
          <label htmlFor="sort-filter" className={styles.filterLabel}>
            Sort Order
          </label>
          <NativeSelect
            id="sort-filter"
            value={localFilters.sort}
            onChange={(e) => handleInputChange('sort', e.target.value)}
          >
            <option value="desc">Newest First</option>
            <option value="asc">Oldest First</option>
          </NativeSelect>
        </div>

        {/* Clear Filters Button */}
        {hasActiveFilters && (
          <div className={styles.clearButtonContainer}>
            <Button onClick={onClearFilters} variant="outline" size="sm">
              Clear All Filters
            </Button>
          </div>
        )}
      </div>

      {hasActiveFilters && (
        <div className={styles.activeFilters}>
          <strong>Active filters:</strong>
          {localFilters.status && (
            <span className={styles.filterTag}>Status: {localFilters.status}</span>
          )}
          {localFilters.task_type && (
            <span className={styles.filterTag}>Type: {localFilters.task_type}</span>
          )}
          {localFilters.date_from && (
            <span className={styles.filterTag}>
              From: {new Date(localFilters.date_from).toLocaleString()}
            </span>
          )}
          {localFilters.date_to && (
            <span className={styles.filterTag}>
              To: {new Date(localFilters.date_to).toLocaleString()}
            </span>
          )}
          {localFilters.sort !== 'desc' && (
            <span className={styles.filterTag}>
              Sort: {localFilters.sort === 'asc' ? 'Oldest First' : 'Newest First'}
            </span>
          )}
        </div>
      )}
    </div>
  );
};

export default TasksFilter;
