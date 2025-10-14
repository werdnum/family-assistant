import React, { FormEvent } from 'react';
import { Button } from '@/components/ui/button';
import { Label } from '@/components/ui/label';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { Checkbox } from '@/components/ui/checkbox';
import { Card } from '@/components/ui/card';
import styles from './EventFilters.module.css';

export interface EventFiltersState {
  source_id: string;
  hours: number;
  only_triggered: boolean;
}

interface EventFiltersProps {
  filters: EventFiltersState;
  onFiltersChange: (newFilters: EventFiltersState) => void;
  onClearFilters: () => void;
  loading?: boolean;
}

const EventFilters: React.FC<EventFiltersProps> = ({
  filters,
  onFiltersChange,
  onClearFilters,
  loading = false,
}) => {
  const handleFilterChange = (
    filterName: keyof EventFiltersState,
    value: string | number | boolean
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

  const hasActiveFilters = (): boolean => {
    return !!filters.source_id || filters.hours !== 24 || filters.only_triggered;
  };

  return (
    <Card className={styles.filtersCard}>
      <form onSubmit={handleSubmit} className={styles.filtersForm}>
        <details className={styles.filtersDetails} open>
          <summary className={styles.filtersSummary}>
            <span>Filters</span>
            {hasActiveFilters() && <span className={styles.activeFiltersIndicator}>Active</span>}
          </summary>

          <div className={styles.filtersContent}>
            <div className={styles.filtersGrid}>
              <div className={styles.filterGroup}>
                <Label htmlFor="source_id">Event Source</Label>
                <Select
                  value={filters.source_id || '_all'}
                  onValueChange={(value) =>
                    handleFilterChange('source_id', value === '_all' ? '' : value)
                  }
                  disabled={loading}
                >
                  <SelectTrigger id="source_id">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="_all">All Sources</SelectItem>
                    <SelectItem value="home_assistant">Home Assistant</SelectItem>
                    <SelectItem value="indexing">Indexing</SelectItem>
                    <SelectItem value="webhook">Webhook</SelectItem>
                  </SelectContent>
                </Select>
              </div>

              <div className={styles.filterGroup}>
                <Label htmlFor="hours">Time Range</Label>
                <Select
                  value={String(filters.hours || 24)}
                  onValueChange={(value) => handleFilterChange('hours', parseInt(value, 10))}
                  disabled={loading}
                >
                  <SelectTrigger id="hours">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="1">Last 1 hour</SelectItem>
                    <SelectItem value="6">Last 6 hours</SelectItem>
                    <SelectItem value="24">Last 24 hours</SelectItem>
                    <SelectItem value="48">Last 48 hours</SelectItem>
                  </SelectContent>
                </Select>
              </div>

              <div className={styles.filterGroup}>
                <div className={styles.checkboxWrapper}>
                  <Checkbox
                    id="only_triggered"
                    checked={filters.only_triggered || false}
                    onCheckedChange={(checked) => handleFilterChange('only_triggered', !!checked)}
                    disabled={loading}
                  />
                  <Label htmlFor="only_triggered" className={styles.checkboxLabel}>
                    Only show events that triggered listeners
                  </Label>
                </div>
              </div>
            </div>

            <div className={styles.filtersActions}>
              <Button type="button" variant="outline" onClick={onClearFilters} disabled={loading}>
                Clear Filters
              </Button>
            </div>
          </div>
        </details>
      </form>
    </Card>
  );
};

export default EventFilters;
