import React from 'react';
import { Button } from '@/components/ui/button';
import styles from './EventsPagination.module.css';

interface EventsPaginationProps {
  currentPage: number;
  totalPages: number;
  totalItems: number;
  pageSize: number;
  onPageChange: (newPage: number) => void;
  loading?: boolean;
}

const EventsPagination: React.FC<EventsPaginationProps> = ({
  currentPage,
  totalPages,
  totalItems,
  pageSize,
  onPageChange,
  loading = false,
}) => {
  if (totalPages <= 1) {
    return null;
  }

  const handlePrevious = () => {
    if (currentPage > 1 && !loading) {
      onPageChange(currentPage - 1);
    }
  };

  const handleNext = () => {
    if (currentPage < totalPages && !loading) {
      onPageChange(currentPage + 1);
    }
  };

  return (
    <nav className={styles.pagination} aria-label="Pagination">
      <div className={styles.paginationInfo}>
        Showing {Math.min((currentPage - 1) * pageSize + 1, totalItems)} -{' '}
        {Math.min(currentPage * pageSize, totalItems)} of {totalItems} events
      </div>

      <div className={styles.paginationControls}>
        <Button
          onClick={handlePrevious}
          disabled={currentPage <= 1 || loading}
          variant="outline"
          size="sm"
          aria-label="Previous page"
        >
          ← Previous
        </Button>

        <span className={styles.pageIndicator}>
          Page {currentPage} of {totalPages}
        </span>

        <Button
          onClick={handleNext}
          disabled={currentPage >= totalPages || loading}
          variant="outline"
          size="sm"
          aria-label="Next page"
        >
          Next →
        </Button>
      </div>
    </nav>
  );
};

export default EventsPagination;
