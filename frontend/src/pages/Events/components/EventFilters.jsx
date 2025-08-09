import React from 'react';
import { Button } from '@/components/ui/button';
import styles from './EventFilters.module.css';

const EventFilters = ({ filters, onFiltersChange, onClearFilters, loading = false }) => {
  const handleFilterChange = (filterName, value) => {
    const newFilters = {
      ...filters,
      [filterName]: value,
    };
    onFiltersChange(newFilters);
  };

  const handleSubmit = (e) => {
    e.preventDefault();
    // Form submit is handled by parent component via onFiltersChange
  };

  const hasActiveFilters = () => {
    return filters.source_id || filters.hours !== 24 || filters.only_triggered;
  };

  return (
    <form onSubmit={handleSubmit} className={styles.filtersForm}>
      <details className={styles.filtersDetails} open>
        <summary className={styles.filtersSummary}>
          <span>Filters</span>
          {hasActiveFilters() && <span className={styles.activeFiltersIndicator}>Active</span>}
        </summary>

        <div className={styles.filtersGrid}>
          <div className={styles.filterGroup}>
            <label htmlFor="source_id">Event Source:</label>
            <select
              name="source_id"
              id="source_id"
              value={filters.source_id || ''}
              onChange={(e) => handleFilterChange('source_id', e.target.value)}
              disabled={loading}
            >
              <option value="">All Sources</option>
              <option value="home_assistant">Home Assistant</option>
              <option value="indexing">Indexing</option>
              <option value="webhook">Webhook</option>
            </select>
          </div>

          <div className={styles.filterGroup}>
            <label htmlFor="hours">Time Range:</label>
            <select
              name="hours"
              id="hours"
              value={filters.hours || 24}
              onChange={(e) => handleFilterChange('hours', parseInt(e.target.value))}
              disabled={loading}
            >
              <option value={1}>Last 1 hour</option>
              <option value={6}>Last 6 hours</option>
              <option value={24}>Last 24 hours</option>
              <option value={48}>Last 48 hours</option>
            </select>
          </div>

          <div className={styles.filterGroup}>
            <label className={styles.checkboxLabel}>
              <input
                type="checkbox"
                name="only_triggered"
                checked={filters.only_triggered || false}
                onChange={(e) => handleFilterChange('only_triggered', e.target.checked)}
                disabled={loading}
              />
              <span className={styles.checkboxText}>Only show events that triggered listeners</span>
            </label>
          </div>
        </div>

        <div className={styles.filtersActions}>
          <Button type="button" variant="secondary" onClick={onClearFilters} disabled={loading}>
            Clear Filters
          </Button>
        </div>
      </details>
    </form>
  );
};

export default EventFilters;
