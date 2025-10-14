import React, { FormEvent, ChangeEvent } from 'react';
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

export interface HistoryFiltersState {
  interface_type: string;
  conversation_id: string;
  date_from: Date | null;
  date_to: Date | null;
}

interface HistoryFiltersProps {
  filters: HistoryFiltersState;
  onFiltersChange: (newFilters: HistoryFiltersState) => void;
  onClearFilters: () => void;
  loading?: boolean;
}

const HistoryFilters: React.FC<HistoryFiltersProps> = ({
  filters,
  onFiltersChange,
  onClearFilters,
  loading = false,
}) => {
  const handleFilterChange = (
    filterName: keyof HistoryFiltersState,
    value: string | Date | null
  ) => {
    const newFilters = {
      ...filters,
      [filterName]: value,
    };
    onFiltersChange(newFilters);
  };

  const handleSubmit = (e: FormEvent<HTMLFormElement>) => {
    e.preventDefault();
  };

  const formatDate = (date: Date | null): string => {
    if (!date) {
      return '';
    }
    if (isNaN(date.getTime())) {
      return '';
    }
    return date.toISOString().split('T')[0];
  };

  const parseDate = (dateString: string): Date | null => {
    if (!dateString) {
      return null;
    }
    const date = new Date(dateString + 'T00:00:00Z');
    return isNaN(date.getTime()) ? null : date;
  };

  const handleDateChange = (e: ChangeEvent<HTMLInputElement>) => {
    const { name, value } = e.target;
    handleFilterChange(name as keyof HistoryFiltersState, parseDate(value));
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
              value={formatDate(filters.date_from)}
              onChange={handleDateChange}
              disabled={loading}
            />
          </div>

          <div className={styles.filterGroup}>
            <Label htmlFor="date_to">To Date</Label>
            <Input
              type="date"
              name="date_to"
              id="date_to"
              value={formatDate(filters.date_to)}
              onChange={handleDateChange}
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
