import React, { useState, useEffect, FormEvent, ChangeEvent } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import { Button } from '@/components/ui/button';
import styles from './ErrorsList.module.css';

// Type Definitions
interface ErrorItem {
  id: string;
  timestamp: string;
  level: 'CRITICAL' | 'ERROR' | 'WARNING' | 'INFO' | 'DEBUG';
  logger_name: string;
  message: string;
  exception_type: string | null;
  exception_message: string | null;
}

interface ErrorsResponse {
  errors: ErrorItem[];
  total_pages: number;
  total_count: number;
}

interface Filters {
  level: string;
  logger: string;
  days: number;
}

const ErrorsList: React.FC = () => {
  const [searchParams, setSearchParams] = useSearchParams();
  const [errors, setErrors] = useState<ErrorItem[]>([]);
  const [loading, setLoading] = useState<boolean>(true);
  const [error, setError] = useState<string | null>(null);
  const [totalPages, setTotalPages] = useState<number>(0);
  const [totalCount, setTotalCount] = useState<number>(0);

  const currentPage = parseInt(searchParams.get('page') || '1', 10);
  const currentLevel = searchParams.get('level') || '';
  const currentLogger = searchParams.get('logger') || '';
  const currentDays = parseInt(searchParams.get('days') || '7', 10);

  const [filters, setFilters] = useState<Filters>({
    level: currentLevel,
    logger: currentLogger,
    days: currentDays,
  });

  useEffect(() => {
    const fetchErrors = async (page: number, level: string, logger: string, days: number) => {
      setLoading(true);
      setError(null);

      try {
        const params = new URLSearchParams({
          page: page.toString(),
          days: days.toString(),
        });

        if (level) {
          params.append('level', level);
        }
        if (logger) {
          params.append('logger', logger);
        }

        const response = await fetch(`/api/errors/?${params}`);
        if (!response.ok) {
          throw new Error(`Failed to fetch errors: ${response.statusText}`);
        }

        const data: ErrorsResponse = await response.json();
        setErrors(data.errors);
        setTotalPages(data.total_pages);
        setTotalCount(data.total_count);
      } catch (err: any) {
        setError(err.message);
      } finally {
        setLoading(false);
      }
    };

    fetchErrors(currentPage, currentLevel, currentLogger, currentDays);
  }, [currentPage, currentLevel, currentLogger, currentDays]);

  const handleFilterSubmit = (e: FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    const newParams = new URLSearchParams();
    newParams.set('page', '1');
    newParams.set('days', filters.days.toString());
    if (filters.level) {
      newParams.set('level', filters.level);
    }
    if (filters.logger) {
      newParams.set('logger', filters.logger);
    }
    setSearchParams(newParams);
  };

  const handleClearFilters = () => {
    setFilters({ level: '', logger: '', days: 7 });
    setSearchParams({ page: '1', days: '7' });
  };

  const handlePageChange = (newPage: number) => {
    const newParams = new URLSearchParams(searchParams);
    newParams.set('page', newPage.toString());
    setSearchParams(newParams);
  };

  const getLevelBadgeClass = (level: ErrorItem['level']): string => {
    switch (level) {
      case 'CRITICAL':
        return `${styles.badge} ${styles.badgeDanger}`;
      case 'ERROR':
        return `${styles.badge} ${styles.badgeWarning}`;
      case 'WARNING':
        return `${styles.badge} ${styles.badgeWarning}`;
      default:
        return `${styles.badge} ${styles.badgeSecondary}`;
    }
  };

  const getRowClass = (level: ErrorItem['level']): string => {
    if (level === 'CRITICAL') {
      return styles.tableDanger;
    }
    if (level === 'ERROR') {
      return styles.tableWarning;
    }
    return '';
  };

  const formatTimestamp = (timestamp: string): string => {
    const date = new Date(timestamp);
    return date.toLocaleString('en-US', {
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit',
      hour12: false,
    });
  };

  const renderPagination = () => {
    if (totalPages <= 1) {
      return null;
    }

    const pages = [];
    const maxVisible = 5;
    const startPage = Math.max(1, currentPage - Math.floor(maxVisible / 2));
    const endPage = Math.min(totalPages, startPage + maxVisible - 1);

    pages.push(
      <li key="prev" className={`page-item ${currentPage === 1 ? 'disabled' : ''}`}>
        <Button
          variant="outline"
          size="sm"
          onClick={() => handlePageChange(currentPage - 1)}
          disabled={currentPage === 1}
        >
          Previous
        </Button>
      </li>
    );

    if (startPage > 1) {
      pages.push(
        <li key={1} className="page-item">
          <Button variant="outline" size="sm" onClick={() => handlePageChange(1)}>
            1
          </Button>
        </li>
      );
      if (startPage > 2) {
        pages.push(
          <li key="ellipsis1" className="page-item disabled">
            <span className="page-link">...</span>
          </li>
        );
      }
    }

    for (let i = startPage; i <= endPage; i++) {
      pages.push(
        <li key={i} className={`page-item ${i === currentPage ? 'active' : ''}`}>
          <Button
            variant={i === currentPage ? 'default' : 'outline'}
            size="sm"
            onClick={() => handlePageChange(i)}
          >
            {i}
          </Button>
        </li>
      );
    }

    if (endPage < totalPages) {
      if (endPage < totalPages - 1) {
        pages.push(
          <li key="ellipsis2" className="page-item disabled">
            <span className="page-link">...</span>
          </li>
        );
      }
      pages.push(
        <li key={totalPages} className="page-item">
          <Button variant="outline" size="sm" onClick={() => handlePageChange(totalPages)}>
            {totalPages}
          </Button>
        </li>
      );
    }

    pages.push(
      <li key="next" className={`page-item ${currentPage === totalPages ? 'disabled' : ''}`}>
        <Button
          variant="outline"
          size="sm"
          onClick={() => handlePageChange(currentPage + 1)}
          disabled={currentPage === totalPages}
        >
          Next
        </Button>
      </li>
    );

    return (
      <nav aria-label="Error log pagination">
        <ul className={styles.pagination}>{pages}</ul>
      </nav>
    );
  };

  return (
    <div className={styles.errorsList}>
      <h1>Error Logs</h1>

      <div className={styles.filterForm}>
        <form onSubmit={handleFilterSubmit} className={styles.filterRow}>
          <div className={styles.filterCol}>
            <label htmlFor="level">Level</label>
            <select
              id="level"
              value={filters.level}
              onChange={(e: ChangeEvent<HTMLSelectElement>) =>
                setFilters({ ...filters, level: e.target.value })
              }
            >
              <option value="">All Levels</option>
              <option value="ERROR">ERROR</option>
              <option value="CRITICAL">CRITICAL</option>
              <option value="WARNING">WARNING</option>
            </select>
          </div>
          <div className={styles.filterCol}>
            <label htmlFor="logger">Logger Name</label>
            <input
              type="text"
              id="logger"
              value={filters.logger}
              onChange={(e: ChangeEvent<HTMLInputElement>) =>
                setFilters({ ...filters, logger: e.target.value })
              }
              placeholder="e.g., family_assistant"
            />
          </div>
          <div className={styles.filterCol}>
            <label htmlFor="days">Time Range</label>
            <select
              id="days"
              value={filters.days}
              onChange={(e: ChangeEvent<HTMLSelectElement>) =>
                setFilters({ ...filters, days: parseInt(e.target.value, 10) })
              }
            >
              <option value={1}>Last 24 hours</option>
              <option value={7}>Last 7 days</option>
              <option value={30}>Last 30 days</option>
              <option value={90}>Last 90 days</option>
            </select>
          </div>
          <div className={styles.filterColAuto}>
            <Button type="submit" variant="default">
              Filter
            </Button>
            <Button type="button" onClick={handleClearFilters} variant="secondary">
              Clear
            </Button>
          </div>
        </form>
      </div>

      {!loading && (
        <div className={styles.resultsSummary}>
          <strong>{totalCount}</strong> error(s) found
        </div>
      )}
      {loading && <div className={styles.loading}>Loading errors...</div>}
      {error && (
        <div className={styles.errorMessage}>
          <strong>Error loading data:</strong> {error}
        </div>
      )}

      {!loading && !error && (
        <div className={styles.tableResponsive}>
          <table className={styles.table}>
            <thead>
              <tr>
                <th style={{ width: '150px' }}>Timestamp</th>
                <th style={{ width: '80px' }}>Level</th>
                <th>Logger</th>
                <th>Message</th>
                <th style={{ width: '100px' }}>Actions</th>
              </tr>
            </thead>
            <tbody>
              {errors.map((errorItem) => (
                <tr key={errorItem.id} className={getRowClass(errorItem.level)}>
                  <td>
                    <small>{formatTimestamp(errorItem.timestamp)}</small>
                  </td>
                  <td>
                    <span className={getLevelBadgeClass(errorItem.level)}>{errorItem.level}</span>
                  </td>
                  <td>
                    <small>{errorItem.logger_name}</small>
                  </td>
                  <td>
                    <div
                      className={styles.textTruncate}
                      style={{ maxWidth: '500px' }}
                      title={errorItem.message}
                    >
                      {errorItem.message}
                    </div>
                    {errorItem.exception_type && (
                      <small className={styles.textMuted}>
                        {errorItem.exception_type}: {errorItem.exception_message}
                      </small>
                    )}
                  </td>
                  <td>
                    <Link to={`/errors/${errorItem.id}`} className={styles.viewDetailsLink}>
                      View Details
                    </Link>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {!loading && !error && errors.length === 0 && (
        <div className={styles.noResults}>No errors found matching your criteria.</div>
      )}
      {!loading && !error && renderPagination()}
    </div>
  );
};

export default ErrorsList;