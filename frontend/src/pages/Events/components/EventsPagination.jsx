import React from 'react';
import styles from './EventsPagination.module.css';

const EventsPagination = ({
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
        <button
          onClick={handlePrevious}
          disabled={currentPage <= 1 || loading}
          className={styles.paginationButton}
          aria-label="Previous page"
        >
          ← Previous
        </button>

        <span className={styles.pageIndicator}>
          Page {currentPage} of {totalPages}
        </span>

        <button
          onClick={handleNext}
          disabled={currentPage >= totalPages || loading}
          className={styles.paginationButton}
          aria-label="Next page"
        >
          Next →
        </button>
      </div>
    </nav>
  );
};

export default EventsPagination;
