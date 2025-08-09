import React, { useState, useEffect } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import { Button } from '@/components/ui/button';

const ErrorsList = () => {
  const [searchParams, setSearchParams] = useSearchParams();
  const [errors, setErrors] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [totalPages, setTotalPages] = useState(0);
  const [totalCount, setTotalCount] = useState(0);

  // Get current filter values from URL params
  const currentPage = parseInt(searchParams.get('page') || '1');
  const currentLevel = searchParams.get('level') || '';
  const currentLogger = searchParams.get('logger') || '';
  const currentDays = parseInt(searchParams.get('days') || '7');

  // Form state for filters
  const [filters, setFilters] = useState({
    level: currentLevel,
    logger: currentLogger,
    days: currentDays,
  });

  const fetchErrors = async (page, level, logger, days) => {
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

      const data = await response.json();
      setErrors(data.errors);
      setTotalPages(data.total_pages);
      setTotalCount(data.total_count);
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  };

  // Fetch errors when URL params change
  useEffect(() => {
    fetchErrors(currentPage, currentLevel, currentLogger, currentDays);
  }, [currentPage, currentLevel, currentLogger, currentDays]);

  const handleFilterSubmit = (e) => {
    e.preventDefault();

    // Update URL parameters
    const newParams = new URLSearchParams();
    newParams.set('page', '1'); // Reset to first page
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

  const handlePageChange = (newPage) => {
    const newParams = new URLSearchParams(searchParams);
    newParams.set('page', newPage.toString());
    setSearchParams(newParams);
  };

  const getLevelBadgeClass = (level) => {
    switch (level) {
      case 'CRITICAL':
        return 'badge bg-danger';
      case 'ERROR':
        return 'badge bg-warning text-dark';
      case 'WARNING':
        return 'badge bg-warning text-dark';
      default:
        return 'badge bg-secondary';
    }
  };

  const getRowClass = (level) => {
    if (level === 'CRITICAL') {
      return 'table-danger';
    }
    if (level === 'ERROR') {
      return 'table-warning';
    }
    return '';
  };

  const formatTimestamp = (timestamp) => {
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

    // Previous button
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

    // First page
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

    // Visible pages
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

    // Last page
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

    // Next button
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
        <ul className="pagination">{pages}</ul>
      </nav>
    );
  };

  return (
    <div className="errors-list">
      <h1 className="mb-4">Error Logs</h1>

      {/* Filter Form */}
      <div className="filter-form">
        <form onSubmit={handleFilterSubmit} className="row">
          <div className="col">
            <label htmlFor="level">Level</label>
            <select
              id="level"
              value={filters.level}
              onChange={(e) => setFilters({ ...filters, level: e.target.value })}
            >
              <option value="">All Levels</option>
              <option value="ERROR">ERROR</option>
              <option value="CRITICAL">CRITICAL</option>
              <option value="WARNING">WARNING</option>
            </select>
          </div>

          <div className="col">
            <label htmlFor="logger">Logger Name</label>
            <input
              type="text"
              id="logger"
              value={filters.logger}
              onChange={(e) => setFilters({ ...filters, logger: e.target.value })}
              placeholder="e.g., family_assistant"
            />
          </div>

          <div className="col">
            <label htmlFor="days">Time Range</label>
            <select
              id="days"
              value={filters.days}
              onChange={(e) => setFilters({ ...filters, days: parseInt(e.target.value) })}
            >
              <option value={1}>Last 24 hours</option>
              <option value={7}>Last 7 days</option>
              <option value={30}>Last 30 days</option>
              <option value={90}>Last 90 days</option>
            </select>
          </div>

          <div className="col col-auto">
            <Button type="submit" variant="default">
              Filter
            </Button>
            <Button type="button" onClick={handleClearFilters} variant="secondary">
              Clear
            </Button>
          </div>
        </form>
      </div>

      {/* Results Summary */}
      {!loading && (
        <div className="results-summary">
          <strong>{totalCount}</strong> error(s) found
        </div>
      )}

      {/* Loading State */}
      {loading && <div className="loading">Loading errors...</div>}

      {/* Error State */}
      {error && (
        <div className="error-message">
          <strong>Error loading data:</strong> {error}
        </div>
      )}

      {/* Error List */}
      {!loading && !error && (
        <div className="table-responsive">
          <table className="table">
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
                      className="text-truncate"
                      style={{ maxWidth: '500px' }}
                      title={errorItem.message}
                    >
                      {errorItem.message}
                    </div>
                    {errorItem.exception_type && (
                      <small className="text-muted">
                        {errorItem.exception_type}: {errorItem.exception_message}
                      </small>
                    )}
                  </td>
                  <td>
                    <Link to={`/errors/${errorItem.id}`} className="btn-outline-primary btn-sm">
                      View Details
                    </Link>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {/* No Results */}
      {!loading && !error && errors.length === 0 && (
        <div className="alert alert-info">No errors found matching your criteria.</div>
      )}

      {/* Pagination */}
      {!loading && !error && renderPagination()}
    </div>
  );
};

export default ErrorsList;
