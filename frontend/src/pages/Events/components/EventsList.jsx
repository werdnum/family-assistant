import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { Button } from '@/components/ui/button';
import EventCard from './EventCard';
import EventFilters from './EventFilters';
import styles from './EventsList.module.css';
import EventsPagination from './EventsPagination';

const EventsList = () => {
  const [searchParams, setSearchParams] = useSearchParams();
  const [events, setEvents] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [reloadVersion, setReloadVersion] = useState(0);
  const [totalCount, setTotalCount] = useState(0);
  const [totalPages, setTotalPages] = useState(0);

  // Pagination settings
  const pageSize = 20;
  const currentPage = parseInt(searchParams.get('page') || '1');

  // Get current filter values from URL params (always in sync with URL)
  const filters = useMemo(
    () => ({
      source_id: searchParams.get('source_id') || '',
      hours: parseInt(searchParams.get('hours') || '24'),
      only_triggered: searchParams.get('only_triggered') === 'true',
    }),
    [searchParams]
  );

  const fetchEvents = useCallback(
    async (page, currentFilters, signal) => {
      setLoading(true);
      setError(null);

      try {
        const params = new URLSearchParams({
          limit: pageSize.toString(),
          offset: ((page - 1) * pageSize).toString(),
          hours: currentFilters.hours.toString(),
        });

        // Add filters to API call
        if (currentFilters.source_id) {
          params.append('source_id', currentFilters.source_id);
        }
        if (currentFilters.only_triggered) {
          params.append('only_triggered', 'true');
        }

        const response = await fetch(`/api/events?${params}`, { signal });

        if (!response.ok) {
          throw new Error(`Failed to fetch events: ${response.statusText}`);
        }

        const data = await response.json();
        if (signal.aborted) {
          return;
        }
        setEvents(data.events || []);
        setTotalCount(data.total || 0);
        setTotalPages(Math.ceil((data.total || 0) / pageSize));
      } catch (err) {
        if (signal.aborted) {
          return;
        }
        setError(err.message);
      } finally {
        if (!signal.aborted) {
          setLoading(false);
        }
      }
    },
    [pageSize]
  );

  // Fetch data when page or filters change
  useEffect(() => {
    const controller = new AbortController();
    fetchEvents(currentPage, filters, controller.signal);
    return () => controller.abort();
  }, [
    fetchEvents,
    reloadVersion,
    currentPage,
    filters.source_id,
    filters.hours,
    filters.only_triggered,
  ]);

  // Update filters and URL params
  const handleFiltersChange = (newFilters) => {
    // Update URL params (filters will be derived from URL automatically)
    const newParams = new URLSearchParams();

    if (newFilters.source_id) {
      newParams.set('source_id', newFilters.source_id);
    }
    if (newFilters.hours !== 24) {
      newParams.set('hours', newFilters.hours.toString());
    }
    if (newFilters.only_triggered) {
      newParams.set('only_triggered', 'true');
    }

    newParams.set('page', '1'); // Reset to first page when filtering
    setSearchParams(newParams);

    // useEffect will trigger fetchEvents when filters change
  };

  const handleClearFilters = () => {
    // Clear URL params (filters will be derived from URL automatically)
    setSearchParams({});
    // useEffect will trigger fetchEvents when filters change
  };

  const handlePageChange = (newPage) => {
    const newParams = new URLSearchParams(searchParams);
    newParams.set('page', newPage.toString());
    setSearchParams(newParams);
  };

  const hasActiveFilters = () => {
    return filters.source_id || filters.hours !== 24 || filters.only_triggered;
  };

  return (
    <div className={styles.eventsList}>
      <h1>Events</h1>

      {/* Filters */}
      <EventFilters
        filters={filters}
        onFiltersChange={handleFiltersChange}
        onClearFilters={handleClearFilters}
        loading={loading}
      />

      {loading && (
        <div className={styles.loading} role="status">
          Loading events...
        </div>
      )}

      {/* Results Summary */}
      {!loading && !error && (
        <div className={styles.resultsInfo}>
          <p>
            Found {totalCount} event{totalCount !== 1 ? 's' : ''}
            {hasActiveFilters() && ' matching your criteria'}
          </p>
        </div>
      )}

      {error && (
        <div className={styles.error} role="alert">
          <p>Error: {error}</p>
          <Button variant="outline" onClick={() => setReloadVersion((version) => version + 1)}>
            Try again
          </Button>
        </div>
      )}

      {/* Events List */}
      {events.length > 0 ? (
        <>
          <div className={styles.eventsContainer}>
            {events.map((event) => (
              <EventCard key={event.event_id} event={event} />
            ))}
          </div>

          {/* Pagination */}
          <EventsPagination
            currentPage={currentPage}
            totalPages={totalPages}
            totalItems={totalCount}
            pageSize={pageSize}
            onPageChange={handlePageChange}
            loading={loading}
          />
        </>
      ) : !loading && !error ? (
        <div className={styles.emptyState}>
          <p>No events found matching your criteria.</p>
          {hasActiveFilters() && (
            <Button onClick={handleClearFilters} variant="secondary">
              Clear Filters
            </Button>
          )}
        </div>
      ) : null}
    </div>
  );
};

export default EventsList;
