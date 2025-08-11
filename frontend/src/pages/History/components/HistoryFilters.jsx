import React from 'react';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import styles from './HistoryFilters.module.css';

const HistoryFilters = ({ filters, onFiltersChange, onClearFilters, loading = false }) => {
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

  const formatDate = (date) => {
    if (!date) {
      return '';
    }
    // Check if date is valid before calling toISOString
    if (Number.isNaN(date.getTime())) {
      return '';
    }
    // Convert to YYYY-MM-DD format for input
    return date.toISOString().split('T')[0];
  };

  const parseDate = (dateString) => {
    if (!dateString) {
      return null;
    }
    // Ensure consistent UTC interpretation across browsers
    const date = new Date(dateString + 'T00:00:00Z');
    // Return null for invalid dates to avoid crashes
    return Number.isNaN(date.getTime()) ? null : date;
  };

  return (
    <form onSubmit={handleSubmit} className={styles.filtersForm}>
      <details className={styles.filtersDetails} open>
        <summary className={styles.filtersSummary}>
          <span>Filters</span>
          {(filters.interface_type ||
            filters.conversation_id ||
            filters.date_from ||
            filters.date_to) && <span className={styles.activeFiltersIndicator}>Active</span>}
        </summary>

        <div className={styles.filtersGrid}>
          <div className={styles.filterGroup}>
            <Label htmlFor="interface_type">Interface Type</Label>
            <Select
              value={filters.interface_type || '_all'}
              onValueChange={(value) =>
                handleFilterChange('interface_type', value === '_all' ? '' : value)
              }
              disabled={loading}
            >
              <SelectTrigger data-testid="interface-type-select">
                <SelectValue placeholder="All Interfaces" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="_all">All Interfaces</SelectItem>
                <SelectItem value="web">Web</SelectItem>
                <SelectItem value="telegram">Telegram</SelectItem>
                <SelectItem value="api">API</SelectItem>
                <SelectItem value="email">Email</SelectItem>
              </SelectContent>
            </Select>
          </div>

          <div className={styles.filterGroup}>
            <Label htmlFor="conversation_id">Conversation ID</Label>
            <Input
              type="text"
              name="conversation_id"
              id="conversation_id"
              value={filters.conversation_id || ''}
              onChange={(e) => handleFilterChange('conversation_id', e.target.value)}
              disabled={loading}
              placeholder="Enter conversation ID (e.g., web_conv_...)"
            />
          </div>

          <div className={styles.filterGroup}>
            <Label htmlFor="date_from">From Date</Label>
            <Input
              type="date"
              name="date_from"
              id="date_from"
              value={filters.date_from ? formatDate(filters.date_from) : ''}
              onChange={(e) => handleFilterChange('date_from', parseDate(e.target.value))}
              disabled={loading}
            />
          </div>

          <div className={styles.filterGroup}>
            <Label htmlFor="date_to">To Date</Label>
            <Input
              type="date"
              name="date_to"
              id="date_to"
              value={filters.date_to ? formatDate(filters.date_to) : ''}
              onChange={(e) => handleFilterChange('date_to', parseDate(e.target.value))}
              disabled={loading}
            />
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

export default HistoryFilters;
