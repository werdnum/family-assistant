import React, { useState, ChangeEvent } from 'react';
import { Button } from '@/components/ui/button';
import styles from './TasksFilter.module.css';

// Type Definitions
export interface Filters {
  status: string;
  task_type: string;
  date_from: string;
  date_to: string;
  sort: 'asc' | 'desc';
}

interface TasksFilterProps {
  filters: Filters;
  taskTypes: string[];
  onFilterChange: (newFilters: Filters) => void;
  hasActiveFilters: boolean;
  onClearFilters: () => void;
}

const TasksFilter: React.FC<TasksFilterProps> = ({
  filters,
  taskTypes,
  onFilterChange,
  hasActiveFilters,
  onClearFilters,
}) => {
  const [localFilters, setLocalFilters] = useState<Filters>(filters);

  const handleInputChange = (field: keyof Filters, value: string) => {
    const newFilters = { ...localFilters, [field]: value };
    setLocalFilters(newFilters);
    onFilterChange(newFilters);
  };

  const formatDateForInput = (isoString: string): string => {
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

  const formatDateForAPI = (inputValue: string): string => {
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

  const handleDateChange = (field: 'date_from' | 'date_to', value: string) => {
    const isoDate = formatDateForAPI(value);
    handleInputChange(field, isoDate);
  };

  return (
    <div className={styles.tasksFilter}>
      <h3 className={styles.filterTitle}>Filters</h3>
      <div className={styles.filterGrid}>
        <div className={styles.filterField}>
          <label htmlFor="status-filter" className={styles.filterLabel}>
            Status:
          </label>
          <select
            id="status-filter"
            value={localFilters.status}
            onChange={(e: ChangeEvent<HTMLSelectElement>) =>
              handleInputChange('status', e.target.value)
            }
            className={styles.filterSelect}
          >
            <option value="">All</option>
            <option value="pending">Pending</option>
            <option value="processing">Processing</option>
            <option value="done">Done</option>
            <option value="failed">Failed</option>
          </select>
        </div>

        <div className={styles.filterField}>
          <label htmlFor="task-type-filter" className={styles.filterLabel}>
            Task Type:
          </label>
          <input
            type="text"
            id="task-type-filter"
            list="task-types"
            value={localFilters.task_type}
            onChange={(e: ChangeEvent<HTMLInputElement>) =>
              handleInputChange('task_type', e.target.value)
            }
            placeholder="Filter by task type..."
            className={styles.filterInput}
          />
          <datalist id="task-types">
            {taskTypes.map((type) => (
              <option key={type} value={type} />
            ))}
          </datalist>
        </div>

        <div className={styles.filterField}>
          <label htmlFor="date-from-filter" className={styles.filterLabel}>
            From Date:
          </label>
          <input
            type="datetime-local"
            id="date-from-filter"
            value={formatDateForInput(localFilters.date_from)}
            onChange={(e: ChangeEvent<HTMLInputElement>) =>
              handleDateChange('date_from', e.target.value)
            }
            className={styles.filterInput}
          />
        </div>

        <div className={styles.filterField}>
          <label htmlFor="date-to-filter" className={styles.filterLabel}>
            To Date:
          </label>
          <input
            type="datetime-local"
            id="date-to-filter"
            value={formatDateForInput(localFilters.date_to)}
            onChange={(e: ChangeEvent<HTMLInputElement>) =>
              handleDateChange('date_to', e.target.value)
            }
            className={styles.filterInput}
          />
        </div>

        <div className={styles.filterField}>
          <label htmlFor="sort-filter" className={styles.filterLabel}>
            Sort Order:
          </label>
          <select
            id="sort-filter"
            value={localFilters.sort}
            onChange={(e: ChangeEvent<HTMLSelectElement>) =>
              handleInputChange('sort', e.target.value as 'asc' | 'desc')
            }
            className={styles.filterSelect}
          >
            <option value="desc">Newest First</option>
            <option value="asc">Oldest First</option>
          </select>
        </div>

        {hasActiveFilters && (
          <div className={styles.clearButtonContainer}>
            <Button onClick={onClearFilters} variant="destructive" size="sm">
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